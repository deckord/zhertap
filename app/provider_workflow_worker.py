"""One-request provider units with durable DB progress and short persistence transactions."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.auction_object_enrichment import (
    JerlerEnrichmentDeferred,
    sync_auction_source_objects_detached,
)
from app.auction_service import upsert_auction_lot
from app.auction_v2 import (
    EGKN_CONTEXT_LAYERS,
    _apply_egkn_lookup_result,
    _apply_osm_surroundings,
    _egkn_check_due,
    _egkn_context_check_due,
    _get_or_build_geo_check,
    _gov_kz_evidence_text,
    _gov_kz_lot_match,
    _osm_check_due,
    _upsert_egkn_context_layer_evidence,
    _upsert_evidence,
    _upsert_gov_kz_attachments,
    sync_auction_v2_documents,
)
from app.config import settings
from app.models import AuctionDocument, AuctionLot, AuctionLotGeoCheck, AuctionSource
from app.provider_guard import ProviderCallDeferred
from app.provider_workflow_store import (
    ClaimedProviderUnit,
    ProviderUnitSpec,
    claim_provider_unit,
    complete_provider_unit,
    create_provider_workflow,
    defer_provider_unit,
    fail_provider_unit,
    provider_workflow_pending,
    stable_unit_key,
)
from app.providers.egkn import DistrictInfo, EgknProvider, EgknProviderError
from app.providers.eqazyna import EqazynaError, EqazynaProvider, extract_source_lot_id
from app.providers.gov_kz import GovKzAnnouncement, GovKzError, GovKzProvider
from app.providers.osm import OsmProvider, OsmProviderError

MAX_WORKFLOW_LOTS = 1_000


@dataclass(frozen=True, slots=True)
class ProviderWorkflowStepResult:
    workflow_key: str
    status: str
    unit_kind: str | None
    pending: int
    retry_after_seconds: float | None = None


def seed_eqazyna_page_workflow(
    session_factory: sessionmaker[Session],
    *,
    workflow_key: str,
    search_status: str,
    max_pages: int,
    start_page: int = 1,
    publish_date_window: tuple[str, str] | None = None,
    run_key: str | None = None,
    skip_existing_details: bool = False,
) -> int:
    bounded_start = max(1, min(int(start_page), 1_000))
    bounded_end = min(1_000, bounded_start + max(1, int(max_pages)) - 1)
    units = [
        ProviderUnitSpec(
            unit_key=f"page:{page:04d}",
            unit_kind="eqazyna_list_page",
            input_payload={
                "search_status": search_status[:64],
                "page": page,
                "publish_date_window": list(publish_date_window)
                if publish_date_window
                else None,
                "skip_existing_details": bool(skip_existing_details),
            },
        )
        for page in range(bounded_start, bounded_end + 1)
    ]
    return create_provider_workflow(
        session_factory,
        workflow_key=workflow_key,
        provider="eqazyna",
        workflow_kind="auction_list_and_detail",
        units=units,
        run_key=run_key,
    )


def seed_spatial_provider_workflows(
    session_factory: sessionmaker[Session], *, workflow_prefix: str, run_key: str | None = None
) -> list[str]:
    with session_factory() as session:
        rows = session.execute(
            select(AuctionLot, AuctionLotGeoCheck)
            .outerjoin(AuctionLotGeoCheck, AuctionLotGeoCheck.lot_id == AuctionLot.id)
            .where(AuctionLot.active.is_(True), AuctionLot.object_type == "land")
            .order_by(AuctionLot.id.asc())
            .limit(MAX_WORKFLOW_LOTS)
        ).all()
    keys: list[str] = []
    egkn_units: list[ProviderUnitSpec] = []
    osm_rows: list[tuple[str, float, float]] = []
    context_rows: list[tuple[str, float, float]] = []
    with session_factory() as session:
        for lot, geo in rows:
            lot = session.merge(lot, load=False)
            if lot.cadastre_number and (geo is None or _egkn_check_due(geo)):
                egkn_units.append(
                    ProviderUnitSpec(
                        unit_key=stable_unit_key(
                            "resolve", [lot.id, lot.cadastre_number]
                        ),
                        unit_kind="egkn_resolve_district",
                        input_payload={"lot_id": lot.id, "cadastre": lot.cadastre_number},
                    )
                )
            if geo is None or geo.latitude is None or geo.longitude is None:
                continue
            latitude = float(geo.latitude)
            longitude = float(geo.longitude)
            if _osm_check_due(geo):
                osm_rows.append((lot.id, latitude, longitude))
            if _egkn_context_check_due(session, lot):
                context_rows.append((lot.id, latitude, longitude))
    if egkn_units:
        key = f"{workflow_prefix}:egkn"[:128]
        create_provider_workflow(
            session_factory,
            workflow_key=key,
            provider="egkn",
            workflow_kind="cadastre",
            units=egkn_units,
            run_key=run_key,
        )
        keys.append(key)
    for layer_meta in EGKN_CONTEXT_LAYERS:
        context_units = [
            ProviderUnitSpec(
                unit_key=stable_unit_key("context", [lot_id, layer_meta["code"]]),
                unit_kind="egkn_context_layer",
                input_payload={
                    "lot_id": lot_id,
                    "latitude": latitude,
                    "longitude": longitude,
                    "layer_meta": layer_meta,
                },
            )
            for lot_id, latitude, longitude in context_rows
        ]
        if not context_units:
            continue
        key = f"{workflow_prefix}:egkn-context:{layer_meta['code']}"[:128]
        create_provider_workflow(
            session_factory,
            workflow_key=key,
            provider="egkn",
            workflow_kind="context_layer",
            units=context_units,
            run_key=run_key,
        )
        keys.append(key)
    if osm_rows:
        key = f"{workflow_prefix}:osm"[:128]
        batch_size = max(1, settings.osm_batch_size)
        units = [
            ProviderUnitSpec(
                unit_key=f"batch:{start:06d}",
                unit_kind="osm_batch",
                input_payload={
                    "rows": [list(item) for item in osm_rows[start : start + batch_size]]
                },
            )
            for start in range(0, len(osm_rows), batch_size)
        ]
        create_provider_workflow(
            session_factory,
            workflow_key=key,
            provider="osm_overpass",
            workflow_kind="site_context",
            units=units,
            run_key=run_key,
        )
        keys.append(key)
    with session_factory() as session:
        document_ids = list(
            session.scalars(
                select(AuctionDocument.id)
                .where(
                    AuctionDocument.source_url != "",
                    or_(
                        AuctionDocument.storage_status.is_(None),
                        AuctionDocument.storage_status.in_(("linked", "failed")),
                    ),
                )
                .order_by(AuctionDocument.id.asc())
                .limit(settings.auction_v2_document_download_limit)
            )
        )
    if document_ids:
        key = f"{workflow_prefix}:documents"[:128]
        units = [
            ProviderUnitSpec(
                unit_key=f"document:{document_id}",
                unit_kind="auction_document",
                input_payload={"document_id": int(document_id)},
            )
            for document_id in document_ids
        ]
        create_provider_workflow(
            session_factory,
            workflow_key=key,
            provider="auction_documents",
            workflow_kind="documents",
            units=units,
            run_key=run_key,
        )
        keys.append(key)
    return keys


def seed_gov_kz_workflow(
    session_factory: sessionmaker[Session],
    *,
    workflow_key: str,
    projects: list[str],
    detail_urls: list[str],
    max_pages: int,
    run_key: str | None = None,
) -> int:
    pages = max(1, min(int(max_pages), 1_000))
    units: list[ProviderUnitSpec] = []
    for project in projects[:50]:
        for kind in ("documents", "events"):
            for page in range(pages):
                units.append(
                    ProviderUnitSpec(
                        unit_key=stable_unit_key("gov-page", [project, kind, page]),
                        unit_kind="gov_kz_list_page",
                        input_payload={"project": project[:160], "kind": kind, "page": page},
                    )
                )
        if settings.auction_v2_gov_kz_include_news:
            units.append(
                ProviderUnitSpec(
                    unit_key=stable_unit_key("gov-news-headers", project),
                    unit_kind="gov_kz_news_headers",
                    input_payload={"project": project[:160], "max_pages": pages},
                )
            )
    for url in detail_urls[:200]:
        units.append(
            ProviderUnitSpec(
                unit_key=stable_unit_key("gov-detail", url),
                unit_kind="gov_kz_detail",
                input_payload={"source_url": url[:2048]},
            )
        )
    if not units:
        return 0
    if len(units) > 1_000:
        raise ValueError("gov.kz workflow exceeds 1000 units; split by project")
    return create_provider_workflow(
        session_factory,
        workflow_key=workflow_key,
        provider="gov_kz",
        workflow_kind="announcements",
        units=units,
        run_key=run_key,
    )


def seed_provider_barrier_noop(
    session_factory: sessionmaker[Session], *, workflow_key: str, run_key: str
) -> None:
    create_provider_workflow(
        session_factory,
        workflow_key=workflow_key,
        provider="eqazyna",
        workflow_kind="barrier_noop",
        units=[
            ProviderUnitSpec(
                unit_key="noop",
                unit_kind="provider_barrier_noop",
                input_payload={},
            )
        ],
        run_key=run_key,
    )


def seed_jerler_provider_workflow(
    session_factory: sessionmaker[Session], *, workflow_key: str, run_key: str
) -> None:
    """Seed resumable Jerler batches; per-lot evidence is the durable subcursor."""
    create_provider_workflow(
        session_factory,
        workflow_key=workflow_key,
        provider="jerler",
        workflow_kind="source_object_enrichment",
        units=[
            ProviderUnitSpec(
                unit_key="batch:000000",
                unit_kind="jerler_batch",
                input_payload={"batch": 0, "limit": 100},
            )
        ],
        run_key=run_key,
    )


def _handle_jerler_batch(
    session_factory: sessionmaker[Session], claimed: ClaimedProviderUnit
) -> tuple[str, list[ProviderUnitSpec]]:
    limit = max(1, min(int(claimed.input_payload.get("limit", 100)), 100))
    batch = max(0, min(int(claimed.input_payload.get("batch", 0)), 10_000))
    result = sync_auction_source_objects_detached(session_factory, limit=limit)
    if result.errors:
        raise ValueError(f"Jerler batch recorded {result.errors} fetch errors")
    followups: list[ProviderUnitSpec] = []
    # A full batch may leave more stale objects. Completed lots are checkpointed by
    # their fresh evidence, so a continuation never refetches them.
    if result.selected >= limit and batch < 9_999:
        next_batch = batch + 1
        followups.append(
            ProviderUnitSpec(
                unit_key=f"batch:{next_batch:06d}",
                unit_kind="jerler_batch",
                input_payload={"batch": next_batch, "limit": limit},
            )
        )
    return f"jerler:{result.selected}:{result.fetched}:{result.updated}", followups


def _district_payload(district: DistrictInfo) -> dict[str, object]:
    return asdict(district)


def _district_from_payload(payload: object) -> DistrictInfo:
    if not isinstance(payload, dict):
        raise ValueError("invalid district payload")
    return DistrictInfo(**payload)


def _handle_eqazyna_list(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderUnit,
    provider: EqazynaProvider,
) -> tuple[str, list[ProviderUnitSpec]]:
    payload = claimed.input_payload
    window_value = payload.get("publish_date_window")
    window = (
        tuple(window_value)
        if isinstance(window_value, list) and len(window_value) == 2
        else None
    )
    urls = provider.lot_url_page(
        search_status=str(payload["search_status"]),
        page=int(payload["page"]),
        publish_date_window=window,  # type: ignore[arg-type]
    )
    detail_urls = urls
    if payload.get("skip_existing_details") is True and urls:
        ids_by_url = {url: extract_source_lot_id(url) for url in urls}
        source_ids = {value for value in ids_by_url.values() if value}
        missing_date_retry_cutoff = datetime.now(UTC) - timedelta(days=30)
        with session_factory() as session:
            existing = set(
                session.scalars(
                    select(AuctionLot.source_lot_id).where(
                        AuctionLot.source == "e-qazyna",
                        AuctionLot.source_lot_id.in_(source_ids),
                        or_(
                            AuctionLot.published_at.is_not(None),
                            AuctionLot.updated_at >= missing_date_retry_cutoff,
                        ),
                    )
                )
            ) if source_ids else set()
        detail_urls = [url for url in urls if ids_by_url.get(url) not in existing]
    followups = [
        ProviderUnitSpec(
            unit_key=stable_unit_key("detail", url),
            unit_kind="eqazyna_lot_detail",
            input_payload={"source_url": url, "search_status": payload["search_status"]},
        )
        for url in detail_urls[:1_000]
    ]
    return f"urls:{len(urls)}", followups


def _handle_eqazyna_detail(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderUnit,
    provider: EqazynaProvider,
) -> str:
    lot_data = provider.lot_detail(str(claimed.input_payload["source_url"]))
    lot_data.source_search_status = str(claimed.input_payload.get("search_status") or "")
    with session_factory() as session:
        lot, created, changed = upsert_auction_lot(session, lot_data)
        session.commit()
        outcome = "created" if created else "updated" if changed else "unchanged"
        return f"auction_lot:{outcome}:{lot.id}"


def _handle_egkn_resolve(
    claimed: ClaimedProviderUnit, provider: EgknProvider
) -> tuple[str, list[ProviderUnitSpec]]:
    cadastre = str(claimed.input_payload["cadastre"])
    district = provider.resolve_district_for_cadastre(cadastre)
    followup = ProviderUnitSpec(
        unit_key=stable_unit_key("parcel", [claimed.input_payload["lot_id"], cadastre]),
        unit_kind="egkn_parcel",
        input_payload={
            "lot_id": claimed.input_payload["lot_id"],
            "cadastre": cadastre,
            "district": _district_payload(district),
        },
    )
    return f"district:{district.id}", [followup]


def _handle_egkn_parcel(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderUnit,
    provider: EgknProvider,
) -> str:
    district = _district_from_payload(claimed.input_payload["district"])
    result = provider.lookup_cadastre_direct(
        str(claimed.input_payload["cadastre"]), district=district
    )
    with session_factory() as session:
        lot = session.get(AuctionLot, str(claimed.input_payload["lot_id"]))
        source = session.scalar(
            select(AuctionSource).where(AuctionSource.code == "egkn_public_map")
        )
        if lot is None or source is None:
            raise ValueError("EGKN lot/source disappeared")
        geo = _get_or_build_geo_check(session, lot)
        _apply_egkn_lookup_result(session, lot, geo, source, result)
        session.commit()
    return f"cadastre:{result.cadastre}"


def _handle_egkn_context(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderUnit,
    provider: EgknProvider,
) -> str:
    layer_meta = claimed.input_payload.get("layer_meta")
    if not isinstance(layer_meta, dict):
        raise ValueError("invalid EGKN context layer")
    features = provider.features_around(
        layer=str(layer_meta["layer"]),
        latitude=float(claimed.input_payload["latitude"]),
        longitude=float(claimed.input_payload["longitude"]),
        radius_m=settings.auction_v2_egkn_context_radius_m,
        max_features=settings.auction_v2_egkn_context_max_features_per_layer,
    )
    with session_factory() as session:
        lot = session.get(AuctionLot, str(claimed.input_payload["lot_id"]))
        source = session.scalar(
            select(AuctionSource).where(AuctionSource.code == "egkn_public_map")
        )
        if lot is None or source is None:
            raise ValueError("EGKN context lot/source disappeared")
        _upsert_egkn_context_layer_evidence(
            session,
            lot=lot,
            source=source,
            layer_meta={str(key): str(value) for key, value in layer_meta.items()},
            status="found" if features else "missing",
            features=features,
            message=None,
        )
        session.commit()
    return f"context:{layer_meta['code']}:{len(features)}"


def _max_attempts_for_provider_failure(claimed: ClaimedProviderUnit, exc: Exception) -> int:
    if claimed.unit_kind == "jerler_batch":
        return 1
    if (
        claimed.unit_kind == "egkn_resolve_district"
        and isinstance(exc, EgknProviderError)
        and "Некорректный кадастровый номер" in str(exc)
    ):
        return 1
    return 3


def _handle_osm_batch(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderUnit,
    provider: OsmProvider,
) -> str:
    rows = claimed.input_payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("invalid OSM workflow rows")
    points = [(float(row[1]), float(row[2])) for row in rows]
    results = provider.analyze_batch(points, radius_m=settings.auction_v2_osm_radius_m)
    with session_factory() as session:
        now = datetime.now(UTC)
        for row, surroundings in zip(rows, results, strict=True):
            lot = session.get(AuctionLot, str(row[0]))
            if lot is None:
                continue
            geo = _get_or_build_geo_check(session, lot)
            _apply_osm_surroundings(geo, surroundings, checked_at=now)
        session.commit()
    return f"osm:{len(results)}"


def _handle_auction_document(
    session_factory: sessionmaker[Session], claimed: ClaimedProviderUnit
) -> str:
    document_id = int(claimed.input_payload["document_id"])
    with session_factory() as session:
        result = sync_auction_v2_documents(
            session,
            limit=1,
            enabled=True,
            document_ids=[document_id],
        )
    if result.errors:
        raise ValueError(f"auction document {document_id} failed")
    return f"document:{document_id}:{result.downloaded}"


def _persist_gov_announcements(
    session_factory: sessionmaker[Session], announcements: list[GovKzAnnouncement]
) -> int:
    with session_factory() as session:
        source = session.scalar(
            select(AuctionSource).where(AuctionSource.code == "gov_kz_akimat_announcements")
        )
        if source is None:
            raise ValueError("gov.kz source is missing")
        lots = list(
            session.scalars(
                select(AuctionLot)
                .where(AuctionLot.active.is_(True), AuctionLot.object_type == "land")
                .order_by(AuctionLot.id.asc())
                .limit(MAX_WORKFLOW_LOTS)
            )
        )
        matches = 0
        for announcement in announcements:
            for lot in lots:
                confidence, reasons = _gov_kz_lot_match(lot, announcement)
                if confidence < 0.45:
                    continue
                _upsert_evidence(
                    session,
                    lot=lot,
                    source=source,
                    evidence_type="akimat_announcement",
                    title=announcement.title,
                    status="found",
                    value_text=_gov_kz_evidence_text(announcement, reasons),
                    source_url=announcement.source_url,
                    confidence=confidence,
                    raw_payload_json=json.dumps(announcement.as_dict(), ensure_ascii=False),
                )
                _upsert_gov_kz_attachments(session, lot, announcement)
                matches += 1
        session.commit()
        return matches


def _handle_gov_list(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderUnit,
    provider: GovKzProvider,
) -> str:
    payload = claimed.input_payload
    headers = payload.get("request_headers")
    if headers is not None and not isinstance(headers, dict):
        raise ValueError("invalid gov.kz headers")
    items = provider.list_items_page(
        kind=str(payload["kind"]),
        project=str(payload["project"]),
        page=int(payload["page"]),
        size=settings.auction_v2_gov_kz_page_size,
        request_headers=headers,  # type: ignore[arg-type]
    )
    announcements = []
    for item in items:
        announcement = provider._announcement_from_item(
            item,
            kind=str(payload["kind"]),
            project=str(payload["project"]),
        )
        if announcement is not None:
            announcements.append(announcement)
    matches = _persist_gov_announcements(session_factory, announcements)
    return f"items:{len(items)};matches:{matches}"


def _handle_gov_news_headers(
    claimed: ClaimedProviderUnit, provider: GovKzProvider
) -> tuple[str, list[ProviderUnitSpec]]:
    headers = provider.news_headers_unit()
    project = str(claimed.input_payload["project"])
    pages = max(1, min(int(claimed.input_payload["max_pages"]), 1_000))
    followups = [
        ProviderUnitSpec(
            unit_key=stable_unit_key("gov-page", [project, "news", page]),
            unit_kind="gov_kz_list_page",
            input_payload={
                "project": project,
                "kind": "news",
                "page": page,
                "request_headers": headers,
            },
        )
        for page in range(pages)
    ]
    return "news-headers", followups


def _handle_gov_detail(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderUnit,
    provider: GovKzProvider,
) -> str:
    announcement = provider.fetch_detail_url(str(claimed.input_payload["source_url"]))
    matches = _persist_gov_announcements(session_factory, [announcement])
    return f"detail-matches:{matches}"


def process_provider_workflow_step(
    session_factory: sessionmaker[Session],
    *,
    workflow_key: str,
    eqazyna: EqazynaProvider | None = None,
    egkn: EgknProvider | None = None,
    osm: OsmProvider | None = None,
    gov_kz: GovKzProvider | None = None,
) -> ProviderWorkflowStepResult:
    claimed = claim_provider_unit(session_factory, workflow_key=workflow_key)
    if claimed is None:
        pending = provider_workflow_pending(session_factory, workflow_key)
        return ProviderWorkflowStepResult(
            workflow_key, "complete" if pending == 0 else "waiting", None, pending
        )
    try:
        followups: list[ProviderUnitSpec] = []
        if claimed.unit_kind == "eqazyna_list_page":
            result_ref, followups = _handle_eqazyna_list(
                session_factory, claimed, eqazyna or EqazynaProvider()
            )
        elif claimed.unit_kind == "eqazyna_lot_detail":
            result_ref = _handle_eqazyna_detail(
                session_factory, claimed, eqazyna or EqazynaProvider()
            )
        elif claimed.unit_kind == "egkn_resolve_district":
            result_ref, followups = _handle_egkn_resolve(claimed, egkn or EgknProvider())
        elif claimed.unit_kind == "egkn_parcel":
            result_ref = _handle_egkn_parcel(session_factory, claimed, egkn or EgknProvider())
        elif claimed.unit_kind == "egkn_context_layer":
            result_ref = _handle_egkn_context(
                session_factory, claimed, egkn or EgknProvider()
            )
        elif claimed.unit_kind == "osm_batch":
            result_ref = _handle_osm_batch(session_factory, claimed, osm or OsmProvider())
        elif claimed.unit_kind == "auction_document":
            result_ref = _handle_auction_document(session_factory, claimed)
        elif claimed.unit_kind == "jerler_batch":
            result_ref, followups = _handle_jerler_batch(session_factory, claimed)
        elif claimed.unit_kind == "gov_kz_list_page":
            provider = gov_kz or GovKzProvider()
            try:
                result_ref = _handle_gov_list(session_factory, claimed, provider)
            finally:
                if gov_kz is None:
                    provider.close()
        elif claimed.unit_kind == "gov_kz_news_headers":
            provider = gov_kz or GovKzProvider()
            try:
                result_ref, followups = _handle_gov_news_headers(claimed, provider)
            finally:
                if gov_kz is None:
                    provider.close()
        elif claimed.unit_kind == "gov_kz_detail":
            provider = gov_kz or GovKzProvider()
            try:
                result_ref = _handle_gov_detail(session_factory, claimed, provider)
            finally:
                if gov_kz is None:
                    provider.close()
        elif claimed.unit_kind == "provider_barrier_noop":
            result_ref = "noop"
        else:
            raise ValueError(f"unsupported provider unit: {claimed.unit_kind}")
    except (ProviderCallDeferred, JerlerEnrichmentDeferred) as exc:
        defer_provider_unit(
            session_factory,
            claimed,
            retry_after_seconds=exc.retry_after_seconds,
            error=f"{exc.provider}:{exc.reason}",
        )
        return ProviderWorkflowStepResult(
            workflow_key,
            "deferred",
            claimed.unit_kind,
            provider_workflow_pending(session_factory, workflow_key),
            exc.retry_after_seconds,
        )
    except (EqazynaError, EgknProviderError, OsmProviderError, GovKzError, ValueError) as exc:
        failure_status = fail_provider_unit(
            session_factory,
            claimed,
            error=f"{type(exc).__name__}:{exc}",
            max_attempts=_max_attempts_for_provider_failure(claimed, exc),
        )
        pending = provider_workflow_pending(session_factory, workflow_key)
        return ProviderWorkflowStepResult(
            workflow_key,
            failure_status,
            claimed.unit_kind,
            pending,
            60 if failure_status == "error" else None,
        )
    try:
        complete_provider_unit(
            session_factory,
            claimed,
            result_ref=result_ref,
            followup_units=followups,
        )
    except ValueError as exc:
        failure_status = fail_provider_unit(
            session_factory, claimed, error=f"workflow_bound:{exc}", max_attempts=1
        )
        return ProviderWorkflowStepResult(
            workflow_key,
            failure_status,
            claimed.unit_kind,
            provider_workflow_pending(session_factory, workflow_key),
        )
    pending = provider_workflow_pending(session_factory, workflow_key)
    return ProviderWorkflowStepResult(
        workflow_key, "complete" if pending == 0 else "progress", claimed.unit_kind, pending
    )
