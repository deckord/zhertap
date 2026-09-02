"""Authoritative E-Qazyna adapter for the interesting-lot evidence contract.

This adapter emits only facts present in a freshly observed official lot record. It
never fills GIS, planning, territorial or market gaps from text similarity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import math
from typing import Mapping
from urllib.parse import urlparse

MAX_SOURCE_AGE = timedelta(days=30)
MAX_DOCUMENTS = 20
MAX_BOUNDARY_POSITIONS = 10_000
MAX_MARKET_PAYLOAD_BYTES = 64_000
MIN_COMPARABLE_DISCOUNT_PERCENT = 10.0
TERMINAL_REPEAT_STATUSES = frozenset(
    {"SuccessProtocolSigned", "FailureProtocolSigned", "NullifyResultProtocolSigned"}
)


@dataclass(frozen=True, slots=True)
class OfficialLotQueueInput:
    lot_id: str
    source_search_status: str | None
    source_url: str | None
    source_observed_at: datetime | None
    purpose: str | None
    land_rights: str | None
    lease_term_years: float | int | None
    auction_starts_at: datetime | None
    start_price_kzt: float | int | None
    document_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OfficialAuctionEventInput:
    """Two official publications linked by one exact Jerler land identity."""

    canonical_key: str | None
    identity_confidence: str | None
    current_source_lot_id: str | None
    current_source_url: str | None
    current_auction_starts_at: datetime | None
    current_observed_at: datetime | None
    current_start_price_kzt: float | int | None
    previous_source_lot_id: str | None
    previous_source_url: str | None
    previous_auction_starts_at: datetime | None
    previous_source_search_status: str | None
    previous_start_price_kzt: float | int | None


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _clean(value: object, *, limit: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] if cleaned else None


def _official_url(value: object, *, host: str | None = None) -> str | None:
    cleaned = _clean(value, limit=1000)
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if host and parsed.hostname != host:
        return None
    return cleaned


def _valid_polygon(raw: object) -> bool:
    """Boundedly validate stored parcel GeoJSON without repairing it."""
    if not isinstance(raw, str) or len(raw) > 1_000_000:
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(value, dict) or value.get("type") not in {"Polygon", "MultiPolygon"}:
        return False
    coordinates = value.get("coordinates")
    positions = 0

    def walk(node: object) -> bool:
        nonlocal positions
        if not isinstance(node, list) or not node:
            return False
        if len(node) >= 2 and all(
            isinstance(number, (int, float)) and not isinstance(number, bool)
            for number in node[:2]
        ):
            positions += 1
            return positions <= MAX_BOUNDARY_POSITIONS and all(
                math.isfinite(float(number)) for number in node[:2]
            )
        return all(walk(child) for child in node)

    return walk(coordinates) and positions >= 4


def _base(
    *, key: str, classification: str, status: str, title: str, detail: str,
    source_kind: str, source_url: str | None, observed_at: datetime | None,
) -> dict[str, object]:
    return {
        "key": key,
        "category": "auction" if key.startswith("auction_") else "legal",
        "classification": classification,
        "status": status,
        "title": title,
        "detail": detail,
        "source_kind": source_kind,
        "source_url": source_url,
        "observed_at": observed_at,
        "authority_verified": source_kind in {"official_lot", "official_document"},
        "coverage_verified": False,
        "geometry_verified": False,
    }


def _manual(key: str, category: str, title: str, detail: str, source_kind: str) -> dict[str, object]:
    item = _base(
        key=key,
        classification="unverified",
        status="manual_required",
        title=title,
        detail=detail,
        source_kind=source_kind,
        source_url=None,
        observed_at=None,
    )
    item["category"] = category
    return item


def _official(
    key: str, title: str, detail: str, source: OfficialLotQueueInput, observed: datetime
) -> dict[str, object]:
    return _base(
        key=key,
        classification="confirmed",
        status="found",
        title=title,
        detail=detail,
        source_kind="official_lot",
        source_url=source.source_url,
        observed_at=observed,
    )


def produce_official_repeat_event(
    source: OfficialAuctionEventInput | None,
    *,
    evaluated_at: datetime,
) -> Mapping[str, object]:
    """Confirm only a chronologically prior terminal attempt for the exact Jerler object."""
    fallback = lambda detail: _manual(  # noqa: E731 - every gap is deliberately fail-closed
        "official_auction_event", "auction_event", "Повторная публикация аукциона не подтверждена",
        detail, "official_auction_history",
    )
    evaluated = _aware(evaluated_at)
    if evaluated is None:
        raise ValueError("evaluated_at must be timezone-aware")
    if source is None:
        return fallback("Нет предыдущей официальной попытки, связанной точной идентичностью Jerler.")

    canonical_key = _clean(source.canonical_key)
    current_id = _clean(source.current_source_lot_id)
    previous_id = _clean(source.previous_source_lot_id)
    current_url = _official_url(source.current_source_url)
    previous_url = _official_url(source.previous_source_url)
    current_starts = _aware(source.current_auction_starts_at)
    previous_starts = _aware(source.previous_auction_starts_at)
    observed = _aware(source.current_observed_at)
    urls_are_official = bool(current_url and previous_url) and all(
        urlparse(url).hostname in {"e-qazyna.kz", "www.e-qazyna.kz", "sauda.e-qazyna.kz"}
        for url in (current_url, previous_url)
    )
    if not (
        canonical_key and canonical_key.startswith("jerler:")
        and source.identity_confidence == "jerler"
        and current_id and previous_id and current_id != previous_id
        and current_url and previous_url and urls_are_official
        and current_starts and previous_starts and previous_starts < current_starts
        and source.previous_source_search_status in TERMINAL_REPEAT_STATUSES
        and observed and timedelta(0) <= evaluated - observed <= MAX_SOURCE_AGE
    ):
        return fallback(
            "Нужны разные официальные публикации, завершённый предыдущий результат, "
            "точная Jerler-идентичность и корректная хронология."
        )

    current_price = source.current_start_price_kzt
    previous_price = source.previous_start_price_kzt
    valid_prices = all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 < float(value) < 10**15
        for value in (current_price, previous_price)
    )
    price_change = (
        (float(current_price) - float(previous_price)) / float(previous_price) * 100.0
        if valid_prices
        else None
    )
    price_metric = (
        f"; стартовая цена {float(previous_price):,.0f} → {float(current_price):,.0f} KZT "
        f"({price_change:+g}%)"
        if price_change is not None
        else "; изменение стартовой цены не подтверждено"
    )
    result = {
        "key": "official_auction_event",
        "category": "auction_event",
        "classification": "confirmed",
        "status": "found",
        "title": "Лот выставлен повторно после завершённой предыдущей попытки",
        "detail": (
            f"Предыдущая публикация {previous_id}: {previous_starts.isoformat()}; "
            f"текущая {current_id}: {current_starts.isoformat()}. "
            "Это объясняет аукционную ситуацию, но не является инвестиционной рекомендацией."
        ),
        "source_kind": "official_auction_history",
        "source_url": previous_url,
        "observed_at": observed,
        "authority_verified": True,
        "coverage_verified": True,
        "geometry_verified": False,
        "comparison_basis": f"официальные публикации {previous_id} и {current_id} для {canonical_key}",
        "metric": (
            f"попытка №2; {previous_starts.isoformat()} → {current_starts.isoformat()}"
            f"{price_metric}"
        ),
        "comparison_method": (
            "точное совпадение canonical Jerler object, разные source_lot_id и "
            "хронологически более ранний подписанный итоговый протокол"
        ),
        "event_type": "repeat",
        "current_source_url": current_url,
        "previous_source_url": previous_url,
    }
    if price_change is not None:
        result["price_change_percent"] = round(price_change, 1)
    return result


def produce_rare_lease_term_reason(
    lot: object,
    alternatives: list[object],
    *,
    filter_label: str,
    evaluated_at: datetime,
) -> Mapping[str, object]:
    """Compare an official lease term with fresh active alternatives in one exact filter.

    Scope selection belongs to the read service. This bounded adapter only confirms a
    uniquely longer term; missing, duplicated, stale or non-official rows fail closed.
    """
    fallback = lambda detail: _manual(  # noqa: E731
        "rare_lease_term",
        "rare_comparative_feature",
        "Срок права не даёт подтверждённой причины выделить лот",
        detail,
        "official_lot",
    )
    evaluated = _aware(evaluated_at)
    if evaluated is None:
        raise ValueError("evaluated_at must be timezone-aware")
    label = _clean(filter_label)

    def checked_row(value: object) -> tuple[str, float, str, datetime] | None:
        row_id = _clean(str(getattr(value, "id", "") or ""))
        term = getattr(value, "lease_term_years", None)
        url = _official_url(getattr(value, "source_url", None))
        observed = _aware(getattr(value, "last_seen_at", None))
        if not (
            row_id and isinstance(term, (int, float)) and not isinstance(term, bool)
            and math.isfinite(float(term)) and 0 < float(term) <= 100
            and url and urlparse(url).hostname in {
                "e-qazyna.kz", "www.e-qazyna.kz", "sauda.e-qazyna.kz"
            }
            and observed and timedelta(0) <= evaluated - observed <= MAX_SOURCE_AGE
        ):
            return None
        return row_id, float(term), url, observed

    target = checked_row(lot)
    if target is None or not label:
        return fallback("Нужны свежие официальные срок, URL, дата наблюдения и точный фильтр.")
    target_id, target_term, target_url, target_observed = target
    checked: dict[str, tuple[str, float, str, datetime]] = {}
    for alternative in alternatives[:501]:
        row = checked_row(alternative)
        if row is not None and row[0] != target_id:
            checked[row[0]] = row
    if len(alternatives) > 500 or len(checked) < 3:
        return fallback("Нужно минимум три разные свежие официальные активные альтернативы того же фильтра.")
    next_best = max(row[1] for row in checked.values())
    if target_term <= next_best or target_term - next_best < 1.0:
        return fallback("Срок лота не является уникально самым длинным с разницей хотя бы один год.")
    observed_values = [row[3] for row in checked.values()]
    return {
        "key": "rare_lease_term",
        "category": "rare_comparative_feature",
        "classification": "confirmed",
        "status": "found",
        "title": "Срок права дольше, чем у активных альтернатив того же фильтра",
        "detail": (
            "Подтверждён только сравнительный срок права из официальных карточек; "
            "это не инвестиционная рекомендация."
        ),
        "source_kind": "official_lot",
        "source_url": target_url,
        "observed_at": target_observed,
        "authority_verified": True,
        "coverage_verified": True,
        "geometry_verified": False,
        "comparison_basis": f"{label}; активные альтернативы n={len(checked)}",
        "metric": (
            f"срок лота {target_term:g} лет; максимум альтернатив {next_best:g} лет; "
            f"наблюдения {min(observed_values).date().isoformat()}–{max(observed_values).date().isoformat()}"
        ),
        "comparison_method": (
            "точное совпадение region, district, functional_purpose_level2 и land_rights; "
            "только active ApplicationsAccept/Pending/Running E-Qazyna; разные lot id; "
            "срок лота уникально выше максимума когорты минимум на один год"
        ),
        "feature_kind": "lease_term",
        "active_alternatives_count": len(checked),
        "target_term_years": target_term,
        "next_best_term_years": next_best,
        "alternative_source_urls": tuple(row[2] for row in checked.values()),
    }


def produce_identity_evidence(lot: object, *, evaluated_at: datetime) -> list[Mapping[str, object]]:
    """Emit exact canonical identity and polygon facts, or explicit manual gaps."""
    evaluated = _aware(evaluated_at)
    if evaluated is None:
        raise ValueError("evaluated_at must be timezone-aware")
    land_object = getattr(lot, "land_object", None)
    observed = _aware(getattr(lot, "last_seen_at", None))
    identity_fresh = bool(observed and timedelta(0) <= evaluated - observed <= MAX_SOURCE_AGE)
    official_url = _official_url(getattr(lot, "source_url", None))
    jerler_url = _official_url(
        getattr(lot, "source_object_url", None), host="jerler.e-qazyna.kz"
    )
    identity: Mapping[str, object]
    if land_object is None:
        identity = _manual(
            "reliable_identity", "identity", "Надёжная идентичность участка не проверена",
            "Нет canonical land object, созданного по точному Jerler/ЕГКН/кадастровому идентификатору.", "jerler",
        )
    else:
        egkn_id = _clean(getattr(land_object, "egkn_id", None))
        cadastre = _clean(getattr(land_object, "cadastre_number", None))
        jerler_id = _clean(getattr(land_object, "jerler_object_id", None))
        confidence = _clean(getattr(land_object, "identity_confidence", None))
        identifiers = [
            label
            for label, value in (
                ("ЕГКН ID", egkn_id), ("кадастр", cadastre), ("Jerler ID", jerler_id)
            )
            if value
        ]
        source_kind = "jerler" if jerler_id and jerler_url else "egkn" if egkn_id else "official_lot"
        source_url = jerler_url if source_kind == "jerler" else official_url
        if confidence not in {"official", "cadastre", "jerler"} or not identifiers or not source_url or not identity_fresh:
            identity = _manual(
                "reliable_identity", "identity", "Надёжная идентичность участка не подтверждена",
                "Нужны точный официальный идентификатор, допустимый confidence, URL и дата наблюдения.", "jerler",
            )
        else:
            identity = _base(
                key="reliable_identity", classification="confirmed", status="found",
                title="Участок связан по точному официальному идентификатору",
                detail="Связаны: " + ", ".join(identifiers) + ". Текстовое или координатное угадывание не применялось.",
                source_kind=source_kind, source_url=source_url, observed_at=observed,
            )
            identity["category"] = "identity"

    boundary = land_object and getattr(land_object, "boundary_geojson", None)
    boundary_source = _clean(getattr(land_object, "boundary_source", None)) if land_object else None
    boundary_observed = _aware(getattr(land_object, "boundary_observed_at", None)) if land_object else None
    source_token = (boundary_source or "").casefold()
    polygon_kind = "jerler" if "jerler" in source_token else "egkn" if "egkn" in source_token else None
    polygon_url = jerler_url if polygon_kind == "jerler" else official_url if polygon_kind == "egkn" else None
    fresh = bool(boundary_observed and timedelta(0) <= evaluated - boundary_observed <= MAX_SOURCE_AGE)
    if polygon_kind and polygon_url and fresh and _valid_polygon(boundary):
        polygon: Mapping[str, object] = _base(
            key="verified_polygon", classification="confirmed", status="found",
            title="Polygon участка сохранён из именованного официального источника",
            detail=f"Источник границы: {boundary_source}. Геометрия прошла bounded GeoJSON validation.",
            source_kind=polygon_kind, source_url=polygon_url, observed_at=boundary_observed,
        )
        polygon["category"] = "identity"
        polygon["coverage_verified"] = True
        polygon["geometry_verified"] = True
    else:
        polygon = _manual(
            "verified_polygon", "identity", "Polygon участка не подтверждён",
            "Нужны валидная граница, официальный Jerler/ЕГКН provenance и наблюдение не старше 30 дней.", "egkn",
        )
    return [identity, polygon]


def produce_official_lot_evidence(
    source: OfficialLotQueueInput, *, evaluated_at: datetime
) -> list[Mapping[str, object]]:
    """Map one official record to bounded evidence; unavailable sources stay manual."""
    evaluated = _aware(evaluated_at)
    if evaluated is None:
        raise ValueError("evaluated_at must be timezone-aware")
    observed = _aware(source.source_observed_at)
    source_url = _clean(source.source_url, limit=1000)
    provenance_ok = bool(
        source_url
        and source_url.startswith(("https://", "http://"))
        and observed
        and timedelta(0) <= evaluated - observed <= MAX_SOURCE_AGE
    )
    if not provenance_ok:
        return [
            _manual(
                "official_source_provenance", "auction", "Официальная карточка требует повторной проверки",
                "Нет URL, timezone-aware даты наблюдения либо наблюдение старше 30 дней.", "official_lot",
            ),
            _manual("official_purpose", "legal", "Назначение не подтверждено", "Проверить официальную карточку.", "official_lot"),
            _manual("official_right", "legal", "Право не подтверждено", "Проверить официальную карточку.", "official_lot"),
            _manual("official_lease_term", "legal", "Срок права не подтверждён", "Проверить официальную карточку и документы.", "official_lot"),
            _manual("official_document_inventory", "documentation", "Документация не подтверждена", "Открыть официальный список приложений.", "official_document"),
        ]

    assert observed is not None
    evidence: list[Mapping[str, object]] = []
    purpose = _clean(source.purpose)
    rights = _clean(source.land_rights)
    if purpose:
        evidence.append(_official("official_purpose", "Назначение указано в официальной карточке", purpose, source, observed))
    else:
        evidence.append(_manual("official_purpose", "legal", "Назначение не указано", "Проверить карточку и документацию.", "official_lot"))
    if rights:
        evidence.append(_official("official_right", "Вид права указан в официальной карточке", rights, source, observed))
    else:
        evidence.append(_manual("official_right", "legal", "Вид права не указан", "Проверить карточку и документацию.", "official_lot"))
    term = source.lease_term_years
    if isinstance(term, (int, float)) and not isinstance(term, bool) and 0 < float(term) <= 100:
        evidence.append(_official("official_lease_term", "Срок права указан в официальной карточке", f"{term:g} лет", source, observed))
    else:
        evidence.append(_manual("official_lease_term", "legal", "Срок права не подтверждён", "Проверить условия и проект договора.", "official_document"))

    document_urls = tuple(
        url for raw in source.document_urls[: MAX_DOCUMENTS + 1]
        if (url := _clean(raw, limit=1000)) and url.startswith(("https://", "http://"))
    )
    if len(source.document_urls) <= MAX_DOCUMENTS and document_urls:
        evidence.append(_base(
            key="official_document_inventory", classification="confirmed", status="found",
            title="К официальной карточке приложены документы",
            detail=f"Доступно приложений: {len(document_urls)}. Содержание этой причиной не оценивается.",
            source_kind="official_document", source_url=document_urls[0], observed_at=observed,
        ))
        evidence[-1]["category"] = "documentation"
    else:
        evidence.append(_manual("official_document_inventory", "documentation", "Документация требует ручной проверки", "Нет проверяемых URL приложений либо их число превышает лимит.", "official_document"))

    starts = _aware(source.auction_starts_at)
    if source.source_search_status == "Running":
        evidence.append(_official("auction_deadline", "Официальный статус: торги проводятся", "Статус Running в текущей карточке.", source, observed))
    elif starts and starts >= evaluated:
        evidence.append(_official("auction_deadline", "Дата начала торгов указана официальным источником", starts.isoformat(), source, observed))
    else:
        evidence.append(_manual("auction_deadline", "auction", "Срок торгов требует проверки", "Нет будущей timezone-aware даты начала.", "official_lot"))
    price = source.start_price_kzt
    if isinstance(price, (int, float)) and not isinstance(price, bool) and 0 < float(price) < 10**15:
        evidence.append(_official("auction_start_price", "Стартовая цена указана официальным источником", f"{float(price):.2f} KZT", source, observed))
    else:
        evidence.append(_manual("auction_start_price", "auction", "Стартовая цена требует проверки", "Цена отсутствует или некорректна.", "official_lot"))

    evidence.extend([
        _manual("reliable_identity", "identity", "Надёжная идентичность участка не проверена", "Нужны согласованные Jerler/ЕГКН/cadastre и polygon.", "jerler"),
        _manual("location_access", "location", "Доступ и окружение не проверены", "Нужна геометрия; official GIS и OSM должны быть разделены.", "official_gis"),
        _manual("planning", "planning", "Генплан, ПДП и красные линии не проверены", "Нужен слой с проверенными authority, coverage и provenance.", "official_planning_layer"),
        _manual("territorial", "territorial", "Официальные территориальные сигналы не проверены", "Нужны gov.kz источник, дата, стадия и геометрическая привязка.", "gov_kz"),
        _manual("strict_comparables", "auction", "Строгие аналоги не проверены", "Нужны подтверждённые завершённые продажи того же года с геометрией.", "strict_verified_comparable"),
    ])
    return evidence


def produce_strict_market_reason(
    lot: object,
    evidence_row: object | None,
    *,
    evaluated_at: datetime,
) -> Mapping[str, object]:
    """Adapt one persisted W9 estimate into a same-year comparative thesis.

    The adapter never runs market discovery.  It accepts only the newest already
    persisted global-geometry cohort and otherwise preserves the comparables gap as
    ``manual_required``.  Observation year is used explicitly because the compact W9
    payload does not expose a different verified sale-event date.
    """
    evaluated = _aware(evaluated_at)
    fallback = lambda detail: _manual(  # noqa: E731 - keeps every failure fail-closed
        "strict_comparables",
        "comparative_price",
        "Строгие аналоги не дают подтверждённой причины выделить лот",
        detail,
        "strict_verified_comparable",
    )
    if evaluated is None:
        raise ValueError("evaluated_at must be timezone-aware")
    if evidence_row is None or getattr(evidence_row, "status", None) != "found":
        return fallback("Нет сохранённой успешной строгой рыночной оценки.")
    raw = getattr(evidence_row, "raw_payload_json", None)
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MARKET_PAYLOAD_BYTES:
        return fallback("Payload строгой рыночной оценки отсутствует или превышает лимит.")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return fallback("Payload строгой рыночной оценки повреждён.")
    if not isinstance(payload, dict):
        return fallback("Payload строгой рыночной оценки имеет неверный формат.")

    scope = payload.get("inventory_scope")
    estimate = payload.get("estimate")
    evaluations = payload.get("evaluations")
    if not (
        payload.get("status") == "ok"
        and payload.get("engine_version") == "strict-market-comparables.v1"
        and payload.get("target_status") == "ready"
        and isinstance(scope, dict)
        and scope.get("kind") == "global_verified_comparable_inventory"
        and scope.get("global_geo_selection_performed") is True
        and isinstance(estimate, dict)
        and isinstance(evaluations, list)
        and len(evaluations) <= 500
    ):
        return fallback("Нужна успешная global-geometry W9 оценка с готовой геометрией цели.")

    cohort_count = estimate.get("verified_comparables_used")
    high_quality_count = payload.get("high_quality_verified_count")
    median_per_ha = estimate.get("median_price_per_ha_kzt")
    start_price = getattr(lot, "start_price_kzt", None)
    area_ha = getattr(lot, "area_ha", None)
    numeric = (int, float)
    if not (
        type(cohort_count) is int
        and cohort_count >= 3
        and type(high_quality_count) is int
        and high_quality_count == cohort_count
        and isinstance(median_per_ha, numeric)
        and not isinstance(median_per_ha, bool)
        and math.isfinite(float(median_per_ha))
        and float(median_per_ha) > 0
        and isinstance(start_price, numeric)
        and not isinstance(start_price, bool)
        and math.isfinite(float(start_price))
        and float(start_price) > 0
        and isinstance(area_ha, numeric)
        and not isinstance(area_ha, bool)
        and math.isfinite(float(area_ha))
        and float(area_ha) > 0
    ):
        return fallback("Не хватает цены, площади или как минимум трёх строгих verified sale аналогов.")

    accepted_dates: list[datetime] = []
    for item in evaluations:
        if not isinstance(item, dict) or not (
            item.get("eligible") is True
            and item.get("quality_grade") == "A"
            and item.get("price_kind") == "verified_sale"
        ):
            continue
        accepted = _aware_from_iso(item.get("observed_at"))
        if accepted is None:
            return fallback("У строгого аналога отсутствует timezone-aware дата наблюдения.")
        accepted_dates.append(accepted)
    if len(accepted_dates) != cohort_count or any(date.year != evaluated.year for date in accepted_dates):
        return fallback("Cohort должен состоять только из строгих verified sale наблюдений того же года.")

    refs = payload.get("provenance_refs")
    urls = tuple(
        dict.fromkeys(
            ref for ref in refs
            if isinstance(refs, list) and isinstance(ref, str) and _official_url(ref)
        )
    ) if isinstance(refs, list) else ()
    if len(urls) < 3:
        return fallback("Не хватает трёх проверяемых HTTPS provenance URL строгих аналогов.")

    target_per_ha = float(start_price) / float(area_ha)
    discount = (float(median_per_ha) - target_per_ha) / float(median_per_ha) * 100.0
    if not math.isfinite(discount) or discount < MIN_COMPARABLE_DISCOUNT_PERCENT:
        return fallback("Разница со строгой медианой меньше установленного порога 10%.")
    rounded_discount = round(discount, 1)
    target_per_sotka = target_per_ha / 100.0
    median_per_sotka = float(median_per_ha) / 100.0
    observed = _aware(getattr(evidence_row, "observed_at", None))
    if observed is None or observed.year != evaluated.year:
        return fallback("У сохранённой рыночной оценки нет даты наблюдения текущего года.")

    return {
        "key": "strict_comparables",
        "category": "comparative_price",
        "classification": "confirmed",
        "status": "found",
        "title": f"Цена за сотку ниже медианы строгих аналогов на {rounded_discount:g}%",
        "detail": (
            f"{target_per_sotka:,.0f} против {median_per_sotka:,.0f} тенге/сотка; "
            f"использовано продаж: {cohort_count}."
        ),
        "source_kind": "strict_verified_comparable",
        "source_url": urls[0],
        "observed_at": observed,
        "authority_verified": True,
        "coverage_verified": True,
        "geometry_verified": True,
        "comparison_basis": (
            f"{cohort_count} завершённых verified sale наблюдений {evaluated.year} года "
            "в strict global-geometry cohort"
        ),
        "metric": (
            f"{target_per_sotka:,.0f} против {median_per_sotka:,.0f} тенге/сотка; "
            f"разница -{rounded_discount:g}%"
        ),
        "comparison_method": "медиана цены за сотку; strict-market-comparables.v1",
        "cohort_count": cohort_count,
        "discount_percent": rounded_discount,
        "comparable_year": evaluated.year,
        "target_year": evaluated.year,
        "sale_verified": True,
    }


def _aware_from_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)
