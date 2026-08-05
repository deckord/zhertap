"""genplan source documents

Revision ID: e2b7c9d1a4f0
Revises: c9e1a7b4d2f6
Create Date: 2026-08-04 22:25:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2b7c9d1a4f0"
down_revision: str | Sequence[str] | None = "c9e1a7b4d2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "genplan_source_documents" in inspector.get_table_names():
        return
    op.create_table(
        "genplan_source_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.String(length=96), nullable=False),
        sa.Column("region", sa.String(length=160), nullable=False),
        sa.Column("district", sa.String(length=160), nullable=False),
        sa.Column("locality", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("filename", sa.String(length=260), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=120), nullable=False),
        sa.Column("detected_format", sa.String(length=24), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("pdf_route", sa.String(length=24), nullable=True),
        sa.Column("has_text_layer", sa.Boolean(), nullable=False),
        sa.Column("vector_object_count", sa.Integer(), nullable=False),
        sa.Column("image_count", sa.Integer(), nullable=False),
        sa.Column("max_image_width", sa.Integer(), nullable=True),
        sa.Column("max_image_height", sa.Integer(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("pipeline_status", sa.String(length=48), nullable=False),
        sa.Column("next_action", sa.String(length=120), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_metadata_json", sa.Text(), nullable=True),
        sa.Column("ingested_by", sa.String(length=120), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", name="uq_genplan_source_document_asset_id"),
    )
    op.create_index(
        "ix_genplan_source_documents_asset_id",
        "genplan_source_documents",
        ["asset_id"],
    )
    op.create_index(
        "ix_genplan_source_documents_scope",
        "genplan_source_documents",
        ["region", "district", "locality"],
    )
    op.create_index(
        "ix_genplan_source_documents_status",
        "genplan_source_documents",
        ["pipeline_status", "detected_format"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "genplan_source_documents" not in inspector.get_table_names():
        return
    op.drop_index("ix_genplan_source_documents_status", table_name="genplan_source_documents")
    op.drop_index("ix_genplan_source_documents_scope", table_name="genplan_source_documents")
    op.drop_index("ix_genplan_source_documents_asset_id", table_name="genplan_source_documents")
    op.drop_table("genplan_source_documents")
