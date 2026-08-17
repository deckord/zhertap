from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only

from app.models import PlanningCandidateReview, PlanningCandidateStatus, UrbanPlanLayer
from app.planning_candidate_reviews import (
    planning_candidate_key,
    upsert_planning_candidate_review,
)
from app.planning_free_space import (
    GOOD_ORIENTATION_DISTANCE_M,
    PlanningCandidatePoint,
    find_planning_candidate_points,
)
from app.planning_service import PlanningScope, _is_search_layer
from app.purposes import ALL_PURPOSES, GARDENING, LPH, LPH_FIELD_LAYER


@dataclass(frozen=True, slots=True)
class PlanningScopeProgress:
    region: str
    district: str
    locality: str
    requested_use: str
    allowed_layers: int
    restriction_layers: int
    search_layers: int
    shadow_layers: int
    queued_points: int
    reviewed_points: int
    empty_points: int
    stage: str
    next_action: str
    candidate_url: str


def build_planning_completion_report(
    session: Session,
    *,
    limit: int = 160,
) -> dict[str, Any]:
    scopes = _layer_scopes(session)
    review_counts = _review_counts(session)
    rows: list[PlanningScopeProgress] = []
    for scope_key, layers in scopes.items():
        region, district, locality, requested_use = scope_key
        allowed_layers = [row for row in layers if row.layer_kind == "allowed"]
        restriction_layers = [
            row for row in layers if row.layer_kind in {"prohibited", "red_line"}
        ]
        search_count = sum(1 for row in layers if _is_search_layer(row))
        shadow_count = len(layers) - search_count
        counts = review_counts.get(scope_key, Counter())
        queued = counts.get(PlanningCandidateStatus.queued.value, 0)
        empty = counts.get(PlanningCandidateStatus.empty.value, 0)
        reviewed = sum(
            count
            for status, count in counts.items()
            if status != PlanningCandidateStatus.queued.value
        )
        stage, next_action = _stage_and_action(
            allowed_count=len(allowed_layers),
            search_count=search_count,
            queued_points=queued,
            reviewed_points=reviewed,
            empty_points=empty,
        )
        rows.append(
            PlanningScopeProgress(
                region=region,
                district=district,
                locality=locality,
                requested_use=requested_use,
                allowed_layers=len(allowed_layers),
                restriction_layers=len(restriction_layers),
                search_layers=search_count,
                shadow_layers=shadow_count,
                queued_points=queued,
                reviewed_points=reviewed,
                empty_points=empty,
                stage=stage,
                next_action=next_action,
                candidate_url=_candidate_url(region, district, locality, requested_use),
            )
        )
    rows.sort(key=_scope_sort_key)
    stage_counts = Counter(row.stage for row in rows)
    return {
        "total_scopes": len(rows),
        "shown_scopes": min(len(rows), limit),
        "ready_scopes": stage_counts.get("ready", 0),
        "queued_scopes": stage_counts.get("queued", 0),
        "review_scopes": stage_counts.get("review", 0),
        "needs_queue_scopes": stage_counts.get("needs_queue", 0),
        "no_allowed_scopes": stage_counts.get("no_allowed", 0),
        "queued_points": sum(row.queued_points for row in rows),
        "reviewed_points": sum(row.reviewed_points for row in rows),
        "empty_points": sum(row.empty_points for row in rows),
        "stage_counts": dict(stage_counts),
        "scopes": rows[:limit],
    }


