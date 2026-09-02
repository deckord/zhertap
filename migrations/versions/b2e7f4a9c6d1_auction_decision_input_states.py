"""add auction decision input worker states

Revision ID: b2e7f4a9c6d1
Revises: a6d4e8f1c2b7
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2e7f4a9c6d1"
down_revision: str | Sequence[str] | None = "a6d4e8f1c2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auction_decision_input_states",
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("source_watermark_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lot_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("history_generation", sa.BigInteger(), nullable=True),
        sa.Column("market_signature", sa.String(length=64), nullable=True),
        sa.Column("market_watermark_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("market_watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("market_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("document_signature", sa.String(length=64), nullable=True),
        sa.Column("document_watermark_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("document_watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("assembler_version", sa.String(length=64), nullable=False),
        sa.Column("spatial_assembler_version", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.String(length=500), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'insufficient', 'error')",
            name="ck_auction_decision_input_state_status",
        ),
        sa.CheckConstraint(
            "source_watermark_id >= 0 AND market_watermark_id >= 0 AND "
            "market_row_count >= 0 AND document_watermark_id >= 0 AND "
            "document_row_count >= 0 AND retry_count >= 0 AND retry_count <= 20",
            name="ck_auction_decision_input_state_counters",
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("lot_id"),
    )
    op.create_index(
        "ix_auction_decision_input_states_status",
        "auction_decision_input_states",
        ["status"],
    )
    op.create_index(
        "ix_auction_decision_input_states_source_watermark_id",
        "auction_decision_input_states",
        ["source_watermark_id"],
    )
    op.create_index(
        "ix_auction_decision_input_states_source_watermark_at",
        "auction_decision_input_states",
        ["source_watermark_at"],
    )
    op.create_index(
        "ix_auction_decision_input_states_input_hash",
        "auction_decision_input_states",
        ["input_hash"],
    )
    op.create_index(
        "ix_auction_decision_input_states_market_watermark_id",
        "auction_decision_input_states",
        ["market_watermark_id"],
    )
    op.create_index(
        "ix_auction_decision_input_states_market_watermark_at",
        "auction_decision_input_states",
        ["market_watermark_at"],
    )
    op.create_index(
        "ix_auction_decision_input_states_document_watermark_id",
        "auction_decision_input_states",
        ["document_watermark_id"],
    )
    op.create_index(
        "ix_auction_decision_input_states_document_watermark_at",
        "auction_decision_input_states",
        ["document_watermark_at"],
    )
    op.create_index(
        "ix_auction_decision_input_states_claim_token",
        "auction_decision_input_states",
        ["claim_token"],
    )
    op.create_index(
        "ix_auction_decision_input_states_claim_expires_at",
        "auction_decision_input_states",
        ["claim_expires_at"],
    )
    op.create_index(
        "ix_auction_decision_input_states_next_attempt_at",
        "auction_decision_input_states",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_auction_decision_input_states_validated_at",
        "auction_decision_input_states",
        ["validated_at"],
    )
    op.create_index(
        "ix_auction_decision_input_state_work",
        "auction_decision_input_states",
        ["status", "next_attempt_at", "updated_at"],
    )
    op.create_index(
        "ix_auction_decision_input_state_watermark",
        "auction_decision_input_states",
        ["source_watermark_id", "validated_at"],
    )


def downgrade() -> None:
    op.drop_table("auction_decision_input_states")
