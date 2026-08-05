"""planning candidate reviews

Revision ID: a8c4d2e9f1b3
Revises: 6d2c93f8a4b1, b6f1d9a2c4e8
Create Date: 2026-08-04 14:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c4d2e9f1b3"
down_revision: str | Sequence[str] | None = ("6d2c93f8a4b1", "b6f1d9a2c4e8")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "planning_candidate_reviews" in inspector.get_table_names():
        return
    op.create_table(
        "planning_candidate_reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("region", sa.String(length=160), nullable=False),
        sa.Column("district", sa.String(length=160), nullable=False),
        sa.Column("locality", sa.String(length=160), nullable=False),
        sa.Column("requested_use", sa.String(length=64), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("google_maps_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("trust_level", sa.String(length=32), nullable=True),
        sa.Column("allowed_area_ha", sa.Float(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=120), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "region",
            "district",
            "locality",
            "requested_use",
            "latitude",
            "longitude",
            name="uq_planning_candidate_review_point",
        ),
    )
    for column in (
        "region",
        "district",
        "locality",
        "requested_use",
        "latitude",
        "longitude",
        "status",
    ):
        op.create_index(
            op.f(f"ix_planning_candidate_reviews_{column}"),
            "planning_candidate_reviews",
            [column],
            unique=False,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "planning_candidate_reviews" not in inspector.get_table_names():
        return
    op.drop_table("planning_candidate_reviews")