def queue_planning_candidates_for_all_scopes(
    session: Session,
    *,
    limit_per_scope: int = 5,
    max_scopes: int = 160,
    grid_step_m: int = 180,
    restriction_buffer_m: int = 20,
    reviewed_by: str | None = None,
) -> dict[str, int]:
    limit_per_scope = max(1, min(limit_per_scope, 20))
    max_scopes = max(1, min(max_scopes, 500))
    scopes = list(_layer_scopes(session).keys())[:max_scopes]
    created = 0
    skipped_existing = 0
    failed = 0
    with_points = 0
    for region, district, locality, requested_use in scopes:
        scope = PlanningScope(
            region=region,
            district=district,
            locality=locality,
            requested_use=requested_use,
        )
        existing = {
            planning_candidate_key(row.latitude, row.longitude)
            for row in session.scalars(
                select(PlanningCandidateReview).where(
                    PlanningCandidateReview.region == region,
                    PlanningCandidateReview.district == district,
                    PlanningCandidateReview.locality == locality,
                    PlanningCandidateReview.requested_use == requested_use,
                )
            )
        }
        try:
            result = find_planning_candidate_points(
                session,
                scope=scope,
                include_shadow=True,
                limit=limit_per_scope,
                grid_step_m=grid_step_m,
                restriction_buffer_m=restriction_buffer_m,
                use_egkn_context=False,
            )
        except Exception:
            failed += 1
            continue
        points = result.get("points") or []
        if points:
            with_points += 1
        for point in points:
            key = planning_candidate_key(point.latitude, point.longitude)
            if key in existing:
                skipped_existing += 1
                continue
            upsert_planning_candidate_review(
                session,
                scope=scope,
                latitude=point.latitude,
                longitude=point.longitude,
                status=PlanningCandidateStatus.queued.value,
                note="auto queue",
                trust_level=point.trust_level,
                allowed_area_ha=result.get("allowed_area_ha"),
                nearby_cadastre=point.nearby_cadastre,
                nearby_distance_m=point.nearby_distance_m,
                nearby_land_use=point.nearby_land_use,
                candidate_area_ha=point.candidate_area_ha,
                selection_reason=point.selection_reason,
                reviewed_by=reviewed_by,
            )
            existing.add(key)
            created += 1
    return {
        "scopes_checked": len(scopes),
        "scopes_with_points": with_points,
        "points_created": created,
        "points_existing": skipped_existing,
        "failed": failed,
    }


def queue_planning_candidates_for_next_scope_with_egkn(
    session: Session,
    *,
    limit_per_scope: int = 2,
    grid_step_m: int = 180,
    restriction_buffer_m: int = 20,
    reviewed_by: str | None = None,
) -> dict[str, Any]:
    limit_per_scope = max(1, min(limit_per_scope, 5))
    grid_step_m = max(30, min(grid_step_m, 500))
    for region, district, locality, requested_use in _layer_scopes(session).keys():
        existing_reviews = list(
            session.scalars(
                select(PlanningCandidateReview).where(
                    PlanningCandidateReview.region == region,
                    PlanningCandidateReview.district == district,
                    PlanningCandidateReview.locality == locality,
                    PlanningCandidateReview.requested_use == requested_use,
                )
            )
        )
        if any(row.selection_reason for row in existing_reviews):
            continue
        scope = PlanningScope(
            region=region,
            district=district,
            locality=locality,
            requested_use=requested_use,
        )
        try:
            result = find_planning_candidate_points(
                session,
                scope=scope,
                include_shadow=True,
                limit=max(10, limit_per_scope * 5),
                grid_step_m=grid_step_m,
                restriction_buffer_m=restriction_buffer_m,
                use_egkn_context=True,
            )
        except Exception as exc:
            return {
                "scope_found": 1,
                "points_created": 0,
                "failed": 1,
                "region": region,
                "district": district,
                "locality": locality,
                "requested_use": requested_use,
                "message": str(exc),
            }
        raw_points = result.get("points") or []
        points = _first_pass_points(raw_points, limit=limit_per_scope)
        for point in points:
            upsert_planning_candidate_review(
                session,
                scope=scope,
                latitude=point.latitude,
                longitude=point.longitude,
                status=PlanningCandidateStatus.queued.value,
                note="smart queue: пустое место по генплану + ориентир ЕГКН",
                trust_level=point.trust_level,
                allowed_area_ha=result.get("allowed_area_ha"),
                nearby_cadastre=point.nearby_cadastre,
                nearby_distance_m=point.nearby_distance_m,
                nearby_land_use=point.nearby_land_use,
                candidate_area_ha=point.candidate_area_ha,
                selection_reason=point.selection_reason,
                reviewed_by=reviewed_by,
            )
        return {
            "scope_found": 1,
            "points_created": len(points),
            "failed": 0,
            "region": region,
            "district": district,
            "locality": locality,
            "requested_use": requested_use,
            "message": result.get("egkn_message") or "",
        }
    return {
        "scope_found": 0,
        "points_created": 0,
        "failed": 0,
        "region": "",
        "district": "",
        "locality": "",
        "requested_use": "",
        "message": "все территории уже имеют умную очередь",
    }


def _first_pass_points(
    points: list[PlanningCandidatePoint],
    *,
    limit: int,
) -> list[PlanningCandidatePoint]:
    strong = [
        point
        for point in points
        if point.nearby_cadastre
        and point.nearby_distance_m is not None
        and point.nearby_distance_m <= GOOD_ORIENTATION_DISTANCE_M
    ]
    return strong[:limit]


