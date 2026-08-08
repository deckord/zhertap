from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auction_access import has_auction_paid_access
from app.auction_documents import (
    auction_document_key,
    deduplicate_lot_documents,
    unique_auction_documents,
)
from app.auction_geo import AuctionGeoMetrics, auction_geo_metrics
from app.i18n import normalize_language
from app.models import (
    AuctionAccess,
    AuctionDocument,
    AuctionFavorite,
    AuctionLot,
    AuctionLotChange,
    AuctionLotHistory,
    AuctionNotification,
    AuctionSubscription,
)
from app.providers.eqazyna import AuctionLotData, EqazynaProvider

logger = logging.getLogger(__name__)
ACTIVE_AUCTION_STATUSES = {
    "Прием заявок",
    "Ожидание начала",
    "Проводится",
}
ACTIVE_EQAZYNA_SEARCH_STATUSES = {
    "ApplicationsAccept",
    "Pending",
    "Running",
}


@dataclass(slots=True)
class AuctionFilters:
    region: str | None = None
    district: str | None = None
    locality: str | None = None
    purpose_query: str | None = None
    min_price_kzt: float | None = None
    max_price_kzt: float | None = None
    min_area_ha: float | None = None
    max_area_ha: float | None = None
    active_only: bool = True


@dataclass(slots=True)
class AuctionSyncResult:
    fetched: int
    created: int
    updated: int
    notifications_sent: int
    errors: int
    detail_errors: int = 0
    deactivated: int = 0
    crawl_complete: bool = False
    url_count: int = 0
    pages_scanned: int = 0
    status_counts: dict[str, int] | None = None


@dataclass(slots=True)
class AuctionLotMetrics:
    price_per_sotka: float | None
    price_per_square_meter: float | None
    district_average_price_per_sotka: float | None
    district_difference_percent: float | None
    publication_count: int
    failed_count: int
    document_count: int
    district_lot_count: int
    district_successful_count: int
    district_failed_count: int
    district_liquidity_percent: float | None
    rating: int


CHANGE_TRACKED_FIELDS = (
    "status",
    "source_search_status",
    "start_price_kzt",
    "sale_price_kzt",
    "auction_starts_at",
    "area_ha",
    "land_rights",
    "purpose",
)


def _history_changed(lot: AuctionLot, data: AuctionLotData) -> bool:
    return any(
        (
            lot.status != data.status,
            lot.source_search_status != data.source_search_status,
            lot.start_price_kzt != data.start_price_kzt,
            lot.sale_price_kzt != data.sale_price_kzt,
            lot.auction_starts_at != data.auction_starts_at,
        )
    )


def _stringify_change_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _changed_fields(lot: AuctionLot, data: AuctionLotData) -> list[AuctionLotChange]:
    changes: list[AuctionLotChange] = []
    for name in CHANGE_TRACKED_FIELDS:
        old_value = getattr(lot, name)
        new_value = getattr(data, name)
        if old_value == new_value:
            continue
        changes.append(
            AuctionLotChange(
                field_name=name,
                old_value=_stringify_change_value(old_value),
                new_value=_stringify_change_value(new_value),
            )
        )
    return changes


def _upsert_documents(lot: AuctionLot, data: AuctionLotData) -> list[AuctionLotChange]:
    removed_urls = deduplicate_lot_documents(lot)
    existing_by_key = {
        auction_document_key(document): document
        for document in lot.documents
        if auction_document_key(document)
    }
    existing_urls = {document.source_url for document in lot.documents}
    changes: list[AuctionLotChange] = []
    for document in data.documents:
        key = auction_document_key(document.source_url, document.title)
        existing_document = existing_by_key.get(key)
        if existing_document is not None:
            existing_document.title = document.title[:320]
            existing_document.file_type = document.file_type or existing_document.file_type
            if (
                document.source_url
                and document.source_url != existing_document.source_url
                and document.source_url not in existing_urls
                and document.source_url not in removed_urls
            ):
                existing_document.source_url = document.source_url
                existing_urls.add(document.source_url)
            continue
        new_document = AuctionDocument(
            title=document.title[:320],
            source_url=document.source_url,
            file_type=document.file_type,
            storage_status="linked",
        )
        lot.documents.append(new_document)
        if key:
            existing_by_key[key] = new_document
        existing_urls.add(document.source_url)
        changes.append(
            AuctionLotChange(
                field_name="documents",
                old_value=None,
                new_value=document.source_url or document.title,
            )
        )
    return changes


def upsert_auction_lot(session: Session, data: AuctionLotData) -> tuple[AuctionLot, bool, bool]:
    lot = session.scalar(
        select(AuctionLot)
        .options(selectinload(AuctionLot.documents))
        .where(
            AuctionLot.source == "e-qazyna",
            AuctionLot.source_lot_id == data.source_lot_id,
        )
    )
    created = lot is None
    changed = False
    changes: list[AuctionLotChange] = []
    if lot is None:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id=data.source_lot_id,
            title=data.title,
            source_url=data.source_url,
        )
        session.add(lot)
        session.flush()
    else:
        changed = _history_changed(lot, data)
        changes = _changed_fields(lot, data)

    for name in (
        "auction_number",
        "source_search_status",
        "auction_type",
        "status",
        "title",
        "description",
        "region",
        "district",
        "locality",
        "location_text",
        "cadastre_number",
        "area_ha",
        "land_rights",
        "functional_purpose_level2",
        "functional_purpose_level3",
        "functional_purpose_level4",
        "use_goal",
        "purpose",
        "start_price_kzt",
        "guarantee_kzt",
        "sale_price_kzt",
        "auction_starts_at",
        "published_at",
        "seller_name",
        "seller_bin",
        "source_url",
        "source_object_url",
    ):
        setattr(lot, name, getattr(data, name))
    lot.raw_payload_json = json.dumps(data.as_dict(), ensure_ascii=False, default=str)
    lot.last_seen_at = datetime.now(UTC)
    lot.active = (
        data.status in ACTIVE_AUCTION_STATUSES
        if data.status
        else data.source_search_status in ACTIVE_EQAZYNA_SEARCH_STATUSES
    )
    document_changes = _upsert_documents(lot, data)
    if not created and document_changes:
        changed = True
        changes.extend(document_changes)
    if created or changed:
        lot.history.append(
            AuctionLotHistory(
                status=data.status,
                start_price_kzt=data.start_price_kzt,
                sale_price_kzt=data.sale_price_kzt,
                auction_starts_at=data.auction_starts_at,
            )
        )
    for change in changes:
        lot.changes.append(change)
    session.flush()
    return lot, created, changed


