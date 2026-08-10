"""add auction decision costs and boundary verification fields

Revision ID: 8a4c2e7f91b6
Revises: d4a7c8e9f2b1
"""

import sqlalchemy as sa
from alembic import op

revision = "8a4c2e7f91b6"
down_revision = "d4a7c8e9f2b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "auction_lot_geo_checks" in tables:
        columns = {column["name"] for column in inspector.get_columns("auction_lot_geo_checks")}
        indexes = {index["name"] for index in inspector.get_indexes("auction_lot_geo_checks")}
        additions = (
            (
                "boundary_status",
                sa.Column(
                    "boundary_status",
                    sa.String(length=32),
                    nullable=False,
                    server_default="unknown",
                ),
            ),
            ("boundary_area_ha", sa.Column("boundary_area_ha", sa.Float(), nullable=True)),
            (
                "boundary_difference_percent",
                sa.Column("boundary_difference_percent", sa.Float(), nullable=True),
            ),
            ("boundary_source", sa.Column("boundary_source", sa.String(length=120), nullable=True)),
        )
        for name, column in additions:
            if name not in columns:
                op.add_column("auction_lot_geo_checks", column)
        if "ix_auction_lot_geo_checks_boundary_status" not in indexes:
            op.create_index(
                "ix_auction_lot_geo_checks_boundary_status",
                "auction_lot_geo_checks",
                ["boundary_status"],
            )
    if "auction_user_lot_pipeline" in tables:
        columns = {column["name"] for column in inspector.get_columns("auction_user_lot_pipeline")}
        if "costs_json" not in columns:
            op.add_column(
                "auction_user_lot_pipeline",
                sa.Column("costs_json", sa.Text(), nullable=False, server_default="{}"),
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_user_lot_pipeline" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("auction_user_lot_pipeline")}
        if "costs_json" in columns:
            op.drop_column("auction_user_lot_pipeline", "costs_json")
    if "auction_lot_geo_checks" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("auction_lot_geo_checks")}
        indexes = {index["name"] for index in inspector.get_indexes("auction_lot_geo_checks")}
        if "ix_auction_lot_geo_checks_boundary_status" in indexes:
            op.drop_index(
                "ix_auction_lot_geo_checks_boundary_status",
                table_name="auction_lot_geo_checks",
            )
        for name in (
            "boundary_source",
            "boundary_difference_percent",
            "boundary_area_ha",
            "boundary_status",
        ):
            if name in columns:
                op.drop_column("auction_lot_geo_checks", name)
