"""add durable official territory observations and parcel applicability

Revision ID: c2f6a8d1e4b9
Revises: b0c5d8e1f3a7
"""

import sqlalchemy as sa
from alembic import op

revision = "c2f6a8d1e4b9"
down_revision = "b0c5d8e1f3a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "auction_territory_observations" not in tables:
        op.create_table(
            "auction_territory_observations",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("identity_key", sa.String(length=71), nullable=False),
            sa.Column("provider_id", sa.String(length=128), nullable=False),
            sa.Column("source_record_id", sa.String(length=160), nullable=False),
            sa.Column("source_revision", sa.Integer(), nullable=False),
            sa.Column("record_kind", sa.String(length=16), nullable=False),
            sa.Column("authority_name", sa.String(length=240), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("territory_code", sa.String(length=64), nullable=True),
            sa.Column("geometry_geojson", sa.Text(), nullable=True),
            sa.Column("geometry_sha256", sa.String(length=64), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("contract_version", sa.String(length=96), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "record_kind IN ('event','demographic')",
                name="ck_auction_territory_record_kind",
            ),
            sa.CheckConstraint(
                "length(content_hash) = 64 AND "
                "(geometry_sha256 IS NULL OR length(geometry_sha256) = 64)",
                name="ck_auction_territory_hashes",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "identity_key", "source_revision", name="uq_auction_territory_identity_revision"
            ),
        )
        op.create_index(
            "ix_auction_territory_provider_record",
            "auction_territory_observations",
            ["provider_id", "source_record_id", "source_revision"],
            unique=False,
        )
    inspector = sa.inspect(op.get_bind())
    if "auction_territory_applicability" not in inspector.get_table_names():
        op.create_table(
            "auction_territory_applicability",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("observation_id", sa.BigInteger(), nullable=False),
            sa.Column("lot_id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("scope", sa.String(length=16), nullable=False),
            sa.Column("basis", sa.String(length=64), nullable=False),
            sa.Column("overlap_ratio", sa.Float(), nullable=True),
            sa.Column("parcel_boundary_sha256", sa.String(length=64), nullable=True),
            sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('applicable','not_applicable','manual_required')",
                name="ck_auction_territory_applicability_status",
            ),
            sa.CheckConstraint(
                "scope IN ('parcel','territory','unknown')",
                name="ck_auction_territory_applicability_scope",
            ),
            sa.CheckConstraint(
                "parcel_boundary_sha256 IS NULL OR length(parcel_boundary_sha256) = 64",
                name="ck_auction_territory_boundary_hash",
            ),
            sa.ForeignKeyConstraint(
                ["lot_id"], ["auction_lots.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["observation_id"], ["auction_territory_observations.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "observation_id", "lot_id", name="uq_auction_territory_observation_lot"
            ),
        )
        op.create_index(
            "ix_auction_territory_applicability_lot",
            "auction_territory_applicability",
            ["lot_id", "status"],
            unique=False,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "auction_territory_applicability" in tables:
        op.drop_index(
            "ix_auction_territory_applicability_lot",
            table_name="auction_territory_applicability",
        )
        op.drop_table("auction_territory_applicability")
    inspector = sa.inspect(op.get_bind())
    if "auction_territory_observations" in inspector.get_table_names():
        op.drop_index(
            "ix_auction_territory_provider_record",
            table_name="auction_territory_observations",
        )
        op.drop_table("auction_territory_observations")
