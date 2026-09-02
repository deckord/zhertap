"""add normalized auction history dataset generations

Revision ID: f8c1d2e3a4b5
Revises: e7b9c2d4f6a1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8c1d2e3a4b5"
down_revision: str | Sequence[str] | None = "e7b9c2d4f6a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ELIGIBLE_PREDICATE = (
    "right_status = 'found' AND purpose_status = 'found' AND area_status = 'found'"
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "auction_history_generations" not in tables:
        op.create_table(
            "auction_history_generations",
            sa.Column("generation", sa.BigInteger(), nullable=False),
            sa.Column("normalization_version", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_high_water_lot_id", sa.String(length=36), nullable=True),
            sa.Column("expected_count", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("processed_count", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("error_count", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("checkpoint_lot_id", sa.String(length=36), nullable=True),
            sa.Column("scan_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("generation"),
        )
        op.create_index(
            "ix_auction_history_generations_status",
            "auction_history_generations",
            ["status"],
        )
        op.create_index(
            "uq_auction_history_generations_one_building",
            "auction_history_generations",
            ["status"],
            unique=True,
            postgresql_where=sa.text("status = 'building'"),
            sqlite_where=sa.text("status = 'building'"),
        )
        op.create_index(
            "uq_auction_history_generations_one_active",
            "auction_history_generations",
            ["status"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        )

    inspector = sa.inspect(op.get_bind())
    if "auction_history_normalized" in inspector.get_table_names():
        return
    op.create_table(
        "auction_history_normalized",
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.Column("normalization_version", sa.String(length=64), nullable=False),
        sa.Column("normalization_key", sa.String(length=64), nullable=False),
        sa.Column("right_kind", sa.String(length=16), nullable=False),
        sa.Column("right_status", sa.String(length=16), nullable=False),
        sa.Column("purpose_group", sa.String(length=32), nullable=False),
        sa.Column("purpose_status", sa.String(length=16), nullable=False),
        sa.Column("lease_band", sa.String(length=24), nullable=False),
        sa.Column("lease_status", sa.String(length=16), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("event_date_status", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("outcome_status", sa.String(length=16), nullable=False),
        sa.Column("area_ha", sa.Numeric(18, 6), nullable=True),
        sa.Column("area_status", sa.String(length=16), nullable=False),
        sa.Column("start_price_kzt", sa.Numeric(20, 2), nullable=True),
        sa.Column("start_price_status", sa.String(length=16), nullable=False),
        sa.Column("sale_price_kzt", sa.Numeric(20, 2), nullable=True),
        sa.Column("sale_price_status", sa.String(length=16), nullable=False),
        sa.Column("sale_to_start_ratio", sa.Numeric(18, 6), nullable=True),
        sa.Column("start_price_per_ha_kzt", sa.Numeric(24, 2), nullable=True),
        sa.Column("sale_price_per_ha_kzt", sa.Numeric(24, 2), nullable=True),
        sa.Column("region_key", sa.String(length=160), nullable=True),
        sa.Column("district_key", sa.String(length=160), nullable=True),
        sa.Column("locality_key", sa.String(length=160), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issues_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("normalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation"],
            ["auction_history_generations.generation"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("generation", "lot_id"),
    )
    op.create_index(
        "ix_auction_history_normalized_normalization_key",
        "auction_history_normalized",
        ["normalization_key"],
    )
    for scope in ("locality", "district", "region"):
        op.create_index(
            f"ix_auction_history_norm_{scope}_dims",
            "auction_history_normalized",
            ["generation", f"{scope}_key", "right_kind", "purpose_group", "lease_band"],
            postgresql_where=sa.text(ELIGIBLE_PREDICATE),
            sqlite_where=sa.text(ELIGIBLE_PREDICATE),
        )
    op.create_index(
        "ix_auction_history_norm_area_date",
        "auction_history_normalized",
        ["generation", "area_ha", "event_date"],
        postgresql_where=sa.text(ELIGIBLE_PREDICATE),
        sqlite_where=sa.text(ELIGIBLE_PREDICATE),
    )
    op.create_index(
        "ix_auction_history_norm_outcome_date",
        "auction_history_normalized",
        ["generation", "outcome", "event_date"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "auction_history_normalized" in tables:
        op.drop_table("auction_history_normalized")
    if "auction_history_generations" in tables:
        op.drop_table("auction_history_generations")
