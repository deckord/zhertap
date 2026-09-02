from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.auction_decision_snapshot import DECISION_ENGINE_VERSION
from app.auction_verdict import RULES_VERSION as VERDICT_RULES_VERSION
from app.models import (
    AuctionDecisionSnapshot,
    AuctionEvidence,
    AuctionLot,
    AuctionLotGeoCheck,
    AuctionLotV2Analysis,
)

MAX_MAP_MARKERS = 500
MAX_FILTER_IDS = 500
UNKNOWN = "unknown"
_ARCHIVED_STATUSES = (
    "SuccessProtocolSigned",
    "FailureProtocolSigned",
    "NullifyResultProtocolSigned",
    "CancelBeforeStart",
)

MapScope = Literal["active", "future", "archive", "all"]


@dataclass(frozen=True, slots=True)
class MapBoundingBox:
    south: float
    west: float
    north: float
    east: float


@dataclass(frozen=True, slots=True)
class AuctionMapProjectionFilter:
    """Index-friendly persisted-field filters supported by the cold map path.

    ``active`` means non-archive DB rows and includes the ``future`` subset;
    ``future`` narrows that set to a known start at/after ``now``. Geo names
    and purpose are exact matches. Full-text search, scenario/right
    inference, account pipeline stage and computed deadline filters intentionally
    remain outside this projection and must be applied before passing ``lot_ids``.
    Score/action are legacy persisted V2-analysis fields, not a final investment
    decision; a decision-snapshot projection can replace them without changing
    this bounded query contract.
    """

    lot_ids: tuple[str, ...] | None = None
    region: str | None = None
    district: str | None = None
    locality: str | None = None
    functional_purpose: str | None = None
    lot_scope: MapScope = "active"
    eqazyna_status: str | None = None
    min_price_kzt: float | None = None
    max_price_kzt: float | None = None
    min_area_ha: float | None = None
    max_area_ha: float | None = None
    min_score: int | None = None
    risk_level: str | None = None
    confidence_level: str | None = None
    recommended_action: str | None = None
    coordinate_status: str | None = None
    bbox: MapBoundingBox | None = None
    limit: int = 300


@dataclass(frozen=True, slots=True)
class AuctionMapProjectionItem:
    id: str
    title: str
    region: str
    district: str
    locality: str
    cadastre: str
    latitude: float | None
    longitude: float | None
    score: int | None
    risk: str
    confidence: str
    recommended_action: str
    analysis_status: str
    coordinate_status: str
    geo_status: str
    start_price_kzt: float | None
    area_ha: float | None
    price_per_sotka: float | None
    auction_starts_at: datetime | None
    scope: str
    source_url: str
    egkn_url: str
    google_maps_url: str
    evidence_status: str
    evidence_count: int
    evidence_conflict_count: int
    evidence_observed_at: datetime | None
    investment_verdict: str = "requires_check"
    data_readiness: str = "insufficient"
    scenario_key: str = "unknown"
    bid_ceiling_kzt: int | None = None
    repeat_attempt_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def as_marker(self) -> dict[str, object] | None:
        if self.latitude is None or self.longitude is None:
            return None
        payload = self.as_dict()
        payload["url"] = f"/cabinet/auctions-v2/{self.id}"
        return payload


@dataclass(frozen=True, slots=True)
class AuctionMapProjectionPayload:
    items: tuple[AuctionMapProjectionItem, ...]
    total: int
    loaded: int
    mapped: int
    without_coordinates: int
    limit: int
    query_contract: str = "persisted_projection.v1"

    def as_dict(self) -> dict[str, object]:
        return {
            "items": [item.as_dict() for item in self.items],
            "markers": [marker for item in self.items if (marker := item.as_marker())],
            "total": self.total,
            "loaded": self.loaded,
            "mapped": self.mapped,
            "without_coordinates": self.without_coordinates,
            "limit": self.limit,
            "query_contract": self.query_contract,
        }


def _finite(value: object, *, low: float, high: float) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if math.isfinite(result) and low <= result <= high else None


def _bounded_text(value: object, *, length: int = 160) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > length:
        raise ValueError("invalid_filter_text")
    return value.strip()


