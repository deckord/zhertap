from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from time import perf_counter
from urllib.parse import urlparse

import httpx
from sqlalchemy import and_, case, or_, select, text
from sqlalchemy.orm import Session

from app.auction_geo import haversine_m
from app.auction_land_identity import (
    is_valid_land_boundary,
    reconcile_lot_land_object,
    set_land_object_boundary,
)
from app.config import get_settings
from app.models import (
    AuctionEvidence,
    AuctionLot,
    AuctionLotChange,
    AuctionLotGeoCheck,
    AuctionSource,
)
from app.provider_backpressure import (
    ProviderBackpressure,
    create_redis_provider_backpressure,
)
from app.providers.eqazyna import EqazynaError, parse_lot_detail
from app.providers.jerler import JerlerObjectData, JerlerProvider, JerlerUpstreamError

SOURCE_CODE = "jerler_source_object"
EVIDENCE_TYPE = "source_object_card"
ERROR_EVIDENCE_TYPE = "source_object_card_error"
DEFAULT_ERROR_RETRY_MINUTES = 15
JERLER_PROVIDER_CODE = "jerler"


class JerlerEnrichmentDeferred(RuntimeError):
    """Typed signal for a Celery caller to reschedule without sleeping a worker."""

    def __init__(
        self,
        reason: str,
        retry_after_seconds: float,
        *,
        partial_result: SourceObjectSyncResult | None = None,
    ) -> None:
        bounded_retry = min(max(float(retry_after_seconds), 0.1), 86_400.0)
        super().__init__(f"Jerler enrichment deferred: {reason}")
        self.provider = JERLER_PROVIDER_CODE
        self.reason = reason
        self.retry_after_seconds = bounded_retry
        self.partial_result = partial_result


@lru_cache(maxsize=4)
def _configured_jerler_backpressure(
    redis_url: str,
    app_env: str,
) -> ProviderBackpressure:
    return create_redis_provider_backpressure(redis_url, app_env=app_env)


def _default_jerler_backpressure() -> ProviderBackpressure:
    settings = get_settings()
    return _configured_jerler_backpressure(settings.redis_url, settings.app_env)


def _fetch_jerler_object(
    client: JerlerProvider,
    source_url: str,
    *,
    backpressure: ProviderBackpressure,
) -> JerlerObjectData:
    """Run exactly one guarded outbound call and finalize its permit exactly once."""
    permit = backpressure.acquire(JERLER_PROVIDER_CODE)
    if not permit.allowed:
        raise JerlerEnrichmentDeferred(permit.status, permit.retry_after_seconds)

    started = perf_counter()
    finalized = False
    try:
        data = client.fetch_object(source_url)
    except JerlerUpstreamError as exc:
        latency_ms = (perf_counter() - started) * 1000
        backpressure.record_result(
            permit,
            success=False,
            latency_ms=latency_ms,
            error=str(exc)[:240],
        )
        finalized = True
        retry_after = backpressure.retry_delay_seconds(
            JERLER_PROVIDER_CODE,
            attempt=0,
            owner_token=permit.owner_token,
            server_retry_after_seconds=exc.retry_after_seconds,
        )
        raise JerlerEnrichmentDeferred("upstream_failure", retry_after) from exc
    except BaseException:
        # Parsing, validation, cancellation and caller errors do not affect the circuit.
        backpressure.release(permit)
        finalized = True
        raise
    else:
        latency_ms = (perf_counter() - started) * 1000
        backpressure.record_result(permit, success=True, latency_ms=latency_ms)
        finalized = True
        return data
    finally:
        if not finalized:
            backpressure.release(permit)


@dataclass(slots=True)
class SourceObjectSyncResult:
    selected: int = 0
    fetched: int = 0
    updated: int = 0
    skipped_fresh: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "selected": self.selected,
            "fetched": self.fetched,
            "updated": self.updated,
            "skipped_fresh": self.skipped_fresh,
            "errors": self.errors,
        }


