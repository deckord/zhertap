"""Persist evidence from bounded official NSDI water-protection checks."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from shapely.geometry import shape

from app.auction_nsdi_checks import (
    WaterProtectionIntersection,
    analyze_water_protection_intersection,
)
from app.models import AuctionEvidence, AuctionLot
from app.providers.nsdi import NationalWaterProtectionProvider, NsdiProviderError

EVIDENCE_TYPE = "nsdi_water_protection"
SOURCE_URL = "https://map.gov.kz/geoserver/ows"
CANONICAL_BOUNDARY_SOURCE = "jerler:source_object"
OUTSIDE_EXTENT_ERROR = "bbox outside published layer extent"
COVERAGE_CONTRACT = "nsdi-regional-coverage/2026.1"


def _bbox(geometry: dict[str, object]) -> tuple[float, float, float, float]:
    bounds = shape(geometry).bounds
    return (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))


def record_water_protection_evidence(
    session,
    lot: AuctionLot,
    *,
    provider: NationalWaterProtectionProvider | None = None,
) -> WaterProtectionIntersection:
    land_object = lot.land_object
    active_provider = provider or NationalWaterProtectionProvider()
    if land_object is None or not land_object.boundary_geojson:
        result = WaterProtectionIntersection("boundary_unavailable", 0, None, True)
    elif land_object.boundary_source != CANONICAL_BOUNDARY_SOURCE:
        result = WaterProtectionIntersection(
            "canonical_polygon_unavailable", 0, None, True
        )
    else:
        try:
            geometry = json.loads(land_object.boundary_geojson)
            parcel_bbox = _bbox(geometry)
            covers_region = getattr(active_provider, "covers_region", None)
            covers_bbox = getattr(active_provider, "covers_bbox", None)
            if callable(covers_region) and not covers_region(lot.region):
                result = WaterProtectionIntersection(
                    "outside_published_coverage", 0, None, True
                )
            elif callable(covers_bbox) and not covers_bbox(parcel_bbox):
                result = WaterProtectionIntersection(
                    "outside_published_extent", 0, None, True
                )
            else:
                features = active_provider.features_for_bbox(parcel_bbox)
                result = analyze_water_protection_intersection(geometry, features)
        except (ValueError, TypeError, NsdiProviderError) as exc:
            result = WaterProtectionIntersection(
                "outside_published_extent"
                if str(exc) == OUTSIDE_EXTENT_ERROR
                else "source_unavailable",
                0,
                None,
                True,
            )
    evidence = (
        session.query(AuctionEvidence)
        .filter_by(lot_id=lot.id, evidence_type=EVIDENCE_TYPE)
        .one_or_none()
    )
    if evidence is None:
        evidence = AuctionEvidence(
            lot_id=lot.id,
            evidence_type=EVIDENCE_TYPE,
            title="Водоохранная зона: опубликованный слой НИПД",
        )
        session.add(evidence)
    evidence.status = (
        "warning" if result.status == "intersection_found" else "manual_required"
    )
    evidence.value_text = (
        f"Опубликованная водоохранная зона затрагивает "
        f"{result.intersection_percent}% участка; требуется ручная проверка акта."
        if result.status == "intersection_found"
        else "Автоматическая проверка водоохранной зоны не даёт юридического "
        "заключения; требуется ручная проверка."
    )
    evidence.source_url = SOURCE_URL
    evidence.confidence = 0.8 if result.status == "intersection_found" else 0.0
    evidence.raw_payload_json = json.dumps(
        {
            "status": result.status,
            "feature_count": result.feature_count,
            "intersection_percent": result.intersection_percent,
            "requires_manual_review": True,
            "boundary_source": getattr(lot.land_object, "boundary_source", None),
            "coverage_area": getattr(active_provider, "coverage_area", None)
            if land_object is not None and land_object.boundary_geojson
            else None,
            "source_layer": getattr(active_provider, "source_layer", None)
            if land_object is not None and land_object.boundary_geojson
            else None,
            "dataset_url": getattr(active_provider, "DATASET_URL", None)
            if land_object is not None and land_object.boundary_geojson
            else None,
            "coverage_contract": COVERAGE_CONTRACT,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    evidence.observed_at = datetime.now(UTC)
    return result
