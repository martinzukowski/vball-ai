from __future__ import annotations

import numpy as np

from vball_ml.evaluate import score, tiou
from vball_ml.rally_model import STEP, decode, labels_for


def test_tiou() -> None:
    assert tiou((0, 10), (0, 10)) == 1
    assert tiou((0, 10), (5, 15)) == 5 / 15
    assert tiou((0, 1), (2, 3)) == 0


def test_score_perfect_and_partial() -> None:
    ref = [(10.0, 20.0), (30.0, 40.0), (50.0, 60.0)]
    assert score(ref, ref).f1_50 == 1
    s = score(ref, [(10.5, 20.5), (30.0, 33.0)])  # one good, one too short
    assert s.recall_50 == 1 / 3
    assert s.precision_50 == 1 / 2
    assert s.start_err_median == 0.5


def test_decode_roundtrips_labels() -> None:
    rallies = [(5.0, 15.0), (20.0, 30.0), (31.0, 33.0)]
    y = labels_for(int(40 / STEP), rallies)
    spans = decode(y.astype(np.float32))
    assert len(spans) == 2  # gap < 1 s joined: (20-30) + (31-33)
    assert abs(spans[0][0] - 5.0) <= 0.4
    assert abs(spans[0][1] - 15.0) <= 0.4


def test_decode_drops_blips() -> None:
    y = labels_for(200, [(10.0, 11.0)])
    assert decode(y) == []
