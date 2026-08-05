"""planning candidate egkn context

Revision ID: c9e1a7b4d2f6
Revises: a8c4d2e9f1b3
Create Date: 2026-08-04 20:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e1a7b4d2f6"
down_revision: str | Sequence[str] | None = "a8c4d2e9f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "planning_candidate_reviews" not in inspector.get_table_names():
        return
    columns = {
        column["name"] for column in inspector.get_columns("planning_candidate_reviews")
    }
    additions = {
        "nearby_cadastre": sa.String(length=64),
        "nearby_distance_m": sa.Float(),
        "nearby_land_use": sa.String(length=240),
        "candidate_area_ha": sa.Float(),
        "selection_reason": sa.Text(),
    }
    for name, column_type in additions.items():
        if name not in columns:
            op.add_column(
                "planning_candidate_reviews",
                sa.Column(name, column_type, nullable=True),
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "planning_candidate_reviews" not in inspector.get_table_names():
        return
    columns = {
        column["name"] for column in inspector.get_columns("planning_candidate_reviews")
    }
    for name in (
        "selection_reason",
        "candidate_area_ha",
        "nearby_land_use",
        "nearby_distance_m",
        "nearby_cadastre",
    ):
        if name in columns:
            op.drop_column("planning_candidate_reviews", name)