def _validated_filter(spec: AuctionMapProjectionFilter) -> AuctionMapProjectionFilter:
    if spec.lot_scope not in {"active", "future", "archive", "all"}:
        raise ValueError("invalid_lot_scope")
    if not isinstance(spec.limit, int) or isinstance(spec.limit, bool):
        raise ValueError("invalid_limit")
    if not 1 <= spec.limit <= MAX_MAP_MARKERS:
        raise ValueError("limit_out_of_bounds")
    lot_ids = spec.lot_ids
    if lot_ids is not None:
        if len(lot_ids) > MAX_FILTER_IDS:
            raise ValueError("lot_id_limit_exceeded")
        if any(
            not isinstance(lot_id, str)
            or not lot_id.strip()
            or len(lot_id) > 36
            for lot_id in lot_ids
        ):
            raise ValueError("invalid_lot_id")
        lot_ids = tuple(dict.fromkeys(lot_id.strip() for lot_id in lot_ids))
    numeric_fields = (
        ("min_price_kzt", spec.min_price_kzt, 0, 1e18),
        ("max_price_kzt", spec.max_price_kzt, 0, 1e18),
        ("min_area_ha", spec.min_area_ha, 0, 1e7),
        ("max_area_ha", spec.max_area_ha, 0, 1e7),
    )
    for name, value, low, high in numeric_fields:
        if value is not None and _finite(value, low=low, high=high) is None:
            raise ValueError(f"invalid_{name}")
    if (
        spec.min_price_kzt is not None
        and spec.max_price_kzt is not None
        and spec.min_price_kzt > spec.max_price_kzt
    ) or (
        spec.min_area_ha is not None
        and spec.max_area_ha is not None
        and spec.min_area_ha > spec.max_area_ha
    ):
        raise ValueError("invalid_range")
    if spec.min_score is not None and (
        not isinstance(spec.min_score, int)
        or isinstance(spec.min_score, bool)
        or not 0 <= spec.min_score <= 100
    ):
        raise ValueError("invalid_min_score")
    bbox = spec.bbox
    if bbox is not None:
        values = (
            _finite(bbox.south, low=40, high=56),
            _finite(bbox.west, low=45, high=90),
            _finite(bbox.north, low=40, high=56),
            _finite(bbox.east, low=45, high=90),
        )
        if any(value is None for value in values) or not (
            bbox.south < bbox.north and bbox.west < bbox.east
        ):
            raise ValueError("invalid_kazakhstan_bbox")
    return AuctionMapProjectionFilter(
        lot_ids=lot_ids,
        region=_bounded_text(spec.region),
        district=_bounded_text(spec.district),
        locality=_bounded_text(spec.locality),
        functional_purpose=_bounded_text(spec.functional_purpose, length=240),
        lot_scope=spec.lot_scope,
        eqazyna_status=_bounded_text(spec.eqazyna_status, length=64),
        min_price_kzt=spec.min_price_kzt,
        max_price_kzt=spec.max_price_kzt,
        min_area_ha=spec.min_area_ha,
        max_area_ha=spec.max_area_ha,
        min_score=spec.min_score,
        risk_level=_bounded_text(spec.risk_level, length=16),
        confidence_level=_bounded_text(spec.confidence_level, length=16),
        recommended_action=_bounded_text(spec.recommended_action, length=64),
        coordinate_status=_bounded_text(spec.coordinate_status, length=32),
        bbox=bbox,
        limit=spec.limit,
    )


def _scope_conditions(scope: MapScope, now: datetime) -> list[object]:
    if scope == "all":
        return []
    if scope == "archive":
        return [
            or_(
                AuctionLot.active.is_(False),
                AuctionLot.auction_starts_at < now,
                AuctionLot.source_search_status.in_(_ARCHIVED_STATUSES),
            )
        ]
    if scope == "future":
        return [
            AuctionLot.active.is_(True),
            AuctionLot.auction_starts_at >= now,
            or_(
                AuctionLot.source_search_status.is_(None),
                AuctionLot.source_search_status.not_in(_ARCHIVED_STATUSES),
            ),
        ]
    return [
        AuctionLot.active.is_(True),
        or_(AuctionLot.auction_starts_at.is_(None), AuctionLot.auction_starts_at >= now),
        or_(
            AuctionLot.source_search_status.is_(None),
            AuctionLot.source_search_status.not_in(_ARCHIVED_STATUSES),
        ),
    ]


def _marker_scope(
    active: bool,
    starts_at: datetime | None,
    source_status: str | None,
    now: datetime,
) -> str:
    if starts_at is not None and starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    if (
        not active
        or (starts_at is not None and starts_at < now)
        or source_status in _ARCHIVED_STATUSES
    ):
        return "archive"
    if starts_at is not None and starts_at >= now:
        return "future"
    return "active"


