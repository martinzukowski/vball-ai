"""Cross-gym experiments for the rally model. Every recipe is trained on all matches but one
and scored on the held-out match (leave-one-match-out), so scores reflect footage from a venue
the model has not seen. Each run is appended to ``ml/experiments/log.jsonl``.

    python -m vball_ml.experiments NAME [--set key=value ...] [--input crop|full]

Inputs: ``data/features/<match>.<input>.npz`` + ``ml/references/<match>.balltime.json``.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import numpy as np

from vball_ml.evaluate import score
from vball_ml.rally_model import STEP, Bundle, add_motion, decode, labels_for, train, video_norm

ROOT = Path(__file__).resolve().parents[3]
LOG = ROOT / "ml" / "experiments" / "log.jsonl"

MATCHES = {
    "tfc": "tfc_playoffs_t2_finals_s2",
    "hjsc": "hjsc_adam_vs_bigchillin_g2s1",
    "tfc2": "tfc_dino_vs_carlos_t2_g1s1",
}
# Venue of each match: "venue" holdout never trains on the test match's venue.
VENUES = {"tfc": "TFC", "tfc2": "TFC", "hjsc": "HJSC"}


@dataclass
class Recipe:
    input: str = "mixed"  # mixed = tfc.crop + hjsc.full (what production saw), or crop/full
    video_norm: bool = True
    motion: bool = False  # add |x_t - x_{t-1}| of the video embedding (smoothed)
    whistle: bool = False  # add whistle-cue audio features (data/features/<m>.whistle.npz)
    width: int = 64
    in_dropout: float = 0.2
    noise: float = 0.0
    epochs: int = 300
    seeds: int = 3
    threshold: str = "0.5"  # number, or "otsu" (per-video adaptive)
    exit_threshold: float = -1.0  # < 0: no hysteresis
    min_gap_s: float = 1.0
    holdout: str = "match"  # "match" (leave one match out) or "venue" (leave one gym out)
    matches: str = "tfc,hjsc"  # which matches take part
    extra: dict[str, str] = field(default_factory=dict)


def load(match: str, inp: str) -> np.ndarray:
    variant = {"mixed": "crop" if match == "tfc" else "full"}.get(inp, inp)
    if variant == "crop" and not (ROOT / "data" / "features" / f"{match}.crop.npz").exists():
        variant = "full"
    path = ROOT / "data" / "features" / f"{match}.{variant}.npz"
    return np.load(path)["x"]


def refs(match: str) -> list[tuple[float, float]]:
    rows = json.loads((ROOT / "ml" / "references" / f"{MATCHES[match]}.balltime.json").read_text())
    return [(float(r["start"]), float(r["end"])) for r in rows]


def prepare(x: np.ndarray, r: Recipe, match: str = "") -> np.ndarray:
    if r.motion:
        x = add_motion(x)
    if r.whistle:
        w = np.load(ROOT / "data" / "features" / f"{match}.whistle.npz")["x"][: len(x)]
        x = np.hstack([x, w])
    return video_norm(x) if r.video_norm else x


def otsu(p: np.ndarray) -> float:
    """Threshold that best separates a video's probabilities into two groups."""
    hist, edges = np.histogram(p, bins=50, range=(0, 1))
    centers = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m0 = np.cumsum(hist * centers) / np.maximum(w0, 1)
    m1 = (np.sum(hist * centers) - np.cumsum(hist * centers)) / np.maximum(w1, 1)
    return float(centers[int(np.argmax(w0 * w1 * (m0 - m1) ** 2))])


def run(name: str, r: Recipe) -> dict[str, object]:
    t0 = time.time()
    names = [m for m in r.matches.split(",") if m]
    data = {m: (prepare(load(m, r.input), r, m), refs(m)) for m in names}
    per: dict[str, dict[str, float]] = {}
    for held in names:
        train_ms = [
            m for m in names if m != held and (r.holdout == "match" or VENUES[m] != VENUES[held])
        ]
        if not train_ms:
            continue
        xs = np.concatenate([data[m][0] for m in train_ms])
        ys = np.concatenate([labels_for(len(data[m][0]), data[m][1]) for m in train_ms])
        nets = [
            train(
                xs,
                ys,
                np.ones(len(xs), bool),
                seed=s,
                epochs=r.epochs,
                width=r.width,
                in_dropout=r.in_dropout,
                noise=r.noise,
            )
            for s in range(r.seeds)
        ]
        prob = Bundle([n for n, _ in nets], nets[0][1], {}).predict(data[held][0])
        thr = otsu(prob) if r.threshold == "otsu" else float(r.threshold)
        spans = decode(
            prob,
            threshold=thr,
            min_gap_s=r.min_gap_s,
            exit_threshold=r.exit_threshold if r.exit_threshold >= 0 else None,
        )
        sc = score(data[held][1], spans)
        per[held] = {
            "f1_50": round(sc.f1_50, 3),
            "f1_70": round(sc.f1_70, 3),
            "start_err": round(sc.start_err_median, 2),
            "end_err": round(sc.end_err_median, 2),
            "pred": sc.n_pred,
            "true": sc.n_ref,
            "threshold": round(thr, 3),
        }
    mean = round(float(np.mean([v["f1_50"] for v in per.values()])), 3)
    row = {
        "name": name,
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "recipe": asdict(r),
        "mean_f1_50": mean,
        "per_match": per,
        "seconds": round(time.time() - t0),
        "step_s": STEP,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--set", action="append", default=[], help="recipe field=value")
    a = ap.parse_args()
    r = Recipe()
    types = {f.name: f.type for f in fields(Recipe)}
    for kv in a.set:
        k, v = kv.split("=", 1)
        cur = getattr(r, k)
        setattr(r, k, type(cur)(v) if not isinstance(cur, bool) else v.lower() in ("1", "true"))
        assert k in types
    row = run(a.name, r)
    per_match: dict[str, dict[str, float]] = row["per_match"]  # type: ignore[assignment]
    per = "  ".join(
        f"{m}: {v['f1_50']:.2f} ({v['start_err']}/{v['end_err']}s)" for m, v in per_match.items()
    )
    print(f"{a.name:28s} mean F1 {row['mean_f1_50']:.3f}  |  {per}")


if __name__ == "__main__":
    main()
