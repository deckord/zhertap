from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only

from app.config import settings
from app.genplan_pipeline import (
    legend_entry_stats,
    list_pipeline_documents,
    pipeline_document_stats,
)
from app.models import UrbanPlanLayer, UrbanPlanSource
from app.planning_candidate_reviews import (
    PLANNING_CANDIDATE_STATUS_LABELS,
    list_planning_candidate_reviews,
    planning_candidate_review_lookup,
)
from app.planning_completion import build_planning_completion_report
from app.planning_free_space import find_planning_candidate_points
from app.planning_service import PlanningScope, planning_coverage


def build_urban_plans_admin_context(
    request: Request,
    session: Session,
) -> dict[str, Any]:
    planning_probe_defaults = {
        "region": request.query_params.get("planning_region", "Акмолинская область"),
        "district": request.query_params.get("planning_district", "г.Акколь"),
        "locality": request.query_params.get("planning_locality", "г.Акколь"),
        "requested_use": request.query_params.get("planning_use", "LPH_HOMESTEAD"),
        "latitude": request.query_params.get("planning_lat", "51.992950"),
        "longitude": request.query_params.get("planning_lon", "70.930765"),
        "include_shadow": request.query_params.get("planning_shadow", "1") == "1",
    }
    candidate_defaults = {
        "region": request.query_params.get("candidate_region", "Акмолинская область"),
        "district": request.query_params.get("candidate_district", "г.Акколь"),
        "locality": request.query_params.get("candidate_locality", "г.Акколь"),
        "requested_use": request.query_params.get("candidate_use", "LPH_HOMESTEAD"),
        "limit": request.query_params.get("candidate_limit", "25"),
        "grid_step_m": request.query_params.get("candidate_step", "90"),
        "restriction_buffer_m": request.query_params.get("candidate_buffer", "20"),
        "include_shadow": request.query_params.get("candidate_shadow", "1") == "1",
        "use_egkn_context": request.query_params.get("candidate_egkn", "1") == "1",
    }
    candidate_scope = _candidate_scope(candidate_defaults)
    planning_probe_result, planning_probe_error = _planning_probe_result(
        request,
        session,
        planning_probe_defaults,
    )
    candidate_result, candidate_error = _candidate_result(
        request,
        session,
        candidate_scope,
        candidate_defaults,
    )
    candidate_reviews = list_planning_candidate_reviews(
        session,
        scope=candidate_scope,
        limit=200,
    )
    return {
        "layers": _urban_plan_layers(session),
        "layer_resolution_stats": _urban_plan_resolution_stats(session),
        "sources": _urban_plan_sources(session),
        "source_stats": _urban_plan_source_stats(session),
        "pipeline_documents": list_pipeline_documents(session, limit=120),
        "pipeline_stats": pipeline_document_stats(session),
        "legend_stats": legend_entry_stats(session),
        "app_name": settings.app_name,
        "strict_mode": settings.urban_plan_check_mode.lower() == "strict",
        "planning_probe_defaults": planning_probe_defaults,
        "planning_probe_result": planning_probe_result,
        "planning_probe_error": planning_probe_error,
        "candidate_defaults": candidate_defaults,
        "candidate_result": candidate_result,
        "candidate_error": candidate_error,
        "candidate_reviews": candidate_reviews,
        "candidate_review_lookup": planning_candidate_review_lookup(candidate_reviews),
        "candidate_status_labels": PLANNING_CANDIDATE_STATUS_LABELS,
        "planning_completion": build_planning_completion_report(session),
    }


def _candidate_scope(candidate_defaults: dict[str, Any]) -> PlanningScope:
    return PlanningScope(
        region=str(candidate_defaults["region"]).strip() or None,
        district=str(candidate_defaults["district"]).strip() or None,
        locality=str(candidate_defaults["locality"]).strip() or None,
        requested_use=str(candidate_defaults["requested_use"]),
    )


