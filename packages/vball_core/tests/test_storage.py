from __future__ import annotations

import pytest

from vball_core.storage import MAX_PARTS, MIB, MIN_PART_SIZE, part_plan


@pytest.mark.parametrize(
    "size",
    [
        1,
        MIN_PART_SIZE - 1,
        MIN_PART_SIZE,
        MIN_PART_SIZE + 1,
        5 * 1024**3,
        20 * 1024**3,
        160 * 1024**3,
    ],
)
def test_part_plan_covers_file_within_limits(size: int) -> None:
    part_size, count = part_plan(size)
    assert part_size >= MIN_PART_SIZE
    assert part_size % MIB == 0
    assert count <= MAX_PARTS
    assert (count - 1) * part_size < size <= count * part_size


def test_part_plan_rejects_empty() -> None:
    with pytest.raises(ValueError, match="positive"):
        part_plan(0)