def deactivate_missing_current_auction_lots(
    session: Session,
    source_lot_ids: set[str],
) -> int:
    if not source_lot_ids:
        return 0
    stale_lots = list(
        session.scalars(
            select(AuctionLot).where(
                AuctionLot.source == "e-qazyna",
                AuctionLot.active.is_(True),
                AuctionLot.source_lot_id.not_in(source_lot_ids),
            )
        ).all()
    )
    now = datetime.now(UTC)
    for lot in stale_lots:
        lot.active = False
        lot.last_seen_at = now
        lot.history.append(
            AuctionLotHistory(
                status=lot.status,
                start_price_kzt=lot.start_price_kzt,
                sale_price_kzt=lot.sale_price_kzt,
                auction_starts_at=lot.auction_starts_at,
            )
        )
    session.flush()
    return len(stale_lots)


def _subscription_matches(lot: AuctionLot, subscription: AuctionSubscription) -> bool:
    if subscription.region and subscription.region != lot.region:
        return False
    if subscription.district and subscription.district != lot.district:
        return False
    if subscription.locality and subscription.locality != lot.locality:
        return False
    if subscription.purpose_query:
        if subscription.purpose_query != lot.functional_purpose_level2:
            return False
    if subscription.min_price_kzt is not None:
        if lot.start_price_kzt is None or lot.start_price_kzt < subscription.min_price_kzt:
            return False
    if subscription.max_price_kzt is not None:
        if lot.start_price_kzt is None or lot.start_price_kzt > subscription.max_price_kzt:
            return False
    if subscription.min_area_ha is not None:
        if lot.area_ha is None or lot.area_ha < subscription.min_area_ha:
            return False
    if subscription.max_area_ha is not None:
        if lot.area_ha is None or lot.area_ha > subscription.max_area_ha:
            return False
    return True


def _format_money(value: float | None) -> str:
    if value is None:
        return "не указана"
    return f"{value:,.0f}".replace(",", " ") + " ₸"


def _price_per_sotka(lot: AuctionLot) -> float | None:
    if lot.start_price_kzt is None or not lot.area_ha or lot.area_ha <= 0:
        return None
    sotka = lot.area_ha * 100
    return lot.start_price_kzt / sotka


def _price_per_square_meter(lot: AuctionLot) -> float | None:
    if lot.start_price_kzt is None or not lot.area_ha or lot.area_ha <= 0:
        return None
    return lot.start_price_kzt / (lot.area_ha * 10_000)


def _format_percent(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.0f}%"


def format_auction_metrics(metrics: AuctionLotMetrics, language: str = "ru") -> str:
    language = normalize_language(language)
    if language == "kz":
        return "\n".join(
            [
                "",
                "📊 <b>Қысқа аналитика</b>",
                f"⭐ Рейтинг: {metrics.rating}/100",
                f"💠 Сотық бағасы: {_format_money(metrics.price_per_sotka)}",
                (
                    "🏘 Аудан бойынша орташа: "
                    f"{_format_money(metrics.district_average_price_per_sotka)}"
                ),
                f"📉 Ауданнан айырмашылығы: {_format_percent(metrics.district_difference_percent)}",
                f"🔁 Жарияланымдар саны: {metrics.publication_count}",
                f"📄 Құжаттар: {metrics.document_count}",
                f"🏘 Аудандағы лоттар: {metrics.district_lot_count}",
                f"✅ Сәтті саудалар: {metrics.district_successful_count}",
                f"⚠️ Өтпеген саудалар: {metrics.district_failed_count}",
                f"📈 Аудан өтімділігі: {_format_percent(metrics.district_liquidity_percent)}",
            ]
        )
    return "\n".join(
        [
            "",
            "📊 <b>Короткая аналитика</b>",
            f"⭐ Рейтинг: {metrics.rating}/100",
            f"💠 Цена за сотку: {_format_money(metrics.price_per_sotka)}",
            f"🏘 Средняя по району: {_format_money(metrics.district_average_price_per_sotka)}",
            f"📉 Отличие от района: {_format_percent(metrics.district_difference_percent)}",
            f"🔁 Публикаций участка: {metrics.publication_count}",
            f"📄 Документов: {metrics.document_count}",
            f"🏘 Лотов в этом районе: {metrics.district_lot_count}",
            f"✅ Успешных торгов: {metrics.district_successful_count}",
            f"⚠️ Несостоявшихся: {metrics.district_failed_count}",
            f"📈 Ликвидность района: {_format_percent(metrics.district_liquidity_percent)}",
        ]
    )


def district_average_price_per_sotka(session: Session, lot: AuctionLot) -> float | None:
    if not lot.region or not lot.district:
        return None
    rows = session.execute(
        select(AuctionLot.start_price_kzt, AuctionLot.area_ha).where(
            AuctionLot.region == lot.region,
            AuctionLot.district == lot.district,
            AuctionLot.start_price_kzt.is_not(None),
            AuctionLot.area_ha.is_not(None),
            AuctionLot.area_ha > 0,
        )
    ).all()
    values = [price / (area * 100) for price, area in rows if price is not None and area]
    if not values:
        return None
    return sum(values) / len(values)


def _auction_status_text(lot: AuctionLot) -> str:
    return f"{lot.status or ''} {lot.source_search_status or ''}".lower()


def _is_successful_auction(lot: AuctionLot) -> bool:
    status_text = _auction_status_text(lot)
    return (
        "successprotocolsigned" in status_text
        or ("СЃРѕСЃС‚РѕСЏ" in status_text and "РЅРµ СЃРѕСЃС‚РѕСЏ" not in status_text)
    )


def _is_failed_auction(lot: AuctionLot) -> bool:
    status_text = _auction_status_text(lot)
    return "failureprotocolsigned" in status_text or "РЅРµ СЃРѕСЃС‚РѕСЏ" in status_text


@dataclass(slots=True)
class _DistrictMarketActivity:
    lot_count: int
    successful_count: int
    failed_count: int
    liquidity_percent: float | None


def _district_market_activity(session: Session, lot: AuctionLot) -> _DistrictMarketActivity:
    if not lot.region or not lot.district:
        return _DistrictMarketActivity(
            lot_count=0,
            successful_count=0,
            failed_count=0,
            liquidity_percent=None,
        )
    lots = list(
        session.scalars(
            select(AuctionLot).where(
                AuctionLot.region == lot.region,
                AuctionLot.district == lot.district,
            )
        ).all()
    )
    successful_count = sum(1 for item in lots if _is_successful_auction(item))
    failed_count = sum(1 for item in lots if _is_failed_auction(item))
    finished_count = successful_count + failed_count
    liquidity_percent = (
        (successful_count / finished_count) * 100 if finished_count else None
    )
    return _DistrictMarketActivity(
        lot_count=len(lots),
        successful_count=successful_count,
        failed_count=failed_count,
        liquidity_percent=liquidity_percent,
    )


