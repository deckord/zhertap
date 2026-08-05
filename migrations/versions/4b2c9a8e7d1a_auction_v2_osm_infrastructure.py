"""auction v2 osm infrastructure

Revision ID: 4b2c9a8e7d1a
Revises: 17e9f4d2a6c3
Create Date: 2026-08-02 12:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4b2c9a8e7d1a"
down_revision: str | Sequence[str] | None = "17e9f4d2a6c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "auction_lot_geo_checks",
        sa.Column(
            "osm_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_checked",
        ),
    )
    op.add_column(
        "auction_lot_geo_checks",
        sa.Column("road_distance_m", sa.Float(), nullable=True),
    )
    op.add_column(
        "auction_lot_geo_checks",
        sa.Column("power_distance_m", sa.Float(), nullable=True),
    )
    op.add_column(
        "auction_lot_geo_checks",
        sa.Column("water_distance_m", sa.Float(), nullable=True),
    )
    op.add_column(
        "auction_lot_geo_checks",
        sa.Column("open_water_distance_m", sa.Float(), nullable=True),
    )
    op.add_column(
        "auction_lot_geo_checks",
        sa.Column("cemetery_distance_m", sa.Float(), nullable=True),
    )
    op.add_column(
        "auction_lot_geo_checks",
        sa.Column("object_distance_m", sa.Float(), nullable=True),
    )
    op.add_column(
        "auction_lot_geo_checks",
        sa.Column("object_kind", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "auction_lot_geo_checks",
        sa.Column("osm_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_auction_lot_geo_checks_osm_status"),
        "auction_lot_geo_checks",
        ["osm_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_geo_checks_osm_checked_at"),
        "auction_lot_geo_checks",
        ["osm_checked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_auction_lot_geo_checks_osm_checked_at"),
        table_name="auction_lot_geo_checks",
    )
    op.drop_index(
        op.f("ix_auction_lot_geo_checks_osm_status"),
        table_name="auction_lot_geo_checks",
    )
    op.drop_column("auction_lot_geo_checks", "osm_checked_at")
    op.drop_column("auction_lot_geo_checks", "object_kind")
    op.drop_column("auction_lot_geo_checks", "object_distance_m")
    op.drop_column("auction_lot_geo_checks", "cemetery_distance_m")
    op.drop_column("auction_lot_geo_checks", "open_water_distance_m")
    op.drop_column("auction_lot_geo_checks", "water_distance_m")
    op.drop_column("auction_lot_geo_checks", "power_distance_m")
    op.drop_column("auction_lot_geo_checks", "road_distance_m")
    op.drop_column("auction_lot_geo_checks", "osm_status")
