"""Rally detection heuristic ("heuristic-v1"): a faithful Python port of the original Rally
Cutter's ``detect.js`` (see reference/rally-cutter-js/). This is hand-written rules over motion
and whistle signals, NOT a trained model; it is the baseline that ML models must beat.

How it works (unchanged from the original):
1. Serves: each referee whistle (when audible) plus the end of the first "freeze" after it;
   without whistles, clear freezes (everyone still) followed by a jump in motion.
2. A rally runs from its serve until motion settles into a pause.
3. Points are at least ``min_dead`` seconds apart.
4. Fallback when too few serves are found: rallies are stretches of high motion.

Rounding follows JavaScript (``Math.round``, ``Number.prototype.toFixed``) so results match the
original exactly; a parity test runs both on the same inputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

DETECTOR = "heuristic-v1"

DEFAULTS: dict[str, float] = {
    "smoothSec": 0.4,
    "freezeQ": 0.15,
    "freezeMin": 0.8,
    "riseQ": 0.5,
    "riseMul": 1.3,
    "riseWithin": 3,
    "whistleLead": 7,
    "whistleDb": 10,
    "endQ": 0.3,
    "endHold": 0.8,
    "minRally": 2.5,
    "maxRally": 35,
    "minDead": 7,
    "preRoll": 2.0,
    "postRoll": 1.0,
    "breakSec": 40,
    "hi": 0.42,
    "lo": 0.22,
    "exitHold": 1.4,
}


def sensitivity_options(sensitivity: int) -> dict[str, float]:
    """The original app's "Fewer / More" slider (-5..5) mapped onto detector options."""
    s = max(-5, min(5, sensitivity)) / 5
    return {
        "freezeQ": 0.15 + 0.06 * s,
        "minDead": 7 - 2 * s,
        "hi": 0.42 - 0.14 * s,
        "lo": 0.22 - 0.07 * s,
    }


# --- JavaScript-compatible numerics ---------------------------------------------------------
def js_round(x: float) -> int:
    """Math.round: halves round towards +infinity."""
    return math.floor(x + 0.5)