def _lot_failed_publication_count(session: Session, lot: AuctionLot) -> int:
    failed_count = (
        session.scalar(
            select(func.count(AuctionLot.id)).where(
                AuctionLot.cadastre_number == lot.cadastre_number,
                AuctionLot.cadastre_number.is_not(None),
                AuctionLot.source_search_status == "FailureProtocolSigned",
            )
        )
        if lot.cadastre_number
        else 0
    ) or 0
    history_failed_count = (
        session.scalar(
            select(func.count(AuctionLotHistory.id)).where(
                AuctionLotHistory.lot_id == lot.id,
                AuctionLotHistory.status.ilike("%не состоя%"),
            )
        )
        or 0
    )
    return max(failed_count, history_failed_count)


def _auction_rating(
    *,
    district_difference_percent: float | None,
    publication_count: int,
    failed_count: int,
    document_count: int,
    district_activity: _DistrictMarketActivity,
    auction_starts_at: datetime | None,
) -> int:
    rating = 45
    if district_difference_percent is not None:
        if district_difference_percent <= -30:
            rating += 24
        elif district_difference_percent <= -15:
            rating += 18
        elif district_difference_percent <= 0:
            rating += 10
        elif district_difference_percent >= 35:
            rating -= 20
        elif district_difference_percent >= 15:
            rating -= 10

    rating += 10 if document_count else -10
    if publication_count >= 4:
        rating += 10
    elif publication_count >= 2:
        rating += 5
    rating += min(12, failed_count * 4)

    if district_activity.lot_count >= 20:
        rating += 8
    elif district_activity.lot_count >= 8:
        rating += 5
    elif district_activity.lot_count >= 3:
        rating += 2

    if district_activity.liquidity_percent is not None:
        if district_activity.liquidity_percent <= 25:
            rating += 8
        elif district_activity.liquidity_percent <= 50:
            rating += 4
        elif district_activity.liquidity_percent >= 80:
            rating -= 4

    auction_starts_at = _aware_datetime(auction_starts_at)
    if auction_starts_at and auction_starts_at > datetime.now(UTC):
        rating += 3
    return max(0, min(100, rating))


def auction_lot_metrics(session: Session, lot: AuctionLot) -> AuctionLotMetrics:
    price_per_sotka = _price_per_sotka(lot)
    price_per_square_meter = _price_per_square_meter(lot)
    district_average = district_average_price_per_sotka(session, lot)
    difference = None
    if price_per_sotka is not None and district_average and district_average > 0:
        difference = ((price_per_sotka - district_average) / district_average) * 100

    publication_count = (
        session.scalar(
            select(func.count(AuctionLot.id)).where(
                AuctionLot.cadastre_number == lot.cadastre_number,
                AuctionLot.cadastre_number.is_not(None),
            )
        )
        if lot.cadastre_number
        else 1
    ) or 1
    failed_count = _lot_failed_publication_count(session, lot)
    document_count = len(unique_auction_documents(lot.documents))
    district_activity = _district_market_activity(session, lot)
    rating = _auction_rating(
        district_difference_percent=difference,
        publication_count=publication_count,
        failed_count=failed_count,
        document_count=document_count,
        district_activity=district_activity,
        auction_starts_at=lot.auction_starts_at,
    )

    return AuctionLotMetrics(
        price_per_sotka=price_per_sotka,
        price_per_square_meter=price_per_square_meter,
        district_average_price_per_sotka=district_average,
        district_difference_percent=difference,
        publication_count=publication_count,
        failed_count=failed_count,
        document_count=document_count,
        district_lot_count=district_activity.lot_count,
        district_successful_count=district_activity.successful_count,
        district_failed_count=district_activity.failed_count,
        district_liquidity_percent=district_activity.liquidity_percent,
        rating=rating,
    )


