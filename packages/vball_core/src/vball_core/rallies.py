"""Effective rallies = the active run's predictions with the user's edits applied.

Predictions are never modified. Edits are append-only and replayed in order:
* ``add`` creates a user rally whose key is the edit's id (survives re-analysis),
* ``modify`` / ``delete`` target either a predicted rally (``base_rally_id``) or a user rally
  (``base_edit_id``).
Edits that target predictions of a run that is no longer active are ignored (re-analysis
starts from fresh predictions; user-added rallies carry over).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

Source = Literal["model", "user"]


class RallyLike(Protocol):
    @property
    def id(self) -> uuid.UUID: ...
    @property
    def start_t(self) -> float: ...
    @property
    def end_t(self) -> float: ...
    @property
    def serve_t(self) -> float | None: ...


class EditLike(Protocol):
    @property
    def id(self) -> uuid.UUID: ...
    @property
    def op(self) -> Any: ...  # str or EditOp
    @property
    def base_rally_id(self) -> uuid.UUID | None: ...
    @property
    def base_edit_id(self) -> uuid.UUID | None: ...
    @property
    def start_t(self) -> float | None: ...
    @property
    def end_t(self) -> float | None: ...


@dataclass(frozen=True, slots=True)
class EffectiveRally:
    key: uuid.UUID
    source: Source
    start_t: float
    end_t: float
    serve_t: float | None
    edited: bool
    predicted_start_t: float | None = None  # model's original bounds, for training labels
    predicted_end_t: float | None = None


def apply_edits(base: Iterable[RallyLike], edits: Iterable[EditLike]) -> list[EffectiveRally]:
    items: dict[uuid.UUID, EffectiveRally] = {
        r.id: EffectiveRally(
            r.id, "model", r.start_t, r.end_t, r.serve_t, False, r.start_t, r.end_t
        )
        for r in base
    }
    for e in edits:
        op = str(getattr(e.op, "value", e.op))
        if op == "add":
            if e.start_t is None or e.end_t is None:
                continue
            items[e.id] = EffectiveRally(e.id, "user", e.start_t, e.end_t, None, True)
            continue
        target = e.base_rally_id or e.base_edit_id
        if target is None or target not in items:
            continue  # stale edit for a previous run
        if op == "delete":
            del items[target]
        elif op == "modify":
            if e.start_t is None or e.end_t is None:
                continue
            cur = items[target]
            serve = (
                cur.serve_t
                if cur.serve_t is not None and e.start_t <= cur.serve_t <= e.end_t
                else None
            )
            items[target] = replace(
                cur, start_t=e.start_t, end_t=e.end_t, serve_t=serve, edited=True
            )
    return sorted(items.values(), key=lambda r: r.start_t)


def overlaps(
    rallies: Iterable[EffectiveRally], start: float, end: float, ignore: uuid.UUID | None
) -> bool:
    return any(r.key != ignore and r.start_t < end and start < r.end_t for r in rallies)


@dataclass(frozen=True, slots=True)
class ReferenceComparison:
    source: str
    n_reference: int
    n_ours: int
    matched: int  # one-to-one pairs overlapping by >= 50% (temporal IoU)
    f1: float
    matched_keys: frozenset[uuid.UUID]  # our rallies that match a reference rally
    missed: list[tuple[float, float]]  # reference rallies we have no match for


def _tiou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def compare_to_reference(
    ours: list[EffectiveRally], reference: list[tuple[float, float]], source: str
) -> ReferenceComparison:
    """Greedy one-to-one matching by temporal IoU >= 0.5 (same criterion as the ML eval)."""
    pairs = sorted(
        (
            (_tiou((r.start_t, r.end_t), ref), i, j)
            for i, r in enumerate(ours)
            for j, ref in enumerate(reference)
        ),
        reverse=True,
    )
    used_o: set[int] = set()
    used_r: set[int] = set()
    for iou, i, j in pairs:
        if iou < 0.5:
            break
        if i in used_o or j in used_r:
            continue
        used_o.add(i)
        used_r.add(j)
    m = len(used_o)
    p = m / len(ours) if ours else 0.0
    rec = m / len(reference) if reference else 0.0
    return ReferenceComparison(
        source=source,
        n_reference=len(reference),
        n_ours=len(ours),
        matched=m,
        f1=round(2 * p * rec / (p + rec), 3) if p + rec else 0.0,
        matched_keys=frozenset(ours[i].key for i in used_o),
        missed=[reference[j] for j in range(len(reference)) if j not in used_r],
    )
