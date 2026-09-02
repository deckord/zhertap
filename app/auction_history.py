from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from sqlalchemy import Float, Integer, and_, case, cast, func, not_, or_, select
from sqlalchemy.orm import Session

from app.auction_history_read import normalized_similar_history
from app.auction_history_store import SqlAlchemyHistoryNormalizationStore
from app.auction_taxonomy import SCENARIO_KEYWORDS, classify_scenario
from app.config import settings
from app.models import AuctionLot
from app.shared_cache import shared_json_cache

_SUCCESS_STATUS_MARKERS = (
    "successprotocolsigned",
    "состоялся",
    "состоялись",
    "өтті",
)
_FAILURE_STATUS_MARKERS = (
    "failureprotocolsigned",
    "nullifyresultprotocolsigned",
    "не состоялся",
    "не состоялись",
    "результат торга отменен",
    "результат торга отменён",
    "өтпеді",
)
_LEASE_MARKERS = ("аренд", "землепольз", "временное пользование")
_OWNERSHIP_MARKERS = ("собствен", "продажа земельного участка")


@dataclass(frozen=True, slots=True)
class AuctionObjectIdentity:
    key: str | None
    kind: str
    confidence: str
    value: str | None


@dataclass(frozen=True, slots=True)
class AuctionAttempt:
    lot_id: str
    source: str
    source_lot_id: str
    auction_number: str | None
    status: str | None
    outcome: str
    start_price_kzt: float | None
    sale_price_kzt: float | None
    sale_to_start_ratio: float | None
    auction_starts_at: datetime | None
    published_at: date | None