def _planning_probe_result(
    request: Request,
    session: Session,
    defaults: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not request.query_params.get("planning_probe"):
        return None, None
    try:
        return (
            planning_coverage(
                session,
                latitude=float(defaults["latitude"]),
                longitude=float(defaults["longitude"]),
                scope=PlanningScope(
                    region=str(defaults["region"]).strip() or None,
                    district=str(defaults["district"]).strip() or None,
                    locality=str(defaults["locality"]).strip() or None,
                    requested_use=str(defaults["requested_use"]),
                ),
                include_shadow=bool(defaults["include_shadow"]),
            ),
            None,
        )
    except (TypeError, ValueError) as exc:
        return None, str(exc)


def _candidate_result(
    request: Request,
    session: Session,
    scope: PlanningScope,
    defaults: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not request.query_params.get("candidate_find"):
        return None, None
    try:
        return (
            find_planning_candidate_points(
                session,
                scope=scope,
                include_shadow=bool(defaults["include_shadow"]),
                limit=int(defaults["limit"]),
                grid_step_m=int(defaults["grid_step_m"]),
                restriction_buffer_m=int(defaults["restriction_buffer_m"]),
                use_egkn_context=bool(defaults["use_egkn_context"]),
            ),
            None,
        )
    except (TypeError, ValueError) as exc:
        return None, str(exc)


def _urban_plan_layers(session: Session) -> list[UrbanPlanLayer]:
    return list(
        session.scalars(
            select(UrbanPlanLayer)
            .options(
                load_only(
                    UrbanPlanLayer.id,
                    UrbanPlanLayer.region,
                    UrbanPlanLayer.district,
                    UrbanPlanLayer.locality,
                    UrbanPlanLayer.purpose,
                    UrbanPlanLayer.layer_kind,
                    UrbanPlanLayer.zone_name,
                    UrbanPlanLayer.title,
                    UrbanPlanLayer.approval_document,
                    UrbanPlanLayer.approval_date,
                    UrbanPlanLayer.source_authority,
                    UrbanPlanLayer.source_url,
                    UrbanPlanLayer.source_epsg,
                    UrbanPlanLayer.source_file_name,
                    UrbanPlanLayer.source_sha256,
                    UrbanPlanLayer.provenance_status,
                    UrbanPlanLayer.identity_status,
                    UrbanPlanLayer.qa_status,
                    UrbanPlanLayer.approved_for_search,
                    UrbanPlanLayer.active,
                    UrbanPlanLayer.created_at,
                )
            )
            .order_by(
                UrbanPlanLayer.active.desc(),
                UrbanPlanLayer.created_at.desc(),
            )
        )
    )


def _urban_plan_sources(session: Session) -> list[UrbanPlanSource]:
    return list(
        session.scalars(
            select(UrbanPlanSource)
            .options(
                load_only(
                    UrbanPlanSource.id,
                    UrbanPlanSource.platform,
                    UrbanPlanSource.source_type,
                    UrbanPlanSource.external_id,
                    UrbanPlanSource.region,
                    UrbanPlanSource.district,
                    UrbanPlanSource.locality,
                    UrbanPlanSource.title,
                    UrbanPlanSource.approval_document,
                    UrbanPlanSource.approval_date,
                    UrbanPlanSource.coverage_status,
                    UrbanPlanSource.import_status,
                    UrbanPlanSource.layer_count,
                    UrbanPlanSource.last_error,
                    UrbanPlanSource.notes,
                    UrbanPlanSource.updated_at,
                )
            )
            .order_by(
                UrbanPlanSource.coverage_status.desc(),
                UrbanPlanSource.locality.asc(),
                UrbanPlanSource.updated_at.desc(),
            )
            .limit(700)
        )
    )


def _urban_plan_resolution_stats(session: Session) -> dict[str, int]:
    result = {
        "active": 0,
        "reviewed_hold": 0,
        "superseded": 0,
        "unresolved": 0,
    }
    for active, qa_status, count in session.execute(
        select(
            UrbanPlanLayer.active,
            UrbanPlanLayer.qa_status,
            func.count(UrbanPlanLayer.id),
        ).group_by(UrbanPlanLayer.active, UrbanPlanLayer.qa_status)
    ):
        value = int(count)
        if active:
            result["active"] += value
        elif qa_status == "REVIEWED_HOLD":
            result["reviewed_hold"] += value
        elif qa_status == "SUPERSEDED":
            result["superseded"] += value
        else:
            result["unresolved"] += value
    result["total"] = sum(result.values())
    result["processed"] = (
        result["active"] + result["reviewed_hold"] + result["superseded"]
    )
    return result


def _urban_plan_source_stats(session: Session) -> list[tuple[str, str, int]]:
    return [
        (str(platform), str(status), int(count))
        for platform, status, count in session.execute(
            select(
                UrbanPlanSource.platform,
                UrbanPlanSource.coverage_status,
                func.count(UrbanPlanSource.id),
            ).group_by(UrbanPlanSource.platform, UrbanPlanSource.coverage_status)
        ).all()
    ]
