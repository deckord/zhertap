import json
from dataclasses import dataclass
from typing import Any

from shapely import make_valid
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SearchRequest, UrbanPlanLayer
from app.providers.urban_plan import _matches_scope
from app.purposes import GARDENING, LPH_FIELD_LAYER, LPH_HOUSEHOLD_LAYER

PLANNING_REQUEST_TO_PURPOSE = {
    "LPH_HOMESTEAD": LPH_HOUSEHOLD_LAYER,
    "LPH_FIELD": LPH_FIELD_LAYER,
    "GARDENING": GARDENING,
}
SEARCH_QA_STATUSES = {"STRICT", "VERIFIED_STRICT"}


@dataclass(frozen=True, slots=True)
class PlanningScope:
    region: str | None = None
    district: str | None = None
    locality: str | None = None
    requested_use: str | None = None


def planning_coverage(
    session: Session,
    *,
    latitude: float,
    longitude: float,
    scope: PlanningScope | None = None,
    include_shadow: bool = False,
) -> dict[str, Any]:
    point = Point(longitude, latitude)
    return planning_check(
        session,
        geometry=point,
        scope=scope or PlanningScope(),
        include_shadow=include_shadow,
    )


def planning_check(
    session: Session,
    *,
    geometry: dict[str, Any] | BaseGeometry,
    scope: PlanningScope | None = None,
    include_shadow: bool = False,
) -> dict[str, Any]:
    scope = scope or PlanningScope()
    candidate = _input_geometry(geometry)
    layers = _candidate_layers(session, scope=scope, include_shadow=include_shadow)
    intersections = [
        _intersection_payload(layer, candidate)
        for layer in layers
        if _layer_geometry(layer).intersects(candidate)
    ]
    allowed = [row for row in intersections if row["layer_type"] == "allowed"]
    restrictions = [
        row
        for row in intersections
        if row["layer_type"] in {"prohibited", "red_line"}
    ]
    search_layers = [row for row in layers if _is_search_layer(row)]
    shadow_layers = [row for row in layers if not _is_search_layer(row)]

    intersecting_search_layers = [
        row for row in intersections if row["trust_level"] == "SEARCH"
    ]
    intersecting_shadow_layers = [
        row for row in intersections if row["trust_level"] == "SHADOW"
    ]

    if intersecting_search_layers:
        coverage_status = "AVAILABLE"
    elif intersecting_shadow_layers:
        coverage_status = "SHADOW_ONLY"
    elif search_layers and _scope_is_specific(scope):
        coverage_status = "AVAILABLE"
    elif shadow_layers and _scope_is_specific(scope):
        coverage_status = "SHADOW_ONLY"
    else:
        coverage_status = "NO_DATA"

    if coverage_status == "NO_DATA":
        result = "MANUAL_REVIEW"
    elif not allowed:
        result = "NO_ALLOWED_ZONE" if search_layers else "MANUAL_REVIEW"
    elif restrictions:
        result = "BLOCKED_BY_RESTRICTION"
    elif all(row["trust_level"] == "SEARCH" for row in allowed):
        result = "POSSIBLE"
    else:
        result = "MANUAL_REVIEW"

    documents = _documents_payload(intersections)
    return {
        "coverage_status": coverage_status,
        "result": result,
        "requested_use": scope.requested_use,
        "confidence": _confidence(coverage_status, result, intersections),
        "documents": documents,
        "intersections": intersections,
        "restrictions": restrictions,
    }


def _input_geometry(value: dict[str, Any] | BaseGeometry) -> BaseGeometry:
    if isinstance(value, BaseGeometry):
        geometry = value
    else:
        geometry = shape(value)
    geometry = make_valid(geometry)
    if geometry.is_empty:
        raise ValueError("geometry is empty")
    min_x, min_y, max_x, max_y = geometry.bounds
    if min_x < -180 or max_x > 180 or min_y < -90 or max_y > 90:
        raise ValueError("geometry must be WGS84 lon/lat")
    return geometry