def _conditions(spec: AuctionMapProjectionFilter, now: datetime) -> list[object]:
    conditions: list[object] = [AuctionLot.object_type == "land"]
    conditions.extend(_scope_conditions(spec.lot_scope, now))
    if spec.lot_ids is not None:
        conditions.append(AuctionLot.id.in_(spec.lot_ids) if spec.lot_ids else False)
    for column, value in (
        (AuctionLot.region, spec.region),
        (AuctionLot.district, spec.district),
        (AuctionLot.locality, spec.locality),
        (AuctionLot.functional_purpose_level2, spec.functional_purpose),
        (AuctionLot.source_search_status, spec.eqazyna_status),
        (AuctionLotV2Analysis.risk_level, spec.risk_level),
        (AuctionLotV2Analysis.confidence_level, spec.confidence_level),
        (AuctionLotV2Analysis.recommended_action, spec.recommended_action),
        (AuctionLotGeoCheck.coordinate_status, spec.coordinate_status),
    ):
        if value is not None:
            conditions.append(column == value)
    for column, minimum, maximum in (
        (AuctionLot.start_price_kzt, spec.min_price_kzt, spec.max_price_kzt),
        (AuctionLot.area_ha, spec.min_area_ha, spec.max_area_ha),
    ):
        if minimum is not None:
            conditions.append(column >= minimum)
        if maximum is not None:
            conditions.append(column <= maximum)
    if spec.min_score is not None:
        conditions.append(AuctionLotV2Analysis.score >= spec.min_score)
    if spec.bbox is not None:
        conditions.extend(
            (
                AuctionLotGeoCheck.latitude >= spec.bbox.south,
                AuctionLotGeoCheck.latitude <= spec.bbox.north,
                AuctionLotGeoCheck.longitude >= spec.bbox.west,
                AuctionLotGeoCheck.longitude <= spec.bbox.east,
            )
        )
    return conditions


