"""add compact durable provider workflow cursors

Revision ID: f1a7c3e9b5d2
Revises: ec8a2f4d6b91
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a7c3e9b5d2"
down_revision: str | Sequence[str] | None = "ec8a2f4d6b91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_sync_runs",
        sa.Column("run_key", sa.String(64), primary_key=True),
        sa.Column("run_kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("child_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_children", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail_limit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details_enqueued", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("downstream_dispatched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "run_kind IN ('current','full','history','sources')",
            name="ck_provider_sync_run_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active','finalizing','complete','error')",
            name="ck_provider_sync_run_status",
        ),
        sa.CheckConstraint(
            "child_count BETWEEN 0 AND 1000 AND completed_children BETWEEN 0 AND child_count "
            "AND detail_limit BETWEEN 0 AND 100000 AND details_enqueued BETWEEN 0 AND detail_limit",
            name="ck_provider_sync_run_counters",
        ),
        sa.CheckConstraint(
            "length(config_json) <= 16000 AND length(policy_version) <= 64",
            name="ck_provider_sync_run_bounds",
        ),
    )
    op.create_index("ix_provider_sync_runs_run_kind", "provider_sync_runs", ["run_kind"])
    op.create_index(
        "uq_provider_sync_run_active_kind",
        "provider_sync_runs",
        ["run_kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('active','finalizing')"),
        sqlite_where=sa.text("status IN ('active','finalizing')"),
    )
    op.create_table(
        "provider_workflow_states",
        sa.Column("workflow_key", sa.String(128), primary_key=True),
        sa.Column(
            "run_key",
            sa.String(64),
            sa.ForeignKey("provider_sync_runs.run_key", ondelete="CASCADE"),
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("workflow_kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("cursor_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("completed_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(1000)),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('eqazyna','egkn','osm_overpass','gov_kz','auction_documents','jerler')",
            name="ck_provider_workflow_provider",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','deferred','complete','error')",
            name="ck_provider_workflow_status",
        ),
        sa.CheckConstraint(
            "completed_units >= 0 AND failed_units >= 0 AND attempts BETWEEN 0 AND 10000",
            name="ck_provider_workflow_counters",
        ),
        sa.CheckConstraint(
            "length(cursor_json) <= 16000 AND length(policy_version) <= 64",
            name="ck_provider_workflow_bounds",
        ),
        sa.CheckConstraint(
            "status != 'processing' OR (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_provider_workflow_claim",
        ),
    )
    op.create_index(
        "ix_provider_workflow_states_provider", "provider_workflow_states", ["provider"]
    )
    op.create_index(
        "ix_provider_workflow_states_run_key", "provider_workflow_states", ["run_key"]
    )
    op.create_index(
        "ix_provider_workflow_due",
        "provider_workflow_states",
        ["status", "next_attempt_at", "claim_expires_at", "workflow_key"],
    )
    op.create_table(
        "provider_run_dispatches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_key",
            sa.String(64),
            sa.ForeignKey("provider_sync_runs.run_key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_key", "action", name="uq_provider_run_dispatch_action"),
        sa.CheckConstraint(
            "action IN ('start_sources','normalize_history','decision_input')",
            name="ck_provider_run_dispatch_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','dispatched','error')",
            name="ck_provider_run_dispatch_status",
        ),
        sa.CheckConstraint(
            "attempts BETWEEN 0 AND 10000 AND length(payload_json) <= 4000",
            name="ck_provider_run_dispatch_bounds",
        ),
    )
    op.create_index(
        "ix_provider_run_dispatches_run_key", "provider_run_dispatches", ["run_key"]
    )
    op.create_index(
        "ix_provider_run_dispatch_due",
        "provider_run_dispatches",
        ["status", "next_attempt_at", "claim_expires_at", "id"],
    )
    op.create_table(
        "provider_workflow_units",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "workflow_key",
            sa.String(128),
            sa.ForeignKey("provider_workflow_states.workflow_key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("unit_key", sa.String(128), nullable=False),
        sa.Column("unit_kind", sa.String(64), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("result_ref", sa.String(512)),
        sa.Column("last_error", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workflow_key", "unit_key", name="uq_provider_workflow_unit_key"),
        sa.CheckConstraint(
            "status IN ('pending','processing','done','error','terminal')",
            name="ck_provider_workflow_unit_status",
        ),
        sa.CheckConstraint(
            "attempts BETWEEN 0 AND 100 AND length(input_json) <= 8000",
            name="ck_provider_workflow_unit_bounds",
        ),
        sa.CheckConstraint(
            "status != 'processing' OR (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_provider_workflow_unit_claim",
        ),
    )
    op.create_index(
        "ix_provider_workflow_units_workflow_key",
        "provider_workflow_units",
        ["workflow_key"],
    )
    op.create_index(
        "ix_provider_workflow_unit_due",
        "provider_workflow_units",
        ["workflow_key", "status", "next_attempt_at", "claim_expires_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_workflow_unit_due", table_name="provider_workflow_units")
    op.drop_index(
        "ix_provider_workflow_units_workflow_key", table_name="provider_workflow_units"
    )
    op.drop_table("provider_workflow_units")
    op.drop_index("ix_provider_run_dispatch_due", table_name="provider_run_dispatches")
    op.drop_index(
        "ix_provider_run_dispatches_run_key", table_name="provider_run_dispatches"
    )
    op.drop_table("provider_run_dispatches")
    op.drop_index("ix_provider_workflow_due", table_name="provider_workflow_states")
    op.drop_index("ix_provider_workflow_states_run_key", table_name="provider_workflow_states")
    op.drop_index("ix_provider_workflow_states_provider", table_name="provider_workflow_states")
    op.drop_table("provider_workflow_states")
    op.drop_index("uq_provider_sync_run_active_kind", table_name="provider_sync_runs")
    op.drop_index("ix_provider_sync_runs_run_kind", table_name="provider_sync_runs")
    op.drop_table("provider_sync_runs")
