"""auction document storage status

Revision ID: 6d2c93f8a4b1
Revises: 4b2c9a8e7d1a
Create Date: 2026-08-03 21:25:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6d2c93f8a4b1"
down_revision: str | Sequence[str] | None = "4b2c9a8e7d1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "auction_documents",
        sa.Column(
            "storage_status",
            sa.String(length=32),
            nullable=False,
            server_default="linked",
        ),
    )
    op.add_column("auction_documents", sa.Column("local_path", sa.Text(), nullable=True))
    op.add_column(
        "auction_documents",
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "auction_documents",
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("auction_documents", sa.Column("download_error", sa.Text(), nullable=True))
    op.create_index(
        op.f("ix_auction_documents_storage_status"),
        "auction_documents",
        ["storage_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_auction_documents_storage_status"), table_name="auction_documents")
    op.drop_column("auction_documents", "download_error")
    op.drop_column("auction_documents", "downloaded_at")
    op.drop_column("auction_documents", "content_sha256")
    op.drop_column("auction_documents", "local_path")
    op.drop_column("auction_documents", "storage_status")