def _candidate_layers(
    session: Session,
    *,
    scope: PlanningScope,
    include_shadow: bool,
) -> list[UrbanPlanLayer]:
    rows = session.scalars(select(UrbanPlanLayer)).all()
    matching = [
        row
        for row in rows
        if _matches_planning_scope(row, scope)
        and (include_shadow or _is_search_layer(row))
    ]
    return _prefer_most_specific_layers(matching)


def _prefer_most_specific_layers(layers: list[UrbanPlanLayer]) -> list[UrbanPlanLayer]:
    """Do not mix a locality genplan with a broader regional fallback."""
    if not layers:
        return []
    max_specificity = max(_layer_scope_specificity(row) for row in layers)
    return [row for row in layers if _layer_scope_specificity(row) == max_specificity]


def _layer_scope_specificity(layer: UrbanPlanLayer) -> int:
    def specific(value: str | None) -> bool:
        return bool(value and value.strip().casefold() not in {"*", "all"})

    if specific(layer.locality):
        return 2
    if specific(layer.district):
        return 1
    return 0


def _matches_planning_scope(layer: UrbanPlanLayer, scope: PlanningScope) -> bool:
    purpose = PLANNING_REQUEST_TO_PURPOSE.get(
        (scope.requested_use or "").upper(),
        scope.requested_use or layer.purpose,
    )
    request = SearchRequest(
        region=scope.region or layer.region,
        district=scope.district or layer.district,
        locality=scope.locality if scope.locality is not None else layer.locality,
        purpose=purpose,
        allotment_type="field" if purpose == LPH_FIELD_LAYER else "household",
    )
    return _matches_scope(layer, request)


def _scope_is_specific(scope: PlanningScope) -> bool:
    return bool(scope.region or scope.district or scope.locality)


def _is_search_layer(layer: UrbanPlanLayer) -> bool:
    return bool(
        layer.active
        and layer.approved_for_search
        and layer.provenance_status == "verified_official"
        and layer.identity_status == "matched"
        and layer.qa_status in SEARCH_QA_STATUSES
        and layer.independent_review
        and layer.source_sha256
    )


def _layer_geometry(layer: UrbanPlanLayer) -> BaseGeometry:
    return make_valid(shape(json.loads(layer.geometry_geojson)))


def _intersection_payload(layer: UrbanPlanLayer, candidate: BaseGeometry) -> dict[str, Any]:
    geometry = _layer_geometry(layer)
    intersection = geometry.intersection(candidate)
    if candidate.area > 0 and intersection.area > 0:
        percent = round((intersection.area / candidate.area) * 100, 2)
    else:
        percent = 100.0 if geometry.intersects(candidate) else 0.0
    return {
        "layer_id": layer.id,
        "layer_type": layer.layer_kind,
        "zone_name": layer.zone_name,
        "document_title": layer.title,
        "approval_document": layer.approval_document,
        "approval_date": layer.approval_date.isoformat() if layer.approval_date else None,
        "source_url": layer.source_url,
        "qa_status": layer.qa_status,
        "trust_level": "SEARCH" if _is_search_layer(layer) else "SHADOW",
        "intersection_percent": percent,
    }


def _documents_payload(intersections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str | None, str]] = set()
    documents: list[dict[str, Any]] = []
    for row in intersections:
        key = (
            row["document_title"],
            row["approval_date"],
            row["source_url"],
        )
        if key in seen:
            continue
        seen.add(key)
        documents.append(
            {
                "title": row["document_title"],
                "approval_document": row["approval_document"],
                "approval_date": row["approval_date"],
                "source_url": row["source_url"],
                "trust_level": row["trust_level"],
            }
        )
    return documents


def _confidence(
    coverage_status: str,
    result: str,
    intersections: list[dict[str, Any]],
) -> float:
    if coverage_status == "NO_DATA":
        return 0.0
    if coverage_status == "SHADOW_ONLY":
        return 0.35 if intersections else 0.2
    if result == "POSSIBLE":
        return 0.82
    if result == "BLOCKED_BY_RESTRICTION":
        return 0.78
    if result == "NO_ALLOWED_ZONE":
        return 0.65
    return 0.5