@dataclass(slots=True)
class SourceObjectLinkSyncResult:
    selected: int = 0
    fetched: int = 0
    updated: int = 0
    errors: int = 0


def _ensure_source(session: Session) -> AuctionSource:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": "auction-source-registry:jerler"},
        )
    source = session.scalar(select(AuctionSource).where(AuctionSource.code == SOURCE_CODE))
    if source is None:
        source = AuctionSource(
            code=SOURCE_CODE,
            source_type="official_object_registry",
            name="Jerler / E-Qazyna — карточка земельного объекта",
            base_url="https://traderesources.e-qazyna.kz",
            region="all",
            parser_kind="public_html",
            priority=95,
            crawl_interval_minutes=1440,
            active=True,
            quality_status="official",
            legal_status="public",
        )
        session.add(source)
        session.flush()
    return source


def _eqazyna_base_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    if not parsed.scheme or not parsed.netloc:
        return "https://sauda.e-qazyna.kz"
    return f"{parsed.scheme}://{parsed.netloc}"


def sync_missing_source_object_links(
    session: Session,
    *,
    transport: httpx.BaseTransport | None = None,
    limit: int = 20,
) -> SourceObjectLinkSyncResult:
    """Fill real Jerler object links from E-Qazyna cards before Jerler enrichment."""
    bounded_limit = max(1, min(int(limit), 50))
    lots = list(
        session.scalars(
            select(AuctionLot)
            .outerjoin(AuctionLotGeoCheck, AuctionLotGeoCheck.lot_id == AuctionLot.id)
            .where(
                AuctionLot.source == "e-qazyna",
                AuctionLot.source_url.is_not(None),
                AuctionLot.source_url != "",
                or_(AuctionLot.source_object_url.is_(None), AuctionLot.source_object_url == ""),
            )
            .order_by(
                case(
                    (
                        and_(
                            AuctionLot.object_type == "land",
                            AuctionLot.active.is_(True),
                            AuctionLotGeoCheck.coordinate_status.in_(
                                ("unconfirmed", "missing", "unknown")
                            ),
                        ),
                        0,
                    ),
                    else_=1,
                ).asc(),
                AuctionLot.last_seen_at.desc().nullslast(),
                AuctionLot.id.asc(),
            )
            .limit(bounded_limit)
        )
    )
    result = SourceObjectLinkSyncResult(selected=len(lots))
    if not lots:
        return result
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ZhertapBot/1.0)"}
    with httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(20.0, connect=8.0),
        follow_redirects=True,
        headers=headers,
    ) as client:
        for lot in lots:
            try:
                response = client.get(str(lot.source_url))
                response.raise_for_status()
                data = parse_lot_detail(
                    response.text,
                    str(lot.source_url),
                    _eqazyna_base_url(str(lot.source_url)),
                )
                result.fetched += 1
            except (httpx.HTTPError, EqazynaError, ValueError):
                result.errors += 1
                continue
            if not data.source_object_url:
                continue
            changed = False
            old_url = lot.source_object_url
            if not old_url:
                lot.source_object_url = data.source_object_url
                changed = True
            if data.land_object_id and not lot.land_object_id:
                lot.land_object_id = data.land_object_id
                changed = True
            if changed:
                lot.changes.append(
                    AuctionLotChange(
                        lot_id=lot.id,
                        field_name="source_object_url",
                        old_value=old_url,
                        new_value=data.source_object_url,
                    )
                )
                result.updated += 1
    session.flush()
    return result


def _existing_evidence(
    session: Session,
    lot_id: str,
    evidence_type: str = EVIDENCE_TYPE,
) -> AuctionEvidence | None:
    return session.scalar(
        select(AuctionEvidence)
        .where(
            AuctionEvidence.lot_id == lot_id,
            AuctionEvidence.evidence_type == evidence_type,
        )
        .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
        .limit(1)
    )


def _meaningful_payload(
    data: JerlerObjectData,
    conflicts: list[dict[str, object]],
) -> dict[str, object]:
    payload = {key: value for key, value in data.as_dict().items() if value is not None}
    if conflicts:
        payload["conflicts"] = conflicts
    return payload


