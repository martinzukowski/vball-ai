"""Per-video play area polygon used to measure motion.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = (
    "play_area IS NULL OR (jsonb_typeof(play_area) = 'array' "
    "AND jsonb_array_length(play_area) BETWEEN 3 AND 16)"
)


def upgrade() -> None:
    op.add_column("videos", sa.Column("play_area", postgresql.JSONB(), nullable=True))
    op.create_check_constraint("ck_videos_play_area_polygon", "videos", CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_videos_play_area_polygon", "videos", type_="check")
    op.drop_column("videos", "play_area")
