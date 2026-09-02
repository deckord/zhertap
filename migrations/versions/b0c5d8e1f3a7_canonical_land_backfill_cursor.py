"""add durable canonical land identity backfill cursor

Revision ID: b0c5d8e1f3a7
Revises: a9c3e7f1b5d2
"""

import sqlalchemy as sa
from alembic import op

revision = "b0c5d8e1f3a7"
down_revision = "a9c3e7f1b5d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_land_identity_backfill_cursors" in inspector.get_table_names():
        return
    op.create_table(
        "auction_land_identity_backfill_cursors",
        sa.Column("cursor_key", sa.String(length=32), nullable=False),
        sa.Column("after_lot_id", sa.String(length=36), nullable=True),
        sa.Column("high_water_lot_id", sa.String(length=36), nullable=True),
        sa.Column("cycle_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scanned_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("linked_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("conflict_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("cursor_key"),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_land_identity_backfill_cursors" in inspector.get_table_names():
        op.drop_table("auction_land_identity_backfill_cursors")
