"""Worker-only orchestration for one claimed trusted spatial feed."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.auction_history import auction_scenario
from app.auction_spatial_decision_input import load_spatial_evidence
from app.auction_spatial_evidence_store import SqlAlchemySpatialEvidenceStore
from app.auction_spatial_evidence_writer import (
    SpatialEvidenceWriterError,
    SpatialFeedIdentity,
    SpatialManifestExpectation,
    SpatialProcessingFailure,
    SpatialWorkClaim,
    event_signature,
    prepare_spatial_observation,
)
from app.auction_spatial_fetch import (
    SpatialFetchDeferred,
    SpatialFetchRuntime,
    SpatialFetchTerminal,
    VerifiedSpatialFeed,
    fetch_verified_spatial_feed,
)
from app.auction_spatial_source_adapters import (
    SpatialAdapterResult,
    adapt_planning_feed,
    adapt_restriction_feed,
    adapt_site_feed,
)
from app.models import (
    AuctionLot,
    AuctionSpatialFeedState,
    AuctionSpatialManifestExpectation,
)
from app.provider_backpressure import ProviderBackpressure


@dataclass(frozen=True, slots=True)
class SpatialClaimResult:
    status: str
    lot_id: str
    enqueue_w14: bool
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SpatialSeedResult:
    lots_scanned: int
    feeds_created_or_changed: int
    enqueue_w14: bool
    next_after_lot_id: str | None
    high_water_lot_id: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class _ClaimContext:
    source_lot_id: str
    profile: str
    parcel_geojson: dict[str, object] | None
    expectation: SpatialManifestExpectation


def seed_spatial_feed_states(
    session_factory: Callable[[], Session],
    *,
    runtime: SpatialFetchRuntime,
    after_lot_id: str | None,
    high_water_lot_id: str | None,
    limit: int = 5,
    checked_at: datetime | None = None,
) -> SpatialSeedResult:
    """Boundedly bootstrap configured feeds; content binding happens after verified fetch."""
    checked = _aware(checked_at or datetime.now(UTC))
    bounded = max(1, min(int(limit), 10))
    endpoints = tuple(runtime.endpoints.values())
    if not 3 <= len(endpoints) <= 20 or {
        endpoint.module for endpoint in endpoints
    } != {"restrictions", "site", "planning"}:
        raise SpatialEvidenceWriterError(
            "configured spatial feeds must cover all modules within 20-feed bound"
        )
    with session_factory() as session:
        expected_version = _expectation_version(runtime)
        active_state = ~and_(
            AuctionSpatialFeedState.status == "terminal",
            AuctionSpatialFeedState.last_error_code == "config_retired",
        )
        active_count = (
            select(func.count(AuctionSpatialFeedState.id))
            .where(
                AuctionSpatialFeedState.lot_id == AuctionLot.id,
                active_state,
            )
            .correlate(AuctionLot)
            .scalar_subquery()
        )
        expected_match = or_(
            *(
                and_(
                    AuctionSpatialFeedState.module == endpoint.module,
                    AuctionSpatialFeedState.provider_id == endpoint.provider_id,
                    AuctionSpatialFeedState.feed_id == endpoint.feed_id,
                )
                for endpoint in endpoints
            )
        )
        matching_count = (
            select(func.count(AuctionSpatialFeedState.id))
            .where(
                AuctionSpatialFeedState.lot_id == AuctionLot.id,
                active_state,
                expected_match,
            )
            .correlate(AuctionLot)
            .scalar_subquery()
        )
        high_water = high_water_lot_id
        if high_water is None:
            high_water = session.scalar(
                select(func.max(AuctionLot.id)).where(
                    AuctionLot.active.is_(True), AuctionLot.object_type == "land"
                )
            )
        if high_water is None:
            return SpatialSeedResult(0, 0, False, after_lot_id, None, False)
        conditions = [
            AuctionLot.active.is_(True),
            AuctionLot.object_type == "land",
            AuctionLot.id <= high_water,
        ]
        if after_lot_id is not None:
            conditions.append(AuctionLot.id > after_lot_id)
        lot_ids = list(
            session.scalars(
                select(AuctionLot.id)
                .outerjoin(
                    AuctionSpatialManifestExpectation,
                    AuctionSpatialManifestExpectation.lot_id == AuctionLot.id,
                )
                .where(*conditions)
                .where(
                    (AuctionSpatialManifestExpectation.lot_id.is_(None))
                    | (AuctionSpatialManifestExpectation.version != expected_version)
                    | (active_count != len(endpoints))
                    | (matching_count != len(endpoints))
                )
                .order_by(AuctionLot.id)
                .limit(bounded)
            )
        )
    changed = 0
    enqueue = False
    for lot_id in lot_ids:
        expectation = _expectation(lot_id, runtime)
        configured = []
        for endpoint in endpoints:
            identity = _endpoint_identity(lot_id, endpoint)
            config_hash = _endpoint_config_hash(runtime, endpoint)
            signature = event_signature(
                lot_id=lot_id,
                module=identity.module,
                provider_id=identity.provider_id,
                feed_id=identity.feed_id,
                source_version="spatial-config-bootstrap/2026.1",
                content_sha256=config_hash,
            )
            configured.append((identity, signature))
        with session_factory() as session:
            result = SqlAlchemySpatialEvidenceStore(session).reconcile_configured_feeds(
                expectation,
                configured=tuple(configured),
                changed_at=checked,
            )
        changed += result.changed_feeds
        enqueue = enqueue or result.enqueue_w14
    next_after = lot_ids[-1] if lot_ids else after_lot_id
    return SpatialSeedResult(
        len(lot_ids),
        changed,
        enqueue,
        next_after,
        high_water,
        len(lot_ids) == bounded,
    )


def process_spatial_claim(
    session_factory: Callable[[], Session],
    claim: SpatialWorkClaim,
    *,
    runtime: SpatialFetchRuntime,
    backpressure: ProviderBackpressure,
    owner_token: str,
    checked_at: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
    fetcher: Callable[..., VerifiedSpatialFeed] = fetch_verified_spatial_feed,
) -> SpatialClaimResult:
    """Fetch and parse outside DB; persistence methods each open a short transaction."""
    checked = _aware(checked_at or datetime.now(UTC))
    with session_factory() as session:
        context = _load_context(session, claim)
    endpoint = runtime.endpoints.get(
        (claim.identity.provider_id, claim.identity.feed_id)
    )
    if endpoint is None or endpoint.module != claim.identity.module:
        return _persist_failure(
            session_factory,
            claim,
            context.expectation,
            SpatialProcessingFailure(
                "feed_not_configured", "claimed feed is not configured", False
            ),
            checked,
        )
    if context.parcel_geojson is None:
        return _persist_failure(
            session_factory,
            claim,
            context.expectation,
            SpatialProcessingFailure(
                "parcel_geometry_missing", "authoritative parcel geometry is missing", True
            ),
            checked,
        )
    try:
        verified = fetcher(
            endpoint,
            source_lot_id=context.source_lot_id,
            backpressure=backpressure,
            owner_token=owner_token,
            transport=transport,
        )
        adapted = _adapt(
            verified,
            expected_lot_id=context.source_lot_id,
            profile=context.profile,
            parcel_geojson=context.parcel_geojson,
            runtime=runtime,
            checked_at=checked,
        )
        if adapted.envelope is None:
            issue = adapted.issues[0] if adapted.issues else None
            raise SpatialFetchTerminal(
                issue.code if issue else "adapter_rejected",
                issue.detail if issue else "spatial adapter rejected verified feed",
            )
        expiry = _valid_until(verified.feed.get("valid_until"))
        provider = runtime.registry[claim.identity.provider_id]
        observation = prepare_spatial_observation(
            identity=claim.identity,
            envelope=adapted.envelope,
            canonical_feed_sha256=verified.canonical_feed_sha256,
            receipt=verified.receipt,
            registry_version=provider.registry_version,
            expires_at=expiry,
            prepared_at=checked,
        )
    except SpatialFetchDeferred as exc:
        result = _persist_failure(
            session_factory,
            claim,
            context.expectation,
            SpatialProcessingFailure(exc.code, exc.code, True),
            checked,
        )
        return SpatialClaimResult(
            result.status,
            result.lot_id,
            result.enqueue_w14,
            max(exc.retry_after_seconds, result.retry_after_seconds or 0),
        )
    except (SpatialFetchTerminal, SpatialEvidenceWriterError, ValueError) as exc:
        code = getattr(exc, "code", "spatial_payload_invalid")
        return _persist_failure(
            session_factory,
            claim,
            context.expectation,
            SpatialProcessingFailure(str(code), str(exc), False),
            checked,
        )
    if observation.source_event_signature != claim.input_signature:
        # A claim for content A must never persist fetched content B. Advance the
        # durable dirty signature; the bounded continuation refetches under a new claim.
        with session_factory() as session:
            pending = SqlAlchemySpatialEvidenceStore(session).mark_pending(
                claim.identity,
                context.expectation,
                input_signature=observation.source_event_signature,
                changed_at=checked,
            )
        return SpatialClaimResult("superseded", claim.identity.lot_id, pending.enqueue_w14, 1)
    with session_factory() as session:
        written = SqlAlchemySpatialEvidenceStore(session).persist_observation_atomic(
            claim, observation, context.expectation, checked_at=checked
        )
    return SpatialClaimResult(
        written.status,
        claim.identity.lot_id,
        written.enqueue_w14,
        written.retry_after_seconds,
    )


def _load_context(session: Session, claim: SpatialWorkClaim) -> _ClaimContext:
    lot = session.get(AuctionLot, claim.identity.lot_id)
    if lot is None:
        raise SpatialEvidenceWriterError("auction lot does not exist")
    expectation = SqlAlchemySpatialEvidenceStore(session).load_expectation(lot.id)
    evidence = load_spatial_evidence(session, lot.id)
    parcel = evidence.get("parcel")
    geometry = parcel.payload.get("parcel_geojson") if parcel else None
    return _ClaimContext(
        lot.source_lot_id,
        auction_scenario(lot),
        geometry if isinstance(geometry, dict) else None,
        expectation,
    )


def _endpoint_identity(lot_id: str, endpoint: object) -> SpatialFeedIdentity:
    return SpatialFeedIdentity(
        lot_id,
        endpoint.module,
        endpoint.provider_id,
        endpoint.feed_id,
    )


def _expectation(
    lot_id: str, runtime: SpatialFetchRuntime
) -> SpatialManifestExpectation:
    identities = [
        _endpoint_identity(lot_id, endpoint)
        for endpoint in runtime.endpoints.values()
    ]
    required = {
        module: tuple(sorted(item.key for item in identities if item.module == module))
        for module in ("restrictions", "site", "planning")
    }
    return SpatialManifestExpectation(
        lot_id,
        required,
        _expectation_version(runtime),
    )


def _expectation_version(runtime: SpatialFetchRuntime) -> str:
    material = _fetch_config_material(runtime)
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    return f"spatial-config-{digest}"


def _endpoint_config_hash(runtime: SpatialFetchRuntime, endpoint: object) -> str:
    material = _endpoint_config_material(runtime, endpoint)
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fetch_config_material(runtime: SpatialFetchRuntime) -> list[dict[str, object]]:
    return [
        _endpoint_config_material(runtime, endpoint)
        for endpoint in sorted(
            runtime.endpoints.values(),
            key=lambda item: (item.provider_id, item.feed_id, item.module),
        )
    ]


def _endpoint_config_material(
    runtime: SpatialFetchRuntime, endpoint: object
) -> dict[str, object]:
    provider = runtime.registry[endpoint.provider_id]
    policy = runtime.policies[endpoint.provider_id]
    secret = endpoint.hmac_secret
    return {
        "provider_id": endpoint.provider_id,
        "feed_id": endpoint.feed_id,
        "module": endpoint.module,
        "url_template": endpoint.url_template,
        "auth_mode": endpoint.auth_mode,
        "hmac_secret_sha256": hashlib.sha256(secret).hexdigest() if secret else None,
        "pinned_sha256": endpoint.pinned_sha256,
        "allowed_hosts": list(endpoint.allowed_hosts),
        "provider_scope": asdict(provider),
        "backpressure_policy": asdict(policy),
    }


def _adapt(
    verified: VerifiedSpatialFeed,
    *,
    expected_lot_id: str,
    profile: str,
    parcel_geojson: dict[str, object],
    runtime: SpatialFetchRuntime,
    checked_at: datetime,
) -> SpatialAdapterResult:
    receipts = {
        f"{verified.receipt.provider_id}:{verified.receipt.feed_id}": verified.receipt
    }
    kind = verified.feed.get("feed_kind")
    common = {
        "expected_lot_id": expected_lot_id,
        "parcel_geojson": parcel_geojson,
        "registry": runtime.registry,
        "receipts": receipts,
        "now": checked_at,
    }
    if kind == "restrictions":
        return adapt_restriction_feed(verified.feed, **common)
    if kind == "planning":
        return adapt_planning_feed(verified.feed, **common)
    if kind == "site":
        return adapt_site_feed(verified.feed, profile=profile, **common)
    raise SpatialFetchTerminal("feed_kind_invalid", "unknown verified feed kind")


def _persist_failure(
    session_factory: Callable[[], Session],
    claim: SpatialWorkClaim,
    expectation: SpatialManifestExpectation,
    failure: SpatialProcessingFailure,
    checked: datetime,
) -> SpatialClaimResult:
    with session_factory() as session:
        result = SqlAlchemySpatialEvidenceStore(session).persist_failure_atomic(
            claim, failure, expectation, checked_at=checked
        )
    return SpatialClaimResult(
        result.status,
        claim.identity.lot_id,
        result.enqueue_w14,
        result.retry_after_seconds,
    )


def _valid_until(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise SpatialFetchTerminal("validity_invalid", "invalid feed validity")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SpatialFetchTerminal("validity_invalid", "invalid feed validity") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SpatialFetchTerminal("validity_invalid", "invalid feed validity")
    return parsed.astimezone(UTC)


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise SpatialEvidenceWriterError("checked_at must be datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
