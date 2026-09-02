"""Conservative canonical identity for recurring auction land lots."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auction_parcel_geometry import (
    GeometryLimits,
    GeometryValidationError,
    validate_parcel_geojson,
)
from app.models import AuctionLandObject, AuctionLot


def _clean(value: str | None) -> str | None:
    normalized = " ".join((value or "").strip().split())
    return normalized or None


def _jerler_object_id(value: str | None) -> str | None:
    parsed = urlparse(value or "")
    if parsed.scheme != "https" or parsed.hostname != "jerler.e-qazyna.kz":
        return None
    # Jerler currently emits both cadastral-like long identifiers and short
    # numeric registry IDs (for example /ru/guest/reestr/objects/list/17830/view).
    # The exact official host and route make the short ID a stable source key;
    # requiring 12 digits leaves otherwise linkable live lots orphaned.
    match = re.fullmatch(
        r"(?:/[a-z]{2}/guest/reestr)?/objects/list/(\d{1,32})/(?:view/?)?",
        parsed.path,
    )
    return match.group(1) if match else None


def _official_cadastre(value: str | None) -> str | None:
    cadastre = _clean(value)
    if cadastre is None or "_" in cadastre:
        return None
    # Partial masks and punctuation placeholders are not a stable object key.
    if len(re.sub(r"\D", "", cadastre)) < 8:
        return None
    return cadastre


def reconcile_lot_land_object(session: Session, lot: AuctionLot) -> AuctionLandObject | None:
    """Attach a lot to a canonical object only on exact official identifiers."""
    egkn_id = _clean(lot.land_object_id)
    cadastre_number = _official_cadastre(lot.cadastre_number)
    jerler_object_id = _jerler_object_id(lot.source_object_url)
    if not egkn_id and not cadastre_number and not jerler_object_id:
        return None

    matched_objects: list[AuctionLandObject] = []
    for column, value in (
        (AuctionLandObject.egkn_id, egkn_id),
        (AuctionLandObject.cadastre_number, cadastre_number),
        (AuctionLandObject.jerler_object_id, jerler_object_id),
    ):
        if not value:
            continue
        matched = session.scalar(select(AuctionLandObject).where(column == value))
        if matched is not None:
            matched_objects.append(matched)

    # Never guess which canonical object wins when official identifiers point
    # at different existing rows. Keep the lot unlinked for explicit review.
    if len({matched.id for matched in matched_objects}) > 1:
        lot.land_object_ref_id = None
        return None

    object_row = matched_objects[0] if matched_objects else None
    if object_row is not None:
        supplied_identifiers = (
            (object_row.egkn_id, egkn_id),
            (object_row.cadastre_number, cadastre_number),
        )
        # A match on one official parcel key must not override a contradiction
        # on another populated parcel key. Jerler registry row IDs are not part
        # of this check: repeated auction publications can use different Jerler
        # rows for the same EGKN/cadastral parcel. False parcel merges corrupt
        # object history, boundaries, and comparables, so hold contradictions
        # for explicit review.
        if any(
            stored is not None and supplied is not None and stored != supplied
            for stored, supplied in supplied_identifiers
        ):
            lot.land_object_ref_id = None
            return None

    if object_row is None:
        object_row = AuctionLandObject.from_identifiers(
            egkn_id=egkn_id,
            cadastre_number=cadastre_number,
            jerler_object_id=jerler_object_id,
        )
        session.add(object_row)
        session.flush()
    else:
        if egkn_id and not object_row.egkn_id:
            object_row.egkn_id = egkn_id
            object_row.canonical_key = f"egkn:{egkn_id}"
            object_row.identity_confidence = "official"
        if cadastre_number and not object_row.cadastre_number:
            object_row.cadastre_number = cadastre_number
        if jerler_object_id and not object_row.jerler_object_id:
            object_row.jerler_object_id = jerler_object_id

    lot.land_object_ref_id = object_row.id
    return object_row


@dataclass(frozen=True)
class CanonicalLandBackfillPage:
    scanned: int
    selected: int
    linked: int
    unlinked: int
    last_scanned_lot_id: str | None
    has_more: bool


def canonical_land_backfill_high_water(session: Session) -> str | None:
    """Freeze one pass at the current highest unlinked land-lot key."""
    return session.scalar(
        select(func.max(AuctionLot.id)).where(
            AuctionLot.object_type == "land",
            AuctionLot.land_object_ref_id.is_(None),
        )
    )


def backfill_canonical_land_objects_page(
    session: Session,
    *,
    limit: int = 250,
    after_lot_id: str | None,
    high_water_lot_id: str | None,
) -> CanonicalLandBackfillPage:
    """Process one resumable keyset page without starving older eligible lots."""
    if limit < 1 or high_water_lot_id is None:
        return CanonicalLandBackfillPage(0, 0, 0, 0, after_lot_id, False)

    bounded_limit = min(int(limit), 1_000)
    scan_limit = min(max(bounded_limit * 12, 500), 5_000)
    conditions = [
        AuctionLot.object_type == "land",
        AuctionLot.land_object_ref_id.is_(None),
        AuctionLot.id <= high_water_lot_id,
    ]
    if after_lot_id is not None:
        conditions.append(AuctionLot.id > after_lot_id)
    candidates = list(
        session.scalars(
            select(AuctionLot)
            .where(*conditions)
            .order_by(AuctionLot.id)
            .limit(scan_limit)
        ).all()
    )

    scanned = 0
    selected = 0
    linked = 0
    last_scanned_lot_id = after_lot_id
    for lot in candidates:
        scanned += 1
        last_scanned_lot_id = lot.id
        eligible = bool(
            _clean(lot.land_object_id)
            or _official_cadastre(lot.cadastre_number)
            or _jerler_object_id(lot.source_object_url)
        )
        if not eligible:
            continue
        selected += 1
        if reconcile_lot_land_object(session, lot) is not None:
            linked += 1
        if selected >= bounded_limit:
            break

    has_more = False
    if last_scanned_lot_id is not None:
        has_more = session.scalar(
            select(AuctionLot.id)
            .where(
                AuctionLot.object_type == "land",
                AuctionLot.land_object_ref_id.is_(None),
                AuctionLot.id > last_scanned_lot_id,
                AuctionLot.id <= high_water_lot_id,
            )
            .order_by(AuctionLot.id)
            .limit(1)
        ) is not None
    return CanonicalLandBackfillPage(
        scanned=scanned,
        selected=selected,
        linked=linked,
        unlinked=selected - linked,
        last_scanned_lot_id=last_scanned_lot_id,
        has_more=has_more,
    )


def backfill_canonical_land_objects(session: Session, *, limit: int = 250) -> dict[str, int]:
    """Compatibility wrapper for one bounded canonical-identity pass."""
    page = backfill_canonical_land_objects_page(
        session,
        limit=limit,
        after_lot_id=None,
        high_water_lot_id=canonical_land_backfill_high_water(session),
    )
    return {"selected": page.selected, "linked": page.linked, "unlinked": page.unlinked}


def set_land_object_boundary(
    land_object: AuctionLandObject,
    geometry_geojson: dict[str, object] | None,
    *,
    source: str,
) -> bool:
    """Persist a source-labelled polygon only when its GeoJSON shape is explicit."""
    if not isinstance(source, str) or not source.strip() or len(source) > 200:
        return False
    if not _valid_boundary_geometry(geometry_geojson):
        return False
    try:
        serialized = json.dumps(
            geometry_geojson,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False
    changed = (
        land_object.boundary_geojson != serialized
        or land_object.boundary_source != source
    )
    if changed:
        land_object.boundary_geojson = serialized
        land_object.boundary_source = source
        land_object.boundary_observed_at = datetime.now(UTC)
    return changed


MAX_BOUNDARY_POSITIONS = 10_000


def is_valid_land_boundary(geometry: object) -> bool:
    """Return whether an external GeoJSON value is safe to treat as a parcel boundary."""
    return _valid_boundary_geometry(geometry)


def _valid_boundary_geometry(geometry: object) -> bool:
    try:
        validate_parcel_geojson(
            geometry,
            limits=GeometryLimits(max_vertices=MAX_BOUNDARY_POSITIONS),
        )
    except GeometryValidationError:
        return False
    return True
