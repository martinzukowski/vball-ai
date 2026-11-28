"""Align a reference rally cut (e.g. a Balltime export) to the original video by frame fingerprints.

Usage: python align_reference_cut.py ORIGINAL.mp4 CUT.mp4 RALLY_TIMES.txt OUT.json
RALLY_TIMES.txt: rally ranges on the cut timeline, e.g. "0:00-0:07 0:09-0:19 ...".
Output: each rally's start/end in the original (about +/-1 s), with match strength.
"""

import json
import subprocess
import sys

import numpy as np

W, H, FPS = 32, 18, 5


def fingerprints(path):
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-an",
            "-vf",
            f"fps={FPS},scale={W}:{H}:flags=area,format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    x = np.frombuffer(raw, np.uint8).reshape(-1, W * H).astype(np.float32)
    x -= x.mean(1, keepdims=True)
    x /= x.std(1, keepdims=True) + 1e-6
    return x


def main():
    src, cut, bt_file, out = sys.argv[1:5]
    S, C = fingerprints(src), fingerprints(cut)
    print("frames: source", len(S), "cut", len(C))
    tosec = lambda m: int(m.split(":")[0]) * 60 + int(m.split(":")[1])
    bt = [tuple(map(tosec, p.split("-"))) for p in open(bt_file).read().split()]
    res, last = [], 0
    for k, (a, b) in enumerate(bt, 1):
        # use the middle of the clip (avoid fades / list rounding at the edges)
        i0, i1 = int((a + 1) * FPS), int((b - 1) * FPS)
        if i1 - i0 < 5:
            i0, i1 = int(a * FPS), int(b * FPS)
        Q = C[i0:i1]
        m = len(Q)
        # sliding mean correlation of Q against S
        win = np.lib.stride_tricks.sliding_window_view(S, (m, S.shape[1]))[:, 0]
        score = np.einsum("nmd,md->n", win, Q) / (m * S.shape[1])
        o = int(np.argmax(score))
        best = float(score[o])
        second = float(np.max(np.delete(score, range(max(0, o - 25), o + 25))))
        offset = o / FPS - i0 / FPS  # source_time = cut_time + offset
        res.append(
            {
                "rally": k,
                "cut_start": a,
                "cut_end": b,
                "start": round(a + offset, 1),
                "end": round(b + offset, 1),
                "match": round(best, 3),
                "runner_up": round(second, 3),
            }
        )
    mono = all(res[i]["start"] > res[i - 1]["start"] for i in range(1, len(res)))
    print(
        "monotonic:",
        mono,
        "| weakest match",
        min(r["match"] for r in res),
        "| best runner-up",
        max(r["runner_up"] for r in res),
    )
    json.dump(res, open(out, "w"), indent=1)
    for r in res[:6] + res[-3:]:
        print(r)


if __name__ == "__main__":
    main()
