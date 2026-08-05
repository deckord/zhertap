"""auction v2 intelligence

Revision ID: 9c75a7cda2f1
Revises: f0ff6f61408d
Create Date: 2026-08-02 04:10:00.000000
"""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c75a7cda2f1"
down_revision: str | Sequence[str] | None = "f0ff6f61408d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auction_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("source_type", sa.String(length=48), nullable=False),
        sa.Column("name", sa.String(length=220), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("region", sa.String(length=160), nullable=False),
        sa.Column("parser_kind", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("crawl_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("quality_status", sa.String(length=32), nullable=False),
        sa.Column("legal_status", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_auction_source_code"),
    )
    op.create_index(op.f("ix_auction_sources_active"), "auction_sources", ["active"], unique=False)
    op.create_index(op.f("ix_auction_sources_code"), "auction_sources", ["code"], unique=False)
    op.create_index(
        op.f("ix_auction_sources_last_checked_at"),
        "auction_sources",
        ["last_checked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_sources_legal_status"),
        "auction_sources",
        ["legal_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_sources_parser_kind"),
        "auction_sources",
        ["parser_kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_sources_priority"), "auction_sources", ["priority"], unique=False
    )
    op.create_index(
        op.f("ix_auction_sources_quality_status"),
        "auction_sources",
        ["quality_status"],
        unique=False,
    )
    op.create_index(op.f("ix_auction_sources_region"), "auction_sources", ["region"], unique=False)
    op.create_index(
        op.f("ix_auction_sources_source_type"),
        "auction_sources",
        ["source_type"],
        unique=False,
    )

    op.create_table(
        "auction_crawl_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("items_seen", sa.Integer(), nullable=False),
        sa.Column("items_created", sa.Integer(), nullable=False),
        sa.Column("items_updated", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_payload_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["auction_sources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_auction_crawl_runs_source_id"),
        "auction_crawl_runs",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_crawl_runs_started_at"),
        "auction_crawl_runs",
        ["started_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_crawl_runs_status"),
        "auction_crawl_runs",
        ["status"],
        unique=False,
    )

    op.create_table(
        "auction_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("raw_payload_json", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["auction_sources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_auction_evidence_confidence"),
        "auction_evidence",
        ["confidence"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_evidence_evidence_type"),
        "auction_evidence",
        ["evidence_type"],
        unique=False,
    )
    op.create_index(op.f("ix_auction_evidence_lot_id"), "auction_evidence", ["lot_id"], unique=False)
    op.create_index(
        op.f("ix_auction_evidence_observed_at"),
        "auction_evidence",
        ["observed_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_evidence_source_id"),
        "auction_evidence",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_evidence_status"), "auction_evidence", ["status"], unique=False
    )

    op.create_table(
        "auction_lot_geo_checks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.Column("cadastre_status", sa.String(length=32), nullable=False),
        sa.Column("coordinate_status", sa.String(length=32), nullable=False),
        sa.Column("urban_plan_status", sa.String(length=32), nullable=False),
        sa.Column("red_line_status", sa.String(length=32), nullable=False),
        sa.Column("engineering_status", sa.String(length=32), nullable=False),
        sa.Column("market_status", sa.String(length=32), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("egkn_url", sa.Text(), nullable=True),
        sa.Column("google_maps_url", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lot_id", name="uq_auction_lot_geo_check_lot"),
    )
    op.create_index(
        op.f("ix_auction_lot_geo_checks_cadastre_status"),
        "auction_lot_geo_checks",
        ["cadastre_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_geo_checks_checked_at"),
        "auction_lot_geo_checks",
        ["checked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_geo_checks_coordinate_status"),
        "auction_lot_geo_checks",
        ["coordinate_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_geo_checks_engineering_status"),
        "auction_lot_geo_checks",
        ["engineering_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_geo_checks_lot_id"),
        "auction_lot_geo_checks",
        ["lot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_geo_checks_market_status"),
        "auction_lot_geo_checks",
        ["market_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_geo_checks_red_line_status"),
        "auction_lot_geo_checks",
        ["red_line_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_geo_checks_urban_plan_status"),
        "auction_lot_geo_checks",
        ["urban_plan_status"],
        unique=False,
    )

    op.create_table(
        "auction_lot_v2_analysis",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("confidence_level", sa.String(length=16), nullable=False),
        sa.Column("recommended_action", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("readiness_json", sa.Text(), nullable=False),
        sa.Column("risk_flags_json", sa.Text(), nullable=False),
        sa.Column("source_status_json", sa.Text(), nullable=False),
        sa.Column("max_bid_conservative_kzt", sa.Float(), nullable=True),
        sa.Column("max_bid_market_kzt", sa.Float(), nullable=True),
        sa.Column("max_bid_aggressive_kzt", sa.Float(), nullable=True),
        sa.Column("price_per_sotka", sa.Float(), nullable=True),
        sa.Column("district_average_price_per_sotka", sa.Float(), nullable=True),
        sa.Column("district_difference_percent", sa.Float(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lot_id", name="uq_auction_lot_v2_analysis_lot"),
    )
    op.create_index(
        op.f("ix_auction_lot_v2_analysis_checked_at"),
        "auction_lot_v2_analysis",
        ["checked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_v2_analysis_confidence_level"),
        "auction_lot_v2_analysis",
        ["confidence_level"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_v2_analysis_lot_id"),
        "auction_lot_v2_analysis",
        ["lot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_v2_analysis_recommended_action"),
        "auction_lot_v2_analysis",
        ["recommended_action"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_v2_analysis_risk_level"),
        "auction_lot_v2_analysis",
        ["risk_level"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_lot_v2_analysis_score"),
        "auction_lot_v2_analysis",
        ["score"],
        unique=False,
    )

    op.create_table(
        "auction_market_comparables",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=True),
        sa.Column("source_name", sa.String(length=80), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("region", sa.String(length=160), nullable=True),
        sa.Column("district", sa.String(length=160), nullable=True),
        sa.Column("locality", sa.String(length=160), nullable=True),
        sa.Column("area_ha", sa.Float(), nullable=True),
        sa.Column("price_kzt", sa.Float(), nullable=True),
        sa.Column("price_per_sotka", sa.Float(), nullable=True),
        sa.Column("listing_status", sa.String(length=32), nullable=False),
        sa.Column("raw_payload_json", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_auction_market_comparables_district"),
        "auction_market_comparables",
        ["district"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_market_comparables_listing_status"),
        "auction_market_comparables",
        ["listing_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_market_comparables_locality"),
        "auction_market_comparables",
        ["locality"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_market_comparables_lot_id"),
        "auction_market_comparables",
        ["lot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_market_comparables_observed_at"),
        "auction_market_comparables",
        ["observed_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_market_comparables_price_per_sotka"),
        "auction_market_comparables",
        ["price_per_sotka"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_market_comparables_region"),
        "auction_market_comparables",
        ["region"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_market_comparables_source_name"),
        "auction_market_comparables",
        ["source_name"],
        unique=False,
    )

    op.create_table(
        "auction_watchlists",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("region", sa.String(length=160), nullable=True),
        sa.Column("district", sa.String(length=160), nullable=True),
        sa.Column("locality", sa.String(length=160), nullable=True),
        sa.Column("purpose_query", sa.String(length=160), nullable=True),
        sa.Column("min_score", sa.Integer(), nullable=True),
        sa.Column("max_price_kzt", sa.Float(), nullable=True),
        sa.Column("min_area_ha", sa.Float(), nullable=True),
        sa.Column("max_area_ha", sa.Float(), nullable=True),
        sa.Column("notify_channels_json", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_auction_watchlists_account_id"),
        "auction_watchlists",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_watchlists_active"), "auction_watchlists", ["active"], unique=False
    )
    op.create_index(
        op.f("ix_auction_watchlists_district"),
        "auction_watchlists",
        ["district"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_watchlists_locality"),
        "auction_watchlists",
        ["locality"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_watchlists_region"), "auction_watchlists", ["region"], unique=False
    )

    op.create_table(
        "auction_user_lot_pipeline",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("lot_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("decision", sa.String(length=40), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("max_bid_kzt", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["lot_id"], ["auction_lots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "lot_id", name="uq_auction_pipeline_account_lot"),
    )
    op.create_index(
        op.f("ix_auction_user_lot_pipeline_account_id"),
        "auction_user_lot_pipeline",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_user_lot_pipeline_decided_at"),
        "auction_user_lot_pipeline",
        ["decided_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_user_lot_pipeline_decision"),
        "auction_user_lot_pipeline",
        ["decision"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_user_lot_pipeline_lot_id"),
        "auction_user_lot_pipeline",
        ["lot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_user_lot_pipeline_pinned"),
        "auction_user_lot_pipeline",
        ["pinned"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_user_lot_pipeline_priority"),
        "auction_user_lot_pipeline",
        ["priority"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_user_lot_pipeline_reminder_at"),
        "auction_user_lot_pipeline",
        ["reminder_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auction_user_lot_pipeline_stage"),
        "auction_user_lot_pipeline",
        ["stage"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_auction_user_lot_pipeline_stage"), table_name="auction_user_lot_pipeline")
    op.drop_index(
        op.f("ix_auction_user_lot_pipeline_reminder_at"),
        table_name="auction_user_lot_pipeline",
    )
    op.drop_index(
        op.f("ix_auction_user_lot_pipeline_priority"),
        table_name="auction_user_lot_pipeline",
    )
    op.drop_index(
        op.f("ix_auction_user_lot_pipeline_pinned"),
        table_name="auction_user_lot_pipeline",
    )
    op.drop_index(op.f("ix_auction_user_lot_pipeline_lot_id"), table_name="auction_user_lot_pipeline")
    op.drop_index(
        op.f("ix_auction_user_lot_pipeline_decision"),
        table_name="auction_user_lot_pipeline",
    )
    op.drop_index(
        op.f("ix_auction_user_lot_pipeline_decided_at"),
        table_name="auction_user_lot_pipeline",
    )
    op.drop_index(
        op.f("ix_auction_user_lot_pipeline_account_id"),
        table_name="auction_user_lot_pipeline",
    )
    op.drop_table("auction_user_lot_pipeline")

    op.drop_index(op.f("ix_auction_watchlists_region"), table_name="auction_watchlists")
    op.drop_index(op.f("ix_auction_watchlists_locality"), table_name="auction_watchlists")
    op.drop_index(op.f("ix_auction_watchlists_district"), table_name="auction_watchlists")
    op.drop_index(op.f("ix_auction_watchlists_active"), table_name="auction_watchlists")
    op.drop_index(op.f("ix_auction_watchlists_account_id"), table_name="auction_watchlists")
    op.drop_table("auction_watchlists")

    op.drop_index(
        op.f("ix_auction_market_comparables_source_name"),
        table_name="auction_market_comparables",
    )
    op.drop_index(op.f("ix_auction_market_comparables_region"), table_name="auction_market_comparables")
    op.drop_index(
        op.f("ix_auction_market_comparables_price_per_sotka"),
        table_name="auction_market_comparables",
    )
    op.drop_index(
        op.f("ix_auction_market_comparables_observed_at"),
        table_name="auction_market_comparables",
    )
    op.drop_index(op.f("ix_auction_market_comparables_lot_id"), table_name="auction_market_comparables")
    op.drop_index(
        op.f("ix_auction_market_comparables_locality"),
        table_name="auction_market_comparables",
    )
    op.drop_index(
        op.f("ix_auction_market_comparables_listing_status"),
        table_name="auction_market_comparables",
    )
    op.drop_index(
        op.f("ix_auction_market_comparables_district"),
        table_name="auction_market_comparables",
    )
    op.drop_table("auction_market_comparables")

    op.drop_index(op.f("ix_auction_lot_v2_analysis_score"), table_name="auction_lot_v2_analysis")
    op.drop_index(
        op.f("ix_auction_lot_v2_analysis_risk_level"),
        table_name="auction_lot_v2_analysis",
    )
    op.drop_index(
        op.f("ix_auction_lot_v2_analysis_recommended_action"),
        table_name="auction_lot_v2_analysis",
    )
    op.drop_index(op.f("ix_auction_lot_v2_analysis_lot_id"), table_name="auction_lot_v2_analysis")
    op.drop_index(
        op.f("ix_auction_lot_v2_analysis_confidence_level"),
        table_name="auction_lot_v2_analysis",
    )
    op.drop_index(
        op.f("ix_auction_lot_v2_analysis_checked_at"),
        table_name="auction_lot_v2_analysis",
    )
    op.drop_table("auction_lot_v2_analysis")

    op.drop_index(
        op.f("ix_auction_lot_geo_checks_urban_plan_status"),
        table_name="auction_lot_geo_checks",
    )
    op.drop_index(
        op.f("ix_auction_lot_geo_checks_red_line_status"),
        table_name="auction_lot_geo_checks",
    )
    op.drop_index(
        op.f("ix_auction_lot_geo_checks_market_status"),
        table_name="auction_lot_geo_checks",
    )
    op.drop_index(op.f("ix_auction_lot_geo_checks_lot_id"), table_name="auction_lot_geo_checks")
    op.drop_index(
        op.f("ix_auction_lot_geo_checks_engineering_status"),
        table_name="auction_lot_geo_checks",
    )
    op.drop_index(
        op.f("ix_auction_lot_geo_checks_coordinate_status"),
        table_name="auction_lot_geo_checks",
    )
    op.drop_index(
        op.f("ix_auction_lot_geo_checks_checked_at"),
        table_name="auction_lot_geo_checks",
    )
    op.drop_index(
        op.f("ix_auction_lot_geo_checks_cadastre_status"),
        table_name="auction_lot_geo_checks",
    )
    op.drop_table("auction_lot_geo_checks")

    op.drop_index(op.f("ix_auction_evidence_status"), table_name="auction_evidence")
    op.drop_index(op.f("ix_auction_evidence_source_id"), table_name="auction_evidence")
    op.drop_index(op.f("ix_auction_evidence_observed_at"), table_name="auction_evidence")
    op.drop_index(op.f("ix_auction_evidence_lot_id"), table_name="auction_evidence")
    op.drop_index(op.f("ix_auction_evidence_evidence_type"), table_name="auction_evidence")
    op.drop_index(op.f("ix_auction_evidence_confidence"), table_name="auction_evidence")
    op.drop_table("auction_evidence")

    op.drop_index(op.f("ix_auction_crawl_runs_status"), table_name="auction_crawl_runs")
    op.drop_index(op.f("ix_auction_crawl_runs_started_at"), table_name="auction_crawl_runs")
    op.drop_index(op.f("ix_auction_crawl_runs_source_id"), table_name="auction_crawl_runs")
    op.drop_table("auction_crawl_runs")

    op.drop_index(op.f("ix_auction_sources_source_type"), table_name="auction_sources")
    op.drop_index(op.f("ix_auction_sources_region"), table_name="auction_sources")
    op.drop_index(op.f("ix_auction_sources_quality_status"), table_name="auction_sources")
    op.drop_index(op.f("ix_auction_sources_priority"), table_name="auction_sources")
    op.drop_index(op.f("ix_auction_sources_parser_kind"), table_name="auction_sources")
    op.drop_index(op.f("ix_auction_sources_legal_status"), table_name="auction_sources")
    op.drop_index(op.f("ix_auction_sources_last_checked_at"), table_name="auction_sources")
    op.drop_index(op.f("ix_auction_sources_code"), table_name="auction_sources")
    op.drop_index(op.f("ix_auction_sources_active"), table_name="auction_sources")
    op.drop_table("auction_sources")