def _same_value(current: object, incoming: object) -> bool:
    if isinstance(current, str) and isinstance(incoming, str):
        return " ".join(current.casefold().split()) == " ".join(incoming.casefold().split())
    if isinstance(current, (float, int)) and isinstance(incoming, (float, int)):
        return abs(float(current) - float(incoming)) < 0.000_001
    return current == incoming


def _set_if_found(
    lot: AuctionLot,
    field_name: str,
    value: object,
    changes: list[AuctionLotChange],
    conflicts: list[dict[str, object]],
) -> bool:
    if value is None or value == "":
        return False
    old_value = getattr(lot, field_name)
    if old_value is not None and old_value != "":
        if not _same_value(old_value, value):
            conflicts.append(
                {
                    "field": field_name,
                    "lot_value": old_value,
                    "source_object_value": value,
                    "resolution": "preserved_lot_value",
                }
            )
        return False
    setattr(lot, field_name, value)
    changes.append(
        AuctionLotChange(
            lot_id=lot.id,
            field_name=field_name,
            old_value=None if old_value is None else str(old_value),
            new_value=str(value),
        )
    )
    return True


def _valid_kazakhstan_coordinates(latitude: float, longitude: float) -> bool:
    return 40.0 <= latitude <= 56.5 and 46.0 <= longitude <= 88.5


def _geometry_points(value: object) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if isinstance(value, dict):
        return _geometry_points(value.get("coordinates"))
    if not isinstance(value, list):
        return points
    if len(value) >= 2 and not isinstance(value[0], (list, dict)):
        try:
            longitude = float(str(value[0]).replace(",", "."))
            latitude = float(str(value[1]).replace(",", "."))
        except (TypeError, ValueError):
            return points
        if _valid_kazakhstan_coordinates(latitude, longitude):
            points.append((latitude, longitude))
        return points
    for item in value:
        points.extend(_geometry_points(item))
    return points


def _geometry_center(geometry: dict[str, object] | None) -> tuple[float, float] | None:
    points = _geometry_points(geometry)
    if not points:
        return None
    latitudes = [point[0] for point in points]
    longitudes = [point[1] for point in points]
    return (sum(latitudes) / len(latitudes), sum(longitudes) / len(longitudes))


def _apply_source_geometry_to_geo_check(
    session: Session,
    lot: AuctionLot,
    data: JerlerObjectData,
) -> bool:
    if not is_valid_land_boundary(data.geometry_geojson):
        return False
    center = _geometry_center(data.geometry_geojson)
    if center is None:
        return False
    latitude, longitude = center
    geo_check = session.scalar(
        select(AuctionLotGeoCheck).where(AuctionLotGeoCheck.lot_id == lot.id)
    )
    if geo_check is None:
        geo_check = AuctionLotGeoCheck(lot_id=lot.id)
        session.add(geo_check)
    elif (geo_check.boundary_source or "").startswith("egkn:"):
        # The cadastral layer is authoritative for the persisted map point. Jerler
        # remains evidence, but must not replace EGKN coordinates or invalidate
        # already calculated surroundings merely because it was fetched later.
        return False
    changed = (
        geo_check.latitude != latitude
        or geo_check.longitude != longitude
        or geo_check.coordinate_status != "found"
    )
    previous_osm_checked = geo_check.osm_checked_at is not None
    geo_check.coordinate_status = "found"
    geo_check.latitude = latitude
    geo_check.longitude = longitude
    geo_check.google_maps_url = f"https://www.google.com/maps/search/{latitude:.6f},{longitude:.6f}"
    geo_check.boundary_status = "verified"
    geo_check.boundary_source = "jerler:source_object"
    if changed:
        geo_check.osm_status = "stale" if previous_osm_checked else "not_checked"
        geo_check.osm_checked_at = None
        geo_check.road_distance_m = None
        geo_check.power_distance_m = None
        geo_check.water_distance_m = None
        geo_check.open_water_distance_m = None
        geo_check.cemetery_distance_m = None
        geo_check.object_distance_m = None
        geo_check.object_kind = None
        geo_check.engineering_status = "manual_required"
    geo_check.notes = (
        "Координаты и контур найдены в карточке Jerler. Перед ставкой нужно сверить "
        "красные линии, ПДП/генплан и документы лота."
    )
    geo_check.checked_at = datetime.now(UTC)
    geo_check.updated_at = geo_check.checked_at
    return changed


