"""Analyze: detect rallies on a ready video with the heuristic detector.

Downloads the playback proxy, measures motion/whistle signals inside the play area, stores
them as an artifact (so changing sensitivity later doesn't need re-decoding), runs
``heuristic-v1`` and writes an analysis run + rallies. The new run becomes the active one
atomically with the job's success.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, insert, update
from sqlalchemy.orm import Session

from vball_core import pipelines
from vball_core.db.enums import CalibrationSource, RunStatus, VideoStatus
from vball_core.db.models import AnalysisRun, CameraSegment, Rally, Video
from vball_core.settings import Settings
from vball_core.storage import ObjectStorage
from vball_worker.context import JobContext, JobResult
from vball_worker.media import InvalidMediaError
from vball_worker.rally.features import extract_features
from vball_worker.rally.heuristic import DETECTOR, detect_rallies, sensitivity_options


def run_analyze(ctx: JobContext, settings: Settings, storage: ObjectStorage) -> JobResult:
    with Session(ctx.engine, expire_on_commit=False) as session:
        video = session.get(Video, ctx.job.video_id)
        if video is not None:
            session.expunge(video)
    if video is None or video.status == VideoStatus.DELETED:
        raise InvalidMediaError("The video was deleted.")
    if video.status != VideoStatus.READY or not video.proxy_key or not video.duration_s:
        raise InvalidMediaError("The video isn't ready for analysis.")
    duration = float(video.duration_s)
    proxy_key = video.proxy_key
    sensitivity = int(ctx.job.params.get("sensitivity", 0))
    play_area = video.play_area
    run_id = uuid.uuid4()

    with tempfile.TemporaryDirectory(dir=settings.worker_scratch_dir, prefix="analyze-") as tmp:
        proxy = Path(tmp) / "proxy.mp4"
        with ctx.stage("download"):
            storage.download(proxy_key, str(proxy))
        ctx.progress(0.05)
        with ctx.stage("features"):
            feats = extract_features(
                proxy,
                duration,
                play_area,
                on_progress=lambda p: ctx.progress(0.05 + 0.85 * p),
            )
        blob = gzip.compress(json.dumps(feats.to_json()).encode())
        features_key = f"artifacts/{video.owner_id}/{video.id}/{run_id}/features.json.gz"
        with ctx.stage("upload"):
            path = Path(tmp) / "features.json.gz"
            path.write_bytes(blob)
            storage.upload(str(path), features_key, "application/gzip")
    with ctx.stage("detect"):
        det = detect_rallies(feats, sensitivity_options(sensitivity))
    ctx.progress(0.97)

    params: dict[str, Any] = {
        "detector": DETECTOR,
        "sensitivity": sensitivity,
        "play_area": play_area,
        "mode": det.mode,
        "whistles": len(det.whistles),
        "features_key": features_key,
        "features_sha256": hashlib.sha256(blob).hexdigest(),
        "camera_moves": feats.cams[1:],
    }

    def finalize(conn: Connection) -> None:
        conn.execute(
            update(AnalysisRun)
            .where(AnalysisRun.video_id == video.id, AnalysisRun.is_active)
            .values(is_active=False)
        )
        conn.execute(
            insert(AnalysisRun).values(
                id=run_id,
                video_id=video.id,
                job_id=ctx.job.id,
                pipeline_version=pipelines.RALLY_HEURISTIC,
                params=params,
                status=RunStatus.SUCCEEDED,
                is_active=True,
            )
        )
        bounds = [*feats.cams, duration]
        segs = [
            {
                "analysis_run_id": run_id,
                "start_t": bounds[i],
                "end_t": bounds[i + 1],
                "source": CalibrationSource.MANUAL if play_area else None,
                "image_keypoints": {"play_area": play_area} if play_area else None,
            }
            for i in range(len(feats.cams))
            if bounds[i + 1] > bounds[i]
        ]
        if segs:
            conn.execute(insert(CameraSegment), segs)
        rows = []
        for r in det.rallies:
            start, end = float(r["start"]), min(float(r["end"]), duration)
            if end <= start:
                continue
            serve = r.get("serve")
            rows.append(
                {
                    "analysis_run_id": run_id,
                    "video_id": video.id,
                    "start_t": start,
                    "end_t": end,
                    "serve_t": serve if serve is not None and start <= serve <= end else None,
                    "confidence": None,  # heuristic scores aren't probabilities
                    "detector": DETECTOR,
                    "features": {"score": r.get("score"), "whistle": bool(r.get("whistle"))},
                }
            )
        if rows:
            conn.execute(insert(Rally), rows)

    return JobResult(finalize=finalize)
