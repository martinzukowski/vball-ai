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

from sqlalchemy import Connection, insert, select, update
from sqlalchemy.orm import Session

from vball_core import pipelines
from vball_core.db.enums import CalibrationSource, ModelStatus, ModelTask, RunStatus, VideoStatus
from vball_core.db.models import AnalysisRun, CameraSegment, ModelVersion, Rally, Video
from vball_core.logging import get_logger
from vball_core.settings import Settings
from vball_core.storage import ObjectStorage
from vball_worker.context import JobContext, JobResult
from vball_worker.media import InvalidMediaError
from vball_worker.rally.features import extract_features
from vball_worker.rally.heuristic import DETECTOR, detect_rallies, sensitivity_options

log = get_logger("vball_worker.analyze")
_BUNDLES: dict[str, Any] = {}  # sha256 -> loaded model, reused across jobs


def production_rally_model(ctx: JobContext) -> ModelVersion | None:
    with Session(ctx.engine, expire_on_commit=False) as session:
        mv = session.scalars(
            select(ModelVersion).where(
                ModelVersion.task == ModelTask.RALLY, ModelVersion.status == ModelStatus.PRODUCTION
            )
        ).first()
        if mv is not None:
            session.expunge(mv)
        return mv


def load_model(mv: ModelVersion, storage: ObjectStorage, settings: Settings) -> Any:
    if mv.sha256 in _BUNDLES:
        return _BUNDLES[mv.sha256]
    from vball_ml.rally_model import load_bundle  # heavy import (torch) only when needed

    cache = Path(settings.worker_scratch_dir or tempfile.gettempdir()) / "models"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{mv.sha256}.pt"
    if not path.exists():
        storage.download(mv.artifact_uri, str(path))
    if hashlib.sha256(path.read_bytes()).hexdigest() != mv.sha256:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"model artifact {mv.artifact_uri} failed its sha256 check")
    _BUNDLES[mv.sha256] = load_bundle(str(path))
    return _BUNDLES[mv.sha256]


def learned_rallies(
    ctx: JobContext, proxy: Path, play_area: Any, bundle: Any, work: Path, duration: float
) -> list[dict[str, Any]]:
    from vball_ml.features import extract
    from vball_ml.rally_model import STEP, decode

    with ctx.stage("embeddings"):
        x = extract(
            proxy,
            play_area,
            work / "features",
            on_progress=lambda p: ctx.progress(0.05 + 0.8 * p),
            expected_steps=int(duration / STEP) + 1,
        )
    ctx.progress(0.85)
    with ctx.stage("model"):
        prob = bundle.predict(x)
        spans = decode(prob)
    return [
        {
            "start": s,
            "end": e,
            "confidence": float(prob[int(s / STEP) : max(int(s / STEP) + 1, int(e / STEP))].mean()),
        }
        for s, e in spans
    ]


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

    mv = production_rally_model(ctx)
    learned: list[dict[str, Any]] | None = None
    fallback_reason: str | None = None if mv else "no production rally model"
    feats = None
    det = None
    with tempfile.TemporaryDirectory(dir=settings.worker_scratch_dir, prefix="analyze-") as tmp:
        work = Path(tmp)
        proxy = work / "proxy.mp4"
        with ctx.stage("download"):
            storage.download(proxy_key, str(proxy))
        ctx.progress(0.05)
        if mv is not None:
            try:
                learned = learned_rallies(
                    ctx, proxy, play_area, load_model(mv, storage, settings), work, duration
                )
            except Exception as exc:  # keep the user's video usable: fall back to the heuristic
                fallback_reason = f"learned model failed: {type(exc).__name__}"
                log.error("learned_model_failed", error=type(exc).__name__, detail=str(exc)[:300])
        if learned is None:
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
                path = work / "features.json.gz"
                path.write_bytes(blob)
                storage.upload(str(path), features_key, "application/gzip")
            with ctx.stage("detect"):
                det = detect_rallies(feats, sensitivity_options(sensitivity))
    ctx.progress(0.97)

    if learned is not None and mv is not None:
        detector = f"learned:{mv.version}"
        pipeline = pipelines.RALLY_LEARNED
        found = learned
        params: dict[str, Any] = {
            "detector": detector,
            "model_version": mv.version,
            "sensitivity": 0,
            "play_area": play_area,
            "mode": "learned",
            "camera_moves": [],
        }
        cams = [0.0]
    else:
        if feats is None or det is None:
            raise RuntimeError("no rally detection result")
        detector, pipeline = DETECTOR, pipelines.RALLY_HEURISTIC
        found = [
            {
                "start": r["start"],
                "end": r["end"],
                "serve": r.get("serve"),
                "whistle": r.get("whistle"),
                "score": r.get("score"),
            }
            for r in det.rallies
        ]
        params = {
            "detector": detector,
            "sensitivity": sensitivity,
            "play_area": play_area,
            "mode": det.mode,
            "whistles": len(det.whistles),
            "features_key": features_key,
            "features_sha256": hashlib.sha256(blob).hexdigest(),
            "camera_moves": feats.cams[1:],
            "fallback_reason": fallback_reason,
        }
        cams = feats.cams

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
                pipeline_version=pipeline,
                params=params,
                model_version_ids=[mv.id] if (mv is not None and learned is not None) else [],
                status=RunStatus.SUCCEEDED,
                is_active=True,
            )
        )
        bounds = [*cams, duration]
        segs = [
            {
                "analysis_run_id": run_id,
                "start_t": bounds[i],
                "end_t": bounds[i + 1],
                "source": CalibrationSource.MANUAL if play_area else None,
                "image_keypoints": {"play_area": play_area} if play_area else None,
            }
            for i in range(len(cams))
            if bounds[i + 1] > bounds[i]
        ]
        if segs:
            conn.execute(insert(CameraSegment), segs)
        rows = []
        for r in found:
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
                    # Learned: mean model probability inside the rally (not calibrated yet).
                    # Heuristic scores aren't probabilities, so they stay NULL.
                    "confidence": r.get("confidence"),
                    "detector": detector,
                    "model_version_id": mv.id if learned is not None and mv is not None else None,
                    "features": {"score": r.get("score"), "whistle": bool(r.get("whistle"))},
                }
            )
        if rows:
            conn.execute(insert(Rally), rows)

    return JobResult(finalize=finalize)