def auction_lots_metrics(
    session: Session,
    lots: list[AuctionLot],
) -> dict[str, AuctionLotMetrics]:
    """Build list-card metrics with bounded database work.

    The detail view still uses ``auction_lot_metrics`` for a single lot. List
    views need a batch path because district and publication statistics are
    shared by many cards on the same page.
    """
    if not lots:
        return {}

    district_keys = {
        (lot.region, lot.district)
        for lot in lots
        if lot.region and lot.district
    }
    district_averages: dict[tuple[str, str], float] = {}
    district_activity_by_key: dict[tuple[str, str], _DistrictMarketActivity] = {}
    if district_keys:
        status_text = func.lower(
            func.coalesce(AuctionLot.status, "")
            + " "
            + func.coalesce(AuctionLot.source_search_status, "")
        )
        successful_condition = or_(
            status_text.like("%successprotocolsigned%"),
            and_(
                status_text.like("%состоя%"),
                ~status_text.like("%не состоя%"),
            ),
        )
        failed_condition = or_(
            status_text.like("%failureprotocolsigned%"),
            status_text.like("%не состоя%"),
        )
        price_per_sotka = case(
            (
                and_(
                    AuctionLot.start_price_kzt.is_not(None),
                    AuctionLot.area_ha.is_not(None),
                    AuctionLot.area_ha > 0,
                ),
                AuctionLot.start_price_kzt / (AuctionLot.area_ha * 100),
            ),
            else_=None,
        )
        district_rows = session.execute(
            select(
                AuctionLot.region,
                AuctionLot.district,
                func.count(AuctionLot.id),
                func.sum(case((successful_condition, 1), else_=0)),
                func.sum(case((failed_condition, 1), else_=0)),
                func.avg(price_per_sotka),
            )
            .where(
                or_(
                    *(
                        and_(
                            AuctionLot.region == region,
                            AuctionLot.district == district,
                        )
                        for region, district in district_keys
                    )
                )
            )
            .group_by(AuctionLot.region, AuctionLot.district)
        ).all()
        for (
            region,
            district,
            lot_count,
            successful_count,
            failed_count,
            average_price,
        ) in district_rows:
            if not region or not district:
                continue
            key = (region, district)
            successful_count = int(successful_count or 0)
            failed_count = int(failed_count or 0)
            if average_price is not None:
                district_averages[key] = float(average_price)
            finished_count = successful_count + failed_count
            district_activity_by_key[key] = _DistrictMarketActivity(
                lot_count=int(lot_count or 0),
                successful_count=successful_count,
                failed_count=failed_count,
                liquidity_percent=(successful_count / finished_count) * 100
                if finished_count
                else None,
            )

    cadastre_numbers = {lot.cadastre_number for lot in lots if lot.cadastre_number}
    publication_counts: dict[str, int] = {}
    failed_publication_counts: dict[str, int] = {}
    if cadastre_numbers:
        publication_rows = session.execute(
            select(
                AuctionLot.cadastre_number,
                func.count(AuctionLot.id),
                func.sum(
                    case(
                        (AuctionLot.source_search_status == "FailureProtocolSigned", 1),
                        else_=0,
                    )
                ),
            )
            .where(AuctionLot.cadastre_number.in_(cadastre_numbers))
            .group_by(AuctionLot.cadastre_number)
        ).all()
        for cadastre_number, publication_count, failed_count in publication_rows:
            if cadastre_number:
                publication_counts[cadastre_number] = int(publication_count or 0)
                failed_publication_counts[cadastre_number] = int(failed_count or 0)

    history_failed_counts: dict[str, int] = {}
    if lots:
        history_rows = session.execute(
            select(AuctionLotHistory.lot_id, func.count(AuctionLotHistory.id))
            .where(
                AuctionLotHistory.lot_id.in_([lot.id for lot in lots]),
                AuctionLotHistory.status.ilike("%не состоя%"),
            )
            .group_by(AuctionLotHistory.lot_id)
        ).all()
        history_failed_counts = {
            lot_id: int(failed_count or 0) for lot_id, failed_count in history_rows
        }

    metrics_by_lot: dict[str, AuctionLotMetrics] = {}
    for lot in lots:
        price_per_sotka = _price_per_sotka(lot)
        district_average = district_averages.get((lot.region, lot.district))
        difference = None
        if price_per_sotka is not None and district_average and district_average > 0:
            difference = ((price_per_sotka - district_average) / district_average) * 100

        publication_count = (
            publication_counts.get(lot.cadastre_number, 1)
            if lot.cadastre_number
            else 1
        )
        failed_count = max(
            failed_publication_counts.get(lot.cadastre_number, 0)
            if lot.cadastre_number
            else 0,
            history_failed_counts.get(lot.id, 0),
        )
        activity = district_activity_by_key.get(
            (lot.region, lot.district),
            _DistrictMarketActivity(0, 0, 0, None),
        )
        document_count = len(unique_auction_documents(lot.documents))
        metrics_by_lot[lot.id] = AuctionLotMetrics(
            price_per_sotka=price_per_sotka,
            price_per_square_meter=_price_per_square_meter(lot),
            district_average_price_per_sotka=district_average,
            district_difference_percent=difference,
            publication_count=publication_count,
            failed_count=failed_count,
            document_count=document_count,
            district_lot_count=activity.lot_count,
            district_successful_count=activity.successful_count,
            district_failed_count=activity.failed_count,
            district_liquidity_percent=activity.liquidity_percent,
            rating=_auction_rating(
                district_difference_percent=difference,
                publication_count=publication_count,
                failed_count=failed_count,
                document_count=document_count,
                district_activity=activity,
                auction_starts_at=lot.auction_starts_at,
            ),
        )
    return metrics_by_lot


def auction_lot_geo_metrics(lot: AuctionLot) -> AuctionGeoMetrics:
    return auction_geo_metrics(lot)


def auction_lot_history(session: Session, lot_id: str) -> list[AuctionLotHistory]:
    return list(
        session.scalars(
            select(AuctionLotHistory)
            .where(AuctionLotHistory.lot_id == lot_id)
            .order_by(AuctionLotHistory.observed_at.desc())
        ).all()
    )


def auction_lot_changes(session: Session, lot_id: str) -> list[AuctionLotChange]:
    return list(
        session.scalars(
            select(AuctionLotChange)
            .where(AuctionLotChange.lot_id == lot_id)
            .order_by(AuctionLotChange.changed_at.desc())
        ).all()
    )


def _format_area(value: float | None, language: str) -> str:
    if value is None:
        return "көрсетілмеген" if language == "kz" else "не указана"
    return f"{value:g} га"


def _format_date(value: datetime | None, language: str) -> str:
    if value is None:
        return "көрсетілмеген" if language == "kz" else "не указана"
    return value.strftime("%d.%m.%Y %H:%M")


def _aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def format_auction_card(
    lot: AuctionLot,
    language: str = "ru",
    *,
    compact: bool = False,
) -> str:
    language = normalize_language(language)
    if language == "kz":
        lines = [
            f"🏷 <b>№{lot.auction_number or lot.source_lot_id}</b>",
            f"📍 {lot.location_text or lot.region or 'Орналасқан жері көрсетілмеген'}",
            (
                "🗂 E-Qazyna санаты: "
                f"{lot.functional_purpose_level2 or 'көрсетілмеген'}"
            ),
            f"🎯 Нысаналы мақсаты: {lot.purpose or lot.title}",
        f"📐 {_format_area(lot.area_ha, language)}",
        f"💰 Бастапқы баға: {_format_money(lot.start_price_kzt)}",
        f"💠 Сотық бағасы: {_format_money(_price_per_sotka(lot))}",
        f"📅 Сауда: {_format_date(lot.auction_starts_at, language)}",
        f"📌 Мәртебе: {lot.status or 'көрсетілмеген'}",
        ]
        if compact:
            return "\n".join(lines)
        lines.extend(
            [
                f"🔢 Кадастрлық нөмір: {lot.cadastre_number or 'көрсетілмеген'}",
                f"📄 Жер құқығы: {lot.land_rights or 'көрсетілмеген'}",
                f"🏛 Сатушы: {lot.seller_name or 'көрсетілмеген'}",
                "",
                "📎 Құжаттар E-Qazyna ресми порталындағы ашық сілтемелер арқылы ашылады. "
                "Бот құжаттарды өзгертпейді және куәландырмайды.",
                "",
                "ℹ️ Бұл E-Qazyna-да жарияланған ресми лот. "
                "Бот сауда алаңы емес және өтінімге ЭЦҚ қоймайды.",
            ]
        )
        return "\n".join(lines)
    lines = [
        f"🏷 <b>Лот №{lot.auction_number or lot.source_lot_id}</b>",
        f"📍 {lot.location_text or lot.region or 'Местоположение не указано'}",
        (
            "🗂 Функциональное назначение E-Qazyna: "
            f"{lot.functional_purpose_level2 or 'не указано'}"
        ),
        f"🎯 Целевое назначение: {lot.purpose or lot.title}",
        f"📐 {_format_area(lot.area_ha, language)}",
        f"💰 Стартовая цена: {_format_money(lot.start_price_kzt)}",
        f"💠 Цена за сотку: {_format_money(_price_per_sotka(lot))}",
        f"📅 Торги: {_format_date(lot.auction_starts_at, language)}",
        f"📌 Статус: {lot.status or 'не указан'}",
    ]
    if compact:
        return "\n".join(lines)
    lines.extend(
        [
            f"🔢 Кадастровый номер: {lot.cadastre_number or 'не указан'}",
            f"📄 Право на землю: {lot.land_rights or 'не указано'}",
            f"🏛 Продавец: {lot.seller_name or 'не указан'}",
            "",
            "📎 Документы открываются по публичным ссылкам официального портала E-Qazyna. "
            "Бот не изменяет и не заверяет документы.",
            "",
            "ℹ️ Это официальный лот, опубликованный на E-Qazyna. "
            "Бот не является торговой площадкой и не подписывает заявки ЭЦП.",
        ]
    )
    return "\n".join(lines)


