"""add private auction due diligence request registry

Revision ID: c6d8e1f3a5b7
Revises: f1a7c3e9b5d2, f3a2b6c9d8e1
"""

import sqlalchemy as sa
from alembic import op

revision = "c6d8e1f3a5b7"
down_revision = ("f1a7c3e9b5d2", "f3a2b6c9d8e1")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auction_due_diligence_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.Column("check_code", sa.String(length=32), nullable=False),
        sa.Column("authority", sa.String(length=240), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("why", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("external_reference", sa.String(length=160), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'prepared', 'sent', 'waiting', 'received', 'verified', "
            "'risk', 'cancelled')",
            name="ck_auction_dd_request_status",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auction_due_diligence_requests_account_id",
        "auction_due_diligence_requests",
        ["account_id"],
    )
    op.create_index(
        "ix_auction_due_diligence_requests_lot_id",
        "auction_due_diligence_requests",
        ["lot_id"],
    )
    op.create_index(
        "ix_auction_due_diligence_requests_check_code",
        "auction_due_diligence_requests",
        ["check_code"],
    )
    op.create_index(
        "ix_auction_due_diligence_requests_status",
        "auction_due_diligence_requests",
        ["status"],
    )
    op.create_index(
        "ix_auction_dd_requests_account_lot_status",
        "auction_due_diligence_requests",
        ["account_id", "lot_id", "status"],
    )
    op.create_index(
        "ix_auction_due_diligence_requests_response_due_at",
        "auction_due_diligence_requests",
        ["response_due_at"],
    )

    op.create_table(
        "auction_due_diligence_attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["auction_due_diligence_requests.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auction_due_diligence_attachments_request_id",
        "auction_due_diligence_attachments",
        ["request_id"],
    )
    op.create_index(
        "ix_auction_due_diligence_attachments_account_id",
        "auction_due_diligence_attachments",
        ["account_id"],
    )
    op.create_index(
        "ix_auction_dd_attachments_request",
        "auction_due_diligence_attachments",
        ["request_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auction_dd_attachments_request",
        table_name="auction_due_diligence_attachments",
    )
    op.drop_index(
        "ix_auction_due_diligence_attachments_account_id",
        table_name="auction_due_diligence_attachments",
    )
    op.drop_index(
        "ix_auction_due_diligence_attachments_request_id",
        table_name="auction_due_diligence_attachments",
    )
    op.drop_table("auction_due_diligence_attachments")
    op.drop_index(
        "ix_auction_due_diligence_requests_response_due_at",
        table_name="auction_due_diligence_requests",
    )
    op.drop_index(
        "ix_auction_dd_requests_account_lot_status",
        table_name="auction_due_diligence_requests",
    )
    op.drop_index(
        "ix_auction_due_diligence_requests_status",
        table_name="auction_due_diligence_requests",
    )
    op.drop_index(
        "ix_auction_due_diligence_requests_check_code",
        table_name="auction_due_diligence_requests",
    )
    op.drop_index(
        "ix_auction_due_diligence_requests_lot_id",
        table_name="auction_due_diligence_requests",
    )
    op.drop_index(
        "ix_auction_due_diligence_requests_account_id",
        table_name="auction_due_diligence_requests",
    )
    op.drop_table("auction_due_diligence_requests")