def _layer_scopes(
    session: Session,
) -> dict[tuple[str, str, str, str], list[UrbanPlanLayer]]:
    rows = session.scalars(
        select(UrbanPlanLayer)
        .options(
            load_only(
                UrbanPlanLayer.region,
                UrbanPlanLayer.district,
                UrbanPlanLayer.locality,
                UrbanPlanLayer.purpose,
                UrbanPlanLayer.layer_kind,
                UrbanPlanLayer.active,
                UrbanPlanLayer.approved_for_search,
                UrbanPlanLayer.provenance_status,
                UrbanPlanLayer.identity_status,
                UrbanPlanLayer.qa_status,
                UrbanPlanLayer.independent_review,
                UrbanPlanLayer.source_sha256,
            )
        )
        .where(UrbanPlanLayer.layer_kind.in_(("allowed", "prohibited", "red_line")))
        .order_by(
            UrbanPlanLayer.region.asc(),
            UrbanPlanLayer.district.asc(),
            UrbanPlanLayer.locality.asc(),
            UrbanPlanLayer.purpose.asc(),
        )
    ).all()
    scopes: dict[tuple[str, str, str, str], list[UrbanPlanLayer]] = {}
    for row in rows:
        requested_use = _requested_use_for_purpose(row.purpose)
        key = (row.region, row.district, row.locality or "", requested_use)
        scopes.setdefault(key, []).append(row)
    return {
        key: layers
        for key, layers in scopes.items()
        if any(row.layer_kind == "allowed" for row in layers)
    }


def _review_counts(
    session: Session,
) -> dict[tuple[str, str, str, str], Counter[str]]:
    counts: dict[tuple[str, str, str, str], Counter[str]] = {}
    rows = session.execute(
        select(
            PlanningCandidateReview.region,
            PlanningCandidateReview.district,
            PlanningCandidateReview.locality,
            PlanningCandidateReview.requested_use,
            PlanningCandidateReview.status,
            func.count(PlanningCandidateReview.id),
        ).group_by(
            PlanningCandidateReview.region,
            PlanningCandidateReview.district,
            PlanningCandidateReview.locality,
            PlanningCandidateReview.requested_use,
            PlanningCandidateReview.status,
        )
    ).all()
    for region, district, locality, requested_use, status, count in rows:
        key = (region, district, locality or "", requested_use)
        counts.setdefault(key, Counter())[status] += int(count)
    return counts


def _requested_use_for_purpose(purpose: str | None) -> str:
    if purpose == GARDENING:
        return "GARDENING"
    if purpose == LPH_FIELD_LAYER:
        return "LPH_FIELD"
    if purpose in {ALL_PURPOSES, LPH, None, ""}:
        return "LPH_HOMESTEAD"
    return "LPH_HOMESTEAD"


def _stage_and_action(
    *,
    allowed_count: int,
    search_count: int,
    queued_points: int,
    reviewed_points: int,
    empty_points: int,
) -> tuple[str, str]:
    if allowed_count <= 0:
        return "no_allowed", "Найти разрешенную зону"
    if search_count > 0 and empty_points > 0:
        return "ready", "Проверять пустые точки по кадастру"
    if search_count > 0:
        return "ready", "Есть строгие слои, набрать пустые точки"
    if empty_points > 0:
        return "review", "Проверять пустые точки по кадастру"
    if reviewed_points > 0:
        return "review", "Продолжить ручную проверку"
    if queued_points > 0:
        return "queued", "Открывать Google и отмечать статус"
    return "needs_queue", "Создать точки для проверки"


def _candidate_url(
    region: str,
    district: str,
    locality: str,
    requested_use: str,
) -> str:
    params = urlencode(
        {
            "candidate_find": "1",
            "candidate_region": region,
            "candidate_district": district,
            "candidate_locality": locality,
            "candidate_use": requested_use,
            "candidate_limit": "10",
            "candidate_step": "180",
            "candidate_buffer": "20",
            "candidate_shadow": "1",
        }
    )
    return f"/admin/urban-plans?{params}#planning-candidates"


def _scope_sort_key(row: PlanningScopeProgress) -> tuple[int, str, str, str, str]:
    stage_rank = {
        "needs_queue": 0,
        "queued": 1,
        "review": 2,
        "ready": 3,
        "no_allowed": 4,
    }.get(row.stage, 9)
    return (
        stage_rank,
        row.region.lower(),
        row.district.lower(),
        row.locality.lower(),
        row.requested_use,
    )


__all__ = [
    "PlanningScopeProgress",
    "build_planning_completion_report",
    "queue_planning_candidates_for_all_scopes",
    "queue_planning_candidates_for_next_scope_with_egkn",
]
