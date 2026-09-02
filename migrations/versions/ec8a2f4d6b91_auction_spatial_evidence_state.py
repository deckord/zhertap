"""add durable auction spatial evidence state and manifest outbox

Revision ID: ec8a2f4d6b91
Revises: da7c4e9b1f62
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ec8a2f4d6b91"
down_revision: str | Sequence[str] | None = "da7c4e9b1f62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auction_spatial_manifest_expectations",
        sa.Column("lot_id", sa.String(36), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("checklist_hash", sa.String(64), nullable=False),
        sa.Column("required_feed_keys_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(checklist_hash) = 64 AND length(required_feed_keys_json) <= 16384",
            name="ck_auction_spatial_expectation_bounds",
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("lot_id"),
    )
    op.create_table(
        "auction_spatial_feed_states",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("lot_id", sa.String(36), nullable=False),
        sa.Column("module", sa.String(16), nullable=False),
        sa.Column("provider_id", sa.String(128), nullable=False),
        sa.Column("feed_id", sa.String(128), nullable=False),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("input_signature", sa.String(64), nullable=False),
        sa.Column("current_evidence_id", sa.Integer(), nullable=True),
        sa.Column("current_generation_id", sa.String(64), nullable=True),
        sa.Column("current_payload_hash", sa.String(64), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_validation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(128), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_from_status", sa.String(24), nullable=True),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "module IN ('restrictions','site','planning')",
            name="ck_auction_spatial_feed_module",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','ready','conflict','retryable',"
            "'terminal','quarantined','expired')",
            name="ck_auction_spatial_feed_status",
        ),
        sa.CheckConstraint(
            "attempts BETWEEN 0 AND 10000", name="ck_auction_spatial_feed_attempts"
        ),
        sa.CheckConstraint(
            "length(identity_key) = 64 AND length(input_signature) = 64 AND "
            "(current_generation_id IS NULL OR length(current_generation_id) = 64) AND "
            "(current_payload_hash IS NULL OR length(current_payload_hash) = 64)",
            name="ck_auction_spatial_feed_hashes",
        ),
        sa.CheckConstraint(
            "status != 'processing' OR "
            "(claim_token IS NOT NULL AND claim_expires_at IS NOT NULL "
            "AND claimed_from_status IS NOT NULL)",
            name="ck_auction_spatial_feed_claim",
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["current_evidence_id"], ["auction_evidence.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lot_id",
            "module",
            "provider_id",
            "feed_id",
            name="uq_auction_spatial_feed_identity",
        ),
        sa.UniqueConstraint("identity_key", name="uq_auction_spatial_feed_identity_key"),
    )
    op.create_index(
        "ix_auction_spatial_feed_lot_module",
        "auction_spatial_feed_states",
        ["lot_id", "module", "id"],
    )
    op.create_index(
        "ix_auction_spatial_feed_pending",
        "auction_spatial_feed_states",
        ["status", "id"],
    )
    op.create_index(
        "ix_auction_spatial_feed_retry_due",
        "auction_spatial_feed_states",
        ["status", "next_attempt_at", "id"],
    )
    op.create_index(
        "ix_auction_spatial_feed_claim_due",
        "auction_spatial_feed_states",
        ["status", "claim_expires_at", "id"],
    )
    op.create_index(
        "ix_auction_spatial_feed_validation_due",
        "auction_spatial_feed_states",
        ["status", "next_validation_at", "expires_at", "id"],
    )
    op.create_table(
        "auction_spatial_generation_manifests",
        sa.Column("lot_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("module_generations_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("missing_feed_keys_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("blocking_feed_keys_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("watermark", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('complete','incomplete','conflict')",
            name="ck_auction_spatial_manifest_status",
        ),
        sa.CheckConstraint(
            "length(manifest_hash) = 64 AND length(module_generations_json) <= 8192 "
            "AND length(missing_feed_keys_json) <= 8192 "
            "AND length(blocking_feed_keys_json) <= 8192",
            name="ck_auction_spatial_manifest_bounds",
        ),
        sa.CheckConstraint(
            "watermark >= 1", name="ck_auction_spatial_manifest_watermark"
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("lot_id"),
    )
    op.create_index(
        "ix_auction_spatial_manifest_status",
        "auction_spatial_generation_manifests",
        ["status", "settled", "updated_at"],
    )
    op.create_index(
        "ix_auction_spatial_manifest_expiry",
        "auction_spatial_generation_manifests",
        ["expires_at", "lot_id"],
    )
    op.create_table(
        "auction_spatial_decision_signals",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("lot_id", sa.String(36), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("manifest_watermark", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','dispatched','failed')",
            name="ck_auction_spatial_signal_status",
        ),
        sa.CheckConstraint(
            "length(manifest_hash) = 64 AND manifest_watermark >= 1 "
            "AND attempts BETWEEN 0 AND 10000",
            name="ck_auction_spatial_signal_bounds",
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lot_id", "manifest_watermark", name="uq_auction_spatial_signal_watermark"
        ),
    )
    op.create_index(
        "ix_auction_spatial_signal_due",
        "auction_spatial_decision_signals",
        ["status", "next_attempt_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auction_spatial_signal_due", table_name="auction_spatial_decision_signals"
    )
    op.drop_table("auction_spatial_decision_signals")
    op.drop_index(
        "ix_auction_spatial_manifest_expiry",
        table_name="auction_spatial_generation_manifests",
    )
    op.drop_index(
        "ix_auction_spatial_manifest_status",
        table_name="auction_spatial_generation_manifests",
    )
    op.drop_table("auction_spatial_generation_manifests")
    op.drop_index(
        "ix_auction_spatial_feed_validation_due",
        table_name="auction_spatial_feed_states",
    )
    op.drop_index(
        "ix_auction_spatial_feed_claim_due", table_name="auction_spatial_feed_states"
    )
    op.drop_index(
        "ix_auction_spatial_feed_retry_due", table_name="auction_spatial_feed_states"
    )
    op.drop_index(
        "ix_auction_spatial_feed_pending", table_name="auction_spatial_feed_states"
    )
    op.drop_index(
        "ix_auction_spatial_feed_lot_module", table_name="auction_spatial_feed_states"
    )
    op.drop_table("auction_spatial_feed_states")
    op.drop_table("auction_spatial_manifest_expectations")
