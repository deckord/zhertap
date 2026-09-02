"""add due diligence attachment extraction state

Revision ID: d7e9f2a4b6c8
Revises: c6d8e1f3a5b7
"""

import sqlalchemy as sa
from alembic import op

revision = "d7e9f2a4b6c8"
down_revision = "c6d8e1f3a5b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "auction_due_diligence_attachments",
        sa.Column(
            "extraction_status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "auction_due_diligence_attachments",
        sa.Column("extraction_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "auction_due_diligence_attachments",
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_auction_due_diligence_attachments_extraction_status",
        "auction_due_diligence_attachments",
        ["extraction_status"],
    )
    op.alter_column("auction_due_diligence_attachments", "extraction_status", server_default=None)


def downgrade() -> None:
    op.drop_index(
        "ix_auction_due_diligence_attachments_extraction_status",
        table_name="auction_due_diligence_attachments",
    )
    op.drop_column("auction_due_diligence_attachments", "extracted_at")
    op.drop_column("auction_due_diligence_attachments", "extraction_json")
    op.drop_column("auction_due_diligence_attachments", "extraction_status")
