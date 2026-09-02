"""add stable land object identity and normalized auction terms

Revision ID: e7b9c2d4f6a1
Revises: c4f8d1a6e2b9
"""

import sqlalchemy as sa
from alembic import op

revision = "e7b9c2d4f6a1"
down_revision = "c4f8d1a6e2b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_lots" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("auction_lots")}
    indexes = {index["name"] for index in inspector.get_indexes("auction_lots")}
    additions = (
        ("land_object_id", sa.Column("land_object_id", sa.String(length=64), nullable=True)),
        ("lease_term_years", sa.Column("lease_term_years", sa.Float(), nullable=True)),
        ("divisible", sa.Column("divisible", sa.Boolean(), nullable=True)),
        ("additional_payment_kzt", sa.Column("additional_payment_kzt", sa.Float(), nullable=True)),
        ("annual_rent_kzt", sa.Column("annual_rent_kzt", sa.Float(), nullable=True)),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("auction_lots", column)
    for name in ("land_object_id", "lease_term_years"):
        index_name = f"ix_auction_lots_{name}"
        if index_name not in indexes:
            op.create_index(index_name, "auction_lots", [name])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "auction_lots" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("auction_lots")}
    indexes = {index["name"] for index in inspector.get_indexes("auction_lots")}
    for name in ("lease_term_years", "land_object_id"):
        index_name = f"ix_auction_lots_{name}"
        if index_name in indexes:
            op.drop_index(index_name, table_name="auction_lots")
    for name in (
        "annual_rent_kzt",
        "additional_payment_kzt",
        "divisible",
        "lease_term_years",
        "land_object_id",
    ):
        if name in columns:
            op.drop_column("auction_lots", name)
