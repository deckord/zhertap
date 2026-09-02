"""Durable storage and fail-closed parcel linkage for official territory facts."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auction_territory_intelligence import (
    TerritoryObservation,
    assess_parcel_geographic_applicability,
    normalize_territory_observation,
    territory_identity_key,
    transition_decision,
)
from app.models import (
    AuctionLot,
    AuctionTerritoryApplicability,
    AuctionTerritoryObservation,
)


class TerritoryObservationConflict(ValueError):
    """An immutable revision or lifecycle rule was contradicted."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_payload(observation: TerritoryObservation) -> dict[str, object]:
    event: dict[str, object] | None = None
    if observation.event is not None:
        event = {
            "event_key": observation.event.event_key,
            "event_code": observation.event.event_code,
            "direction": observation.event.direction,
            "direction_basis": observation.event.direction_basis,
            "lifecycle_state": observation.event.lifecycle_state,
            "event_date": observation.event.event_date.isoformat(),
            "correction_of_revision": observation.event.correction_of_revision,
            "label": observation.event.label,
        }
    demographic: dict[str, object] | None = None
    if observation.demographic is not None:
        demographic = {
            "indicator_code": observation.demographic.indicator_code,
            "period_start": observation.demographic.period_start.isoformat(),
            "period_end": observation.demographic.period_end.isoformat(),
            "value": observation.demographic.value,
            "unit": observation.demographic.unit,
            "methodology_code": observation.demographic.methodology_code,
        }
    return {
        "provider_id": observation.provider_id,
        "source_record_id": observation.source_record_id,
        "source_revision": observation.source_revision,
        "record_kind": observation.record_kind,
        "authority_name": observation.authority_name,
        "source_url": observation.source_url,
        "source_published_at": observation.source_published_at.isoformat(),
        "observed_at": observation.observed_at.isoformat(),
        "territory_code": observation.territory_code,
        "geometry_geojson": observation.geometry_geojson,
        "event": event,
        "demographic": demographic,
    }


def _parse_payload(payload_json: str) -> dict[str, object]:
    payload = json.loads(payload_json)
    payload["source_published_at"] = datetime.fromisoformat(payload["source_published_at"])
    payload["observed_at"] = datetime.fromisoformat(payload["observed_at"])
    if payload.get("event"):
        payload["event"]["event_date"] = date.fromisoformat(payload["event"]["event_date"])
    if payload.get("demographic"):
        payload["demographic"]["period_start"] = date.fromisoformat(
            payload["demographic"]["period_start"]
        )
        payload["demographic"]["period_end"] = date.fromisoformat(
            payload["demographic"]["period_end"]
        )
    return payload


def load_territory_observation(row: AuctionTerritoryObservation) -> TerritoryObservation:
    observation = normalize_territory_observation(_parse_payload(row.payload_json))
    if observation.content_hash != row.content_hash:
        raise TerritoryObservationConflict("persisted_content_hash_mismatch")
    return observation


def persist_territory_observation(
    session: Session, payload: Mapping[str, object]
) -> AuctionTerritoryObservation:
    observation = normalize_territory_observation(payload)
    identity = territory_identity_key(observation)
    existing = session.scalar(
        select(AuctionTerritoryObservation).where(
            AuctionTerritoryObservation.identity_key == identity,
            AuctionTerritoryObservation.source_revision == observation.source_revision,
        )
    )
    if existing is not None:
        if existing.content_hash != observation.content_hash:
            raise TerritoryObservationConflict("revision_content_conflict")
        return existing

    latest = session.scalar(
        select(AuctionTerritoryObservation)
        .where(AuctionTerritoryObservation.identity_key == identity)
        .order_by(AuctionTerritoryObservation.source_revision.desc())
        .limit(1)
    )
    if latest is not None:
        if observation.source_revision < latest.source_revision:
            raise TerritoryObservationConflict("stale_revision")
        previous = load_territory_observation(latest)
        if previous.event is not None and observation.event is not None:
            decision = transition_decision(
                previous.event.lifecycle_state,
                observation.event.lifecycle_state,
                current_revision=previous.source_revision,
                next_revision=observation.source_revision,
                correction_of_revision=observation.event.correction_of_revision,
            )
            if decision in {"conflict", "stale"}:
                raise TerritoryObservationConflict(f"lifecycle_{decision}")

    row = AuctionTerritoryObservation(
        identity_key=identity,
        provider_id=observation.provider_id,
        source_record_id=observation.source_record_id,
        source_revision=observation.source_revision,
        record_kind=observation.record_kind,
        authority_name=observation.authority_name,
        source_url=observation.source_url,
        source_published_at=observation.source_published_at,
        observed_at=observation.observed_at,
        territory_code=observation.territory_code,
        geometry_geojson=(
            _json(observation.geometry_geojson)
            if observation.geometry_geojson is not None
            else None
        ),
        geometry_sha256=observation.geometry_sha256,
        payload_json=_json(_normalized_payload(observation)),
        content_hash=observation.content_hash,
        contract_version=observation.contract_version,
    )
    session.add(row)
    session.flush()
    return row


def _parcel_payload(lot: AuctionLot) -> tuple[dict[str, object] | None, str | None]:
    raw = lot.land_object.boundary_geojson if lot.land_object is not None else None
    if not raw:
        return None, None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise TerritoryObservationConflict("invalid_canonical_boundary_json") from exc
    canonical = _json(parsed)
    return parsed, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assess_and_persist_lot_applicability(
    session: Session,
    observation_row: AuctionTerritoryObservation,
    lot: AuctionLot,
    *,
    parcel_territory_code: str | None = None,
    assessed_at: datetime | None = None,
) -> AuctionTerritoryApplicability:
    observation = load_territory_observation(observation_row)
    parcel, boundary_hash = _parcel_payload(lot)
    result = assess_parcel_geographic_applicability(
        observation,
        parcel_geojson=parcel,
        parcel_territory_code=parcel_territory_code,
    )
    row = session.scalar(
        select(AuctionTerritoryApplicability).where(
            AuctionTerritoryApplicability.observation_id == observation_row.id,
            AuctionTerritoryApplicability.lot_id == lot.id,
        )
    )
    when = assessed_at or datetime.now(UTC)
    if row is None:
        row = AuctionTerritoryApplicability(
            observation_id=observation_row.id,
            lot_id=lot.id,
            status=result.status,
            scope=result.scope,
            basis=result.basis,
            overlap_ratio=result.overlap_ratio,
            parcel_boundary_sha256=boundary_hash,
            assessed_at=when,
        )
        session.add(row)
    elif row.parcel_boundary_sha256 != boundary_hash:
        row.status = result.status
        row.scope = result.scope
        row.basis = result.basis
        row.overlap_ratio = result.overlap_ratio
        row.parcel_boundary_sha256 = boundary_hash
        row.assessed_at = when
    session.flush()
    return row