def list_auction_regions(session: Session) -> list[tuple[str, int]]:
    rows = session.execute(
        select(AuctionLot.region, func.count(AuctionLot.id))
        .where(AuctionLot.active.is_(True), AuctionLot.region.is_not(None))
        .group_by(AuctionLot.region)
        .order_by(AuctionLot.region)
    ).all()
    return [(region, count) for region, count in rows if region]


def list_auction_functional_purposes(
    session: Session,
    region: str | None = None,
) -> list[tuple[str, int]]:
    query = (
        select(
            AuctionLot.functional_purpose_level2,
            func.count(AuctionLot.id),
        )
        .where(
            AuctionLot.active.is_(True),
            AuctionLot.functional_purpose_level2.is_not(None),
        )
        .group_by(AuctionLot.functional_purpose_level2)
        .order_by(AuctionLot.functional_purpose_level2)
    )
    if region:
        query = query.where(AuctionLot.region == region)
    rows = session.execute(query).all()
    return [(purpose, count) for purpose, count in rows if purpose]


def auction_lots_query(filters: AuctionFilters):
    conditions = []
    if filters.active_only:
        conditions.append(AuctionLot.active.is_(True))
    if filters.region:
        conditions.append(AuctionLot.region == filters.region)
    if filters.district:
        conditions.append(AuctionLot.district == filters.district)
    if filters.locality:
        conditions.append(AuctionLot.locality == filters.locality)
    if filters.purpose_query:
        conditions.append(
            AuctionLot.functional_purpose_level2 == filters.purpose_query
        )
    if filters.min_price_kzt is not None:
        conditions.append(AuctionLot.start_price_kzt >= filters.min_price_kzt)
    if filters.max_price_kzt is not None:
        conditions.append(AuctionLot.start_price_kzt <= filters.max_price_kzt)
    if filters.min_area_ha is not None:
        conditions.append(AuctionLot.area_ha >= filters.min_area_ha)
    if filters.max_area_ha is not None:
        conditions.append(AuctionLot.area_ha <= filters.max_area_ha)
    query = select(AuctionLot).options(selectinload(AuctionLot.documents))
    if conditions:
        query = query.where(and_(*conditions))
    return query.order_by(
        AuctionLot.auction_starts_at.is_(None),
        AuctionLot.auction_starts_at,
        AuctionLot.created_at.desc(),
    )


def list_auction_lots(
    session: Session,
    filters: AuctionFilters,
    *,
    offset: int = 0,
    limit: int = 10,
) -> tuple[list[AuctionLot], int]:
    query = auction_lots_query(filters)
    count_query = select(func.count()).select_from(query.order_by(None).subquery())
    total = session.scalar(count_query) or 0
    lots = session.scalars(query.offset(offset).limit(limit)).all()
    return list(lots), int(total)


def _valid_kazakhstan_coordinates(latitude: float, longitude: float) -> bool:
    return 40.0 <= latitude <= 56.5 and 46.0 <= longitude <= 88.5


def _coordinate_pair_from_values(first: object, second: object) -> tuple[float, float] | None:
    try:
        first_float = float(str(first).replace(",", "."))
        second_float = float(str(second).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if _valid_kazakhstan_coordinates(first_float, second_float):
        return first_float, second_float
    if _valid_kazakhstan_coordinates(second_float, first_float):
        return second_float, first_float
    return None


def _coordinates_from_payload(value: object) -> tuple[float, float] | None:
    if isinstance(value, dict):
        lower_keys = {str(key).lower(): key for key in value}
        latitude_key = next(
            (
                lower_keys[key]
                for key in ("latitude", "lat", "y")
                if key in lower_keys
            ),
            None,
        )
        longitude_key = next(
            (
                lower_keys[key]
                for key in ("longitude", "lon", "lng", "x")
                if key in lower_keys
            ),
            None,
        )
        if latitude_key is not None and longitude_key is not None:
            pair = _coordinate_pair_from_values(value[latitude_key], value[longitude_key])
            if pair is not None:
                return pair

        coordinates_key = lower_keys.get("coordinates")
        if coordinates_key is not None:
            coordinates = value[coordinates_key]
            if (
                isinstance(coordinates, (list, tuple))
                and len(coordinates) >= 2
                and not isinstance(coordinates[0], (list, tuple, dict))
            ):
                pair = _coordinate_pair_from_values(coordinates[0], coordinates[1])
                if pair is not None:
                    return pair
            nested = _coordinates_from_payload(coordinates)
            if nested is not None:
                return nested

        for nested_value in value.values():
            nested = _coordinates_from_payload(nested_value)
            if nested is not None:
                return nested
    elif isinstance(value, (list, tuple)):
        if len(value) >= 2 and not isinstance(value[0], (list, tuple, dict)):
            pair = _coordinate_pair_from_values(value[0], value[1])
            if pair is not None:
                return pair
        for nested_value in value:
            nested = _coordinates_from_payload(nested_value)
            if nested is not None:
                return nested
    return None


def _coordinates_from_text(text: str | None) -> tuple[float, float] | None:
    if not text:
        return None
    for match in re.finditer(
        r"(?<!\d)([4-8]\d(?:[.,]\d{3,})?)\s*[,;\s]\s*([4-8]\d(?:[.,]\d{3,})?)(?!\d)",
        text,
    ):
        pair = _coordinate_pair_from_values(match.group(1), match.group(2))
        if pair is not None:
            return pair
    return None


def auction_lot_coordinates(lot: AuctionLot) -> tuple[float, float] | None:
    if lot.raw_payload_json:
        try:
            payload = json.loads(lot.raw_payload_json)
        except json.JSONDecodeError:
            payload = None
        if payload is not None:
            coordinates = _coordinates_from_payload(payload)
            if coordinates is not None:
                return coordinates
    return _coordinates_from_text(
        " ".join(
            part
            for part in (
                lot.location_text,
                lot.description,
                lot.title,
            )
            if part
        )
    )


def active_auction_lots_geojson(
    session: Session,
    filters: AuctionFilters | None = None,
) -> dict[str, object]:
    filters = filters or AuctionFilters()
    active_filters = AuctionFilters(
        region=filters.region,
        district=filters.district,
        locality=filters.locality,
        purpose_query=filters.purpose_query,
        min_price_kzt=filters.min_price_kzt,
        max_price_kzt=filters.max_price_kzt,
        min_area_ha=filters.min_area_ha,
        max_area_ha=filters.max_area_ha,
        active_only=True,
    )
    lots = session.scalars(auction_lots_query(active_filters)).all()
    features: list[dict[str, object]] = []
    for lot in lots:
        coordinates = auction_lot_coordinates(lot)
        if coordinates is None:
            continue
        latitude, longitude = coordinates
        metrics = auction_lot_metrics(session, lot)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [longitude, latitude],
                },
                "properties": {
                    "id": lot.id,
                    "price": lot.start_price_kzt,
                    "area": lot.area_ha,
                    "cadastre": lot.cadastre_number,
                    "rating": metrics.rating,
                    "source_url": lot.source_url,
                },
            }
        )
    return {
        "type": "FeatureCollection",
        "features": features,
    }


