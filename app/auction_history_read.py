from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, and_, case, cast, func, select
from sqlalchemy.orm import Session

from app.auction_history_normalization import (
    RawAuctionHistoryRecord,
    SimilarHistoryAggregate,
    SimilarHistoryTarget,
    build_similar_history_query_spec,
    normalize_history_record,
)
from app.auction_history_store import SqlAlchemyHistoryNormalizationStore
from app.models import AuctionHistoryGeneration, AuctionHistoryNormalized, AuctionLot


def _raw_target(lot: AuctionLot) -> RawAuctionHistoryRecord:
    return RawAuctionHistoryRecord(
        lot_id=lot.id,
        source_updated_at=lot.updated_at,
        status=lot.status,
        source_search_status=lot.source_search_status,
        land_rights=lot.land_rights,
        purpose=lot.purpose,
        title=lot.title,
        use_goal=lot.use_goal,
        functional_purpose=lot.functional_purpose_level4
        or lot.functional_purpose_level3
        or lot.functional_purpose_level2,
        purpose_claims=tuple(
            value
            for value in (
                lot.functional_purpose_level2,
                lot.functional_purpose_level3,
                lot.functional_purpose_level4,
            )
            if value
        ),
        lease_term_years=lot.lease_term_years,
        auction_starts_at=lot.auction_starts_at,
        published_at=lot.published_at,
        area_ha=lot.area_ha,
        start_price_kzt=lot.start_price_kzt,
        sale_price_kzt=lot.sale_price_kzt,
        region=lot.region,
        district=lot.district,
        locality=lot.locality,
    )


def _target_object_lot_ids(lot: AuctionLot) -> object:
    # Local import avoids the read-path/history facade dependency cycle.
    from app.auction_history import auction_object_identity

    identity = auction_object_identity(lot)
    if identity.kind == "land_object_id" and identity.value is not None:
        condition = AuctionLot.land_object_id == identity.value
    elif identity.kind == "source_object_url" and lot.source_object_url:
        condition = AuctionLot.source_object_url == lot.source_object_url
    elif identity.kind == "cadastre_number" and identity.value is not None:
        condition = and_(
            AuctionLot.land_object_id.is_(None),
            AuctionLot.cadastre_number == identity.value,
        )
    else:
        condition = AuctionLot.id == lot.id
    return select(AuctionLot.id).where(condition)


def _median_scalar(base: object, column_name: str, predicate: object) -> object:
    column = getattr(base.c, column_name)
    ranked = (
        select(
            column.label("value"),
            func.row_number().over(order_by=column.asc()).label("position"),
            func.count().over().label("total"),
        )
        .where(predicate, column.is_not(None))
        .cte(f"ranked_{column_name}")
    )
    lower = cast((ranked.c.total + 1) / 2, Integer)
    upper = cast((ranked.c.total + 2) / 2, Integer)
    return (
        select(func.avg(ranked.c.value))
        .where(ranked.c.position.in_((lower, upper)))
        .scalar_subquery()
    )


def _median_float(values: list[object]) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _normalize_geography_filter(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).casefold()
    return normalized or None


def annual_history_cohorts(
    session: Session,
    *,
    region_key: str | None = None,
    district_key: str | None = None,
    locality_key: str | None = None,
) -> dict[str, object]:
    """Return nominal historic observations separated strictly by event year."""
    generation = session.scalar(
        select(AuctionHistoryGeneration)
        .where(AuctionHistoryGeneration.status == "active")
        .order_by(AuctionHistoryGeneration.generation.desc())
        .limit(1)
    )
    if generation is None:
        return {"status": "insufficient_data", "historical_only": True, "rows": [], "generation": None}
    # Annual analytics is an archive view, not a current-market aggregate.
    # Joining the source lot and requiring terminal E-Qazyna statuses prevents
    # ApplicationsAccept/Pending/Running rows (even stale inactive snapshots)
    # from being mixed into historical nominal-price cohorts.
    historical_statuses = (
        "SuccessProtocolSigned",
        "FailureProtocolSigned",
        "NullifyResultProtocolSigned",
        "CancelBeforeStart",
    )
    conditions = [
        AuctionHistoryNormalized.generation == generation.generation,
        AuctionHistoryNormalized.event_date.is_not(None),
        AuctionHistoryNormalized.event_date_status == "found",
        AuctionLot.source_search_status.in_(historical_statuses),
    ]
    for column, value in (
        (AuctionHistoryNormalized.region_key, region_key),
        (AuctionHistoryNormalized.district_key, district_key),
        (AuctionHistoryNormalized.locality_key, locality_key),
    ):
        normalized_value = _normalize_geography_filter(value)
        if normalized_value:
            conditions.append(column == normalized_value)
    records = session.execute(
        select(
            AuctionHistoryNormalized.event_date,
            AuctionHistoryNormalized.start_price_kzt,
            AuctionHistoryNormalized.sale_price_kzt,
            AuctionHistoryNormalized.start_price_per_ha_kzt,
            AuctionHistoryNormalized.sale_price_per_ha_kzt,
            AuctionHistoryNormalized.outcome,
            AuctionHistoryNormalized.outcome_status,
            AuctionHistoryNormalized.sale_price_status,
        )
        .join(AuctionLot, AuctionLot.id == AuctionHistoryNormalized.lot_id)
        .where(*conditions)
    ).all()
    buckets: dict[int, list[object]] = {}
    for record in records:
        buckets.setdefault(record.event_date.year, []).append(record)
    rows: list[dict[str, object]] = []
    for year in sorted(buckets, reverse=True):
        values = buckets[year]
        confirmed = [item for item in values if item.outcome == "success" and item.outcome_status == "found" and item.sale_price_status == "found"]
        rows.append({
            "year": year,
            "count": len(values),
            "median_start_price_kzt": _median_float([item.start_price_kzt for item in values]),
            "median_sale_price_kzt": _median_float([item.sale_price_kzt for item in confirmed]),
            "median_start_price_per_sotka": (_median_float([item.start_price_per_ha_kzt for item in values]) / 100 if _median_float([item.start_price_per_ha_kzt for item in values]) is not None else None),
            "median_sale_price_per_sotka": (_median_float([item.sale_price_per_ha_kzt for item in confirmed]) / 100 if _median_float([item.sale_price_per_ha_kzt for item in confirmed]) is not None else None),
            "successful_count": len(confirmed),
            "success_percent": round(len(confirmed) * 100 / len(values), 1) if values else 0.0,
            "date_from": min(item.event_date for item in values),
            "date_to": max(item.event_date for item in values),
        })
    return {
        "status": "ok" if rows else "insufficient_data",
        "historical_only": True,
        "generation": generation.generation,
        "source_cutoff": generation.source_cutoff,
        "activated_at": generation.activated_at,
        "rows": rows,
    }


