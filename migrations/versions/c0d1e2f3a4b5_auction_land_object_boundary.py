"""add canonical land object boundary provenance

Revision ID: c0d1e2f3a4b5
Revises: b9d8c7e6f5a4
"""

import sqlalchemy as sa
from alembic import op

revision = "c0d1e2f3a4b5"
down_revision = "b9d8c7e6f5a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_land_objects" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("auction_land_objects")}
    for name, column in (
        ("boundary_geojson", sa.Column("boundary_geojson", sa.Text())),
        ("boundary_source", sa.Column("boundary_source", sa.String(120))),
        ("boundary_observed_at", sa.Column("boundary_observed_at", sa.DateTime(timezone=True))),
    ):
        if name not in columns:
            op.add_column("auction_land_objects", column)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_land_objects" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("auction_land_objects")}
    for name in ("boundary_observed_at", "boundary_source", "boundary_geojson"):
        if name in columns:
            op.drop_column("auction_land_objects", name)
