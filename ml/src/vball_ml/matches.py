"""Reference-match pipeline: turn a match with known rallies into an honest test, then into
training data, then (maybe) a better production model.

    python -m vball_ml.matches score  VIDEO_ID REF.json   # current model vs reference
    python -m vball_ml.matches import VIDEO_ID REF.json   # store as the video's labels
    python -m vball_ml.matches retrain --version V        # LOMO eval, train, gated promote

REF.json is the output of ml/eval/align_reference_cut.py (list of {start, end}).
Uses VBALL_* settings (database, storage) from the environment.

Promotion gate: the new recipe's leave-one-match-out F1 (each match predicted by a model that
never saw it) must be at least the current production model's F1 on labelled matches it was
not trained on. If there is no such match yet, the gate falls back to beating heuristic-v1's
recorded baseline. Nothing is promoted silently: the decision and the numbers are printed and
stored with the model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import Engine, create_engine, select, text, update
from sqlalchemy.dialects.postgresql import insert

from vball_core.db.enums import ModelStatus, ModelTask
from vball_core.db.models import AnalysisRun, ModelVersion, Rally, RallyLabelSet, Video
from vball_core.settings import get_settings
from vball_core.storage import ObjectStorage
from vball_ml.evaluate import Scores, score
from vball_ml.features import EXTRACTOR_VERSION, extract
from vball_ml.rally_model import Bundle, decode, labels_for, save_bundle, train, video_norm

Span = tuple[float, float]


def _ref(path: str) -> list[Span]:
    return [(float(r["start"]), float(r["end"])) for r in json.loads(Path(path).read_text())]


def _active_rallies(engine: Engine, video_id: uuid.UUID) -> tuple[str, list[Span]]:
    with engine.connect() as c:
        run = c.execute(
            select(AnalysisRun).where(AnalysisRun.video_id == video_id, AnalysisRun.is_active)
        ).first()
        if run is None:
            raise SystemExit("video has no analysis yet")
        rows = c.execute(
            select(Rally.start_t, Rally.end_t)
            .where(Rally.analysis_run_id == run.id)
            .order_by(Rally.start_t)
        ).all()
    return str(run.params.get("detector")), [(float(a), float(b)) for a, b in rows]


def cmd_score(video_id: str, ref_path: str) -> Scores:
    engine = create_engine(get_settings().database_dsn())
    detector, pred = _active_rallies(engine, uuid.UUID(video_id))
    s = score(_ref(ref_path), pred)
    print(f"{detector} vs reference: {s.row()}")
    return s


def cmd_import(video_id: str, ref_path: str, source: str = "reference:balltime") -> None:
    engine = create_engine(get_settings().database_dsn())
    ref = _ref(ref_path)
    with engine.begin() as c:
        owner, dur = c.execute(
            select(Video.owner_id, Video.duration_s).where(Video.id == uuid.UUID(video_id))
        ).one()
        rallies = [
            {
                "start_t": s,
                "end_t": e,
                "source": source,
                "edited": False,
                "predicted_start_t": None,
                "predicted_end_t": None,
            }
            for s, e in ref
        ]
        c.execute(
            insert(RallyLabelSet).values(
                video_id=uuid.UUID(video_id),
                user_id=owner,
                duration_s=dur,
                rallies=rallies,
                n_rallies=len(rallies),
            )
        )
    print(f"stored {len(ref)} reference rallies as labels for {video_id}")


# --- retraining ----------------------------------------------------------------------------
def _labelled_matches(engine: Engine) -> list[dict[str, Any]]:
    """Latest label set per video, with where to find its features."""
    sql = text(
        """
        SELECT DISTINCT ON (l.video_id) l.video_id, l.rallies, v.proxy_key, v.play_area,
               v.filename,
               (SELECT a.params->>'learned_features_key' FROM analysis_runs a
                 WHERE a.video_id = l.video_id AND a.params ? 'learned_features_key'
                   AND a.params->>'learned_features_key' IS NOT NULL
                 ORDER BY a.created_at DESC LIMIT 1) AS features_key
        FROM rally_label_sets l JOIN videos v ON v.id = l.video_id
        WHERE v.status <> 'deleted'
        ORDER BY l.video_id, l.created_at DESC
        """
    )
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(sql)]


def _features(m: dict[str, Any], storage: ObjectStorage, work: Path) -> np.ndarray:
    out = work / f"{m['video_id']}.npz"
    if m["features_key"]:
        storage.download(m["features_key"], str(out))
        return np.load(out)["x"]
    # No saved features: compute from the proxy (slow on CPU) and keep them for next time.
    proxy = work / f"{m['video_id']}.mp4"
    storage.download(m["proxy_key"], str(proxy))
    return extract(proxy, m["play_area"], work / "cache")


def _spans(label_rallies: list[dict[str, Any]]) -> list[Span]:
    return [(float(r["start_t"]), float(r["end_t"])) for r in label_rallies]


def cmd_retrain(version: str, seeds: int = 3) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_dsn())
    storage = ObjectStorage(settings)
    matches = _labelled_matches(engine)
    if not matches:
        raise SystemExit("no labelled matches")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        data = []
        for m in matches:
            x = video_norm(_features(m, storage, work))
            data.append((m, x, labels_for(len(x), _spans(m["rallies"]))))
            print(f"  {m['filename']}: {len(x)} steps, {len(m['rallies'])} rallies")

        # Leave-one-match-out: each match predicted by a model trained on the others.
        lomo: dict[str, float] = {}
        if len(data) >= 2:
            for i, (m, x, _y) in enumerate(data):
                xs = np.concatenate([d[1] for j, d in enumerate(data) if j != i])
                ys = np.concatenate([d[2] for j, d in enumerate(data) if j != i])
                nets = [
                    train(xs, ys, np.ones(len(xs), bool), seed=s, epochs=300) for s in range(seeds)
                ]
                b = Bundle([n for n, _ in nets], nets[0][1], {})  # x already video-normalised
                s = score(_spans(m["rallies"]), decode(b.predict(x)))
                lomo[m["filename"]] = round(s.f1_50, 3)
                print(f"  held-out {m['filename']}: {s.row()}")
        candidate = float(np.mean(list(lomo.values()))) if lomo else None

        # Current production model on matches it never trained on.
        from sqlalchemy.orm import Session

        with Session(engine, expire_on_commit=False) as session:
            prod = (
                session.execute(
                    select(ModelVersion).where(
                        ModelVersion.task == ModelTask.RALLY,
                        ModelVersion.status == ModelStatus.PRODUCTION,
                    )
                )
                .scalars()
                .first()
            )
        prod_unseen: dict[str, float] = {}
        if prod is not None:
            trained_on = set((prod.metrics or {}).get("trained_on", []))
            from vball_ml.rally_model import load_bundle

            art = work / "prod.pt"
            storage.download(prod.artifact_uri, str(art))
            pb = load_bundle(str(art))
            for m, _x, _y in data:
                if str(m["video_id"]) in trained_on:
                    continue
                raw = _features(m, storage, work)
                s = score(_spans(m["rallies"]), decode(pb.predict(raw)))
                prod_unseen[m["filename"]] = round(s.f1_50, 3)
                print(f"  production {prod.version} on unseen {m['filename']}: {s.row()}")
        bar = (
            float(np.mean(list(prod_unseen.values())))
            if prod_unseen
            else float((prod.metrics or {}).get("baseline_heuristic_f1_50", 0.0))
            if prod
            else 0.0
        )
        promote = candidate is not None and candidate >= bar
        decision = "PROMOTE" if promote else "keep current"
        print(f"candidate LOMO F1 {candidate} vs bar {bar:.3f} -> {decision}")

        # Final model on all matches.
        xs = np.concatenate([d[1] for d in data])
        ys = np.concatenate([d[2] for d in data])
        fitted = [train(xs, ys, np.ones(len(xs), bool), seed=s, epochs=300) for s in range(seeds)]
        meta: dict[str, Any] = {
            "version": version,
            "extractor": EXTRACTOR_VERSION,
            "input_norm": "per_video",
            "trained_at": datetime.now(UTC).isoformat(),
            "trained_on": [str(d[0]["video_id"]) for d in data],
            "lomo_f1_50": lomo,
            "lomo_mean_f1_50": candidate,
            "previous_unseen_f1_50": prod_unseen,
            "promotion_bar": bar,
        }
        path = work / "model.pt"
        save_bundle(str(path), Bundle([n for n, _ in fitted], fitted[0][1], meta))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key = f"models/rally/{version}/model.pt"
        storage.upload(str(path), key, "application/octet-stream")
        with engine.begin() as c:
            if promote:
                c.execute(
                    update(ModelVersion)
                    .where(
                        ModelVersion.task == ModelTask.RALLY,
                        ModelVersion.status == ModelStatus.PRODUCTION,
                    )
                    .values(status=ModelStatus.RETIRED)
                )
            c.execute(
                insert(ModelVersion).values(
                    task=ModelTask.RALLY,
                    name="rally-tcn-dinov2",
                    version=version,
                    artifact_uri=key,
                    sha256=digest,
                    framework="pytorch",
                    input_spec={"extractor": EXTRACTOR_VERSION, "step_s": 0.2},
                    training_dataset_ref=f"{len(data)} match(es)",
                    metrics=meta,
                    status=ModelStatus.PRODUCTION if promote else ModelStatus.CANDIDATE,
                    promoted_at=datetime.now(UTC) if promote else None,
                    promoted_by="vball_ml.matches retrain" if promote else None,
                )
            )
        print(f"registered {version} as {'production' if promote else 'candidate'}")


def cmd_publish(path: str, version: str) -> None:
    """Upload a saved model file to the configured storage and make it the production model in
    the configured database (e.g. the cloud deployment). The model's own metadata (recipe,
    training matches, held-out scores) is registered with it."""
    from vball_ml.rally_model import load_bundle
    from vball_ml.train_rally import register

    meta: dict[str, Any] = dict(load_bundle(path).meta)
    meta.setdefault("extractor", EXTRACTOR_VERSION)
    meta.setdefault("matches", len(meta.get("trained_on", [])) or 1)
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    register(path, version, digest, meta)
    print(f"published {version} ({digest[:12]}) as production")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("score", "import"):
        p = sub.add_parser(name)
        p.add_argument("video_id")
        p.add_argument("ref")
    r = sub.add_parser("retrain")
    r.add_argument("--version", required=True)
    r.add_argument("--seeds", type=int, default=3)
    pub = sub.add_parser("publish")
    pub.add_argument("path")
    pub.add_argument("--version", required=True)
    a = ap.parse_args()
    if a.cmd == "score":
        cmd_score(a.video_id, a.ref)
    elif a.cmd == "import":
        cmd_import(a.video_id, a.ref)
    elif a.cmd == "publish":
        cmd_publish(a.path, a.version)
    else:
        cmd_retrain(a.version, a.seeds)


if __name__ == "__main__":
    main()