def get_auction_lot(session: Session, lot_id: str) -> AuctionLot | None:
    return session.scalar(
        select(AuctionLot)
        .options(selectinload(AuctionLot.documents))
        .where(AuctionLot.id == lot_id)
    )


def toggle_favorite(session: Session, telegram_user_id: str, lot_id: str) -> bool:
    favorite = session.scalar(
        select(AuctionFavorite).where(
            AuctionFavorite.telegram_user_id == telegram_user_id,
            AuctionFavorite.lot_id == lot_id,
        )
    )
    if favorite:
        session.delete(favorite)
        session.commit()
        return False
    if session.get(AuctionLot, lot_id) is None:
        raise ValueError("Лот не найден")
    session.add(AuctionFavorite(telegram_user_id=telegram_user_id, lot_id=lot_id))
    session.commit()
    return True


def is_favorite(session: Session, telegram_user_id: str, lot_id: str) -> bool:
    return (
        session.scalar(
            select(AuctionFavorite.id).where(
                AuctionFavorite.telegram_user_id == telegram_user_id,
                AuctionFavorite.lot_id == lot_id,
            )
        )
        is not None
    )


def list_favorites(session: Session, telegram_user_id: str) -> list[AuctionLot]:
    return list(
        session.scalars(
            select(AuctionLot)
            .join(AuctionFavorite, AuctionFavorite.lot_id == AuctionLot.id)
            .where(AuctionFavorite.telegram_user_id == telegram_user_id)
            .order_by(AuctionFavorite.created_at.desc())
        ).all()
    )


def create_subscription(
    session: Session,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
    language: str,
    filters: AuctionFilters,
) -> AuctionSubscription:
    subscription = session.scalar(
        select(AuctionSubscription).where(
            AuctionSubscription.telegram_user_id == telegram_user_id,
            AuctionSubscription.region == filters.region,
            AuctionSubscription.district == filters.district,
            AuctionSubscription.locality == filters.locality,
            AuctionSubscription.purpose_query == filters.purpose_query,
            AuctionSubscription.min_price_kzt == filters.min_price_kzt,
            AuctionSubscription.max_price_kzt == filters.max_price_kzt,
            AuctionSubscription.min_area_ha == filters.min_area_ha,
            AuctionSubscription.max_area_ha == filters.max_area_ha,
        )
    )
    if subscription is None:
        subscription = AuctionSubscription(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            language=normalize_language(language),
            region=filters.region,
            district=filters.district,
            locality=filters.locality,
            purpose_query=filters.purpose_query,
            min_price_kzt=filters.min_price_kzt,
            max_price_kzt=filters.max_price_kzt,
            min_area_ha=filters.min_area_ha,
            max_area_ha=filters.max_area_ha,
        )
        session.add(subscription)
    else:
        subscription.telegram_chat_id = telegram_chat_id
        subscription.language = normalize_language(language)
        subscription.active = True
    session.commit()
    session.refresh(subscription)
    return subscription


def list_subscriptions(session: Session, telegram_user_id: str) -> list[AuctionSubscription]:
    return list(
        session.scalars(
            select(AuctionSubscription)
            .where(AuctionSubscription.telegram_user_id == telegram_user_id)
            .order_by(AuctionSubscription.created_at.desc())
        ).all()
    )


def disable_subscription(
    session: Session,
    telegram_user_id: str,
    subscription_id: int,
) -> bool:
    subscription = session.scalar(
        select(AuctionSubscription).where(
            AuctionSubscription.id == subscription_id,
            AuctionSubscription.telegram_user_id == telegram_user_id,
        )
    )
    if subscription is None:
        return False
    subscription.active = False
    session.commit()
    return True


def _notify_new_lot(
    session: Session,
    lot: AuctionLot,
    subscriptions: list[AuctionSubscription],
) -> int:
    from app.services import telegram_request

    if not lot.active:
        return 0
    sent = 0
    for subscription in subscriptions:
        if not has_auction_paid_access(session, subscription.telegram_user_id):
            continue
        if not _subscription_matches(lot, subscription):
            continue
        exists = session.scalar(
            select(AuctionNotification.id).where(
                AuctionNotification.subscription_id == subscription.id,
                AuctionNotification.lot_id == lot.id,
            )
        )
        if exists:
            continue
        notification = AuctionNotification(
            subscription_id=subscription.id,
            lot_id=lot.id,
        )
        session.add(notification)
        try:
            language = normalize_language(subscription.language)
            intro = (
                "🔔 <b>Сүзгіңіз бойынша жаңа жер лоты табылды</b>\n\n"
                if language == "kz"
                else "🔔 <b>Новый земельный лот по вашему фильтру</b>\n\n"
            )
            telegram_request(
                "sendMessage",
                {
                    "chat_id": subscription.telegram_chat_id,
                    "text": intro + format_auction_card(lot, language, compact=True),
                    "parse_mode": "HTML",
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "E-Qazyna ↗",
                                    "url": lot.source_url,
                                }
                            ],
                            [
                                {
                                    "text": (
                                        "Толығырақ"
                                        if language == "kz"
                                        else "Подробнее"
                                    ),
                                    "callback_data": f"auction:lot:{lot.id}",
                                }
                            ],
                        ]
                    },
                },
            )
            notification.sent_at = datetime.now(UTC)
            sent += 1
        except Exception as exc:
            logger.exception("Failed to send auction notification for lot %s", lot.id)
            notification.error_message = str(exc)
    session.flush()
    return sent