def normalized_similar_history(
    session: Session,
    lot: AuctionLot,
    *,
    lookback_days: int = 365,
) -> tuple[int | None, SimilarHistoryAggregate]:
    """Return one aggregate row from the active materialized generation only."""
    store = SqlAlchemyHistoryNormalizationStore(session)
    active = store.get_active_generation()
    if active is None:
        return None, SimilarHistoryAggregate(
            "insufficient_data", 0, 0, 0, 0, 0, None, None, None, None, None,
            "no_active_normalized_generation",
        )
    target_row = normalize_history_record(_raw_target(lot), generation=active.generation)
    today = datetime.now(UTC).date()
    target = SimilarHistoryTarget(
        lot_id=lot.id,
        right_kind=target_row.right_kind,
        purpose_group=target_row.purpose_group,
        lease_band=target_row.lease_band,
        area_ha=target_row.area_ha or 0,
        region_key=target_row.region_key,
        district_key=target_row.district_key,
        locality_key=target_row.locality_key,
        event_date_from=today - timedelta(days=max(1, min(lookback_days, 3650))),
        event_date_to=today,
    )
    try:
        spec = build_similar_history_query_spec(target, store)
    except ValueError as exc:
        return active.generation, SimilarHistoryAggregate(
            "insufficient_data", 0, 0, 0, 0, 0, None, None, None, None, None,
            str(exc)[:200],
        )

    conditions = [
        AuctionHistoryNormalized.generation == spec.generation,
        AuctionHistoryNormalized.lot_id.not_in(_target_object_lot_ids(lot)),
        AuctionHistoryNormalized.right_kind == spec.right_kind,
        AuctionHistoryNormalized.purpose_group == spec.purpose_group,
        getattr(AuctionHistoryNormalized, spec.geography_column) == spec.geography_value,
        AuctionHistoryNormalized.area_ha >= spec.area_min_ha,
        AuctionHistoryNormalized.area_ha <= spec.area_max_ha,
    ]
    if spec.lease_band is not None:
        conditions.append(AuctionHistoryNormalized.lease_band == spec.lease_band)
    for status_column, expected in spec.eligibility_statuses:
        conditions.append(getattr(AuctionHistoryNormalized, status_column) == expected)
    if spec.event_date_from is not None:
        conditions.append(AuctionHistoryNormalized.event_date >= spec.event_date_from)
    if spec.event_date_to is not None:
        conditions.append(AuctionHistoryNormalized.event_date <= spec.event_date_to)

    base = select(AuctionHistoryNormalized).where(*conditions).cte("eligible_history")
    strict_sale = (
        (base.c.outcome == "success")
        & (base.c.outcome_status == "found")
        & (base.c.sale_price_status == "found")
    )
    row = session.execute(
        select(
            func.count(base.c.lot_id),
            func.sum(case((base.c.outcome == "success", 1), else_=0)),
            func.sum(case((base.c.outcome == "failure", 1), else_=0)),
            func.sum(case((base.c.outcome == "unresolved", 1), else_=0)),
            func.sum(case((base.c.outcome == "conflict", 1), else_=0)),
            _median_scalar(base, "start_price_kzt", base.c.start_price_status == "found"),
            _median_scalar(base, "sale_price_kzt", strict_sale),
            _median_scalar(base, "sale_to_start_ratio", strict_sale),
            _median_scalar(
                base,
                "start_price_per_ha_kzt",
                base.c.start_price_status == "found",
            ),
            _median_scalar(base, "sale_price_per_ha_kzt", strict_sale),
        )
    ).one()
    matched = int(row[0] or 0)
    return active.generation, SimilarHistoryAggregate(
        status="ok" if matched else "insufficient_data",
        matched_count=matched,
        successful_count=int(row[1] or 0),
        failed_count=int(row[2] or 0),
        unresolved_count=int(row[3] or 0),
        conflict_count=int(row[4] or 0),
        median_start_price_kzt=float(row[5]) if row[5] is not None else None,
        median_sale_price_kzt=float(row[6]) if row[6] is not None else None,
        median_sale_to_start_ratio=float(row[7]) if row[7] is not None else None,
        median_start_price_per_ha_kzt=float(row[8]) if row[8] is not None else None,
        median_sale_price_per_ha_kzt=float(row[9]) if row[9] is not None else None,
        detail=None if matched else "no_strict_matches",
    )
