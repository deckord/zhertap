"""add Jerler object identity to canonical land objects

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
"""

import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_land_objects" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("auction_land_objects")}
    indexes = {index["name"] for index in inspector.get_indexes("auction_land_objects")}
    if "jerler_object_id" not in columns:
        op.add_column("auction_land_objects", sa.Column("jerler_object_id", sa.String(64)))
    if "ix_auction_land_objects_jerler_object_id" not in indexes:
        op.create_index("ix_auction_land_objects_jerler_object_id", "auction_land_objects", ["jerler_object_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_land_objects" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("auction_land_objects")}
    indexes = {index["name"] for index in inspector.get_indexes("auction_land_objects")}
    if "ix_auction_land_objects_jerler_object_id" in indexes:
        op.drop_index("ix_auction_land_objects_jerler_object_id", table_name="auction_land_objects")
    if "jerler_object_id" in columns:
        op.drop_column("auction_land_objects", "jerler_object_id")
