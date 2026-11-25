"""The Python port must give the same rallies as the original detect.js on the same inputs."""

from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vball_worker.rally.heuristic import Features, detect_rallies, sensitivity_options

RUNNER = Path(__file__).with_name("js_detect.cjs")


def synthetic_match(
    seed: int, minutes: float = 12, freezes: bool = True, jitter: bool = True
) -> Features:
    """Plausible signals: walking between points, a freeze before each serve, a burst of play,
    a pause after; whistles on some points; occasional camera moves; jittery sample times."""
    rng = random.Random(seed)
    step = 0.2
    t, act, wh, lo, bad = [], [], [], [], []
    cams = [0.0]
    clock = rng.uniform(0, 0.15) if jitter else 0.0
    audible = rng.random() < 0.6
    noise = rng.uniform(0.005, 0.03)

    def emit(level: float, *, whistle: bool = False, is_bad: bool = False) -> None:
        nonlocal clock
        t.append(round(clock, 2))
        act.append(max(0.0, round(level + rng.gauss(0, noise), 4)))
        base = -70 + rng.gauss(0, 3)
        wh.append(round(base + (35 if whistle else 0), 1))
        lo.append(round(-60 + rng.gauss(0, 3), 1))
        bad.append(1 if is_bad else 0)
        clock = clock + step + rng.uniform(-0.03, 0.05) if jitter else round(clock + step, 1)

    end = minutes * 60
    while clock < end:
        for _ in range(int(rng.uniform(4, 14) / step)):  # walking / ball fetching
            emit(rng.uniform(0.08, 0.3))
        if rng.random() < 0.05:  # timeout
            for _ in range(int(rng.uniform(30, 70) / step)):
                emit(rng.uniform(0.02, 0.15))
        if rng.random() < 0.04:  # camera bumped
            for _ in range(int(rng.uniform(2, 6) / step)):
                emit(0.5, is_bad=True)
            cams.append(round(clock, 2))
        if rng.random() < 0.3:  # a short pause 3-5 s before the real one (near-duplicate serve)
            for _ in range(int(rng.uniform(1.0, 1.6) / step)):
                emit(rng.uniform(0.0, 0.03))
            for _ in range(int(rng.uniform(1.0, 2.6) / step)):
                emit(rng.uniform(0.3, 0.5))
        whistle_now = audible and rng.random() < 0.85
        for k in range(int(rng.uniform(1.0, 2.5) / step)):  # pre-serve freeze
            level = rng.uniform(0.0, 0.04) if freezes else rng.uniform(0.1, 0.3)
            emit(level, whistle=whistle_now and k == 1)
        for _ in range(int(rng.uniform(3, 28) / step)):  # rally
            emit(rng.uniform(0.2, 0.6))
        for _ in range(int(rng.uniform(0.8, 2) / step)):  # ball dead
            emit(rng.uniform(0.0, 0.05))
    return Features(t, act, wh, lo, bad, duration=round(clock, 2), wh_valid=audible, cams=cams)


def run_js(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI for the parity test")
        pytest.skip("node not installed")
    out = subprocess.run(
        [node, str(RUNNER)], input=json.dumps(cases), capture_output=True, text=True, check=True
    )
    result: list[dict[str, Any]] = json.loads(out.stdout)
    return result


def normalise(r: dict[str, Any]) -> dict[str, Any]:
    keys = ("start", "end", "serve", "whistle", "score")
    return {k: r[k] for k in keys if k in r}


# Seeds 100-109 have no pre-serve freezes (forces the motion-level fallback path); seeds
# 200-219 use perfectly regular sample times so exact-equality boundaries (>=) are exercised.
SEEDS = [*range(30), *range(100, 110), *range(200, 220)]
CASES = [(seed, sens) for seed in SEEDS for sens in (0, -3, 4)]


def match_for(seed: int) -> Features:
    return synthetic_match(seed, freezes=not 100 <= seed < 110, jitter=seed < 200)


def test_python_port_matches_original_detect_js() -> None:
    feats = {seed: match_for(seed) for seed in SEEDS}
    cases = [
        {"features": feats[seed].to_json(), "options": sensitivity_options(sens)}
        for seed, sens in CASES
    ]
    js_results = run_js(cases)
    modes = set()
    total = 0
    for (seed, sens), js in zip(CASES, js_results, strict=True):
        py = detect_rallies(feats[seed], sensitivity_options(sens))
        where = f"seed={seed} sensitivity={sens}"
        assert py.mode == js["mode"], where
        assert py.whistles == pytest.approx(js["whistles"]), where
        assert py.serves == pytest.approx(js["serves"]), where
        assert [normalise(r) for r in py.rallies] == [normalise(r) for r in js["rallies"]], where
        assert py.breaks == js["breaks"], where
        modes.add(py.mode)
        total += len(py.rallies)
    # Make sure the synthetic data exercised real behaviour, not just empty results.
    assert total > 300
    assert modes == {"serves", "motion"}


def test_short_input_returns_nothing() -> None:
    f = Features([0.0, 0.2], [0.1, 0.2], [-70, -70], [-60, -60], [0, 0], duration=0.4)
    assert detect_rallies(f).rallies == []


def test_json_roundtrip() -> None:
    f = synthetic_match(1, minutes=1)
    g = Features.from_json(json.loads(json.dumps(f.to_json())))
    assert g == f
    assert math.isclose(g.duration, f.duration)
