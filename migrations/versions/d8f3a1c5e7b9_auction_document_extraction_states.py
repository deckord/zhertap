"""add durable auction document extraction states

Revision ID: d8f3a1c5e7b9
Revises: b2e7f4a9c6d1
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "d8f3a1c5e7b9"
down_revision: str | Sequence[str] | None = "b2e7f4a9c6d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auction_document_extraction_states",
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.Column("document_signature", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("document_path", sa.String(length=2048), nullable=False),
        sa.Column("extractor_version", sa.String(length=64), nullable=False),
        sa.Column("writer_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("current_evidence_id", sa.Integer(), nullable=True),
        sa.Column("current_evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'terminal', 'retryable')",
            name="ck_auction_document_extraction_state_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= 10000",
            name="ck_auction_document_extraction_state_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["auction_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["current_evidence_id"], ["auction_evidence.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("document_id"),
    )
    op.create_index(
        "ix_auction_document_extraction_states_lot_id",
        "auction_document_extraction_states",
        ["lot_id"],
    )
    op.create_index(
        "ix_auction_document_extraction_state_work",
        "auction_document_extraction_states",
        ["status", "next_attempt_at", "document_id"],
    )
    op.create_index(
        "ix_auction_document_extraction_state_validation",
        "auction_document_extraction_states",
        ["status", "last_validated_at", "document_id"],
    )
    op.create_index(
        "ix_auction_document_extraction_state_claim",
        "auction_document_extraction_states",
        ["status", "claim_expires_at", "document_id"],
    )
    op.create_index(
        "ix_auction_documents_downloaded_id",
        "auction_documents",
        ["downloaded_at", "id"],
    )
    op.create_table(
        "auction_document_extraction_cursors",
        sa.Column("cursor_key", sa.String(length=32), nullable=False),
        sa.Column("backfill_document_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("backfill_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("watermark_downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("watermark_document_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("cursor_key"),
    )
    cursor_table = sa.table(
        "auction_document_extraction_cursors",
        sa.column("cursor_key", sa.String(length=32)),
        sa.column("backfill_document_id", sa.Integer()),
        sa.column("backfill_complete", sa.Boolean()),
        sa.column("watermark_document_id", sa.Integer()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        cursor_table,
        [
            {
                "cursor_key": "default",
                "backfill_document_id": 0,
                "backfill_complete": False,
                "watermark_document_id": 0,
                "updated_at": datetime.now(UTC),
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("auction_document_extraction_cursors")
    op.drop_index("ix_auction_documents_downloaded_id", table_name="auction_documents")
    op.drop_index(
        "ix_auction_document_extraction_state_claim",
        table_name="auction_document_extraction_states",
    )
    op.drop_index(
        "ix_auction_document_extraction_state_validation",
        table_name="auction_document_extraction_states",
    )
    op.drop_index(
        "ix_auction_document_extraction_state_work",
        table_name="auction_document_extraction_states",
    )
    op.drop_index(
        "ix_auction_document_extraction_states_lot_id",
        table_name="auction_document_extraction_states",
    )
    op.drop_table("auction_document_extraction_states")
