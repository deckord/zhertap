"""auction v2 watchlist notifications

Revision ID: 17e9f4d2a6c3
Revises: 9c75a7cda2f1
Create Date: 2026-08-02 09:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "17e9f4d2a6c3"
down_revision: str | Sequence[str] | None = "9c75a7cda2f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auction_watchlist_notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("watchlist_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"]),
        sa.ForeignKeyConstraint(["watchlist_id"], ["auction_watchlists.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "watchlist_id",
            "lot_id",
            "channel",
            name="uq_auction_watchlist_notification_channel",
        ),
    )
    op.create_index(
        op.f("ix_auction_watchlist_notifications_channel"),
        "auction_watchlist_notifications",
        ["channel"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_watchlist_notifications_lot_id"),
        "auction_watchlist_notifications",
        ["lot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_watchlist_notifications_sent_at"),
        "auction_watchlist_notifications",
        ["sent_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_watchlist_notifications_seen_at"),
        "auction_watchlist_notifications",
        ["seen_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_watchlist_notifications_status"),
        "auction_watchlist_notifications",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_watchlist_notifications_watchlist_id"),
        "auction_watchlist_notifications",
        ["watchlist_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_auction_watchlist_notifications_watchlist_id"),
        table_name="auction_watchlist_notifications",
    )
    op.drop_index(
        op.f("ix_auction_watchlist_notifications_status"),
        table_name="auction_watchlist_notifications",
    )
    op.drop_index(
        op.f("ix_auction_watchlist_notifications_seen_at"),
        table_name="auction_watchlist_notifications",
    )
    op.drop_index(
        op.f("ix_auction_watchlist_notifications_sent_at"),
        table_name="auction_watchlist_notifications",
    )
    op.drop_index(
        op.f("ix_auction_watchlist_notifications_lot_id"),
        table_name="auction_watchlist_notifications",
    )
    op.drop_index(
        op.f("ix_auction_watchlist_notifications_channel"),
        table_name="auction_watchlist_notifications",
    )
    op.drop_table("auction_watchlist_notifications")