def load_auction_map_projection(
    session: Session,
    filters: AuctionMapProjectionFilter,
    *,
    now: datetime | None = None,
) -> AuctionMapProjectionPayload:
    """Load the cold map in two SQL queries regardless of marker count."""
    spec = _validated_filter(filters)
    observed_now = now or datetime.now(UTC)
    if observed_now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    statement = (
        select(
            AuctionLot.id,
            func.substr(AuctionLot.title, 1, 240).label("title"),
            AuctionLot.region,
            AuctionLot.district,
            AuctionLot.locality,
            AuctionLot.cadastre_number,
            AuctionLot.active,
            AuctionLot.source_search_status,
            AuctionLot.start_price_kzt,
            AuctionLot.area_ha,
            AuctionLot.auction_starts_at,
            func.substr(AuctionLot.source_url, 1, 1000).label("source_url"),
            AuctionLotV2Analysis.score,
            AuctionLotV2Analysis.risk_level,
            AuctionLotV2Analysis.confidence_level,
            AuctionLotV2Analysis.recommended_action,
            AuctionLotV2Analysis.price_per_sotka,
            AuctionLotGeoCheck.latitude,
            AuctionLotGeoCheck.longitude,
            AuctionLotGeoCheck.coordinate_status,
            AuctionLotGeoCheck.cadastre_status,
            func.substr(AuctionLotGeoCheck.egkn_url, 1, 1000).label("egkn_url"),
            func.substr(AuctionLotGeoCheck.google_maps_url, 1, 1000).label(
                "google_maps_url"
            ),
            AuctionDecisionSnapshot.verdict.label("decision_verdict"),
            AuctionDecisionSnapshot.data_readiness.label("decision_readiness"),
            AuctionDecisionSnapshot.scenario_key.label("decision_scenario"),
            AuctionDecisionSnapshot.bid_ceiling_kzt,
            AuctionDecisionSnapshot.repeat_attempt_count,
            func.count().over().label("total_count"),
        )
        .select_from(AuctionLot)
        .outerjoin(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id)
        .outerjoin(AuctionLotGeoCheck, AuctionLotGeoCheck.lot_id == AuctionLot.id)
        .outerjoin(
            AuctionDecisionSnapshot,
            and_(
                AuctionDecisionSnapshot.lot_id == AuctionLot.id,
                AuctionDecisionSnapshot.engine_version == DECISION_ENGINE_VERSION,
                AuctionDecisionSnapshot.rules_version == VERDICT_RULES_VERSION,
                AuctionDecisionSnapshot.is_current.is_(True),
            ),
        )
        .where(and_(*_conditions(spec, observed_now)))
        .order_by(
            AuctionLotV2Analysis.score.is_(None),
            AuctionLotV2Analysis.score.desc(),
            AuctionLot.auction_starts_at.is_(None),
            AuctionLot.auction_starts_at,
            AuctionLot.id,
        )
        .limit(spec.limit)
    )
    rows = session.execute(statement).all()
    lot_ids = [row.id for row in rows]
    evidence_by_lot: dict[str, tuple[int, int, int, datetime | None]] = {}
    if lot_ids:
        evidence_rows = session.execute(
            select(
                AuctionEvidence.lot_id,
                func.count(AuctionEvidence.id),
                func.sum(case((AuctionEvidence.status == "found", 1), else_=0)),
                func.sum(case((AuctionEvidence.status == "conflict", 1), else_=0)),
                func.max(AuctionEvidence.observed_at),
            )
            .where(AuctionEvidence.lot_id.in_(lot_ids))
            .group_by(AuctionEvidence.lot_id)
        ).all()
        evidence_by_lot = {
            row[0]: (
                int(row[1] or 0),
                int(row[2] or 0),
                int(row[3] or 0),
                row[4],
            )
            for row in evidence_rows
        }

    items: list[AuctionMapProjectionItem] = []
    for row in rows:
        evidence_count, found_count, conflict_count, evidence_at = evidence_by_lot.get(
            row.id, (0, 0, 0, None)
        )
        score_number = _finite(row.score, low=0, high=100)
        has_analysis = row.score is not None
        has_geo = row.coordinate_status is not None
        latitude = _finite(row.latitude, low=40, high=56)
        longitude = _finite(row.longitude, low=45, high=90)
        invalid_coordinates = (
            (row.latitude is not None and latitude is None)
            or (row.longitude is not None and longitude is None)
        )
        items.append(
            AuctionMapProjectionItem(
                id=row.id,
                title=row.title or "Лот без названия",
                region=row.region or "",
                district=row.district or "",
                locality=row.locality or "",
                cadastre=row.cadastre_number or "",
                latitude=round(latitude, 7) if latitude is not None else None,
                longitude=round(longitude, 7) if longitude is not None else None,
                score=int(score_number) if score_number is not None else None,
                risk=row.risk_level or UNKNOWN,
                confidence=row.confidence_level or UNKNOWN,
                recommended_action=row.recommended_action or "inspect",
                analysis_status=(
                    "persisted" if has_analysis and score_number is not None else UNKNOWN
                ),
                coordinate_status=(
                    "invalid" if invalid_coordinates else row.coordinate_status or UNKNOWN
                ),
                geo_status=(
                    "invalid" if invalid_coordinates else "persisted" if has_geo else UNKNOWN
                ),
                start_price_kzt=_finite(row.start_price_kzt, low=0, high=1e18),
                area_ha=_finite(row.area_ha, low=0, high=1e7),
                price_per_sotka=_finite(row.price_per_sotka, low=0, high=1e18),
                auction_starts_at=row.auction_starts_at,
                scope=_marker_scope(
                    bool(row.active),
                    row.auction_starts_at,
                    row.source_search_status,
                    observed_now,
                ),
                source_url=row.source_url or "",
                egkn_url=row.egkn_url or "",
                google_maps_url=row.google_maps_url or "",
                evidence_status=(
                    "conflict" if conflict_count else "found" if found_count else UNKNOWN
                ),
                evidence_count=evidence_count,
                evidence_conflict_count=conflict_count,
                evidence_observed_at=evidence_at,
                investment_verdict=row.decision_verdict or "requires_check",
                data_readiness=row.decision_readiness or "insufficient",
                scenario_key=row.decision_scenario or "unknown",
                bid_ceiling_kzt=(
                    int(row.bid_ceiling_kzt)
                    if isinstance(row.bid_ceiling_kzt, int)
                    and not isinstance(row.bid_ceiling_kzt, bool)
                    and 0 <= row.bid_ceiling_kzt <= 10**15
                    else None
                ),
                repeat_attempt_count=(
                    int(row.repeat_attempt_count)
                    if isinstance(row.repeat_attempt_count, int)
                    and not isinstance(row.repeat_attempt_count, bool)
                    and 0 <= row.repeat_attempt_count <= 10_000
                    else 0
                ),
            )
        )
    mapped = sum(item.latitude is not None and item.longitude is not None for item in items)
    return AuctionMapProjectionPayload(
        items=tuple(items),
        total=int(rows[0].total_count if rows else 0),
        loaded=len(items),
        mapped=int(mapped),
        without_coordinates=len(items) - int(mapped),
        limit=spec.limit,
    )
