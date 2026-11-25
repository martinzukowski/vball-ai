from __future__ import annotations

import uuid
from dataclasses import dataclass

from vball_core.rallies import apply_edits, overlaps


@dataclass
class R:
    id: uuid.UUID
    start_t: float
    end_t: float
    serve_t: float | None = None


@dataclass
class E:
    id: uuid.UUID
    op: str
    base_rally_id: uuid.UUID | None = None
    base_edit_id: uuid.UUID | None = None
    start_t: float | None = None
    end_t: float | None = None


def uid() -> uuid.UUID:
    return uuid.uuid4()


def test_edits_layer_on_predictions() -> None:
    a, b, c = R(uid(), 10, 20, 12), R(uid(), 30, 40), R(uid(), 50, 60)
    add = E(uid(), "add", start_t=70, end_t=75)
    edits = [
        E(uid(), "modify", base_rally_id=a.id, start_t=11, end_t=18),
        E(uid(), "delete", base_rally_id=b.id),
        add,
        E(uid(), "modify", base_edit_id=add.id, start_t=69, end_t=76),
        E(uid(), "delete", base_rally_id=uid()),  # stale: rally from another run
    ]
    out = apply_edits([c, a, b], edits)
    assert [(r.start_t, r.end_t, r.source, r.edited) for r in out] == [
        (11, 18, "model", True),
        (50, 60, "model", False),
        (69, 76, "user", True),
    ]
    assert out[0].serve_t == 12
    assert (out[0].predicted_start_t, out[0].predicted_end_t) == (10, 20)
    assert out[2].key == add.id


def test_trim_past_serve_drops_it() -> None:
    a = R(uid(), 10, 20, 12)
    out = apply_edits([a], [E(uid(), "modify", base_rally_id=a.id, start_t=13, end_t=20)])
    assert out[0].serve_t is None


def test_later_edit_wins() -> None:
    a = R(uid(), 10, 20)
    edits = [
        E(uid(), "modify", base_rally_id=a.id, start_t=9, end_t=19),
        E(uid(), "modify", base_rally_id=a.id, start_t=8, end_t=15),
    ]
    assert apply_edits([a], edits)[0].start_t == 8


def test_overlaps_ignores_itself() -> None:
    a = R(uid(), 10, 20)
    rs = apply_edits([a], [])
    assert overlaps(rs, 15, 25, None)
    assert not overlaps(rs, 15, 25, a.id)
    assert not overlaps(rs, 20, 25, None)  # touching is fine