def _notify_changed_favorite_lot(session: Session, lot: AuctionLot) -> int:
    from app.services import telegram_request

    changes = auction_lot_changes(session, lot.id)[:5]
    if not changes:
        return 0
    favorites = list(
        session.scalars(
            select(AuctionFavorite).where(AuctionFavorite.lot_id == lot.id)
        ).all()
    )
    sent = 0
    for favorite in favorites:
        if not has_auction_paid_access(session, favorite.telegram_user_id):
            continue
        access = session.scalar(
            select(AuctionAccess).where(
                AuctionAccess.telegram_user_id == favorite.telegram_user_id
            )
        )
        language = normalize_language(access.language if access else "ru")
        title = (
            "🔔 <b>Таңдаулы лот бойынша өзгеріс</b>"
            if language == "kz"
            else "🔔 <b>Изменение по избранному лоту</b>"
        )
        change_lines = []
        for change in changes:
            change_lines.append(
                f"• {change.field_name}: {change.old_value or '—'} → {change.new_value or '—'}"
            )
        try:
            telegram_request(
                "sendMessage",
                {
                    "chat_id": access.telegram_chat_id if access else favorite.telegram_user_id,
                    "text": "\n".join(
                        [
                            title,
                            "",
                            format_auction_card(lot, language, compact=True),
                            "",
                            *change_lines,
                        ]
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "E-Qazyna ↗",
                                    "url": lot.source_url,
                                }
                            ],
                            [
                                {
                                    "text": (
                                        "Толығырақ"
                                        if language == "kz"
                                        else "Подробнее"
                                    ),
                                    "callback_data": f"auction:lot:{lot.id}",
                                }
                            ],
                        ]
                    },
                },
            )
            sent += 1
        except Exception:
            logger.exception(
                "Failed to send favorite auction change notification for lot %s",
                lot.id,
            )
    return sent


def sync_current_auctions(
    session: Session,
    *,
    provider: EqazynaProvider | None = None,
    max_pages: int | None = None,
    max_lots: int | None = None,
    statuses: list[str] | None = None,
    publish_date_windows: list[tuple[str, str]] | None = None,
    deactivate_missing: bool = True,
    send_notifications: bool = True,
) -> AuctionSyncResult:
    provider = provider or EqazynaProvider()
    crawl = provider.current_lots_with_report(
        max_pages=max_pages,
        max_lots=max_lots,
        statuses=statuses,
        publish_date_windows=publish_date_windows,
    )
    data_items = crawl.lots
    created = 0
    updated = 0
    errors = 0
    deactivated = 0
    new_lots: list[AuctionLot] = []
    changed_lots: list[AuctionLot] = []
    for detail_error in crawl.detail_errors:
        logger.warning(
            "Failed to fetch E-Qazyna lot detail %s (%s): %s",
            detail_error.source_lot_id or "unknown",
            detail_error.source_url,
            detail_error.message,
        )
    for data in data_items:
        try:
            lot, is_created, changed = upsert_auction_lot(session, data)
            if is_created:
                created += 1
                new_lots.append(lot)
            elif changed:
                updated += 1
                changed_lots.append(lot)
        except Exception:
            errors += 1
            session.rollback()
            logger.exception("Failed to store E-Qazyna lot %s", data.source_lot_id)
    if deactivate_missing and crawl.complete:
        deactivated = deactivate_missing_current_auction_lots(
            session,
            crawl.source_lot_ids,
        )
    elif deactivate_missing and crawl.source_lot_ids:
        logger.warning(
            "Skipping stale E-Qazyna deactivation: crawl incomplete "
            "(urls=%s, pages=%s, detail_errors=%s)",
            crawl.url_count,
            crawl.pages_scanned,
            len(crawl.detail_errors),
        )
    elif not deactivate_missing:
        logger.info(
            "Skipping stale E-Qazyna deactivation by request "
            "(urls=%s, pages=%s, complete=%s)",
            crawl.url_count,
            crawl.pages_scanned,
            crawl.complete,
        )
    session.commit()

    notifications_sent = 0
    if send_notifications and new_lots:
        subscriptions = list(
            session.scalars(
                select(AuctionSubscription).where(AuctionSubscription.active.is_(True))
            ).all()
        )
        for lot in new_lots:
            notifications_sent += _notify_new_lot(session, lot, subscriptions)
        session.commit()
    if send_notifications and changed_lots:
        for lot in changed_lots:
            notifications_sent += _notify_changed_favorite_lot(session, lot)
        session.commit()
    return AuctionSyncResult(
        fetched=len(data_items),
        created=created,
        updated=updated,
        notifications_sent=notifications_sent,
        errors=errors + len(crawl.detail_errors),
        detail_errors=len(crawl.detail_errors),
        deactivated=deactivated,
        crawl_complete=crawl.complete,
        url_count=crawl.url_count,
        pages_scanned=crawl.pages_scanned,
        status_counts=dict(crawl.status_counts),
    )


def auction_stats(
    session: Session,
    *,
    excluded_user_ids: set[str] | None = None,
) -> dict[str, int | datetime | None]:
    excluded_user_ids = excluded_user_ids or set()
    favorites_query = select(func.count(AuctionFavorite.id))
    subscriptions_query = select(func.count(AuctionSubscription.id)).where(
        AuctionSubscription.active.is_(True)
    )
    paid_access_query = select(func.count(AuctionAccess.id)).where(
        AuctionAccess.paid_access.is_(True),
        or_(
            AuctionAccess.access_expires_at.is_(None),
            AuctionAccess.access_expires_at > datetime.now(UTC),
        ),
    )
    if excluded_user_ids:
        favorites_query = favorites_query.where(
            AuctionFavorite.telegram_user_id.not_in(excluded_user_ids)
        )
        subscriptions_query = subscriptions_query.where(
            AuctionSubscription.telegram_user_id.not_in(excluded_user_ids)
        )
        paid_access_query = paid_access_query.where(
            AuctionAccess.telegram_user_id.not_in(excluded_user_ids)
        )
    return {
        "total": session.scalar(select(func.count(AuctionLot.id))) or 0,
        "active": session.scalar(
            select(func.count(AuctionLot.id)).where(AuctionLot.active.is_(True))
        )
        or 0,
        "favorites": session.scalar(favorites_query) or 0,
        "subscriptions": session.scalar(subscriptions_query) or 0,
        "paid_access": session.scalar(paid_access_query) or 0,
        "last_sync": session.scalar(select(func.max(AuctionLot.last_seen_at))),
    }