def to_fixed(x: float, digits: int = 2) -> float:
    """+x.toFixed(digits): decimal rounding of the exact binary value, ties away from zero."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(x).quantize(q, rounding=ROUND_HALF_UP))


def pct(arr: list[float | None], q: float) -> float:
    a = sorted(v for v in arr if v is not None and math.isfinite(v))
    return a[min(len(a) - 1, math.floor(q * (len(a) - 1)))] if a else 0.0


def smooth(arr: list[float | None], k: int) -> list[float]:
    out: list[float] = []
    n = len(arr)
    for i in range(n):
        s, c = 0.0, 0
        for j in range(max(0, i - k), min(n - 1, i + k) + 1):
            v = arr[j]
            if v is not None:
                s += v
                c += 1
        out.append(s / c if c else 0.0)
    return out


# --- data -------------------------------------------------------------------------------
@dataclass
class Features:
    """Samples every ``step`` seconds. ``act`` = share of the play area that changed since the
    previous sample; ``wh``/``lo`` = loudest dB in the whistle (2-4.5 kHz) and low
    (200-1500 Hz) bands; ``bad`` = sample unreliable (e.g. camera moving)."""

    t: list[float]
    act: list[float]
    wh: list[float]
    lo: list[float]
    bad: list[int]
    duration: float
    step: float = 0.2
    wh_valid: bool = True
    cams: list[float] = field(default_factory=lambda: [0.0])  # camera segment start times

    def to_json(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "whValid": self.wh_valid,
            "duration": self.duration,
            "t": self.t,
            "act": self.act,
            "wh": self.wh,
            "lo": self.lo,
            "bad": self.bad,
            "cams": [{"from": c} for c in self.cams],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Features:
        return cls(
            t=list(d["t"]),
            act=list(d["act"]),
            wh=list(d["wh"]),
            lo=list(d["lo"]),
            bad=[int(b) for b in d["bad"]],
            duration=float(d.get("duration") or 0),
            step=float(d.get("step") or 0.2),
            wh_valid=bool(d.get("whValid", True)),
            cams=[float(c["from"]) for c in d.get("cams") or [{"from": 0}]],
        )


@dataclass
class Detection:
    rallies: list[dict[str, Any]]  # {start, end, serve?, whistle, score}
    breaks: list[dict[str, float]]
    whistles: list[float]
    serves: list[float]
    mode: str  # "serves" or "motion" (fallback)


def _resample(f: Features) -> Features:
    step, m = f.step or 0.2, len(f.t)
    if not m:
        return Features([], [], [], [], [], f.duration, step, f.wh_valid, f.cams)
    t0 = f.t[0]
    n = math.floor((f.t[m - 1] - t0) / step) + 1
    out = Features([], [], [], [], [], f.duration, step, f.wh_valid, f.cams)
    j = 0
    for i in range(n):
        t = t0 + i * step
        while j < m - 1 and f.t[j + 1] <= t:
            j += 1
        k = j + 1 if j < m - 1 and abs(f.t[j + 1] - t) < abs(f.t[j] - t) else j
        out.t.append(to_fixed(t))
        out.act.append(f.act[k])
        out.wh.append(f.wh[k])
        out.lo.append(f.lo[k])
        out.bad.append(1 if (f.bad[k] or abs(f.t[k] - t) > 1.5) else 0)
    return out


def detect_rallies(raw: Features, options: dict[str, float] | None = None) -> Detection:
    o = {**DEFAULTS, **(options or {})}
    f = _resample(raw)
    n, step = len(f.t), f.step or 0.2
    if n < 10:
        return Detection([], [], [], [], "serves")
    act = smooth(
        [None if f.bad[i] else v for i, v in enumerate(f.act)],
        max(1, js_round(o["smoothSec"] / step)),
    )

    def idx(t: float) -> int:
        return max(0, min(n - 1, js_round((t - f.t[0]) / step)))

    # Per-camera-position levels (gyms, lenses and camera heights differ).
    def cam_at(t: float) -> int:
        c = 0
        for i, start in enumerate(f.cams or [0.0]):
            if t >= start:
                c = i
        return c

    cam_idx = [cam_at(t) for t in f.t]
    lv: dict[int, dict[str, float]] = {}
    for c in set(cam_idx):
        vals: list[float | None] = [
            a for i, a in enumerate(act) if cam_idx[i] == c and not f.bad[i]
        ]
        lev = {
            "still": pct(vals, o["freezeQ"]),
            "end": pct(vals, o["endQ"]),
            "rise": pct(vals, o["riseQ"]) * o["riseMul"],
            "quiet": pct(vals, 0.2),
            "busy": pct(vals, 0.92),
        }
        lev["hi"] = lev["quiet"] + o["hi"] * (lev["busy"] - lev["quiet"])
        lev["lo"] = lev["quiet"] + o["lo"] * (lev["busy"] - lev["quiet"])
        lv[c] = lev

    def lvl(i: int) -> dict[str, float]:
        return lv[cam_idx[i]]

    # Whistles.
    whistles: list[float] = []
    if f.wh_valid:
        diff: list[float | None] = [w - f.lo[i] for i, w in enumerate(f.wh)]
        base = pct(diff, 0.5)
        loud_band = pct(list(f.wh), 0.5)
        for i in range(n):
            if f.bad[i]:
                continue
            d = diff[i]
            assert d is not None
            loud = d - base >= o["whistleDb"] + 8 and f.wh[i] - loud_band >= o["whistleDb"]
            if loud and (not whistles or f.t[i] - whistles[-1] > 3):
                whistles.append(f.t[i])

    # Freezes that end in a jump of motion = serve candidates (time = end of the freeze).
    freezes: list[float] = []
    i = 0
    while i < n:
        if f.bad[i] or act[i] >= lvl(i)["still"]:
            i += 1
            continue
        j = i
        while j < n and not f.bad[j] and act[j] < lvl(j)["still"] * 1.25:
            j += 1
        if (j - i) * step >= o["freezeMin"]:
            k = j
            while k < n and (k - j) * step <= o["riseWithin"]:
                if act[k] > lvl(k)["rise"]:
                    freezes.append(f.t[min(j, n - 1)])
                    break
                k += 1
        i = j + 1

    # Serves: whistles anchor them when the ref is audible; otherwise freezes.
    use_whistles = len(whistles) >= 4
    if use_whistles:
        serves = []
        for w in whistles:
            fz = next((x for x in freezes if w <= x <= w + o["whistleLead"]), None)
            serves.append(fz if fz is not None else w + 2.5)
        for fz in freezes:  # a missed whistle: accept a freeze far from any whistle
            if not any(abs(fz - w) < 12 for w in whistles):
                serves.append(fz)
    else:
        serves = list(freezes)
    serves.sort()
    serves = [s for si, s in enumerate(serves) if si == 0 or s - serves[si - 1] > 4]

    # Rally from each serve until motion settles into a pause.
    dur = f.duration or f.t[n - 1]
    cands: list[dict[str, Any]] = []
    for si, s in enumerate(serves):
        limit = min(s + o["maxRally"], serves[si + 1] - 2 if si + 1 < len(serves) else dur)
        end: float | None = None
        quiet_since: float | None = None
        i = idx(s + 2)
        while i < n and f.t[i] <= limit:
            if f.bad[i]:
                end = f.t[i]
                break
            if act[i] < lvl(i)["end"]:
                if quiet_since is None:
                    quiet_since = f.t[i]
                if f.t[i] - quiet_since >= o["endHold"]:
                    end = quiet_since
                    break
            else:
                quiet_since = None
            i += 1
        if end is None:
            end = min(limit, s + 12)
        whistle = use_whistles and any(0 <= s - w <= o["whistleLead"] for w in whistles)
        if end - s >= o["minRally"] - o["preRoll"]:
            cands.append({"s": s, "end": end, "whistle": whistle})

    # Points are at least min_dead apart; resolve candidates that are closer than that.
    chosen: list[dict[str, Any]] = []
    for cand in cands:
        last = chosen[-1] if chosen else None
        if last is None or cand["s"] >= last["end"] + o["minDead"]:
            chosen.append(cand)
            continue
        if cand["whistle"] and last["whistle"]:
            last["end"] = max(last["s"] + 1, min(last["end"], cand["s"] - 3))
            chosen.append(cand)
            continue
        better = (
            cand["whistle"]
            if cand["whistle"] != last["whistle"]
            else cand["end"] - cand["s"] > last["end"] - last["s"]
        )
        before = chosen[-2] if len(chosen) >= 2 else None
        if better and (before is None or cand["s"] >= before["end"] + o["minDead"]):
            chosen[-1] = cand
    rallies: list[dict[str, Any]] = [
        {
            "start": max(0.0, c["s"] - o["preRoll"]),
            "end": min(dur, c["end"] + o["postRoll"]),
            "serve": to_fixed(c["s"]),
            "whistle": c["whistle"],
        }
        for c in chosen
    ]

    # Fallback for footage without usable freezes: stretches of high motion.
    mode = "serves"
    if len(rallies) < max(3, dur / 120):
        mode = "motion"
        rallies = []
        in_r, start, low_since = False, 0.0, None
        spans: list[tuple[float, float]] = []
        for i in range(n):
            a, t, lev = act[i], f.t[i], lvl(i)
            if f.bad[i]:
                if in_r:
                    spans.append((start, t))
                    in_r = False
                continue
            if not in_r:
                if a > lev["hi"]:
                    in_r, start, low_since = True, t, None
            elif a < lev["lo"]:
                if low_since is None:
                    low_since = t
                if t - low_since >= o["exitHold"]:
                    spans.append((start, low_since))
                    in_r = False
            else:
                low_since = None
        if in_r:
            spans.append((start, f.t[n - 1]))
        for s, e in spans:
            last_r = rallies[-1] if rallies else None
            if last_r is not None and s - o["preRoll"] < last_r["end"] + 1:
                last_r["end"] = min(dur, e + o["postRoll"])
            elif e - s >= o["minRally"]:
                rallies.append(
                    {
                        "start": max(0.0, s - o["preRoll"]),
                        "end": min(dur, e + o["postRoll"]),
                        "whistle": False,
                    }
                )

    # Strength (for display only: normalised motion, not a probability) and tidy numbers.
    for r in rallies:
        total, cnt = 0.0, 0
        for i in range(idx(r["start"]), idx(r["end"]) + 1):
            total += act[i]
            cnt += 1
        lev = lvl(idx(r["start"]))
        r["score"] = to_fixed(
            max(
                0.0,
                min(
                    1.0,
                    (total / max(1, cnt) - lev["quiet"]) / max(1e-6, lev["busy"] - lev["quiet"]),
                ),
            )
        )
        r["start"] = to_fixed(r["start"])
        r["end"] = to_fixed(r["end"])
    for i in range(1, len(rallies)):
        if rallies[i]["start"] < rallies[i - 1]["end"]:
            mid = to_fixed((rallies[i]["start"] + rallies[i - 1]["end"]) / 2)
            rallies[i - 1]["end"] = mid
            rallies[i]["start"] = mid

    breaks: list[dict[str, float]] = []
    pe = 0.0
    for r in [*rallies, {"start": dur, "end": dur}]:
        if r["start"] - pe >= o["breakSec"]:
            breaks.append({"start": to_fixed(pe), "end": to_fixed(r["start"])})
        pe = r["end"]
    return Detection(rallies, breaks, whistles, serves, mode)
