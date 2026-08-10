"""add auction investment scenario, field inspection and deal room

Revision ID: b7e5a9c3d2f4
Revises: 8a4c2e7f91b6
"""

import sqlalchemy as sa
from alembic import op

revision = "b7e5a9c3d2f4"
down_revision = "8a4c2e7f91b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_user_lot_pipeline" not in inspector.get_table_names():
        return
    columns = {
        column["name"]
        for column in inspector.get_columns("auction_user_lot_pipeline")
    }
    additions = (
        (
            "investment_json",
            sa.Column(
                "investment_json", sa.Text(), nullable=False, server_default="{}"
            ),
        ),
        (
            "inspection_json",
            sa.Column(
                "inspection_json", sa.Text(), nullable=False, server_default="{}"
            ),
        ),
        (
            "activity_json",
            sa.Column(
                "activity_json", sa.Text(), nullable=False, server_default="[]"
            ),
        ),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("auction_user_lot_pipeline", column)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_user_lot_pipeline" not in inspector.get_table_names():
        return
    columns = {
        column["name"]
        for column in inspector.get_columns("auction_user_lot_pipeline")
    }
    for name in ("activity_json", "inspection_json", "investment_json"):
        if name in columns:
            op.drop_column("auction_user_lot_pipeline", name)
