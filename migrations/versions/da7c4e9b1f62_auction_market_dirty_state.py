"""add event-driven auction market dirty state

Revision ID: da7c4e9b1f62
Revises: c9e4b7a2d5f8
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "da7c4e9b1f62"
down_revision: str | Sequence[str] | None = "c9e4b7a2d5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auction_market_inventory_generations",
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("generation_signature", sa.String(64), nullable=False),
        sa.Column("changed_cells_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("global_reconciliation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("changed_identity_count", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_auction_market_generation_positive"),
        sa.CheckConstraint(
            "changed_identity_count BETWEEN 0 AND 1000",
            name="ck_auction_market_generation_identity_count",
        ),
        sa.CheckConstraint(
            "length(generation_signature) = 64 AND length(policy_version) <= 64",
            name="ck_auction_market_generation_signatures",
        ),
        sa.CheckConstraint(
            "length(changed_cells_json) <= 32000",
            name="ck_auction_market_generation_cells_bound",
        ),
        sa.PrimaryKeyConstraint("generation"),
        sa.UniqueConstraint("generation_signature"),
    )
    op.create_index(
        "ix_auction_market_inventory_generations_completed_at",
        "auction_market_inventory_generations",
        ["completed_at"],
    )
    op.create_table(
        "auction_market_target_states",
        sa.Column("lot_id", sa.String(36), nullable=False),
        sa.Column("target_signature", sa.String(64), nullable=False),
        sa.Column("coverage_cells_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("validated_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ready','insufficient','error','pending','processing')",
            name="ck_auction_market_target_state_status",
        ),
        sa.CheckConstraint(
            "validated_generation >= 0 AND attempts BETWEEN 0 AND 10000",
            name="ck_auction_market_target_state_counters",
        ),
        sa.CheckConstraint(
            "length(target_signature) = 64 AND length(coverage_cells_json) <= 2048",
            name="ck_auction_market_target_state_bounds",
        ),
        sa.CheckConstraint(
            "status != 'processing' OR (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_auction_market_target_state_claim",
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("lot_id"),
    )
    op.create_index(
        "ix_auction_market_target_state_due",
        "auction_market_target_states",
        ["status", "next_attempt_at", "claim_expires_at", "lot_id"],
    )
    op.create_index(
        "ix_auction_market_target_state_watermark",
        "auction_market_target_states",
        ["validated_generation", "lot_id"],
    )
    op.create_table(
        "auction_market_scan_cursors",
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("scan_cursor_lot_id", sa.String(36), nullable=True),
        sa.Column("high_water_lot_id", sa.String(36), nullable=True),
        sa.Column("latest_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("latest_generation >= 0", name="ck_auction_market_cursor_generation"),
        sa.PrimaryKeyConstraint("policy_version"),
    )


def downgrade() -> None:
    op.drop_table("auction_market_scan_cursors")
    op.drop_index(
        "ix_auction_market_target_state_watermark", table_name="auction_market_target_states"
    )
    op.drop_index("ix_auction_market_target_state_due", table_name="auction_market_target_states")
    op.drop_table("auction_market_target_states")
    op.drop_index(
        "ix_auction_market_inventory_generations_completed_at",
        table_name="auction_market_inventory_generations",
    )
    op.drop_table("auction_market_inventory_generations")
