"""Fail-closed contract for structured official Territory Intelligence facts.

This module is deliberately pure and network-free. It accepts only provider-mapped
codes; labels and locality prose are display evidence and never determine event
meaning or parcel applicability. Persistence and spatial linking are separate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from app.auction_parcel_geometry import GeometryValidationError, validate_parcel_geojson

CONTRACT_VERSION = "territory-intelligence/2026.2-source-date-applicability"
MAX_TEXT = 320
MAX_URL = 2_048
MAX_GEOMETRY_BYTES = 256_000
KZ_LATITUDE = (40.0, 56.0)
KZ_LONGITUDE = (46.0, 88.0)

RecordKind = Literal["event", "demographic"]
Direction = Literal["positive", "negative"]
LifecycleState = Literal[
    "announced", "approved", "in_progress", "completed", "suspended", "cancelled"
]
TransitionDecision = Literal["same", "advance", "correction", "conflict", "stale"]

EVENT_CODES = frozenset(
    {
        "infrastructure_commissioned",
        "road_opened",
        "public_facility_opened",
        "planning_act_approved",
        "project_cancelled",
        "facility_closed",
        "emergency_declared",
        "restriction_established",
    }
)
INDICATOR_CODES = frozenset(
    {"population_total", "births_total", "deaths_total", "migration_balance"}
)
LIFECYCLE_STATES = frozenset(
    {"announced", "approved", "in_progress", "completed", "suspended", "cancelled"}
)
FORWARD_TRANSITIONS: dict[str, frozenset[str]] = {
    "announced": frozenset({"approved", "in_progress", "completed", "suspended", "cancelled"}),
    "approved": frozenset({"in_progress", "completed", "suspended", "cancelled"}),
    "in_progress": frozenset({"completed", "suspended", "cancelled"}),
    "suspended": frozenset({"in_progress", "completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


class TerritoryIntelligenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TerritoryEvent:
    event_key: str
    event_code: str
    direction: Direction
    direction_basis: Literal["official_field", "source_code_policy"]
    lifecycle_state: LifecycleState
    event_date: date
    correction_of_revision: int | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class DemographicValue:
    indicator_code: str
    period_start: date
    period_end: date
    value: int
    unit: Literal["persons"]
    methodology_code: str | None = None


@dataclass(frozen=True, slots=True)
class TerritoryObservation:
    provider_id: str
    source_record_id: str
    source_revision: int
    record_kind: RecordKind
    authority_name: str
    source_url: str
    source_published_at: datetime
    observed_at: datetime
    territory_code: str | None
    geometry_geojson: dict[str, object] | None
    geometry_sha256: str | None
    event: TerritoryEvent | None
    demographic: DemographicValue | None
    content_hash: str
    linkage_eligible: bool
    contract_version: str = CONTRACT_VERSION


@dataclass(frozen=True, slots=True)
class GeographicApplicability:
    status: Literal["applicable", "not_applicable", "manual_required"]
    scope: Literal["parcel", "territory", "unknown"]
    basis: Literal[
        "polygon_contains_parcel",
        "polygon_excludes_parcel",
        "territory_code_match",
        "territory_code_mismatch",
        "scope_polygon_covers_parcel",
        "scope_polygon_intersects_parcel",
        "scope_polygon_excludes_parcel",
        "insufficient_official_scope",
    ]
    overlap_ratio: float | None = None


def _text(value: object, *, limit: int = MAX_TEXT, required: bool = True) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        if required:
            raise TerritoryIntelligenceError("invalid_text")
        return None
    result = re.sub(r"\s+", " ", value).strip()
    if not result or len(result) > limit:
        if required:
            raise TerritoryIntelligenceError("invalid_text")
        return None
    return result


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _revision(value: object, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2**31 - 1:
        raise TerritoryIntelligenceError("invalid_source_revision")
    return value


def _coordinates(value: object) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []

    def visit(node: object) -> None:
        if not isinstance(node, Sequence) or isinstance(node, (str, bytes, bytearray)):
            raise TerritoryIntelligenceError("invalid_geometry")
        if len(node) >= 2 and all(
            isinstance(item, (int, float)) and not isinstance(item, bool) for item in node[:2]
        ):
            lon, lat = float(node[0]), float(node[1])
            if not (
                math.isfinite(lon)
                and math.isfinite(lat)
                and KZ_LONGITUDE[0] <= lon <= KZ_LONGITUDE[1]
                and KZ_LATITUDE[0] <= lat <= KZ_LATITUDE[1]
            ):
                raise TerritoryIntelligenceError("invalid_geometry")
            result.append((lon, lat))
            return
        if not node:
            raise TerritoryIntelligenceError("invalid_geometry")
        for child in node:
            visit(child)

    visit(value)
    return result


def _geometry(value: object) -> tuple[dict[str, object] | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, Mapping):
        raise TerritoryIntelligenceError("invalid_geometry")
    kind = value.get("type")
    if kind not in {"Point", "LineString", "Polygon", "MultiPolygon"}:
        raise TerritoryIntelligenceError("invalid_geometry")
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(canonical.encode("utf-8")) > MAX_GEOMETRY_BYTES:
        raise TerritoryIntelligenceError("invalid_geometry")
    points = _coordinates(value.get("coordinates"))
    minimum = {"Point": 1, "LineString": 2, "Polygon": 4, "MultiPolygon": 4}[str(kind)]
    if len(points) < minimum:
        raise TerritoryIntelligenceError("invalid_geometry")
    if kind in {"Polygon", "MultiPolygon"} and points[0] != points[-1]:
        raise TerritoryIntelligenceError("invalid_geometry")
    normalized = json.loads(canonical)
    return normalized, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event(value: object) -> TerritoryEvent:
    if not isinstance(value, Mapping):
        raise TerritoryIntelligenceError("event_required")
    code = value.get("event_code")
    if code not in EVENT_CODES:
        raise TerritoryIntelligenceError("unsupported_event_code")
    direction = value.get("direction")
    if direction not in {"positive", "negative"}:
        raise TerritoryIntelligenceError("invalid_event_direction")
    basis = value.get("direction_basis")
    if basis not in {"official_field", "source_code_policy"}:
        raise TerritoryIntelligenceError("invalid_direction_basis")
    lifecycle = value.get("lifecycle_state")
    if lifecycle not in LIFECYCLE_STATES:
        raise TerritoryIntelligenceError("invalid_lifecycle_state")
    event_date = value.get("event_date")
    if not isinstance(event_date, date) or isinstance(event_date, datetime):
        raise TerritoryIntelligenceError("event_date_required")
    return TerritoryEvent(
        event_key=_text(value.get("event_key"), limit=160) or "",
        event_code=str(code),
        direction=direction,  # type: ignore[arg-type]
        direction_basis=basis,  # type: ignore[arg-type]
        lifecycle_state=lifecycle,  # type: ignore[arg-type]
        event_date=event_date,
        correction_of_revision=_revision(value.get("correction_of_revision"), optional=True),
        label=_text(value.get("label"), required=False),
    )


def _demographic(value: object) -> DemographicValue:
    if not isinstance(value, Mapping):
        raise TerritoryIntelligenceError("demographic_required")
    code = value.get("indicator_code")
    if code not in INDICATOR_CODES:
        raise TerritoryIntelligenceError("unsupported_indicator_code")
    start, end = value.get("period_start"), value.get("period_end")
    if (
        not isinstance(start, date)
        or isinstance(start, datetime)
        or not isinstance(end, date)
        or isinstance(end, datetime)
        or end < start
    ):
        raise TerritoryIntelligenceError("invalid_demographic_period")
    number = value.get("value")
    if isinstance(number, bool) or not isinstance(number, int) or not -(10**9) <= number <= 10**9:
        raise TerritoryIntelligenceError("invalid_demographic_value")
    if code != "migration_balance" and number < 0:
        raise TerritoryIntelligenceError("invalid_demographic_value")
    if value.get("unit") != "persons":
        raise TerritoryIntelligenceError("invalid_demographic_unit")
    return DemographicValue(
        indicator_code=str(code),
        period_start=start,
        period_end=end,
        value=number,
        unit="persons",
        methodology_code=_text(value.get("methodology_code"), limit=160, required=False),
    )


def normalize_territory_observation(payload: Mapping[str, object]) -> TerritoryObservation:
    provider = _text(payload.get("provider_id"), limit=128) or ""
    record = _text(payload.get("source_record_id"), limit=160) or ""
    try:
        authority = _text(payload.get("authority_name"), limit=240) or ""
    except TerritoryIntelligenceError as exc:
        raise TerritoryIntelligenceError("invalid_authority") from exc
    source_url = _text(payload.get("source_url"), limit=MAX_URL) or ""
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise TerritoryIntelligenceError("official_https_source_required")
    observed = payload.get("observed_at")
    if not _aware(observed):
        raise TerritoryIntelligenceError("aware_observed_at_required")
    published = payload.get("source_published_at")
    if not _aware(published):
        raise TerritoryIntelligenceError("aware_source_published_at_required")
    if published > observed:
        raise TerritoryIntelligenceError("source_published_after_observation")
    revision = _revision(payload.get("source_revision"))
    kind = payload.get("record_kind")
    if kind not in {"event", "demographic"}:
        raise TerritoryIntelligenceError("invalid_record_kind")
    geometry, geometry_hash = _geometry(payload.get("geometry_geojson"))
    territory_code = _text(payload.get("territory_code"), limit=64, required=False)
    event = _event(payload.get("event")) if kind == "event" else None
    demographic = _demographic(payload.get("demographic")) if kind == "demographic" else None
    if kind == "event" and payload.get("demographic") is not None:
        raise TerritoryIntelligenceError("mixed_record_payload")
    if kind == "demographic" and payload.get("event") is not None:
        raise TerritoryIntelligenceError("mixed_record_payload")
    material = {
        "provider_id": provider,
        "source_record_id": record,
        "source_revision": revision,
        "record_kind": kind,
        "authority_name": authority,
        "source_url": source_url,
        "source_published_at": published.isoformat(),  # type: ignore[union-attr]
        "observed_at": observed.isoformat(),  # type: ignore[union-attr]
        "territory_code": territory_code,
        "geometry_sha256": geometry_hash,
        "event": None,
        "demographic": None,
    }
    # Build deterministic representations explicitly for slots dataclasses.
    if event:
        material["event"] = {
            "event_key": event.event_key,
            "event_code": event.event_code,
            "direction": event.direction,
            "direction_basis": event.direction_basis,
            "lifecycle_state": event.lifecycle_state,
            "event_date": event.event_date.isoformat(),
            "correction_of_revision": event.correction_of_revision,
            "label": event.label,
        }
    if demographic:
        material["demographic"] = {
            "indicator_code": demographic.indicator_code,
            "period_start": demographic.period_start.isoformat(),
            "period_end": demographic.period_end.isoformat(),
            "value": demographic.value,
            "unit": demographic.unit,
            "methodology_code": demographic.methodology_code,
        }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return TerritoryObservation(
        provider_id=provider,
        source_record_id=record,
        source_revision=revision or 0,
        record_kind=kind,  # type: ignore[arg-type]
        authority_name=authority,
        source_url=source_url,
        source_published_at=published,  # type: ignore[arg-type]
        observed_at=observed,  # type: ignore[arg-type]
        territory_code=territory_code,
        geometry_geojson=geometry,
        geometry_sha256=geometry_hash,
        event=event,
        demographic=demographic,
        content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        linkage_eligible=geometry is not None,
    )


def _point_on_segment(
    longitude: float,
    latitude: float,
    start: Sequence[object],
    end: Sequence[object],
) -> bool:
    x1, y1, x2, y2 = float(start[0]), float(start[1]), float(end[0]), float(end[1])
    cross = (longitude - x1) * (y2 - y1) - (latitude - y1) * (x2 - x1)
    if abs(cross) > 1e-10:
        return False
    return min(x1, x2) - 1e-10 <= longitude <= max(x1, x2) + 1e-10 and min(
        y1, y2
    ) - 1e-10 <= latitude <= max(y1, y2) + 1e-10


def _ring_contains(ring: Sequence[object], longitude: float, latitude: float) -> bool:
    inside = False
    for index in range(len(ring) - 1):
        start, end = ring[index], ring[index + 1]
        if not isinstance(start, Sequence) or not isinstance(end, Sequence):
            return False
        if _point_on_segment(longitude, latitude, start, end):
            return True
        x1, y1, x2, y2 = float(start[0]), float(start[1]), float(end[0]), float(end[1])
        if (y1 > latitude) != (y2 > latitude):
            crossing = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < crossing:
                inside = not inside
    return inside


def _polygon_contains(
    rings: Sequence[object], longitude: float, latitude: float
) -> bool:
    if not rings or not isinstance(rings[0], Sequence):
        return False
    if not _ring_contains(rings[0], longitude, latitude):
        return False
    return not any(
        isinstance(hole, Sequence) and _ring_contains(hole, longitude, latitude)
        for hole in rings[1:]
    )


def assess_geographic_applicability(
    observation: TerritoryObservation,
    *,
    parcel_longitude: float,
    parcel_latitude: float,
    parcel_territory_code: str | None,
) -> GeographicApplicability:
    """Assess only official geometry/code scope; labels and prose are never inputs."""
    if (
        isinstance(parcel_longitude, bool)
        or isinstance(parcel_latitude, bool)
        or not isinstance(parcel_longitude, (int, float))
        or not isinstance(parcel_latitude, (int, float))
        or not math.isfinite(float(parcel_longitude))
        or not math.isfinite(float(parcel_latitude))
        or not KZ_LONGITUDE[0] <= float(parcel_longitude) <= KZ_LONGITUDE[1]
        or not KZ_LATITUDE[0] <= float(parcel_latitude) <= KZ_LATITUDE[1]
    ):
        raise TerritoryIntelligenceError("invalid_parcel_coordinates")
    geometry = observation.geometry_geojson
    if geometry and geometry.get("type") in {"Polygon", "MultiPolygon"}:
        coordinates = geometry.get("coordinates")
        polygons = coordinates if geometry["type"] == "MultiPolygon" else [coordinates]
        contains = isinstance(polygons, Sequence) and any(
            isinstance(polygon, Sequence)
            and _polygon_contains(polygon, float(parcel_longitude), float(parcel_latitude))
            for polygon in polygons
        )
        return GeographicApplicability(
            status="applicable" if contains else "not_applicable",
            scope="parcel",
            basis="polygon_contains_parcel" if contains else "polygon_excludes_parcel",
        )
    official_code = observation.territory_code
    if official_code and parcel_territory_code:
        if official_code != parcel_territory_code:
            return GeographicApplicability(
                status="not_applicable", scope="territory", basis="territory_code_mismatch"
            )
        return GeographicApplicability(
            status="manual_required", scope="territory", basis="territory_code_match"
        )
    return GeographicApplicability(
        status="manual_required", scope="unknown", basis="insufficient_official_scope"
    )


def assess_parcel_geographic_applicability(
    observation: TerritoryObservation,
    *,
    parcel_geojson: Mapping[str, object] | None,
    parcel_territory_code: str | None,
) -> GeographicApplicability:
    """Relate official scope to the whole validated parcel, never only its centroid.

    A partial overlap is deliberately ``manual_required``: it proves geographic
    relevance but not that the official event/project applies to the entire parcel.
    Point/line records also remain manual because no unversioned distance threshold is
    invented here. Territory-code equality is only a coarse candidate relation.
    """
    geometry = observation.geometry_geojson
    if geometry and geometry.get("type") in {"Polygon", "MultiPolygon"}:
        if parcel_geojson is None:
            return GeographicApplicability(
                "manual_required", "unknown", "insufficient_official_scope"
            )
        try:
            parcel = validate_parcel_geojson(dict(parcel_geojson))
            scope: BaseGeometry = shape(geometry)
        except (GeometryValidationError, TypeError, ValueError) as exc:
            raise TerritoryIntelligenceError("invalid_parcel_geometry") from exc
        if not scope.is_valid or scope.is_empty or scope.geom_type not in {
            "Polygon",
            "MultiPolygon",
        }:
            raise TerritoryIntelligenceError("invalid_geometry")
        if scope.covers(parcel):
            return GeographicApplicability(
                "applicable", "parcel", "scope_polygon_covers_parcel", 1.0
            )
        intersection = scope.intersection(parcel)
        if not intersection.is_empty and intersection.area > 0:
            overlap = max(0.0, min(1.0, float(intersection.area / parcel.area)))
            return GeographicApplicability(
                "manual_required",
                "parcel",
                "scope_polygon_intersects_parcel",
                overlap,
            )
        return GeographicApplicability(
            "not_applicable", "parcel", "scope_polygon_excludes_parcel", 0.0
        )
    if observation.territory_code and parcel_territory_code:
        if observation.territory_code != parcel_territory_code:
            return GeographicApplicability(
                "not_applicable", "territory", "territory_code_mismatch"
            )
        return GeographicApplicability(
            "manual_required", "territory", "territory_code_match"
        )
    return GeographicApplicability(
        "manual_required", "unknown", "insufficient_official_scope"
    )


def territory_identity_key(observation: TerritoryObservation) -> str:
    if observation.event is not None:
        material = (observation.provider_id, "event", observation.event.event_key)
    elif observation.demographic is not None and observation.territory_code:
        value = observation.demographic
        material = (
            observation.provider_id,
            "demographic",
            observation.territory_code,
            value.indicator_code,
            value.period_start.isoformat(),
            value.period_end.isoformat(),
        )
    else:
        raise TerritoryIntelligenceError("territory_identity_incomplete")
    canonical = json.dumps(material, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def transition_decision(
    current_state: str,
    next_state: str,
    *,
    current_revision: int,
    next_revision: int,
    correction_of_revision: int | None = None,
) -> TransitionDecision:
    if current_state not in LIFECYCLE_STATES or next_state not in LIFECYCLE_STATES:
        raise TerritoryIntelligenceError("invalid_lifecycle_state")
    if next_revision <= current_revision:
        return "stale"
    if current_state == next_state:
        return "same"
    if next_state in FORWARD_TRANSITIONS[current_state]:
        return "advance"
    if correction_of_revision == current_revision:
        return "correction"
    return "conflict"
