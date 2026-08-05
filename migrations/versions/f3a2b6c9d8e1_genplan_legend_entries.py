"""genplan legend entries

Revision ID: f3a2b6c9d8e1
Revises: e2b7c9d1a4f0
Create Date: 2026-08-04 23:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a2b6c9d8e1"
down_revision: str | Sequence[str] | None = "e2b7c9d1a4f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "genplan_legend_entries" in inspector.get_table_names():
        return
    op.create_table(
        "genplan_legend_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("color_hex", sa.String(length=7), nullable=False),
        sa.Column("red", sa.Integer(), nullable=False),
        sa.Column("green", sa.Integer(), nullable=False),
        sa.Column("blue", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=48), nullable=False),
        sa.Column("label_ru", sa.String(length=240), nullable=True),
        sa.Column("label_kz", sa.String(length=240), nullable=True),
        sa.Column("target_category", sa.String(length=32), nullable=False),
        sa.Column("layer_kind", sa.String(length=32), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("pixel_count", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["genplan_source_documents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id",
            "color_hex",
            "source",
            name="uq_genplan_legend_entry_document_color_source",
        ),
    )
    op.create_index(
        "ix_genplan_legend_entries_document_id",
        "genplan_legend_entries",
        ["document_id"],
    )
    op.create_index(
        "ix_genplan_legend_entries_color_hex",
        "genplan_legend_entries",
        ["color_hex"],
    )
    op.create_index(
        "ix_genplan_legend_entries_review",
        "genplan_legend_entries",
        ["review_status", "target_category", "layer_kind"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "genplan_legend_entries" not in inspector.get_table_names():
        return
    op.drop_index("ix_genplan_legend_entries_review", table_name="genplan_legend_entries")
    op.drop_index("ix_genplan_legend_entries_color_hex", table_name="genplan_legend_entries")
    op.drop_index("ix_genplan_legend_entries_document_id", table_name="genplan_legend_entries")
    op.drop_table("genplan_legend_entries")
