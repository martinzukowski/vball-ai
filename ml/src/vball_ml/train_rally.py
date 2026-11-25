"""Train the production rally model on all labelled matches and (optionally) register it.

Manifest (JSON): [{"features": "path.npz", "labels": "rallies.json"}, ...] where labels is a
list of {"start": s, "end": e} (or start_t/end_t). Held-out scores come from crossval.py and
are passed in with --metrics so the registry records how the model was validated.

    python -m vball_ml.train_rally MANIFEST.json OUT.pt --version 2026-11-25.1 \
        --metrics metrics.json [--register]

--register uploads the file to object storage (models/rally/<version>/model.pt) and makes it
the production rally model in the database (previous production model -> retired).
Uses VBALL_* settings from the environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from vball_ml.features import EXTRACTOR_VERSION
from vball_ml.rally_model import Bundle, labels_for, save_bundle, train


def _spans(path: str) -> list[tuple[float, float]]:
    rows = json.loads(Path(path).read_text())
    return [
        (float(r.get("start", r.get("start_t"))), float(r.get("end", r.get("end_t")))) for r in rows
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("out")
    ap.add_argument("--version", required=True)
    ap.add_argument("--metrics", default=None)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--register", action="store_true")
    a = ap.parse_args()

    items = json.loads(Path(a.manifest).read_text())
    xs, ys = [], []
    for it in items:
        x = np.load(it["features"])["x"]
        xs.append(x)
        ys.append(labels_for(len(x), _spans(it["labels"])))
    x_all, y_all = np.concatenate(xs), np.concatenate(ys)
    mask = np.ones(len(x_all), bool)
    nets, norm = [], None
    for s in range(a.seeds):
        net, norm = train(x_all, y_all, mask, seed=s, epochs=300)
        nets.append(net)
    assert norm is not None
    metrics = json.loads(Path(a.metrics).read_text()) if a.metrics else {}
    meta: dict[str, object] = {
        "version": a.version,
        "extractor": EXTRACTOR_VERSION,
        "trained_at": datetime.now(UTC).isoformat(),
        "matches": len(items),
        "steps": len(x_all),
        "metrics": metrics,
    }
    save_bundle(a.out, Bundle(nets, norm, meta))
    digest = hashlib.sha256(Path(a.out).read_bytes()).hexdigest()
    print(f"saved {a.out} sha256={digest[:12]}… trained on {len(items)} match(es)")
    if a.register:
        register(a.out, a.version, digest, meta)


def register(path: str, version: str, digest: str, meta: dict[str, object]) -> None:
    from sqlalchemy import create_engine, update
    from sqlalchemy.dialects.postgresql import insert

    from vball_core.db.enums import ModelStatus, ModelTask
    from vball_core.db.models import ModelVersion
    from vball_core.settings import get_settings
    from vball_core.storage import ObjectStorage

    settings = get_settings()
    key = f"models/rally/{version}/model.pt"
    ObjectStorage(settings).upload(path, key, "application/octet-stream")
    engine = create_engine(settings.database_dsn())
    with engine.begin() as conn:
        conn.execute(
            update(ModelVersion)
            .where(
                ModelVersion.task == ModelTask.RALLY, ModelVersion.status == ModelStatus.PRODUCTION
            )
            .values(status=ModelStatus.RETIRED)
        )
        conn.execute(
            insert(ModelVersion).values(
                task=ModelTask.RALLY,
                name="rally-tcn-dinov2",
                version=version,
                artifact_uri=key,
                sha256=digest,
                framework="pytorch",
                input_spec={"extractor": meta["extractor"], "step_s": 0.2},
                training_dataset_ref=f"{meta['matches']} match(es)",
                metrics=meta.get("metrics", {}),
                status=ModelStatus.PRODUCTION,
                promoted_at=datetime.now(UTC),
                promoted_by="train_rally.py",
            )
        )
    print(f"registered rally model {version} as production ({key})")


if __name__ == "__main__":
    main()
