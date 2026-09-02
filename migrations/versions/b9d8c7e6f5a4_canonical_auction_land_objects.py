"""add canonical auction land objects

Revision ID: b9d8c7e6f5a4
Revises: e4a8c2f6b1d9
"""

import sqlalchemy as sa
from alembic import op

revision = "b9d8c7e6f5a4"
down_revision = "e4a8c2f6b1d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "auction_land_objects" not in tables:
        op.create_table(
            "auction_land_objects",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("canonical_key", sa.String(128), nullable=False),
            sa.Column("egkn_id", sa.String(64)),
            sa.Column("cadastre_number", sa.String(64)),
            sa.Column("identity_confidence", sa.String(16), nullable=False, server_default="unverified"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("canonical_key", name="uq_auction_land_object_canonical_key"),
        )
        op.create_index("ix_auction_land_objects_egkn_id", "auction_land_objects", ["egkn_id"])
        op.create_index("ix_auction_land_objects_cadastre_number", "auction_land_objects", ["cadastre_number"])
    columns = {column["name"] for column in inspector.get_columns("auction_lots")}
    if "land_object_ref_id" not in columns:
        op.add_column("auction_lots", sa.Column("land_object_ref_id", sa.String(36)))
        op.create_index("ix_auction_lots_land_object_ref_id", "auction_lots", ["land_object_ref_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_lots" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("auction_lots")}
        indexes = {index["name"] for index in inspector.get_indexes("auction_lots")}
        if "ix_auction_lots_land_object_ref_id" in indexes:
            op.drop_index("ix_auction_lots_land_object_ref_id", table_name="auction_lots")
        if "land_object_ref_id" in columns:
            op.drop_column("auction_lots", "land_object_ref_id")
    if "auction_land_objects" in inspector.get_table_names():
        op.drop_table("auction_land_objects")
