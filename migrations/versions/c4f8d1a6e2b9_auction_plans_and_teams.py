"""add auction plans and team workspaces

Revision ID: c4f8d1a6e2b9
Revises: b7e5a9c3d2f4
"""

import sqlalchemy as sa
from alembic import op

revision = "c4f8d1a6e2b9"
down_revision = "b7e5a9c3d2f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "accounts" in tables:
        columns = {column["name"] for column in inspector.get_columns("accounts")}
        if "auction_plan" not in columns:
            op.add_column(
                "accounts",
                sa.Column(
                    "auction_plan",
                    sa.String(length=24),
                    nullable=False,
                    server_default="observer",
                ),
            )
        indexes = {index["name"] for index in inspector.get_indexes("accounts")}
        if "ix_accounts_auction_plan" not in indexes:
            op.create_index("ix_accounts_auction_plan", "accounts", ["auction_plan"])
    if "account_payments" in tables:
        columns = {
            column["name"] for column in inspector.get_columns("account_payments")
        }
        if "target_plan" not in columns:
            op.add_column(
                "account_payments",
                sa.Column(
                    "target_plan",
                    sa.String(length=24),
                    nullable=False,
                    server_default="investor",
                ),
            )
        indexes = {
            index["name"] for index in inspector.get_indexes("account_payments")
        }
        if "ix_account_payments_target_plan" not in indexes:
            op.create_index(
                "ix_account_payments_target_plan",
                "account_payments",
                ["target_plan"],
            )
    if "auction_workspaces" not in tables:
        op.create_table(
            "auction_workspaces",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("owner_account_id", sa.String(length=36), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["owner_account_id"], ["accounts.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("owner_account_id"),
        )
        op.create_index(
            "ix_auction_workspaces_owner_account_id",
            "auction_workspaces",
            ["owner_account_id"],
            unique=True,
        )
        op.create_index(
            "ix_auction_workspaces_active",
            "auction_workspaces",
            ["active"],
        )
    inspector = sa.inspect(op.get_bind())
    if "auction_workspace_members" not in inspector.get_table_names():
        op.create_table(
            "auction_workspace_members",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("workspace_id", sa.String(length=36), nullable=False),
            sa.Column("account_id", sa.String(length=36), nullable=False),
            sa.Column("role", sa.String(length=24), nullable=False, server_default="analyst"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
            sa.Column("invited_by_account_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
            sa.ForeignKeyConstraint(["invited_by_account_id"], ["accounts.id"]),
            sa.ForeignKeyConstraint(["workspace_id"], ["auction_workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "workspace_id", "account_id", name="uq_auction_workspace_member"
            ),
        )
        for column in (
            "workspace_id",
            "account_id",
            "role",
            "status",
            "invited_by_account_id",
        ):
            op.create_index(
                f"ix_auction_workspace_members_{column}",
                "auction_workspace_members",
                [column],
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "auction_workspace_members" in tables:
        op.drop_table("auction_workspace_members")
    if "auction_workspaces" in tables:
        op.drop_table("auction_workspaces")
    if "account_payments" in tables:
        columns = {
            column["name"] for column in inspector.get_columns("account_payments")
        }
        if "target_plan" in columns:
            op.drop_column("account_payments", "target_plan")
    if "accounts" in tables:
        columns = {column["name"] for column in inspector.get_columns("accounts")}
        if "auction_plan" in columns:
            op.drop_column("accounts", "auction_plan")
