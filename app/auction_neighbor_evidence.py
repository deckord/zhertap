"""Evidence-only polygon-first observations of adjacent EGKN parcels."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from app.auction_neighbors import analyze_neighbor_land_use
from app.models import AuctionEvidence, AuctionLot
from app.providers.egkn import EgknProvider, EgknProviderError

EVIDENCE_TYPE = "neighbor_parcels_polygon"
SOURCE_URL = "https://map.gov4c.kz/geoserver/egkn/ows"


def _payload_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_neighbor_parcel_evidence(session, lot: AuctionLot, *, provider: EgknProvider | None = None, max_features: int = 100) -> dict[str, object]:
    land_object = lot.land_object
    payload: dict[str, object] = {"schema_version": "neighbor-parcels/1", "provider": "egkn_public_map", "provider_layer": "egkn:u_view", "requires_manual_review": True}
    if land_object is None or land_object.boundary_source != "jerler:source_object" or not land_object.boundary_geojson:
        result_status, evidence_status, counts = "canonical_polygon_unavailable", "manual_required", {}
    else:
        try:
            polygon = json.loads(land_object.boundary_geojson)
            payload["subject_polygon_source"] = land_object.boundary_source
            payload["subject_polygon_sha256"] = _payload_hash(polygon)
            query = (provider or EgknProvider()).parcel_features_by_polygon(parcel_geojson=polygon, max_features=max(1, min(int(max_features), 500)))
            payload["provider_response_status"] = query.response_status
            payload["provider_truncated"] = query.truncated
            if query.truncated:
                result_status, evidence_status, counts = "provider_truncated", "manual_required", {}
            elif query.response_status == "empty":
                result_status, evidence_status, counts = "provider_empty", "missing", {}
            else:
                classified = analyze_neighbor_land_use(polygon, query.features)
                result_status = classified.status
                evidence_status = "found" if classified.status == "found" else "missing"
                counts = classified.counts
        except (ValueError, TypeError, json.JSONDecodeError, EgknProviderError):
            result_status, evidence_status, counts = "provider_failure", "error", {}
    payload.update({"result_status": result_status, "counts": counts, "observed_at": datetime.now(UTC).isoformat()})
    evidence = AuctionEvidence(lot_id=lot.id, evidence_type=EVIDENCE_TYPE, title="ЕГКН: соседние участки по контуру Jerler")
    evidence.status = evidence_status
    evidence.value_text = "Смежные участки классифицированы по опубликованным данным ЕГКН; требуется ручная сверка." if result_status == "found" else "Автоматическая проверка соседних участков не даёт юридического заключения; требуется ручная сверка."
    evidence.source_url = SOURCE_URL
    evidence.confidence = 0.7 if result_status == "found" else 0.0
    evidence.raw_payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    session.add(evidence)
    return payload