def apply_source_object_data(
    session: Session,
    lot: AuctionLot,
    data: JerlerObjectData,
    *,
    source: AuctionSource,
) -> bool:
    changes: list[AuctionLotChange] = []
    conflicts: list[dict[str, object]] = []
    changed = False
    for field_name in (
        "land_object_id",
        "cadastre_number",
        "land_rights",
        "lease_term_years",
        "divisible",
        "additional_payment_kzt",
        "annual_rent_kzt",
    ):
        changed |= _set_if_found(
            lot,
            field_name,
            getattr(data, field_name),
            changes,
            conflicts,
        )
    if changes:
        session.add_all(changes)
    if data.geometry_geojson is not None and not is_valid_land_boundary(data.geometry_geojson):
        conflicts.append(
            {
                "field": "geometry_geojson",
                "lot_value": None,
                "source_object_value": "invalid_boundary",
                "resolution": "rejected_invalid_boundary",
            }
        )
    elif data.geometry_geojson is not None:
        geo_check = session.scalar(
            select(AuctionLotGeoCheck).where(AuctionLotGeoCheck.lot_id == lot.id)
        )
        source_center = _geometry_center(data.geometry_geojson)
        if (
            geo_check is not None
            and (geo_check.boundary_source or "").startswith("egkn:")
            and geo_check.latitude is not None
            and geo_check.longitude is not None
            and source_center is not None
            and haversine_m(
                geo_check.latitude,
                geo_check.longitude,
                source_center[0],
                source_center[1],
            )
            > 100.0
        ):
            conflicts.append(
                {
                    "field": "geometry_geojson",
                    "lot_value": geo_check.boundary_source,
                    "source_object_value": "published_boundary",
                    "resolution": "preserved_higher_priority_egkn_boundary",
                }
            )
    changed |= _apply_source_geometry_to_geo_check(session, lot, data)
    land_object = reconcile_lot_land_object(session, lot)
    if (
        land_object is not None
        and data.geometry_geojson is not None
        and land_object.boundary_source in {None, "jerler:source_object"}
    ):
        # Jerler may fill an absent canonical boundary, but must never replace
        # a higher-priority EGKN cadastral boundary on the same object.
        changed |= set_land_object_boundary(
            land_object,
            data.geometry_geojson,
            source="jerler:source_object",
        )

    payload = _meaningful_payload(data, conflicts)
    evidence = _existing_evidence(session, lot.id)
    if evidence is None:
        evidence = AuctionEvidence(
            lot_id=lot.id,
            source_id=source.id,
            evidence_type=EVIDENCE_TYPE,
            title="Официальная карточка земельного объекта",
        )
        session.add(evidence)
    evidence.source_id = source.id
    evidence.status = "conflict" if conflicts else ("found" if len(payload) > 1 else "missing")
    evidence.value_text = _evidence_summary(data, conflicts)
    evidence.source_url = data.source_url
    evidence.confidence = 0.98
    evidence.raw_payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    evidence.observed_at = datetime.now(UTC)
    return changed


