"""Find the rallies in a reference cut (e.g. a Balltime export) without its rally list.

Usage: python segment_reference_cut.py ORIGINAL.mp4 CUT.mp4 OUT.json [--check REF.json]

Every 2 s window of the cut is matched against the original by frame fingerprints (the same
32x18 grey fingerprints as align_reference_cut.py). Inside one rally the offset
(source time - cut time) is constant; it jumps at every cut. Runs of constant offset are the
rallies, written in original-video seconds in the same format as align_reference_cut.py.
--check scores the result against an existing reference (one-to-one, tIoU >= 0.5).
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from align_reference_cut import FPS, fingerprints  # noqa: E402

WIN = 2 * FPS  # frames per matching window
MIN_LEN_S = 2.0


def window_offsets(S: np.ndarray, C: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Best source offset (frames) and its score for the window starting at each cut frame."""
    M = C @ S.T / C.shape[1]  # [cut, source] per-frame correlation
    n_c, n_s = M.shape[0] - WIN + 1, M.shape[1] - WIN + 1
    acc = np.zeros((n_c, n_s), np.float32)
    for k in range(WIN):
        acc += M[k : k + n_c, k : k + n_s]
    acc /= WIN
    best = acc.argmax(1)
    return best - np.arange(n_c), acc[np.arange(n_c), best]


def segments(
    offset: np.ndarray, score: np.ndarray, pad: tuple[float, float] = (0.0, 0.0)
) -> list[dict]:
    """Runs of constant offset. Each cut frame takes the offset of the window centred on it;
    windows straddling a cut give 1-2 s blips of a wrong offset, which are dropped (a real clip
    is longer). ``pad`` trims the clip edges: an export may pad each rally beyond the rally
    times in the reference's own list (measured with --check on a match that has the list)."""
    half = WIN // 2
    n = len(offset) + WIN - 1
    idx = np.clip(np.arange(n) - half, 0, len(offset) - 1)
    off, sc = offset[idx], score[idx]
    runs: list[list[int]] = []  # [first frame, last frame, offset]
    for f in range(n):
        if runs and abs(int(off[f]) - runs[-1][2]) <= 2:
            runs[-1][1] = f
        else:
            runs.append([f, f, int(off[f])])
    runs = [r for r in runs if (r[1] - r[0] + 1) / FPS >= MIN_LEN_S]
    # merge neighbours that share an offset (a blip removed between them)
    merged: list[list[int]] = []
    for r in runs:
        if merged and abs(r[2] - merged[-1][2]) <= 2:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    out = []
    for a, b, o in merged:
        c0, c1 = a / FPS + pad[0], (b + 1) / FPS - pad[1]
        if c1 - c0 < MIN_LEN_S:
            continue
        out.append(
            {
                "rally": len(out) + 1,
                "cut_start": round(c0, 1),
                "cut_end": round(c1, 1),
                "start": round(c0 + o / FPS, 1),
                "end": round(c1 + o / FPS, 1),
                "match": round(float(sc[a : b + 1].mean()), 3),
            }
        )
    return out


def f1(ours: list[tuple[float, float]], ref: list[tuple[float, float]]) -> float:
    def tiou(x: tuple[float, float], y: tuple[float, float]) -> float:
        inter = max(0.0, min(x[1], y[1]) - max(x[0], y[0]))
        return inter / (max(x[1], y[1]) - min(x[0], y[0]))

    pairs = sorted(((tiou(o, r), i, j) for i, o in enumerate(ours) for j, r in enumerate(ref)))
    used_o, used_r = set(), set()
    for v, i, j in reversed(pairs):
        if v >= 0.5 and i not in used_o and j not in used_r:
            used_o.add(i)
            used_r.add(j)
    m = len(used_o)
    return 2 * m / (len(ours) + len(ref)) if ours or ref else 1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("original")
    ap.add_argument("cut")
    ap.add_argument("out")
    ap.add_argument("--check")
    ap.add_argument("--pad", default="-0.4,2.8", help="trim at clip start,end (s); default = Balltime export padding, measured on 2 matches")
    a = ap.parse_args()
    S, C = fingerprints(a.original), fingerprints(a.cut)
    print("frames: source", len(S), "cut", len(C))
    off, score = window_offsets(S, C)
    pad = tuple(float(v) for v in a.pad.split(","))
    res = segments(off, score, pad)  # type: ignore[arg-type]
    mono = all(res[i]["start"] > res[i - 1]["start"] for i in range(1, len(res)))
    print(f"{len(res)} rallies | monotonic: {mono} | weakest match {min(r['match'] for r in res)}")
    json.dump(res, open(a.out, "w"), indent=1)
    if a.check:
        ref = json.load(open(a.check))
        r = [(x["start"], x["end"]) for x in ref]
        o = [(x["start"], x["end"]) for x in res]
        starts = [min(abs(x[0] - y[0]) for y in r) for x in o]
        ends = [min(abs(x[1] - y[1]) for y in r) for x in o]
        sd = [x[0] - min(r, key=lambda y: abs(y[0] - x[0]))[0] for x in o]
        ed = [x[1] - min(r, key=lambda y: abs(y[1] - x[1]))[1] for x in o]
        print(f"signed median start/end diff {np.median(sd):+.1f}/{np.median(ed):+.1f} s")
        print(
            f"vs {a.check}: {len(o)} vs {len(r)} rallies, F1@0.5 {f1(o, r):.3f}, "
            f"median start/end diff {np.median(starts):.1f}/{np.median(ends):.1f} s"
        )


if __name__ == "__main__":
    main()
