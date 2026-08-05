"""auction watchlist eqazyna status

Revision ID: d4a7c8e9f2b1
Revises: c9e1a7b4d2f6
Create Date: 2026-08-04 21:55:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a7c8e9f2b1"
down_revision: str | Sequence[str] | None = "c9e1a7b4d2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_watchlists" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("auction_watchlists")}
    indexes = {index["name"] for index in inspector.get_indexes("auction_watchlists")}
    if "eqazyna_status" not in columns:
        op.add_column(
            "auction_watchlists",
            sa.Column("eqazyna_status", sa.String(length=64), nullable=True),
        )
    if "ix_auction_watchlists_eqazyna_status" not in indexes:
        op.create_index(
            op.f("ix_auction_watchlists_eqazyna_status"),
            "auction_watchlists",
            ["eqazyna_status"],
            unique=False,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_watchlists" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("auction_watchlists")}
    indexes = {index["name"] for index in inspector.get_indexes("auction_watchlists")}
    if "ix_auction_watchlists_eqazyna_status" in indexes:
        op.drop_index(
            op.f("ix_auction_watchlists_eqazyna_status"),
            table_name="auction_watchlists",
        )
    if "eqazyna_status" in columns:
        op.drop_column("auction_watchlists", "eqazyna_status")