def _evidence_summary(
    data: JerlerObjectData,
    conflicts: list[dict[str, object]] | None = None,
) -> str:
    values = []
    if data.land_object_id:
        values.append(f"EGKN ID: {data.land_object_id}")
    if data.land_rights:
        values.append(f"Право: {data.land_rights}")
    if data.lease_term_years is not None:
        values.append(f"Срок: {data.lease_term_years:g} лет")
    if data.arrests_text:
        values.append(f"Аресты: {data.arrests_text}")
    if data.restrictions_text:
        values.append(f"Ограничения: {data.restrictions_text}")
    if data.geometry_geojson:
        values.append("Геометрия опубликована")
    if conflicts:
        fields = ", ".join(str(item["field"]) for item in conflicts)
        values.append(f"Конфликт источников: {fields}")
    return "; ".join(values) or "Карточка доступна, структурированные сведения не найдены"


def _lock_lot_evidence(session: Session, lot_id: str) -> None:
    """Serialize evidence upserts per lot on PostgreSQL without a schema migration."""
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"auction-source-object:{lot_id}"},
    )


def _worklist_query(
    *,
    limit: int,
    ttl_minutes: int,
    error_retry_minutes: int,
    lot_ids: list[str] | None,
):
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=max(5, ttl_minutes))
    missing_geometry_retry_before = now - timedelta(minutes=min(max(5, ttl_minutes), 60))
    retry_before = now - timedelta(minutes=max(1, error_retry_minutes))
    latest_good = (
        select(AuctionEvidence.observed_at)
        .where(
            AuctionEvidence.lot_id == AuctionLot.id,
            AuctionEvidence.evidence_type == EVIDENCE_TYPE,
            AuctionEvidence.status.in_(("found", "conflict")),
        )
        .order_by(AuctionEvidence.observed_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    latest_jerler_geometry = (
        select(AuctionLotGeoCheck.checked_at)
        .where(
            AuctionLotGeoCheck.lot_id == AuctionLot.id,
            AuctionLotGeoCheck.boundary_source == "jerler:source_object",
            AuctionLotGeoCheck.latitude.is_not(None),
            AuctionLotGeoCheck.longitude.is_not(None),
        )
        .order_by(AuctionLotGeoCheck.checked_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    latest_error = (
        select(AuctionEvidence.observed_at)
        .where(
            AuctionEvidence.lot_id == AuctionLot.id,
            AuctionEvidence.evidence_type == ERROR_EVIDENCE_TYPE,
            AuctionEvidence.status == "error",
        )
        .order_by(AuctionEvidence.observed_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    query = (
        select(AuctionLot)
        .where(
            AuctionLot.source_object_url.is_not(None),
            AuctionLot.source_object_url != "",
            or_(
                latest_good.is_(None),
                latest_good < stale_before,
                and_(
                    latest_jerler_geometry.is_(None),
                    latest_good < missing_geometry_retry_before,
                ),
            ),
            or_(latest_error.is_(None), latest_error < retry_before),
        )
        .order_by(latest_good.asc().nullsfirst(), AuctionLot.last_seen_at.desc())
        .limit(limit)
    )
    if lot_ids:
        query = query.where(AuctionLot.id.in_(lot_ids[:limit]))
    return query


def _record_fetch_error(
    session: Session,
    lot: AuctionLot,
    source: AuctionSource,
    exc: Exception,
) -> None:
    evidence = _existing_evidence(session, lot.id, ERROR_EVIDENCE_TYPE)
    if evidence is None:
        evidence = AuctionEvidence(
            lot_id=lot.id,
            source_id=source.id,
            evidence_type=ERROR_EVIDENCE_TYPE,
            title="Ошибка чтения карточки земельного объекта",
        )
        session.add(evidence)
    evidence.status = "error"
    evidence.value_text = str(exc)[:1000]
    evidence.source_url = lot.source_object_url
    evidence.confidence = 0.0
    evidence.observed_at = datetime.now(UTC)


def sync_auction_source_objects(
    session: Session,
    *,
    provider: JerlerProvider | None = None,
    backpressure: ProviderBackpressure | None = None,
    limit: int = 20,
    ttl_minutes: int = 1440,
    error_retry_minutes: int = DEFAULT_ERROR_RETRY_MINUTES,
    lot_ids: list[str] | None = None,
) -> SourceObjectSyncResult:
    """Enrich a bounded batch. Intended for workers, never for a web request."""
    bounded_limit = max(1, min(int(limit), 100))
    query = _worklist_query(
        limit=bounded_limit,
        ttl_minutes=ttl_minutes,
        error_retry_minutes=error_retry_minutes,
        lot_ids=lot_ids,
    )
    lots = list(session.scalars(query))
    result = SourceObjectSyncResult(selected=len(lots))
    if not lots:
        return result
    source = _ensure_source(session)
    client = provider or JerlerProvider()
    limiter = backpressure or _default_jerler_backpressure()
    for lot in lots:
        try:
            data = _fetch_jerler_object(
                client,
                str(lot.source_object_url),
                backpressure=limiter,
            )
            result.fetched += 1
            _lock_lot_evidence(session, lot.id)
            if apply_source_object_data(session, lot, data, source=source):
                result.updated += 1
        except JerlerEnrichmentDeferred:
            raise
        except Exception as exc:
            result.errors += 1
            _lock_lot_evidence(session, lot.id)
            _record_fetch_error(session, lot, source, exc)
    source.last_checked_at = datetime.now(UTC)
    if result.fetched:
        source.last_success_at = datetime.now(UTC)
        source.last_error = None
    elif result.errors:
        source.last_error = f"{result.errors} source-object errors"
    session.flush()
    return result


def sync_auction_source_objects_detached(
    session_factory: Callable[[], Session],
    *,
    provider: JerlerProvider | None = None,
    backpressure: ProviderBackpressure | None = None,
    limit: int = 20,
    ttl_minutes: int = 1440,
    error_retry_minutes: int = DEFAULT_ERROR_RETRY_MINUTES,
    lot_ids: list[str] | None = None,
) -> SourceObjectSyncResult:
    """Worker orchestration with no database transaction held during HTTP calls."""
    bounded_limit = max(1, min(int(limit), 100))
    with session_factory() as session:
        link_result = sync_missing_source_object_links(
            session,
            limit=min(bounded_limit, 20),
        )
        if link_result.updated:
            session.commit()
        source = _ensure_source(session)
        source_id = source.id
        worklist = [
            (lot.id, str(lot.source_object_url))
            for lot in session.scalars(
                _worklist_query(
                    limit=bounded_limit,
                    ttl_minutes=ttl_minutes,
                    error_retry_minutes=error_retry_minutes,
                    lot_ids=lot_ids,
                )
            )
        ]
        session.commit()

    result = SourceObjectSyncResult(selected=len(worklist))
    client = provider or JerlerProvider()
    limiter = backpressure or _default_jerler_backpressure()
    for lot_id, source_url in worklist:
        try:
            data = _fetch_jerler_object(client, source_url, backpressure=limiter)
            result.fetched += 1
            fetch_error: Exception | None = None
        except JerlerEnrichmentDeferred as exc:
            # Earlier detached iterations were committed in their own transactions.
            # Preserve that durable progress for the task result and downstream triggers.
            exc.partial_result = result
            raise
        except Exception as exc:
            data = None
            fetch_error = exc

        # HTTP is complete before opening the short per-lot persistence transaction.
        with session_factory() as session:
            _lock_lot_evidence(session, lot_id)
            lot = session.get(AuctionLot, lot_id)
            source = session.get(AuctionSource, source_id)
            if lot is None or source is None:
                session.rollback()
                continue
            if fetch_error is not None:
                result.errors += 1
                _record_fetch_error(session, lot, source, fetch_error)
            elif data is not None and apply_source_object_data(
                session,
                lot,
                data,
                source=source,
            ):
                result.updated += 1
            session.commit()

    with session_factory() as session:
        source = session.get(AuctionSource, source_id)
        if source is not None:
            source.last_checked_at = datetime.now(UTC)
            if result.fetched:
                source.last_success_at = datetime.now(UTC)
                source.last_error = None
            elif result.errors:
                source.last_error = f"{result.errors} source-object errors"
            session.commit()
    return result
