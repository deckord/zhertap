"""freeze auction history generation membership

Revision ID: a9c3e7f1b5d2
Revises: d1e2f3a4b5c6
"""

import sqlalchemy as sa
from alembic import op

revision = "a9c3e7f1b5d2"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auction_history_generation_lots",
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation"], ["auction_history_generations.generation"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("generation", "lot_id"),
    )
    op.create_index(
        "ix_auction_history_generation_lots_lot_id",
        "auction_history_generation_lots",
        ["lot_id"],
    )
    op.create_index(
        "ix_auction_lots_history_snapshot",
        "auction_lots",
        ["object_type", "created_at", "id"],
    )
    op.execute(
        "UPDATE auction_history_generations "
        "SET status = 'failed', error_count = error_count + 1, "
        "detail = 'generation invalidated by membership snapshot migration', "
        "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
        "updated_at = CURRENT_TIMESTAMP WHERE status = 'building'"
    )


def downgrade() -> None:
    op.drop_index("ix_auction_lots_history_snapshot", table_name="auction_lots")
    op.drop_index(
        "ix_auction_history_generation_lots_lot_id",
        table_name="auction_history_generation_lots",
    )
    op.drop_table("auction_history_generation_lots")