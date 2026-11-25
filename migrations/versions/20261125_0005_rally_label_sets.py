"""rally label sets

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-25 11:27:55.010136
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rally_label_sets",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("video_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("base_run_id", sa.UUID(), nullable=True),
        sa.Column("duration_s", sa.Double(), nullable=False),
        sa.Column("rallies", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("n_rallies", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rallies) = 'array'", name=op.f("ck_rally_label_sets_rallies_array")
        ),
        sa.CheckConstraint("duration_s > 0", name=op.f("ck_rally_label_sets_duration_positive")),
        sa.CheckConstraint(
            "n_rallies = jsonb_array_length(rallies)",
            name=op.f("ck_rally_label_sets_count_matches"),
        ),
        sa.ForeignKeyConstraint(
            ["base_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_rally_label_sets_base_run_id_analysis_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["profiles.id"],
            name=op.f("fk_rally_label_sets_user_id_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_rally_label_sets_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rally_label_sets")),
    )
    op.create_index(
        op.f("ix_rally_label_sets_base_run_id"), "rally_label_sets", ["base_run_id"], unique=False
    )
    op.create_index(
        op.f("ix_rally_label_sets_user_id"), "rally_label_sets", ["user_id"], unique=False
    )
    op.execute('ALTER TABLE "rally_label_sets" ENABLE ROW LEVEL SECURITY')
    op.create_index(
        op.f("ix_rally_label_sets_video_id"), "rally_label_sets", ["video_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rally_label_sets_video_id"), table_name="rally_label_sets")
    op.drop_index(op.f("ix_rally_label_sets_user_id"), table_name="rally_label_sets")
    op.drop_index(op.f("ix_rally_label_sets_base_run_id"), table_name="rally_label_sets")
    op.drop_table("rally_label_sets")
