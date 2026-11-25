"""Blocked cross-validation for a single labelled match: split the timeline into K contiguous
blocks; train on K-1, predict the held-out block (with a 10 s guard band so the model never
sees frames adjacent to what it's tested on). Predictions from all folds are stitched together
and scored against the reference once.

Within-match CV says whether the approach works on THIS footage; it can't show
generalisation to other gyms/cameras. That needs more labelled matches.

Usage:
    python -m vball_ml.crossval FEATURES.npz LABELS.json HEURISTIC.csv [--folds 4] [--seeds 3]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from vball_ml.evaluate import score
from vball_ml.rally_model import STEP, decode, labels_for, predict, train

GUARD_S = 10.0


def run(x: np.ndarray, rallies: list[tuple[float, float]], folds: int, seeds: int) -> np.ndarray:
    n = len(x)
    y = labels_for(n, rallies)
    prob = np.zeros(n, np.float32)
    bounds = np.linspace(0, n, folds + 1).astype(int)
    guard = int(GUARD_S / STEP)
    for k in range(folds):
        a, b = bounds[k], bounds[k + 1]
        mask = np.ones(n, bool)
        mask[max(0, a - guard) : min(n, b + guard)] = False
        p = np.mean([predict(*train(x, y, mask, seed=s), x)[a:b] for s in range(seeds)], axis=0)
        prob[a:b] = p
    return prob


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("features")
    ap.add_argument("labels")
    ap.add_argument("heuristic")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    x = np.load(a.features)["x"]
    ref = [(r["start"], r["end"]) for r in json.loads(Path(a.labels).read_text())]
    heur: list[tuple[float, float]] = []
    for line in Path(a.heuristic).read_text().split():
        s0, e0 = line.split(",")
        heur.append((float(s0), float(e0)))
    prob = run(x, ref, a.folds, a.seeds)
    pred = decode(prob)
    print("heuristic-v1   :", score(ref, heur).row())
    print("learned (CV)   :", score(ref, pred).row())
    n = len(x)
    bounds = np.linspace(0, n * STEP, a.folds + 1)
    for k in range(a.folds):
        lo, hi = bounds[k], bounds[k + 1]
        r = [s for s in ref if lo <= s[0] < hi]
        p = [s for s in pred if lo <= s[0] < hi]
        h = [s for s in heur if lo <= s[0] < hi]
        learned, heuristic = score(r, p).f1_50, score(r, h).f1_50
        print(f"  fold {k + 1}: learned F1@0.5 {learned:.2f} | heuristic {heuristic:.2f}")
    if a.out:
        Path(a.out).write_text(json.dumps({"pred": pred, "prob": prob.round(3).tolist()}))


if __name__ == "__main__":
    main()
