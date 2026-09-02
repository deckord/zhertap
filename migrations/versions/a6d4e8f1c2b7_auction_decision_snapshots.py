"""add immutable auction decision snapshots

Revision ID: a6d4e8f1c2b7
Revises: f8c1d2e3a4b5
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6d4e8f1c2b7"
down_revision: str | Sequence[str] | None = "f8c1d2e3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_decision_snapshots" in inspector.get_table_names():
        return
    op.create_table(
        "auction_decision_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.Column("engine_version", sa.String(length=64), nullable=False),
        sa.Column("rules_version", sa.String(length=64), nullable=False),
        sa.Column("verdict_engine_version", sa.String(length=64), nullable=False),
        sa.Column("scenario_engine_version", sa.String(length=64), nullable=True),
        sa.Column("price_engine_version", sa.String(length=64), nullable=True),
        sa.Column("formula_version", sa.String(length=64), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("data_readiness", sa.String(length=24), nullable=False),
        sa.Column("scenario_key", sa.String(length=64), nullable=False),
        sa.Column("repeat_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("has_repeat", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bid_ceiling_kzt", sa.BigInteger(), nullable=True),
        sa.Column("fair_value_low_kzt", sa.BigInteger(), nullable=True),
        sa.Column("fair_value_high_kzt", sa.BigInteger(), nullable=True),
        sa.Column(
            "evidence_generation_ids_json", sa.Text(), nullable=False, server_default="{}"
        ),
        sa.Column("source_freshness_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("stale_reasons_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validated_evidence_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('participate', 'participate_up_to', 'requires_check', "
            "'high_risk', 'do_not_participate')",
            name="ck_auction_decision_snapshot_verdict",
        ),
        sa.CheckConstraint(
            "data_readiness IN ('complete', 'partial', 'insufficient', 'error')",
            name="ck_auction_decision_snapshot_readiness",
        ),
        sa.CheckConstraint(
            "repeat_attempt_count >= 0 AND repeat_attempt_count <= 10000",
            name="ck_auction_decision_snapshot_repeat_bounds",
        ),
        sa.CheckConstraint(
            "validated_evidence_id >= 0",
            name="ck_auction_decision_snapshot_validated_evidence_nonnegative",
        ),
        sa.CheckConstraint(
            "bid_ceiling_kzt IS NULL OR "
            "(bid_ceiling_kzt >= 0 AND bid_ceiling_kzt <= 1000000000000000)",
            name="ck_auction_decision_snapshot_bid_bounds",
        ),
        sa.CheckConstraint(
            "fair_value_low_kzt IS NULL OR "
            "(fair_value_low_kzt >= 0 AND fair_value_low_kzt <= 1000000000000000)",
            name="ck_auction_decision_snapshot_fair_low_bounds",
        ),
        sa.CheckConstraint(
            "fair_value_high_kzt IS NULL OR "
            "(fair_value_high_kzt >= 0 AND fair_value_high_kzt <= 1000000000000000)",
            name="ck_auction_decision_snapshot_fair_high_bounds",
        ),
        sa.CheckConstraint(
            "fair_value_low_kzt IS NULL OR fair_value_high_kzt IS NULL OR "
            "fair_value_low_kzt <= fair_value_high_kzt",
            name="ck_auction_decision_snapshot_fair_order",
        ),
        sa.CheckConstraint(
            "(verdict = 'participate_up_to' AND bid_ceiling_kzt IS NOT NULL) OR "
            "(verdict <> 'participate_up_to' AND bid_ceiling_kzt IS NULL)",
            name="ck_auction_decision_snapshot_bid_verdict",
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lot_id",
            "engine_version",
            "rules_version",
            "input_hash",
            name="uq_auction_decision_snapshot_input",
        ),
    )
    op.create_index(
        "ix_auction_decision_snapshots_lot_id",
        "auction_decision_snapshots",
        ["lot_id"],
    )
    op.create_index(
        "ix_auction_decision_snapshots_checked_at",
        "auction_decision_snapshots",
        ["checked_at"],
    )
    op.create_index(
        "ix_auction_decision_snapshots_last_validated_at",
        "auction_decision_snapshots",
        ["last_validated_at"],
    )
    op.create_index(
        "uq_auction_decision_snapshot_current",
        "auction_decision_snapshots",
        ["lot_id", "engine_version", "rules_version"],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
        sqlite_where=sa.text("is_current = 1"),
    )
    for name, columns in (
        ("verdict", ["verdict", "is_current", "checked_at"]),
        ("readiness", ["data_readiness", "is_current", "checked_at"]),
        ("scenario", ["scenario_key", "is_current", "checked_at"]),
        ("repeat", ["has_repeat", "repeat_attempt_count", "is_current"]),
        ("stale", ["stale", "is_current", "checked_at"]),
    ):
        op.create_index(
            f"ix_auction_decision_snapshot_{name}_current",
            "auction_decision_snapshots",
            columns,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_decision_snapshots" in inspector.get_table_names():
        op.drop_table("auction_decision_snapshots")