def auction_region_stats(session: Session) -> list[dict[str, object]]:
    rows = session.execute(
        select(
            AuctionLot.region,
            func.count(AuctionLot.id),
            func.sum(case((AuctionLot.active.is_(True), 1), else_=0)),
            func.avg(AuctionLot.start_price_kzt),
            func.avg(AuctionLot.area_ha),
        )
        .where(AuctionLot.region.is_not(None))
        .group_by(AuctionLot.region)
        .order_by(AuctionLot.region)
    ).all()
    return [
        {
            "region": region,
            "total": total or 0,
            "active": active or 0,
            "avg_price": avg_price,
            "avg_area": avg_area,
        }
        for region, total, active, avg_price, avg_area in rows
        if region
    ]


def auction_district_stats(session: Session, region: str | None = None) -> list[dict[str, object]]:
    query = (
        select(
            AuctionLot.region,
            AuctionLot.district,
            func.count(AuctionLot.id),
            func.sum(case((AuctionLot.active.is_(True), 1), else_=0)),
            func.avg(AuctionLot.start_price_kzt),
            func.avg(AuctionLot.area_ha),
        )
        .where(AuctionLot.district.is_not(None))
        .group_by(AuctionLot.region, AuctionLot.district)
        .order_by(AuctionLot.region, AuctionLot.district)
    )
    if region:
        query = query.where(AuctionLot.region == region)
    rows = session.execute(query).all()
    return [
        {
            "region": item_region,
            "district": district,
            "total": total or 0,
            "active": active or 0,
            "avg_price": avg_price,
            "avg_area": avg_area,
        }
        for item_region, district, total, active, avg_price, avg_area in rows
        if district
    ]


def auction_category_stats(session: Session) -> list[dict[str, object]]:
    rows = session.execute(
        select(
            AuctionLot.functional_purpose_level2,
            func.count(AuctionLot.id),
            func.sum(case((AuctionLot.active.is_(True), 1), else_=0)),
            func.avg(AuctionLot.start_price_kzt),
            func.avg(AuctionLot.area_ha),
        )
        .where(AuctionLot.functional_purpose_level2.is_not(None))
        .group_by(AuctionLot.functional_purpose_level2)
        .order_by(AuctionLot.functional_purpose_level2)
    ).all()
    return [
        {
            "category": category,
            "total": total or 0,
            "active": active or 0,
            "avg_price": avg_price,
            "avg_area": avg_area,
        }
        for category, total, active, avg_price, avg_area in rows
        if category
    ]


def auction_status_stats(session: Session) -> list[dict[str, object]]:
    rows = session.execute(
        select(
            AuctionLot.status,
            AuctionLot.source_search_status,
            func.count(AuctionLot.id),
            func.avg(AuctionLot.start_price_kzt),
            func.avg(AuctionLot.sale_price_kzt),
        )
        .group_by(AuctionLot.status, AuctionLot.source_search_status)
        .order_by(AuctionLot.source_search_status, AuctionLot.status)
    ).all()
    return [
        {
            "status": status or source_status or "unknown",
            "source_search_status": source_status,
            "total": total or 0,
            "avg_start_price": avg_start_price,
            "avg_sale_price": avg_sale_price,
        }
        for status, source_status, total, avg_start_price, avg_sale_price in rows
    ]


def auction_monthly_stats(session: Session, *, months: int = 24) -> list[dict[str, object]]:
    lots = session.scalars(select(AuctionLot)).all()
    buckets: dict[str, dict[str, object]] = {}
    for lot in lots:
        source_date = lot.published_at or lot.first_seen_at.date()
        key = f"{source_date.year:04d}-{source_date.month:02d}"
        bucket = buckets.setdefault(
            key,
            {
                "month": key,
                "total": 0,
                "created": 0,
                "active": 0,
                "sold": 0,
                "failed": 0,
                "cancelled": 0,
            },
        )
        bucket["total"] = int(bucket["total"]) + 1
        bucket["created"] = int(bucket["created"]) + 1
        if lot.active:
            bucket["active"] = int(bucket["active"]) + 1
        status_text = f"{lot.status or ''} {lot.source_search_status or ''}".lower()
        if (
            ("состоя" in status_text and "не состоя" not in status_text)
            or "successprotocolsigned" in status_text
        ):
            bucket["sold"] = int(bucket["sold"]) + 1
        if "не состоя" in status_text or "failure" in status_text:
            bucket["failed"] = int(bucket["failed"]) + 1
        if "отмен" in status_text or "cancel" in status_text or "nullify" in status_text:
            bucket["cancelled"] = int(bucket["cancelled"]) + 1
    return sorted(buckets.values(), key=lambda item: str(item["month"]))[-months:]


def auction_district_price_rankings(
    session: Session,
    *,
    limit: int = 10,
) -> dict[str, list[dict[str, object]]]:
    rows = session.execute(
        select(
            AuctionLot.region,
            AuctionLot.district,
            AuctionLot.start_price_kzt,
            AuctionLot.area_ha,
        ).where(
            AuctionLot.region.is_not(None),
            AuctionLot.district.is_not(None),
            AuctionLot.start_price_kzt.is_not(None),
            AuctionLot.area_ha.is_not(None),
            AuctionLot.area_ha > 0,
        )
    ).all()
    grouped: dict[tuple[str, str], list[float]] = {}
    for region, district, price, area in rows:
        if not region or not district or price is None or not area:
            continue
        grouped.setdefault((region, district), []).append(price / (area * 100))
    items = [
        {
            "region": region,
            "district": district,
            "avg_price_per_sotka": sum(values) / len(values),
            "lot_count": len(values),
        }
        for (region, district), values in grouped.items()
        if values
    ]
    items.sort(key=lambda item: float(item["avg_price_per_sotka"]))
    return {
        "cheapest": items[:limit],
        "most_expensive": list(reversed(items[-limit:])),
    }


def auction_market_snapshot(session: Session) -> dict[str, object]:
    return {
        "catalog": auction_stats(session),
        "statuses": auction_status_stats(session),
        "regions": auction_region_stats(session),
        "categories": auction_category_stats(session),
        "monthly": auction_monthly_stats(session),
        "district_price_rankings": auction_district_price_rankings(session),
    }
