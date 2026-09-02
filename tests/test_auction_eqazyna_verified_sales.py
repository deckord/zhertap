from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.auction_eqazyna_verified_sales import (
    EqazynaSaleSourceRow,
    build_eqazyna_verified_sale_fact,
    ingest_eqazyna_verified_sales_batch,
    load_global_market_target_inputs,
    recompute_market_from_global_inventory,
)
from app.db import Base
from app.models import (
    AuctionEvidence,
    AuctionHistoryGeneration,
    AuctionHistoryNormalized,
    AuctionLandObject,
    AuctionLot,
    AuctionMarketInventoryGeneration,
    AuctionSource,
    AuctionVerifiedComparableCurrent,
    AuctionVerifiedComparableObservation,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'eqazyna-sales.sqlite3'}")
    Base.metadata.create_all(
        engine,
        tables=[
            AuctionLot.__table__,
            AuctionSource.__table__,
            AuctionEvidence.__table__,
            AuctionHistoryGeneration.__table__,
            AuctionHistoryNormalized.__table__,
            AuctionLandObject.__table__,
            AuctionVerifiedComparableObservation.__table__,
            AuctionVerifiedComparableCurrent.__table__,
            AuctionMarketInventoryGeneration.__table__,
        ],
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def _lot(index: int, *, target: bool = False, **changes) -> AuctionLot:
    values = {
        "id": f"00000000-0000-0000-0000-{index:012d}",
        "source": "e-qazyna",
        "source_lot_id": str(452_000 + index),
        "source_url": f"https://e-qazyna.kz/lot/{452_000 + index}",
        "title": "Строительство кемпинга",
        "purpose": "строительство кемпинга",
        "object_type": "land",
        "status": "SuccessProtocolSigned" if not target else "ApplicationsAcceptance",
        "source_search_status": "SuccessProtocolSigned" if not target else "active",
        "area_ha": 1.0,
        "land_rights": "временное землепользование",
        "lease_term_years": 3.0,
        "sale_price_kzt": None if target else 10_000_000 + index * 100_000,
        "auction_starts_at": NOW - timedelta(days=20 + index),
        "region": "Абай",
        "district": "Жаңасемей",
        "locality": "Семей",
        "land_object_id": f"land-{index}",
        "active": target,
        "updated_at": NOW,
    }
    values.update(changes)
    return AuctionLot(**values)


def _history(lot: AuctionLot, *, success: bool = True, **changes) -> AuctionHistoryNormalized:
    values = {
        "generation": 1,
        "lot_id": lot.id,
        "normalization_version": "auction-history-normalized.v1",
        "normalization_key": f"{int(lot.source_lot_id):064x}",
        "right_kind": "lease",
        "right_status": "found",
        "purpose_group": "camping",
        "purpose_status": "found",
        "lease_band": "short_3",
        "lease_status": "found",
        "event_date": lot.auction_starts_at.date(),
        "event_date_status": "found",
        "outcome": "success" if success else "unresolved",
        "outcome_status": "found" if success else "unknown",
        "area_ha": Decimal("1"),
        "area_status": "found",
        "start_price_kzt": Decimal("1000000"),
        "start_price_status": "found",
        "sale_price_kzt": (
            Decimal(str(lot.sale_price_kzt)) if lot.sale_price_kzt is not None else None
        ),
        "sale_price_status": "found" if lot.sale_price_kzt is not None else "unknown",
        "sale_to_start_ratio": Decimal("10") if success else None,
        "start_price_per_ha_kzt": Decimal("1000000"),
        "sale_price_per_ha_kzt": (
            Decimal(str(lot.sale_price_kzt)) if lot.sale_price_kzt is not None else None
        ),
        "region_key": "Абай",
        "district_key": "Жаңасемей",
        "locality_key": "Семей",
        "source_updated_at": lot.auction_starts_at,
        "issues_json": "[]",
        "normalized_at": NOW,
    }
    values.update(changes)
    return AuctionHistoryNormalized(**values)


def _geometry(lon: float, lat: float) -> dict[str, object]:
    delta = 0.0002
    return {
        "geometry_geojson": {
            "type": "Polygon",
            "coordinates": [
                [
                    [lon - delta, lat - delta],
                    [lon + delta, lat - delta],
                    [lon + delta, lat + delta],
                    [lon - delta, lat + delta],
                    [lon - delta, lat - delta],
                ]
            ],
        }
    }


def _evidence(lot: AuctionLot, lon: float, lat: float, *, include_geometry=True) -> list:
    site = {
        "physical_access": {"readiness": "ready"},
        "legal_access": {"readiness": "ready"},
        "infrastructure": {"readiness": "ready"},
    }
    rows = [
        AuctionEvidence(
            lot_id=lot.id,
            evidence_type="decision_input:site_context",
            status="found",
            title="W6",
            value_text="a" * 64,
            raw_payload_json=json.dumps(site),
            observed_at=NOW,
        )
    ]
    if include_geometry:
        rows.append(
            AuctionEvidence(
                lot_id=lot.id,
                evidence_type="cadastre_boundary",
                status="found",
                title="ЕГКН",
                raw_payload_json=json.dumps(_geometry(lon, lat)),
                observed_at=NOW,
            )
        )
    return rows


def _generation() -> AuctionHistoryGeneration:
    return AuctionHistoryGeneration(
        generation=1,
        normalization_version="auction-history-normalized.v1",
        status="active",
        source_cutoff=NOW,
        source_high_water_lot_id=None,
        expected_count=0,
        processed_count=0,
        error_count=0,
        scan_complete=True,
        started_at=NOW,
        completed_at=NOW,
        activated_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def test_verified_sale_accepts_exact_canonical_jerler_polygon_without_cadastre() -> None:
    lot = _lot(1)
    history = _history(lot)
    row = EqazynaSaleSourceRow(
        lot_id=lot.id,
        source_lot_id=lot.source_lot_id,
        source_url=lot.source_url,
        status=lot.status,
        source_search_status=lot.source_search_status,
        land_object_id=None,
        cadastre_number=None,
        lease_term_years=lot.lease_term_years,
        auction_starts_at=lot.auction_starts_at,
        title=lot.title,
        locality=lot.locality,
        generation=history.generation,
        normalization_key=history.normalization_key,
        right_kind=history.right_kind,
        right_status=history.right_status,
        purpose_group=history.purpose_group,
        purpose_status=history.purpose_status,
        lease_status=history.lease_status,
        event_date=history.event_date,
        event_date_status=history.event_date_status,
        outcome=history.outcome,
        outcome_status=history.outcome_status,
        area_ha=history.area_ha,
        area_status=history.area_status,
        sale_price_kzt=history.sale_price_kzt,
        sale_price_status=history.sale_price_status,
        canonical_object_id="canonical-land-object-1",
        canonical_boundary_geojson=json.dumps(_geometry(80.2275, 50.4111)["geometry_geojson"]),
        canonical_boundary_source="jerler:source_object",
    )
    site = {
        "physical_access": {"readiness": "ready"},
        "legal_access": {"readiness": "ready"},
        "infrastructure": {"readiness": "ready"},
    }
    fact, _generation_signature = build_eqazyna_verified_sale_fact(
        row,
        cadastre_status="missing",
        cadastre_payload={},
        site_status="found",
        site_payload=site,
    )
    assert fact is not None
    assert fact.object_id == "canonical-land-object-1"
    assert "parcel_geometry:jerler:source_object" in fact.provenance_refs


def test_verified_sale_without_site_readiness_is_inventory_reference_not_inferred_ready() -> None:
    lot = _lot(11)
    history = _history(lot)
    row = EqazynaSaleSourceRow(
        lot_id=lot.id,
        source_lot_id=lot.source_lot_id,
        source_url=lot.source_url,
        status=lot.status,
        source_search_status=lot.source_search_status,
        land_object_id=lot.land_object_id,
        cadastre_number=None,
        lease_term_years=lot.lease_term_years,
        auction_starts_at=lot.auction_starts_at,
        title=lot.title,
        locality=lot.locality,
        generation=history.generation,
        normalization_key=history.normalization_key,
        right_kind=history.right_kind,
        right_status=history.right_status,
        purpose_group=history.purpose_group,
        purpose_status=history.purpose_status,
        lease_status=history.lease_status,
        event_date=history.event_date,
        event_date_status=history.event_date_status,
        outcome=history.outcome,
        outcome_status=history.outcome_status,
        area_ha=history.area_ha,
        area_status=history.area_status,
        sale_price_kzt=history.sale_price_kzt,
        sale_price_status=history.sale_price_status,
    )
    fact, _signature = build_eqazyna_verified_sale_fact(
        row,
        cadastre_status="found",
        cadastre_payload=_geometry(80.2275, 50.4111),
        site_status=None,
        site_payload={},
    )
    assert fact is not None
    assert fact.verification_status == "verified"
    assert fact.access_readiness == "unknown"
    assert fact.infrastructure_readiness == "unknown"
    assert "site_readiness:unknown" in fact.provenance_refs


def test_missing_final_price_coordinates_or_right_never_becomes_verified(tmp_path) -> None:
    factory = _factory(tmp_path)
    no_price = _lot(1, sale_price_kzt=None)
    no_geo = _lot(2)
    no_right = _lot(3)
    failed = _lot(4, status="FailureProtocolSigned", source_search_status="не состоялся")
    with factory() as session, session.begin():
        session.add(_generation())
        session.add_all((no_price, no_geo, no_right, failed))
        session.flush()
        session.add_all(
            (
                _history(no_price, sale_price_kzt=None, sale_price_status="unknown"),
                _history(no_geo),
                _history(no_right, right_kind="unknown", right_status="unknown"),
                _history(failed),
            )
        )
        session.add_all(_evidence(no_price, 80.2275, 50.4111))
        session.add_all(_evidence(no_geo, 80.2280, 50.4111, include_geometry=False))
        session.add_all(_evidence(no_right, 80.2285, 50.4111))
        session.add_all(_evidence(failed, 80.2290, 50.4111))
    result = ingest_eqazyna_verified_sales_batch(factory)
    assert result.selected == 4
    assert result.ingested == 0
    assert result.rejected == 4
    assert result.rejection_reasons["official_success_status_missing_or_conflict"] == 1
    with factory() as session:
        current_count = session.scalar(
            select(func.count(AuctionVerifiedComparableCurrent.observation_id))
        )
        assert current_count == 0


def test_repeat_ingest_is_idempotent_and_unresolved_listing_is_excluded(tmp_path) -> None:
    factory = _factory(tmp_path)
    sold = _lot(1)
    listing = _lot(2, target=True)
    with factory() as session, session.begin():
        session.add(_generation())
        session.add_all((sold, listing))
        session.flush()
        session.add_all((_history(sold), _history(listing, success=False)))
        session.add_all(_evidence(sold, 80.2275, 50.4111))
        session.add_all(_evidence(listing, 80.2280, 50.4111))
    first = ingest_eqazyna_verified_sales_batch(factory)
    second = ingest_eqazyna_verified_sales_batch(factory)
    assert first.ingested == 1 and first.rejected == 1
    assert second.ingested == 0 and second.unchanged == 1 and second.rejected == 1


def test_three_nearby_verified_sales_produce_strict_w9_estimate_and_exclude_same_object(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    sales = [_lot(index) for index in range(1, 5)]
    target = _lot(10, target=True, land_object_id="land-4", auction_starts_at=NOW)
    coordinates = (
        (80.2275, 50.4111),
        (80.2280, 50.4113),
        (80.2290, 50.4115),
        # Same canonical object as target: must be excluded before LIMIT/W9.
        (80.2276, 50.4112),
    )
    with factory() as session, session.begin():
        session.add(_generation())
        session.add_all((*sales, target))
        session.flush()
        session.add_all(_history(lot) for lot in sales)
        for lot, (lon, lat) in zip(sales, coordinates, strict=True):
            session.add_all(_evidence(lot, lon, lat))
        session.add_all(_evidence(target, 80.2275, 50.4111))
    batch = ingest_eqazyna_verified_sales_batch(factory)
    assert batch.ingested == 4
    result = recompute_market_from_global_inventory(factory, target.id, observed_at=NOW)
    assert result.status == "ok"
    with factory() as session:
        evidence = session.scalar(
            select(AuctionEvidence)
            .where(AuctionEvidence.evidence_type == "strict_market_estimate")
            .order_by(AuctionEvidence.id.desc())
        )
    payload = json.loads(evidence.raw_payload_json)
    assert payload["high_quality_verified_count"] == 3
    assert payload["estimate"]["verified_comparables_used"] == 3


def test_target_without_authoritative_coordinates_persists_insufficient_not_exception(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    target = _lot(10, target=True, auction_starts_at=NOW)
    with factory() as session, session.begin():
        session.add(_generation())
        session.add(target)
        session.flush()
        session.add_all(_evidence(target, 80.2275, 50.4111, include_geometry=False))
    result = recompute_market_from_global_inventory(factory, target.id, observed_at=NOW)
    assert result.status == "insufficient_data"
    with factory() as session:
        evidence = session.scalar(
            select(AuctionEvidence)
            .where(AuctionEvidence.evidence_type == "strict_market_estimate")
            .order_by(AuctionEvidence.id.desc())
        )
    assert json.loads(evidence.raw_payload_json)["estimate"] is None


def test_unknown_target_readiness_keeps_estimate_blocked_but_selects_verified_sales(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    sales = [_lot(index) for index in range(1, 4)]
    target = _lot(10, target=True, auction_starts_at=NOW)
    with factory() as session, session.begin():
        session.add(_generation())
        session.add_all((*sales, target))
        session.flush()
        session.add_all(_history(lot) for lot in sales)
        for index, lot in enumerate(sales):
            session.add_all(_evidence(lot, 80.2275 + index * 0.001, 50.4111))
        target_evidence = _evidence(target, 80.2275, 50.4111)
        target_evidence[0].raw_payload_json = "{}"
        session.add_all(target_evidence)

    assert ingest_eqazyna_verified_sales_batch(factory).ingested == 3
    result = recompute_market_from_global_inventory(factory, target.id, observed_at=NOW)
    assert result.status == "insufficient_data"
    with factory() as session:
        evidence = session.scalar(
            select(AuctionEvidence)
            .where(AuctionEvidence.evidence_type == "strict_market_estimate")
            .order_by(AuctionEvidence.id.desc())
        )
    payload = json.loads(evidence.raw_payload_json)
    assert payload["estimate"] is None
    assert payload["current_source_row_ids"]
    assert payload["inventory_scope"] == {
        "global_geo_selection_performed": True,
        "kind": "global_verified_comparable_inventory",
        "provider_ingest_performed": False,
    }
    assert payload["target_missing_reasons"] == [
        "access_readiness_unknown",
        "infrastructure_readiness_unknown",
    ]


def test_historical_evidence_is_reduced_to_one_row_per_lot_type_in_sql(tmp_path) -> None:
    factory = _factory(tmp_path)
    sold = _lot(1)
    with factory() as session, session.begin():
        session.add(_generation())
        session.add(sold)
        session.flush()
        session.add(_history(sold))
        for index in range(60):
            for row in _evidence(sold, 80.2275, 50.4111):
                row.observed_at = NOW - timedelta(days=60 - index)
                session.add(row)
    engine = factory.kw["bind"]
    selects = 0

    def count_selects(_conn, _cursor, statement, _params, _context, _many):
        nonlocal selects
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        result = ingest_eqazyna_verified_sales_batch(factory)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)
    assert result.ingested == 1
    assert selects <= 7


def test_market_target_batch_projection_has_constant_query_count(tmp_path) -> None:
    factory = _factory(tmp_path)
    lots = [_lot(index, target=True) for index in range(1, 26)]
    with factory() as session, session.begin():
        session.add_all(lots)
    engine = factory.kw["bind"]
    selects = 0

    def count_selects(_conn, _cursor, statement, _params, _context, _many):
        nonlocal selects
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        targets = load_global_market_target_inputs(factory, [lot.id for lot in lots])
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)
    assert len(targets) == 25
    assert selects == 3


def test_market_target_batch_normalizes_lease_without_term_to_unknown_right(tmp_path) -> None:
    factory = _factory(tmp_path)
    lot = _lot(1, target=True, lease_term_years=None)
    with factory() as session, session.begin():
        session.add(lot)

    target = load_global_market_target_inputs(factory, [lot.id])[0]

    assert target.right_type is None
    assert target.lease_term_years is None
