"""Track the playback proxy's size so storage accounting covers it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("proxy_size_bytes", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_videos_proxy_size_positive",
        "videos",
        "proxy_size_bytes IS NULL OR proxy_size_bytes > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_videos_proxy_size_positive", "videos", type_="check")
    op.drop_column("videos", "proxy_size_bytes")
