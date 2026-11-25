"""Rally detection metrics: one-to-one matching on temporal IoU (Hungarian), precision /
recall / F1 at tIoU thresholds, and start/end errors of matched rallies."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linear_sum_assignment

Span = tuple[float, float]


def tiou(a: Span, b: Span) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


@dataclass(frozen=True)
class Scores:
    n_ref: int
    n_pred: int
    f1_50: float
    f1_70: float
    precision_50: float
    recall_50: float
    start_err_median: float
    end_err_median: float

    def row(self) -> str:
        return (
            f"F1@0.5 {self.f1_50:.2f} (P {self.precision_50:.2f} R {self.recall_50:.2f}) | "
            f"F1@0.7 {self.f1_70:.2f} | start err {self.start_err_median:.1f}s | "
            f"end err {self.end_err_median:.1f}s | {self.n_pred} predicted / {self.n_ref} true"
        )


def _f1(tp: int, n_pred: int, n_ref: int) -> tuple[float, float, float]:
    p = tp / n_pred if n_pred else 0.0
    r = tp / n_ref if n_ref else 0.0
    return (2 * p * r / (p + r) if p + r else 0.0), p, r


def score(ref: Sequence[Span], pred: Sequence[Span]) -> Scores:
    if not ref or not pred:
        return Scores(len(ref), len(pred), 0, 0, 0, 0, float("nan"), float("nan"))
    m = np.array([[tiou(r, p) for p in pred] for r in ref])
    ri, pi = linear_sum_assignment(-m)
    ious = m[ri, pi]
    f50, p50, r50 = _f1(int((ious >= 0.5).sum()), len(pred), len(ref))
    f70, _, _ = _f1(int((ious >= 0.7).sum()), len(pred), len(ref))
    pairs = [(ref[int(i)], pred[int(j)]) for i, j in zip(ri, pi, strict=True) if m[i, j] >= 0.5]
    se = [abs(p[0] - r[0]) for r, p in pairs] or [float("nan")]
    ee = [abs(p[1] - r[1]) for r, p in pairs] or [float("nan")]
    return Scores(
        len(ref), len(pred), f50, f70, p50, r50, statistics.median(se), statistics.median(ee)
    )
