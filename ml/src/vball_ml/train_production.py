"""Train the production rally model with the best cross-gym recipe on every labelled match in
``data/features`` + ``ml/references``, and register it.

    python -m vball_ml.train_production --version V --trained-on VIDEO_ID,... [--register]

The recipe and its held-out scores come from ml/experiments/log.jsonl (leave-one-match-out /
leave-one-venue-out). The saved model carries the recipe (motion cues, per-video
normalisation, decoding), so the worker runs it exactly as it was evaluated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from vball_ml.experiments import LOG, MATCHES, Recipe, load, prepare, refs
from vball_ml.features import EXTRACTOR_VERSION
from vball_ml.rally_model import Bundle, labels_for, save_bundle, train

BEST = Recipe(motion=True, width=128, in_dropout=0.5, min_gap_s=3.0, matches="tfc,hjsc,tfc2")


def held_out_scores() -> dict[str, Any]:
    """Latest logged held-out results for the production recipe."""
    out: dict[str, Any] = {}
    for line in LOG.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["name"] in ("best+gap3", "3m-best-LOMO", "3m-best-LOVO"):
            out[row["name"]] = {"mean_f1_50": row["mean_f1_50"], "per_match": row["per_match"]}
    return out


def held_out_by_video(
    video_ids: list[str], names: list[str], held_out: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """F1 for each training video when the model was trained without it. Prefers the stricter
    leave-one-venue-out run (no match from that gym in training) over leave-one-match-out."""
    out: dict[str, dict[str, Any]] = {}
    for vid, name in zip(video_ids, names, strict=False):
        for run, test in (("3m-best-LOVO", "gym"), ("3m-best-LOMO", "match")):
            score = held_out.get(run, {}).get("per_match", {}).get(name)
            if score:
                out[vid] = {"f1": score["f1_50"], "held_out": test}
                break
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--trained-on", default="", help="app video ids of the training matches")
    ap.add_argument("--register", action="store_true")
    a = ap.parse_args()

    r = BEST
    names = [m for m in r.matches.split(",") if m]
    xs, ys = [], []
    for m in names:
        x = prepare(load(m, r.input), r, m)
        xs.append(x)
        ys.append(labels_for(len(x), refs(m)))
        print(f"  {m}: {len(x)} steps, {len(refs(m))} rallies")
    x_all, y_all = np.concatenate(xs), np.concatenate(ys)
    fitted = [
        train(
            x_all,
            y_all,
            np.ones(len(x_all), bool),
            seed=s,
            epochs=r.epochs,
            width=r.width,
            in_dropout=r.in_dropout,
            noise=r.noise,
        )
        for s in range(r.seeds)
    ]
    meta: dict[str, Any] = {
        "version": a.version,
        "extractor": EXTRACTOR_VERSION,
        "motion": r.motion,
        "input_norm": "per_video" if r.video_norm else "global",
        "decode": {"threshold": 0.5, "min_gap_s": r.min_gap_s, "min_len_s": 2.0},
        "trained_at": datetime.now(UTC).isoformat(),
        "trained_on": [v for v in a.trained_on.split(",") if v],
        "training_matches": [MATCHES[m] for m in names],
        "held_out": held_out_scores(),
        "matches": len(names),
    }
    meta["held_out_by_video"] = held_out_by_video(meta["trained_on"], names, meta["held_out"])
    # register() stores meta["metrics"]; the retrain gate reads metrics.trained_on and the app
    # shows held_out_by_video instead of the (optimistic) in-sample agreement.
    meta["metrics"] = {
        "held_out": meta["held_out"],
        "trained_on": meta["trained_on"],
        "held_out_by_video": meta["held_out_by_video"],
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "model.pt"
        save_bundle(str(path), Bundle([n for n, _ in fitted], fitted[0][1], meta))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"trained {a.version} on {len(names)} matches, sha256 {digest[:12]}")
        if a.register:
            from vball_ml.train_rally import register

            register(str(path), a.version, digest, meta)


if __name__ == "__main__":
    main()
