"""Default storage quota 9 GiB, below Cloudflare R2's 10 GB free tier.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW = 9 * 1024**3
OLD = 50 * 1024**3
_update = sa.text("UPDATE profiles SET quota_storage_bytes = :new WHERE quota_storage_bytes = :old")


def upgrade() -> None:
    op.alter_column("profiles", "quota_storage_bytes", server_default=sa.text(str(NEW)))
    op.execute(_update.bindparams(new=NEW, old=OLD))


def downgrade() -> None:
    op.alter_column("profiles", "quota_storage_bytes", server_default=sa.text(str(OLD)))
    op.execute(_update.bindparams(new=OLD, old=NEW))