@dataclass(frozen=True, slots=True)
class AuctionObjectHistory:
    identity: AuctionObjectIdentity
    attempts_count: int = 0
    completed_count: int = 0
    successful_count: int = 0
    failed_count: int = 0
    unresolved_count: int = 0
    first_start_price_kzt: float | None = None
    last_start_price_kzt: float | None = None
    start_price_change_kzt: float | None = None
    start_price_change_percent: float | None = None
    sales_with_ratio_count: int = 0
    average_sale_to_start_ratio: float | None = None
    distinct_years_count: int = 0
    first_event_year: int | None = None
    last_event_year: int | None = None
    start_price_trend: str = "insufficient_data"
    price_decrease_count: int = 0
    attempts: list[AuctionAttempt] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SimilarAuctionsAggregate:
    available: bool
    reason: str | None
    right_kind: str
    scenario: str
    territory_scope: str | None
    area_min_ha: float | None
    area_max_ha: float | None
    date_from: date | None = None
    date_to: date | None = None
    lease_term_min_years: float | None = None
    lease_term_max_years: float | None = None
    lots_count: int = 0
    completed_count: int = 0
    successful_count: int = 0
    failed_count: int = 0
    unresolved_count: int = 0
    start_price_observation_count: int = 0
    average_start_price_kzt: float | None = None
    min_start_price_kzt: float | None = None
    max_start_price_kzt: float | None = None
    average_start_price_per_sotka: float | None = None
    sale_price_observation_count: int = 0
    average_sale_price_kzt: float | None = None
    min_sale_price_kzt: float | None = None
    max_sale_price_kzt: float | None = None
    average_sale_price_per_sotka: float | None = None
    sales_with_ratio_count: int = 0
    average_sale_to_start_ratio: float | None = None
    median_sale_to_start_ratio: float | None = None
    normalized_generation: int | None = None
    normalized_status: str | None = None
    median_start_price_kzt: float | None = None
    median_sale_price_kzt: float | None = None
    median_start_price_per_sotka: float | None = None
    median_sale_price_per_sotka: float | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AuctionHistoryPayload:
    object_history: AuctionObjectHistory
    similar_auctions: SimilarAuctionsAggregate

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _clean_identifier(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    return cleaned or None


def _valid_cadastre(value: str | None) -> bool:
    if not value:
        return False
    return bool(
        re.fullmatch(
            r"\s*[0-9]{2}\s*[:-]\s*[0-9]{3}\s*[:-]\s*[0-9]{3}"
            r"\s*[:-]\s*[0-9]{1,6}\s*",
            value,
        )
    )


def _source_object_identity(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in {
        "jerler.e-qazyna.kz",
        "traderesources.e-qazyna.kz",
    }:
        return None
    path_match = re.search(r"/objects/list/([0-9]{1,32})/view/?$", parsed.path)
    if path_match:
        return f"jerler:{path_match.group(1)}"
    if parsed.path.rstrip("/").endswith("/source-object-view"):
        object_ids = parse_qs(parsed.query).get("id", [])
        if len(object_ids) == 1 and re.fullmatch(r"[0-9]{1,32}", object_ids[0]):
            return f"jerler:{object_ids[0]}"
    return None


def auction_object_identity(lot: AuctionLot) -> AuctionObjectIdentity:
    land_object_id = _clean_identifier(lot.land_object_id)
    if land_object_id:
        return AuctionObjectIdentity(
            key=f"land:{land_object_id}",
            kind="land_object_id",
            confidence="high",
            value=land_object_id,
        )
    source_object_id = _source_object_identity(lot.source_object_url)
    if source_object_id:
        return AuctionObjectIdentity(
            key=f"source:{source_object_id}",
            kind="source_object_url",
            confidence="high",
            value=source_object_id,
        )
    cadastre = _clean_identifier(lot.cadastre_number)
    if _valid_cadastre(cadastre):
        return AuctionObjectIdentity(
            key=f"cadastre:{cadastre}",
            kind="cadastre_number",
            confidence="medium",
            value=cadastre,
        )
    return AuctionObjectIdentity(key=None, kind="none", confidence="none", value=None)


def _status_text(lot: AuctionLot) -> str:
    return f"{lot.status or ''} {lot.source_search_status or ''}".casefold()


def _outcome(lot: AuctionLot) -> str:
    text = _status_text(lot)
    # An explicit failed/nullified result invalidates a protocol price as a completed sale.
    if any(marker in text for marker in _FAILURE_STATUS_MARKERS):
        return "failure"
    if lot.sale_price_kzt is not None:
        return "success"
    if any(marker in text for marker in _SUCCESS_STATUS_MARKERS):
        return "success"
    return "unresolved"


def _attempt_sort_key(lot: AuctionLot) -> tuple[datetime, str]:
    if lot.auction_starts_at is not None:
        moment = lot.auction_starts_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    elif lot.published_at is not None:
        moment = datetime.combine(lot.published_at, datetime.min.time(), tzinfo=UTC)
    else:
        moment = lot.first_seen_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    return moment, lot.id


def auction_object_history(session: Session, lot: AuctionLot) -> AuctionObjectHistory:
    identity = auction_object_identity(lot)
    if identity.kind == "none" or identity.value is None:
        return AuctionObjectHistory(identity=identity)

    if identity.kind == "land_object_id":
        identity_condition = AuctionLot.land_object_id == identity.value
    elif identity.kind == "source_object_url":
        identity_condition = AuctionLot.source_object_url == lot.source_object_url
    else:
        # Exact matching deliberately preserves use of ix_auction_lots_cadastre_number.
        # Fuzzy cadastral matching is unsafe and cannot be indexed portably.
        identity_condition = and_(
            AuctionLot.land_object_id.is_(None),
            AuctionLot.cadastre_number == identity.value,
        )
    lots = list(
        session.scalars(
            select(AuctionLot)
            .where(identity_condition)
            .order_by(
                AuctionLot.auction_starts_at.asc(),
                AuctionLot.published_at.asc(),
                AuctionLot.first_seen_at.asc(),
                AuctionLot.id.asc(),
            )
        )
    )
    lots.sort(key=_attempt_sort_key)

    attempts: list[AuctionAttempt] = []
    successes = failures = unresolved = 0
    ratios: list[float] = []
    start_prices: list[float] = []
    event_years: list[int] = []
    for attempt_lot in lots:
        outcome = _outcome(attempt_lot)
        successes += outcome == "success"
        failures += outcome == "failure"
        unresolved += outcome == "unresolved"
        start_price = (
            float(attempt_lot.start_price_kzt)
            if attempt_lot.start_price_kzt is not None
            else None
        )
        raw_sale_price = (
            float(attempt_lot.sale_price_kzt)
            if attempt_lot.sale_price_kzt is not None
            else None
        )
        # A protocol amount attached to a failed/nullified result is not a sale.
        sale_price = raw_sale_price if outcome == "success" else None
        ratio = (
            sale_price / start_price
            if sale_price is not None and start_price is not None and start_price > 0
            else None
        )
        if start_price is not None:
            start_prices.append(start_price)
        if ratio is not None:
            ratios.append(ratio)
        attempts.append(
            AuctionAttempt(
                lot_id=attempt_lot.id,
                source=attempt_lot.source,
                source_lot_id=attempt_lot.source_lot_id,
                auction_number=attempt_lot.auction_number,
                status=attempt_lot.status or attempt_lot.source_search_status,
                outcome=outcome,
                start_price_kzt=start_price,
                sale_price_kzt=sale_price,
                sale_to_start_ratio=ratio,
                auction_starts_at=attempt_lot.auction_starts_at,
                published_at=attempt_lot.published_at,
            )
        )
        event_years.append(_attempt_sort_key(attempt_lot)[0].year)

    first_price = start_prices[0] if start_prices else None
    last_price = start_prices[-1] if start_prices else None
    change = (
        last_price - first_price
        if first_price is not None and last_price is not None
        else None
    )
    change_percent = change / first_price * 100 if change is not None and first_price else None
    price_decrease_count = sum(
        current < previous
        for previous, current in zip(start_prices, start_prices[1:], strict=False)
    )
    if len(start_prices) < 2:
        price_trend = "insufficient_data"
    elif last_price is not None and first_price is not None and last_price < first_price:
        price_trend = "decreased"
    elif last_price is not None and first_price is not None and last_price > first_price:
        price_trend = "increased"
    else:
        price_trend = "unchanged"
    return AuctionObjectHistory(
        identity=identity,
        attempts_count=len(attempts),
        completed_count=successes + failures,
        successful_count=successes,
        failed_count=failures,
        unresolved_count=unresolved,
        first_start_price_kzt=first_price,
        last_start_price_kzt=last_price,
        start_price_change_kzt=change,
        start_price_change_percent=change_percent,
        sales_with_ratio_count=len(ratios),
        average_sale_to_start_ratio=(sum(ratios) / len(ratios) if ratios else None),
        distinct_years_count=len(set(event_years)),
        first_event_year=min(event_years) if event_years else None,
        last_event_year=max(event_years) if event_years else None,
        start_price_trend=price_trend,
        price_decrease_count=price_decrease_count,
        attempts=attempts,
    )


def _analysis_text(lot: AuctionLot) -> str:
    return " ".join(
        str(value or "")
        for value in (
            lot.land_rights,
            lot.auction_type,
            lot.title,
            lot.description,
            lot.purpose,
            lot.use_goal,
            lot.functional_purpose_level2,
            lot.functional_purpose_level3,
            lot.functional_purpose_level4,
        )
    ).casefold()


def auction_right_kind(lot: AuctionLot) -> str:
    text = _analysis_text(lot)
    if any(marker in text for marker in _LEASE_MARKERS):
        return "lease"
    if any(marker in text for marker in _OWNERSHIP_MARKERS):
        return "ownership"
    return "unknown"


def auction_scenario(lot: AuctionLot) -> str:
    scenario = classify_scenario(_analysis_text(lot))
    return "unknown" if scenario == "other" else scenario


def _combined_text_sql() -> object:
    columns = (
        AuctionLot.land_rights,
        AuctionLot.auction_type,
        AuctionLot.title,
        AuctionLot.description,
        AuctionLot.purpose,
        AuctionLot.use_goal,
        AuctionLot.functional_purpose_level2,
        AuctionLot.functional_purpose_level3,
        AuctionLot.functional_purpose_level4,
    )
    expression = func.coalesce(columns[0], "")
    for column in columns[1:]:
        expression = expression + " " + func.coalesce(column, "")
    return func.lower(expression)


def _right_condition(right_kind: str, text: object) -> object:
    lease = or_(*(text.like(f"%{marker}%") for marker in _LEASE_MARKERS))
    if right_kind == "lease":
        return lease
    ownership = or_(*(text.like(f"%{marker}%") for marker in _OWNERSHIP_MARKERS))
    return and_(ownership, not_(lease))


def _scenario_condition(scenario: str, text: object) -> object:
    return or_(*(text.like(f"%{keyword}%") for keyword in SCENARIO_KEYWORDS[scenario]))


def _sql_status_text() -> object:
    return func.lower(
        func.coalesce(AuctionLot.status, "")
        + " "
        + func.coalesce(AuctionLot.source_search_status, "")
    )


def similar_auctions_aggregate(
    session: Session,
    lot: AuctionLot,
    *,
    min_area_factor: float = 0.5,
    max_area_factor: float = 2.0,
    date_from: date | None = None,
    date_to: date | None = None,
    lookback_days: int | None = 365,
) -> SimilarAuctionsAggregate:
    right_kind = auction_right_kind(lot)
    scenario = auction_scenario(lot)
    territory_scope = (
        "locality" if lot.region and lot.district and lot.locality
        else "district" if lot.region and lot.district
        else "region" if lot.region
        else None
    )
    area = float(lot.area_ha) if lot.area_ha and lot.area_ha > 0 else None
    if date_to is None:
        date_to = datetime.now(UTC).date()
    if date_from is None and lookback_days is not None:
        date_from = date_to - timedelta(days=max(lookback_days, 1))
    lease_term = (
        float(lot.lease_term_years)
        if lot.lease_term_years is not None and lot.lease_term_years > 0
        else None
    )
    lease_min: float | None = None
    lease_max: float | None = None
    if right_kind == "lease" and lease_term is not None:
        if lease_term <= 3:
            lease_min, lease_max = 0, 3
        elif lease_term <= 10:
            lease_min, lease_max = 3, 10
        else:
            lease_min, lease_max = 10, None
    unavailable_reason = None
    if right_kind == "unknown":
        unavailable_reason = "unknown_land_right"
    elif scenario == "unknown":
        unavailable_reason = "unknown_use_scenario"
    elif territory_scope is None:
        unavailable_reason = "unknown_territory"
    elif area is None:
        unavailable_reason = "unknown_area"
    elif right_kind == "lease" and lease_term is None:
        unavailable_reason = "unknown_lease_term"
    if unavailable_reason:
        return SimilarAuctionsAggregate(
            available=False,
            reason=unavailable_reason,
            right_kind=right_kind,
            scenario=scenario,
            territory_scope=territory_scope,
            area_min_ha=None,
            area_max_ha=None,
            date_from=date_from,
            date_to=date_to,
        )

    area_min = area * min_area_factor
    area_max = area * max_area_factor
    combined_text = _combined_text_sql()
    status_text = _sql_status_text()
    failure_condition = or_(
        *(status_text.like(f"%{marker}%") for marker in _FAILURE_STATUS_MARKERS)
    )
    success_status_condition = and_(
        or_(*(status_text.like(f"%{marker}%") for marker in _SUCCESS_STATUS_MARKERS)),
        not_(failure_condition),
    )
    success_condition = or_(
        AuctionLot.sale_price_kzt.is_not(None), success_status_condition
    )
    failure_only_condition = and_(AuctionLot.sale_price_kzt.is_(None), failure_condition)
    start_per_sotka = AuctionLot.start_price_kzt / (AuctionLot.area_ha * 100.0)
    sale_per_sotka = AuctionLot.sale_price_kzt / (AuctionLot.area_ha * 100.0)
    sale_to_start = AuctionLot.sale_price_kzt / cast(AuctionLot.start_price_kzt, Float)

    conditions: list[object] = [
        AuctionLot.id != lot.id,
        AuctionLot.area_ha.between(area_min, area_max),
        _right_condition(right_kind, combined_text),
        _scenario_condition(scenario, combined_text),
    ]
    if lease_min is not None:
        conditions.append(AuctionLot.lease_term_years > lease_min)
    if lease_max is not None:
        conditions.append(AuctionLot.lease_term_years <= lease_max)
    if territory_scope == "locality":
        conditions.extend((
            AuctionLot.region == lot.region,
            AuctionLot.district == lot.district,
            AuctionLot.locality == lot.locality,
        ))
    elif territory_scope == "district":
        conditions.extend((AuctionLot.region == lot.region, AuctionLot.district == lot.district))
    else:
        conditions.append(AuctionLot.region == lot.region)
    if date_from is not None:
        from_moment = datetime.combine(date_from, datetime.min.time(), tzinfo=UTC)
        conditions.append(
            or_(
                AuctionLot.auction_starts_at >= from_moment,
                and_(
                    AuctionLot.auction_starts_at.is_(None),
                    AuctionLot.published_at >= date_from,
                ),
                and_(
                    AuctionLot.auction_starts_at.is_(None),
                    AuctionLot.published_at.is_(None),
                    AuctionLot.first_seen_at >= from_moment,
                ),
            )
        )
    if date_to is not None:
        through_moment = datetime.combine(
            date_to + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        )
        conditions.append(
            or_(
                AuctionLot.auction_starts_at < through_moment,
                and_(
                    AuctionLot.auction_starts_at.is_(None),
                    AuctionLot.published_at <= date_to,
                ),
                and_(
                    AuctionLot.auction_starts_at.is_(None),
                    AuctionLot.published_at.is_(None),
                    AuctionLot.first_seen_at < through_moment,
                ),
            )
        )

    identity = auction_object_identity(lot)
    if identity.kind == "land_object_id" and identity.value:
        conditions.append(
            or_(
                AuctionLot.land_object_id.is_(None),
                AuctionLot.land_object_id != identity.value,
            )
        )
    elif identity.kind == "source_object_url" and lot.source_object_url:
        conditions.append(
            or_(
                AuctionLot.source_object_url.is_(None),
                AuctionLot.source_object_url != lot.source_object_url,
            )
        )
    elif identity.kind == "cadastre_number" and identity.value:
        conditions.append(
            or_(
                AuctionLot.cadastre_number.is_(None),
                AuctionLot.cadastre_number != identity.value,
            )
        )

    ratio_available = and_(
        AuctionLot.sale_price_kzt.is_not(None),
        AuctionLot.start_price_kzt.is_not(None),
        AuctionLot.start_price_kzt > 0,
    )
    ratio_rows = (
        select(
            sale_to_start.label("ratio"),
            func.row_number().over(order_by=sale_to_start).label("row_number"),
            func.count().over().label("row_count"),
        )
        .where(*conditions, ratio_available)
        .cte("similar_sale_ratios")
    )
    lower_middle = cast((ratio_rows.c.row_count + 1) / 2, Integer)
    upper_middle = cast((ratio_rows.c.row_count + 2) / 2, Integer)
    median_ratio = (
        select(func.avg(ratio_rows.c.ratio))
        .where(
            or_(
                ratio_rows.c.row_number == lower_middle,
                ratio_rows.c.row_number == upper_middle,
            )
        )
        .scalar_subquery()
    )
    row = session.execute(
        select(
            func.count(AuctionLot.id),
            func.sum(case((success_condition, 1), else_=0)),
            func.sum(case((failure_only_condition, 1), else_=0)),
            func.count(AuctionLot.start_price_kzt),
            func.avg(AuctionLot.start_price_kzt),
            func.min(AuctionLot.start_price_kzt),
            func.max(AuctionLot.start_price_kzt),
            func.avg(case((AuctionLot.start_price_kzt.is_not(None), start_per_sotka))),
            func.count(AuctionLot.sale_price_kzt),
            func.avg(AuctionLot.sale_price_kzt),
            func.min(AuctionLot.sale_price_kzt),
            func.max(AuctionLot.sale_price_kzt),
            func.avg(case((AuctionLot.sale_price_kzt.is_not(None), sale_per_sotka))),
            func.sum(case((ratio_available, 1), else_=0)),
            func.avg(case((ratio_available, sale_to_start))),
            median_ratio,
        ).where(*conditions)
    ).one()
    lots_count = int(row[0] or 0)
    successful_count = int(row[1] or 0)
    failed_count = int(row[2] or 0)
    return SimilarAuctionsAggregate(
        available=True,
        reason=None if lots_count else "no_matches",
        right_kind=right_kind,
        scenario=scenario,
        territory_scope=territory_scope,
        area_min_ha=area_min,
        area_max_ha=area_max,
        date_from=date_from,
        date_to=date_to,
        lease_term_min_years=lease_min,
        lease_term_max_years=lease_max,
        lots_count=lots_count,
        completed_count=successful_count + failed_count,
        successful_count=successful_count,
        failed_count=failed_count,
        unresolved_count=max(lots_count - successful_count - failed_count, 0),
        start_price_observation_count=int(row[3] or 0),
        average_start_price_kzt=row[4],
        min_start_price_kzt=row[5],
        max_start_price_kzt=row[6],
        average_start_price_per_sotka=row[7],
        sale_price_observation_count=int(row[8] or 0),
        average_sale_price_kzt=row[9],
        min_sale_price_kzt=row[10],
        max_sale_price_kzt=row[11],
        average_sale_price_per_sotka=row[12],
        sales_with_ratio_count=int(row[13] or 0),
        average_sale_to_start_ratio=row[14],
        median_sale_to_start_ratio=row[15],
    )


def auction_history_payload(session: Session, lot: AuctionLot) -> AuctionHistoryPayload:
    """Build object attempts plus active-generation normalized history."""
    generation, normalized = normalized_similar_history(session, lot)
    return AuctionHistoryPayload(
        object_history=auction_object_history(session, lot),
        similar_auctions=SimilarAuctionsAggregate(
            available=normalized.status == "ok",
            reason=None if normalized.status == "ok" else "insufficient_data",
            right_kind=auction_right_kind(lot),
            scenario=auction_scenario(lot),
            territory_scope=(
                "locality"
                if lot.locality
                else "district"
                if lot.district
                else "region"
                if lot.region
                else None
            ),
            area_min_ha=float(lot.area_ha) * 0.67 if lot.area_ha else None,
            area_max_ha=float(lot.area_ha) * 1.5 if lot.area_ha else None,
            lots_count=normalized.matched_count,
            completed_count=normalized.successful_count + normalized.failed_count,
            successful_count=normalized.successful_count,
            failed_count=normalized.failed_count,
            unresolved_count=normalized.unresolved_count + normalized.conflict_count,
            sales_with_ratio_count=normalized.successful_count,
            median_sale_to_start_ratio=normalized.median_sale_to_start_ratio,
            normalized_generation=generation,
            normalized_status=normalized.status,
            median_start_price_kzt=normalized.median_start_price_kzt,
            median_sale_price_kzt=normalized.median_sale_price_kzt,
            median_start_price_per_sotka=(
                normalized.median_start_price_per_ha_kzt / 100
                if normalized.median_start_price_per_ha_kzt is not None
                else None
            ),
            median_sale_price_per_sotka=(
                normalized.median_sale_price_per_ha_kzt / 100
                if normalized.median_sale_price_per_ha_kzt is not None
                else None
            ),
        ),
    )


def cached_auction_history_payload(
    session: Session,
    lot: AuctionLot,
    *,
    ttl_seconds: int | None = None,
) -> dict[str, object]:
    """Return an API-safe payload, shared across web workers when Redis is enabled."""
    generation = SqlAlchemyHistoryNormalizationStore(session).get_active_generation()
    namespace = "auction-history-v2"
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "lot_id": lot.id,
                "updated_at": lot.updated_at.isoformat() if lot.updated_at else None,
                "normalized_generation": generation.generation if generation else None,
                "version": 3,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    build_lock_token: str | bool | None = None
    if settings.auction_cache_enabled:
        cached = shared_json_cache.get(namespace, cache_key)
        if isinstance(cached, dict):
            return cached
        build_lock_token = shared_json_cache.acquire_build_lock(
            namespace,
            cache_key,
            ttl_seconds=15,
        )
        if build_lock_token is False:
            waited = shared_json_cache.wait_for_value(
                namespace,
                cache_key,
                timeout_seconds=0.5,
            )
            if isinstance(waited, dict):
                return waited
            # Preserve exact object attempts, but avoid an aggregate-query
            # stampede while another web worker builds the shared value.
            payload = AuctionHistoryPayload(
                object_history=auction_object_history(session, lot),
                similar_auctions=SimilarAuctionsAggregate(
                    available=False,
                    reason="insufficient_data",
                    right_kind=auction_right_kind(lot),
                    scenario=auction_scenario(lot),
                    territory_scope=None,
                    area_min_ha=None,
                    area_max_ha=None,
                    normalized_generation=generation.generation if generation else None,
                    normalized_status="cache_building",
                ),
            )
            return json.loads(json.dumps(payload.as_dict(), default=str))
    try:
        # Normalize datetimes on both cache misses and hits so the API type is stable.
        payload = json.loads(
            json.dumps(auction_history_payload(session, lot).as_dict(), default=str)
        )
        if settings.auction_cache_enabled:
            shared_json_cache.set(
                namespace,
                cache_key,
                payload,
                ttl_seconds=ttl_seconds or settings.auction_cache_ttl_seconds,
            )
        return payload
    finally:
        if isinstance(build_lock_token, str):
            shared_json_cache.release_build_lock(namespace, cache_key, build_lock_token)
