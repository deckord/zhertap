from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import AuctionLot, AuctionLotHistory

AUCTION_CSV_COLUMNS = [
    "id",
    "source",
    "source_lot_id",
    "source_search_status",
    "auction_number",
    "status",
    "active",
    "cadastre_number",
    "region",
    "district",
    "locality",
    "location_text",
    "area_ha",
    "area_sotka",
    "functional_purpose_level2",
    "functional_purpose_level3",
    "functional_purpose_level4",
    "use_goal",
    "purpose",
    "land_rights",
    "start_price_kzt",
    "sale_price_kzt",
    "price_per_sotka_kzt",
    "price_per_square_meter_kzt",
    "guarantee_kzt",
    "auction_starts_at",
    "published_at",
    "seller_name",
    "seller_bin",
    "source_url",
    "source_object_url",
    "first_seen_at",
    "last_seen_at",
]


@dataclass(frozen=True, slots=True)
class AuctionLotHistorySummary:
    identifier_type: str
    identifier: str
    lot_count: int
    publication_count: int
    failed_count: int
    first_start_price_kzt: float | None
    last_start_price_kzt: float | None
    start_price_change_kzt: float | None
    start_price_change_percent: float | None
    publications: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "identifier_type": self.identifier_type,
            "identifier": self.identifier,
            "lot_count": self.lot_count,
            "publication_count": self.publication_count,
            "failed_count": self.failed_count,
            "first_start_price_kzt": self.first_start_price_kzt,
            "last_start_price_kzt": self.last_start_price_kzt,
            "start_price_change_kzt": self.start_price_change_kzt,
            "start_price_change_percent": self.start_price_change_percent,
            "publications": self.publications,
        }


def export_auction_lots_csv(lots: Iterable[AuctionLot]) -> str:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=AUCTION_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for lot in lots:
        writer.writerow(auction_lot_csv_row(lot))
    return output.getvalue()


def auction_lot_csv_row(lot: AuctionLot) -> dict[str, Any]:
    area_sotka = lot.area_ha * 100 if lot.area_ha is not None else None
    return {
        "id": lot.id,
        "source": lot.source,
        "source_lot_id": lot.source_lot_id,
        "source_search_status": lot.source_search_status,
        "auction_number": lot.auction_number,
        "status": lot.status,
        "active": lot.active,
        "cadastre_number": lot.cadastre_number,
        "region": lot.region,
        "district": lot.district,
        "locality": lot.locality,
        "location_text": lot.location_text,
        "area_ha": lot.area_ha,
        "area_sotka": area_sotka,
        "functional_purpose_level2": lot.functional_purpose_level2,
        "functional_purpose_level3": lot.functional_purpose_level3,
        "functional_purpose_level4": lot.functional_purpose_level4,
        "use_goal": lot.use_goal,
        "purpose": lot.purpose,
        "land_rights": lot.land_rights,
        "start_price_kzt": lot.start_price_kzt,
        "sale_price_kzt": lot.sale_price_kzt,
        "price_per_sotka_kzt": _price_per_sotka(lot),
        "price_per_square_meter_kzt": _price_per_square_meter(lot),
        "guarantee_kzt": lot.guarantee_kzt,
        "auction_starts_at": _isoformat(lot.auction_starts_at),
        "published_at": lot.published_at.isoformat() if lot.published_at else None,
        "seller_name": lot.seller_name,
        "seller_bin": lot.seller_bin,
        "source_url": lot.source_url,
        "source_object_url": lot.source_object_url,
        "first_seen_at": _isoformat(lot.first_seen_at),
        "last_seen_at": _isoformat(lot.last_seen_at),
    }


def auction_lot_publication_history(
    session: Session,
    *,
    cadastre_number: str | None = None,
    source_lot_id: str | None = None,
) -> AuctionLotHistorySummary:
    if not cadastre_number and not source_lot_id:
        raise ValueError("cadastre_number or source_lot_id is required")

    filters = []
    if cadastre_number:
        filters.append(AuctionLot.cadastre_number == cadastre_number)
    if source_lot_id:
        filters.append(AuctionLot.source_lot_id == source_lot_id)

    lots = list(
        session.scalars(
            select(AuctionLot)
            .options(selectinload(AuctionLot.history))
            .where(or_(*filters))
            .order_by(AuctionLot.first_seen_at, AuctionLot.source_lot_id)
        ).all()
    )
    publications = sorted(
        [
            _history_publication_dict(lot, item)
            for lot in lots
            for item in lot.history
        ],
        key=lambda item: (str(item["observed_at"] or ""), str(item["source_lot_id"])),
    )
    prices = [
        item["start_price_kzt"]
        for item in publications
        if isinstance(item["start_price_kzt"], int | float)
    ]
    first_price = prices[0] if prices else None
    last_price = prices[-1] if prices else None
    change = (
        last_price - first_price
        if first_price is not None and last_price is not None
        else None
    )
    change_percent = (
        (change / first_price) * 100
        if change is not None and first_price not in (None, 0)
        else None
    )
    identifier_type = "cadastre_number" if cadastre_number else "source_lot_id"
    identifier = cadastre_number or source_lot_id or ""
    return AuctionLotHistorySummary(
        identifier_type=identifier_type,
        identifier=identifier,
        lot_count=len(lots),
        publication_count=len(publications),
        failed_count=sum(
            1
            for item in publications
            if _is_failed_status(item["status"])
            or item["source_search_status"] == "FailureProtocolSigned"
        ),
        first_start_price_kzt=first_price,
        last_start_price_kzt=last_price,
        start_price_change_kzt=change,
        start_price_change_percent=change_percent,
        publications=publications,
    )


def _history_publication_dict(
    lot: AuctionLot,
    history: AuctionLotHistory,
) -> dict[str, Any]:
    return {
        "lot_id": lot.id,
        "source": lot.source,
        "source_lot_id": lot.source_lot_id,
        "source_search_status": lot.source_search_status,
        "auction_number": lot.auction_number,
        "cadastre_number": lot.cadastre_number,
        "region": lot.region,
        "district": lot.district,
        "locality": lot.locality,
        "status": history.status,
        "start_price_kzt": history.start_price_kzt,
        "sale_price_kzt": history.sale_price_kzt,
        "auction_starts_at": _isoformat(history.auction_starts_at),
        "observed_at": _isoformat(history.observed_at),
        "source_url": lot.source_url,
    }


def _price_per_sotka(lot: AuctionLot) -> float | None:
    if lot.start_price_kzt is None or lot.area_ha is None or lot.area_ha <= 0:
        return None
    return lot.start_price_kzt / (lot.area_ha * 100)


def _price_per_square_meter(lot: AuctionLot) -> float | None:
    if lot.start_price_kzt is None or lot.area_ha is None or lot.area_ha <= 0:
        return None
    return lot.start_price_kzt / (lot.area_ha * 10_000)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _is_failed_status(value: object) -> bool:
    text = str(value or "").casefold()
    return (
        "не состоя" in text
        or "несостоя" in text
        or "failure" in text
        or "failed" in text
    )
