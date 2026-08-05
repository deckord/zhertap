"""auction v2 event notifications

Revision ID: b6f1d9a2c4e8
Revises: 4b2c9a8e7d1a
Create Date: 2026-08-02 15:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6f1d9a2c4e8"
down_revision: str | Sequence[str] | None = "4b2c9a8e7d1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_columns = {
        column["name"]
        for column in inspector.get_columns("auction_watchlist_notifications")
    }
    existing_indexes = {
        index["name"]
        for index in inspector.get_indexes("auction_watchlist_notifications")
    }
    with op.batch_alter_table("auction_watchlist_notifications") as batch_op:
        if "seen_at" not in existing_columns:
            batch_op.add_column(sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True))
        if "event_type" not in existing_columns:
            batch_op.add_column(
                sa.Column(
                    "event_type",
                    sa.String(length=48),
                    nullable=False,
                    server_default="new_lot",
                )
            )
        if "event_key" not in existing_columns:
            batch_op.add_column(
                sa.Column(
                    "event_key",
                    sa.String(length=160),
                    nullable=False,
                    server_default="new_lot",
                )
            )
        if "title" not in existing_columns:
            batch_op.add_column(sa.Column("title", sa.String(length=240), nullable=True))
        if "detail" not in existing_columns:
            batch_op.add_column(sa.Column("detail", sa.Text(), nullable=True))
        batch_op.drop_constraint(
            "uq_auction_watchlist_notification_channel",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_auction_watchlist_notification_event",
            ["watchlist_id", "lot_id", "channel", "event_key"],
        )
    if (
        "seen_at" not in existing_columns
        and "ix_auction_watchlist_notifications_seen_at" not in existing_indexes
    ):
        op.create_index(
            op.f("ix_auction_watchlist_notifications_seen_at"),
            "auction_watchlist_notifications",
            ["seen_at"],
            unique=False,
        )
    op.create_index(
        op.f("ix_auction_watchlist_notifications_event_type"),
        "auction_watchlist_notifications",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_watchlist_notifications_event_key"),
        "auction_watchlist_notifications",
        ["event_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_auction_watchlist_notifications_event_key"),
        table_name="auction_watchlist_notifications",
    )
    op.drop_index(
        op.f("ix_auction_watchlist_notifications_event_type"),
        table_name="auction_watchlist_notifications",
    )
    with op.batch_alter_table("auction_watchlist_notifications") as batch_op:
        batch_op.drop_constraint(
            "uq_auction_watchlist_notification_event",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_auction_watchlist_notification_channel",
            ["watchlist_id", "lot_id", "channel"],
        )
        batch_op.drop_column("detail")
        batch_op.drop_column("title")
        batch_op.drop_column("event_key")
        batch_op.drop_column("event_type")
