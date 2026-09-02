"""add global verified comparable observation inventory

Revision ID: c9e4b7a2d5f8
Revises: d8f3a1c5e7b9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e4b7a2d5f8"
down_revision: str | Sequence[str] | None = "d8f3a1c5e7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ELIGIBLE = (
    "fact_status = 'found' AND price_kind = 'verified_sale' "
    "AND verification_status = 'verified' AND verification_ref IS NOT NULL "
    "AND conflicts_json = '[]'"
)


def _common_columns() -> list[sa.Column]:
    return [
        sa.Column("source_sequence_id", sa.BigInteger(), nullable=False),
        sa.Column("source_identity_key", sa.String(length=71), nullable=False),
        sa.Column("source_name", sa.String(length=128), nullable=False),
        sa.Column("source_record_id", sa.String(length=128), nullable=False),
        sa.Column("source_sale_id", sa.String(length=128), nullable=True),
        sa.Column("source_listing_id", sa.String(length=128), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("object_id", sa.String(length=128), nullable=True),
        sa.Column("fact_status", sa.String(length=16), nullable=False),
        sa.Column("price_kind", sa.String(length=20), nullable=False),
        sa.Column("verification_status", sa.String(length=32), nullable=True),
        sa.Column("verification_ref", sa.String(length=512), nullable=True),
        sa.Column("right_type", sa.String(length=16), nullable=True),
        sa.Column("purpose_group", sa.String(length=160), nullable=True),
        sa.Column("lease_term_years", sa.Numeric(8, 3), nullable=True),
        sa.Column("lease_band", sa.String(length=16), nullable=True),
        sa.Column("area_ha", sa.Numeric(18, 6), nullable=True),
        sa.Column("price_kzt", sa.Numeric(20, 0), nullable=True),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("access_readiness", sa.String(length=16), nullable=True),
        sa.Column("infrastructure_readiness", sa.String(length=16), nullable=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=True),
        sa.Column("locality", sa.String(length=160), nullable=True),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("conflicts_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("generation_signature", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
    ]


def _common_checks(prefix: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(
            "source_sequence_id > 0", name=f"ck_{prefix}_sequence"
        ),
        sa.CheckConstraint(
            "length(source_identity_key) = 71 AND length(generation_signature) = 64 "
            "AND length(content_hash) = 64",
            name=f"ck_{prefix}_hashes",
        ),
        sa.CheckConstraint(
            "fact_status IN ('found', 'conflict', 'error')", name=f"ck_{prefix}_status"
        ),
        sa.CheckConstraint(
            "price_kind IN ('verified_sale', 'listing')", name=f"ck_{prefix}_price_kind"
        ),
        sa.CheckConstraint(
            "(price_kind = 'verified_sale' AND source_sale_id IS NOT NULL) OR "
            "(price_kind = 'listing' AND source_listing_id IS NOT NULL)",
            name=f"ck_{prefix}_identity",
        ),
        sa.CheckConstraint(
            "right_type IS NULL OR right_type IN ('ownership', 'lease')",
            name=f"ck_{prefix}_right",
        ),
        sa.CheckConstraint(
            "(right_type IS NULL AND lease_term_years IS NULL AND lease_band IS NULL) OR "
            "(right_type = 'ownership' AND lease_term_years IS NULL AND lease_band IS NULL) OR "
            "(right_type = 'lease' AND lease_term_years > 0 AND lease_term_years <= 99 AND "
            "((lease_term_years <= 3 AND lease_band = 'short_3') OR "
            "(lease_term_years > 3 AND lease_term_years <= 10 AND lease_band = 'medium_10') OR "
            "(lease_term_years > 10 AND lease_band = 'long_99')))",
            name=f"ck_{prefix}_lease",
        ),
        sa.CheckConstraint(
            "access_readiness IS NULL OR access_readiness IN "
            "('none', 'partial', 'ready', 'unknown')",
            name=f"ck_{prefix}_access",
        ),
        sa.CheckConstraint(
            "infrastructure_readiness IS NULL OR infrastructure_readiness IN "
            "('none', 'partial', 'ready', 'unknown')",
            name=f"ck_{prefix}_infrastructure",
        ),
        sa.CheckConstraint(
            "latitude IS NULL OR latitude BETWEEN 40 AND 56", name=f"ck_{prefix}_latitude"
        ),
        sa.CheckConstraint(
            "longitude IS NULL OR longitude BETWEEN 46 AND 88",
            name=f"ck_{prefix}_longitude",
        ),
        sa.CheckConstraint(
            "area_ha IS NULL OR (area_ha >= 0.0001 AND area_ha <= 1000000)",
            name=f"ck_{prefix}_area",
        ),
        sa.CheckConstraint(
            "price_kzt IS NULL OR (price_kzt >= 1 AND price_kzt <= 1000000000000000)",
            name=f"ck_{prefix}_price",
        ),
        sa.CheckConstraint(
            "fact_status != 'found' OR (source_url IS NOT NULL AND title IS NOT NULL "
            "AND right_type IS NOT NULL AND purpose_group IS NOT NULL AND area_ha IS NOT NULL "
            "AND price_kzt IS NOT NULL AND latitude IS NOT NULL AND longitude IS NOT NULL)",
            name=f"ck_{prefix}_found_complete",
        ),
        sa.CheckConstraint(
            "fact_status != 'found' OR price_kind != 'verified_sale' OR "
            "(event_at IS NOT NULL AND verification_status = 'verified' "
            "AND verification_ref IS NOT NULL)",
            name=f"ck_{prefix}_verified_sale",
        ),
    ]


def upgrade() -> None:
    observation_id_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "auction_verified_comparable_observations",
        sa.Column("id", observation_id_type, autoincrement=True, nullable=False),
        *_common_columns(),
        sa.Column("raw_payload_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *_common_checks("auction_verified_comparable_observation"),
        sa.CheckConstraint(
            "length(provenance_json) <= 16384 AND length(conflicts_json) <= 8192 "
            "AND (raw_payload_json IS NULL OR length(raw_payload_json) <= 64000)",
            name="ck_auction_verified_comparable_observation_payload_bounds",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_identity_key",
            "content_hash",
            name="uq_auction_verified_comparable_observation_content",
        ),
    )
    op.create_index(
        "ix_auction_verified_comparable_observation_identity_latest",
        "auction_verified_comparable_observations",
        ["source_identity_key", "observed_at", "source_sequence_id", "id"],
    )
    op.create_index(
        "ix_auction_verified_comparable_observation_generation",
        "auction_verified_comparable_observations",
        ["generation_signature", "id"],
    )
    current_columns = [column.copy() for column in _common_columns()]
    op.create_table(
        "auction_verified_comparable_current",
        *current_columns,
        sa.Column("observation_id", observation_id_type, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        *_common_checks("auction_verified_comparable_current"),
        sa.CheckConstraint(
            "length(provenance_json) <= 16384 AND length(conflicts_json) <= 8192",
            name="ck_auction_verified_comparable_current_payload_bounds",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["auction_verified_comparable_observations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("source_identity_key"),
        sa.UniqueConstraint(
            "observation_id", name="uq_auction_verified_comparable_current_observation"
        ),
    )
    op.create_index(
        "ix_auction_verified_comparable_current_target_geo",
        "auction_verified_comparable_current",
        [
            "right_type",
            "purpose_group",
            "lease_band",
            "latitude",
            "longitude",
            "area_ha",
            "event_at",
            "observed_at",
            "observation_id",
        ],
        postgresql_where=sa.text(ELIGIBLE),
        sqlite_where=sa.text(ELIGIBLE),
    )
    op.create_index(
        "ix_auction_verified_comparable_current_target_event",
        "auction_verified_comparable_current",
        [
            "right_type",
            "purpose_group",
            "lease_band",
            "event_at",
            "observed_at",
            "observation_id",
        ],
        postgresql_where=sa.text(ELIGIBLE),
        sqlite_where=sa.text(ELIGIBLE),
    )
    op.create_index(
        "ix_auction_verified_comparable_current_keyset",
        "auction_verified_comparable_current",
        ["observed_at", "observation_id"],
    )


def downgrade() -> None:
    op.drop_table("auction_verified_comparable_current")
    op.drop_table("auction_verified_comparable_observations")
