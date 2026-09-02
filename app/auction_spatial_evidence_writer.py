"""Durable-state contract for worker-side W5/W6/W7 spatial evidence.

The module deliberately has no SQLAlchemy or task dependency. Production persistence must
implement :class:`SpatialEvidenceStore` with row locks and atomic evidence/state/manifest
commit. Its state table needs a unique ``(lot_id,module,provider_id,feed_id)`` key and separate
partial/composite indexes for ``pending``, ``retryable/next_attempt_at``,
``processing/claim_expires_at`` and ``ready|conflict/next_validation_at``. Immutable evidence
is append-only; the one current manifest row per lot is replaced in the same transaction as
its governing state/evidence. No query may outer-join all lots to discover dirty work. The
in-memory implementation is an executable reference for the lifecycle contract. It never
creates or signs a trusted receipt.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from app.auction_spatial_source_adapters import (
    SpatialSourceEnvelope,
    SpatialTrustedReceipt,
)

WRITER_VERSION = "auction-spatial-evidence-writer/2026.1"
MODULES = ("restrictions", "site", "planning")
MAX_FEEDS_PER_LOT = 24
MAX_BATCH = 50
MAX_PAYLOAD_BYTES = 256_000
MAX_QUARANTINE_TEXT = 500
CLAIM_TTL = timedelta(minutes=10)
MAX_RETRY_DELAY = timedelta(hours=6)
REVALIDATE_AFTER = timedelta(hours=24)

Module = Literal["restrictions", "site", "planning"]
StateStatus = Literal[
    "pending",
    "processing",
    "ready",
    "conflict",
    "retryable",
    "terminal",
    "quarantined",
    "expired",
]


class SpatialEvidenceWriterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SpatialFeedIdentity:
    lot_id: str
    module: Module
    provider_id: str
    feed_id: str

    @property
    def key(self) -> str:
        material = "\x1f".join(
            (self.lot_id, self.module, self.provider_id, self.feed_id)
        )
        return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedSpatialObservation:
    identity: SpatialFeedIdentity
    envelope: SpatialSourceEnvelope
    canonical_feed_sha256: str
    receipt: SpatialTrustedReceipt
    expires_at: datetime | None
    prepared_at: datetime
    input_signature: str
    source_event_signature: str


@dataclass(frozen=True, slots=True)
class SpatialFeedState:
    identity: SpatialFeedIdentity
    status: StateStatus
    input_signature: str
    current_evidence_id: int | None = None
    current_generation_id: str | None = None
    current_payload_hash: str | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    attempts: int = 0
    next_attempt_at: datetime | None = None
    next_validation_at: datetime | None = None
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    claimed_from_status: StateStatus | None = None
    last_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SpatialEvidenceRecord:
    evidence_id: int
    identity: SpatialFeedIdentity
    status: Literal["found", "conflict", "error", "quarantine"]
    generation_id: str | None
    input_signature: str
    payload_hash: str | None
    payload: dict[str, object] | None
    observed_at: datetime
    source_url: str | None
    receipt_sha256: str | None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class SpatialManifestExpectation:
    lot_id: str
    required_feed_keys: Mapping[Module, tuple[str, ...]]
    version: str


@dataclass(frozen=True, slots=True)
class SpatialGenerationManifest:
    lot_id: str
    status: Literal["complete", "incomplete", "conflict"]
    settled: bool
    manifest_hash: str
    module_generations: Mapping[Module, tuple[str, ...]]
    missing_feed_keys: tuple[str, ...]
    blocking_feed_keys: tuple[str, ...]
    expires_at: datetime | None
    version: str
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class SpatialWorkClaim:
    identity: SpatialFeedIdentity
    token: str
    input_signature: str


@dataclass(frozen=True, slots=True)
class SpatialWriteResult:
    status: Literal["written", "already_current", "retryable", "quarantined"]
    evidence_id: int | None
    manifest: SpatialGenerationManifest
    enqueue_w14: bool
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class SpatialPendingResult:
    changed: bool
    manifest: SpatialGenerationManifest
    enqueue_w14: bool


@dataclass(frozen=True, slots=True)
class SpatialConfigReconcileResult:
    changed_feeds: int
    manifest: SpatialGenerationManifest
    enqueue_w14: bool


@dataclass(frozen=True, slots=True)
class SpatialWorklistResult:
    claims: tuple[SpatialWorkClaim, ...]
    invalidated_manifests: tuple[SpatialGenerationManifest, ...]


class SpatialEvidenceStore(Protocol):
    """Production adapter contract; every mutating method is a short transaction."""

    def mark_pending(
        self,
        identity: SpatialFeedIdentity,
        expectation: SpatialManifestExpectation,
        *,
        input_signature: str,
        changed_at: datetime,
    ) -> SpatialPendingResult: ...

    def claim_due(
        self, *, checked_at: datetime, limit: int, owner_token: str
    ) -> SpatialWorklistResult: ...

    def persist_observation_atomic(
        self,
        claim: SpatialWorkClaim,
        observation: PreparedSpatialObservation,
        expectation: SpatialManifestExpectation,
        *,
        checked_at: datetime,
    ) -> SpatialWriteResult: ...

    def persist_failure_atomic(
        self,
        claim: SpatialWorkClaim,
        failure: SpatialProcessingFailure,
        expectation: SpatialManifestExpectation,
        *,
        checked_at: datetime,
    ) -> SpatialWriteResult: ...


@dataclass(frozen=True, slots=True)
class SpatialProcessingFailure:
    code: str
    message: str
    retryable: bool


def prepare_spatial_observation(
    *,
    identity: SpatialFeedIdentity,
    envelope: SpatialSourceEnvelope,
    canonical_feed_sha256: str,
    receipt: SpatialTrustedReceipt,
    registry_version: str,
    expires_at: datetime | None,
    prepared_at: datetime | None = None,
) -> PreparedSpatialObservation:
    """Bind an already verified adapter result to its externally supplied receipt."""
    checked = _aware(prepared_at or datetime.now(UTC), "prepared_at")
    observed = _aware(envelope.observed_at, "observed_at")
    expiry = _optional_aware(expires_at, "expires_at")
    _validate_identity(identity)
    if envelope.key != identity.module:
        raise SpatialEvidenceWriterError("module identity mismatch")
    if receipt.provider_id != identity.provider_id or receipt.feed_id != identity.feed_id:
        raise SpatialEvidenceWriterError("trusted receipt identity mismatch")
    if receipt.canonical_feed_sha256 != canonical_feed_sha256:
        raise SpatialEvidenceWriterError("trusted receipt canonical hash mismatch")
    if not _sha256(canonical_feed_sha256) or not _sha256(receipt.receipt_sha256):
        raise SpatialEvidenceWriterError("invalid trusted receipt hashes")
    if receipt.provenance_kind not in {"signed_feed", "internal_fetch"}:
        raise SpatialEvidenceWriterError("invalid trusted receipt provenance")
    if envelope.status not in {"found", "conflict"}:
        raise SpatialEvidenceWriterError("unsupported envelope status")
    if expiry is not None and expiry < observed:
        raise SpatialEvidenceWriterError("expiry predates observation")
    if expiry is not None and expiry <= checked:
        raise SpatialEvidenceWriterError("spatial observation has expired")
    payload_json = _strict_json(envelope.payload)
    expected_generation = _generation(
        canonical_feed_sha256,
        receipt.receipt_sha256,
        registry_version,
    )
    if envelope.generation_id != expected_generation:
        raise SpatialEvidenceWriterError("adapter generation does not bind receipt/feed")
    signature = hashlib.sha256(
        (
            f"{identity.key}:{expected_generation}:{envelope.status}:"
            f"{hashlib.sha256(payload_json.encode()).hexdigest()}:{expiry}"
        ).encode()
    ).hexdigest()
    source_signature = event_signature(
        lot_id=identity.lot_id,
        module=identity.module,
        provider_id=identity.provider_id,
        feed_id=identity.feed_id,
        source_version=envelope.producer_version,
        content_sha256=canonical_feed_sha256,
    )
    return PreparedSpatialObservation(
        identity=identity,
        envelope=envelope,
        canonical_feed_sha256=canonical_feed_sha256,
        receipt=receipt,
        expires_at=expiry,
        prepared_at=checked,
        input_signature=signature,
        source_event_signature=source_signature,
    )


def spatial_adapter_generation(
    canonical_feed_sha256: str,
    receipt_sha256: str,
    registry_version: str,
) -> str:
    """Shared deterministic generation formula expected from the signed-feed adapter."""
    return _generation(canonical_feed_sha256, receipt_sha256, registry_version)


def validate_manifest_expectation(value: SpatialManifestExpectation) -> None:
    if not isinstance(value, SpatialManifestExpectation) or not _bounded_id(value.lot_id, 64):
        raise SpatialEvidenceWriterError("invalid manifest expectation")
    if set(value.required_feed_keys) != set(MODULES) or not _bounded_id(value.version, 128):
        raise SpatialEvidenceWriterError("manifest must define all spatial modules")
    aggregate = 0
    for module in MODULES:
        keys = value.required_feed_keys[module]
        if not isinstance(keys, tuple) or not keys or len(keys) > MAX_FEEDS_PER_LOT:
            raise SpatialEvidenceWriterError("invalid required feed checklist")
        if len(set(keys)) != len(keys) or any(not _bounded_id(key, 300) for key in keys):
            raise SpatialEvidenceWriterError("invalid required feed key")
        aggregate += len(keys)
    if aggregate > MAX_FEEDS_PER_LOT:
        raise SpatialEvidenceWriterError("required feed checklist exceeds bound")


class InMemorySpatialEvidenceStore:
    """Thread-safe executable specification of the production persistence semantics."""

    def __init__(self, initial_states: Sequence[SpatialFeedState] = ()) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, SpatialFeedState] = {}
        self._evidence: list[SpatialEvidenceRecord] = []
        self._manifests: dict[str, SpatialGenerationManifest] = {}
        self._expectations: dict[str, SpatialManifestExpectation] = {}
        self._due: list[tuple[float, int, str]] = []
        self._schedule_version: dict[str, int] = {}
        self.inspected_last_claim = 0
        if len(initial_states) > 100_000:
            raise SpatialEvidenceWriterError("state snapshot exceeds bound")
        for state in initial_states:
            if not isinstance(state, SpatialFeedState) or state.identity.key in self._states:
                raise SpatialEvidenceWriterError("invalid state snapshot")
            _validate_identity(state.identity)
            self._states[state.identity.key] = state
            due = _state_due_at(state)
            if due is not None:
                self._schedule(state.identity.key, due)

    @property
    def evidence(self) -> tuple[SpatialEvidenceRecord, ...]:
        return tuple(self._evidence)

    def state(self, identity: SpatialFeedIdentity) -> SpatialFeedState | None:
        return self._states.get(identity.key)

    def manifest(self, lot_id: str) -> SpatialGenerationManifest | None:
        return self._manifests.get(lot_id)

    def mark_pending(
        self,
        identity: SpatialFeedIdentity,
        expectation: SpatialManifestExpectation,
        *,
        input_signature: str,
        changed_at: datetime,
    ) -> SpatialPendingResult:
        checked = _aware(changed_at, "changed_at")
        _validate_identity(identity)
        validate_manifest_expectation(expectation)
        if expectation.lot_id != identity.lot_id:
            raise SpatialEvidenceWriterError("pending event/manifest lot mismatch")
        if identity.key not in expectation.required_feed_keys[identity.module]:
            raise SpatialEvidenceWriterError("feed identity is outside manifest checklist")
        if not _sha256(input_signature):
            raise SpatialEvidenceWriterError("invalid input signature")
        with self._lock:
            self._expectations[expectation.lot_id] = expectation
            current = self._states.get(identity.key)
            if current is not None and current.input_signature == input_signature:
                manifest, enqueue = self._reconcile(expectation, checked)
                return SpatialPendingResult(False, manifest, enqueue)
            state = SpatialFeedState(identity, "pending", input_signature)
            self._states[identity.key] = state
            self._schedule(identity.key, checked)
            manifest, enqueue = self._reconcile(expectation, checked)
            return SpatialPendingResult(True, manifest, enqueue)

    def claim_due(
        self, *, checked_at: datetime, limit: int, owner_token: str
    ) -> SpatialWorklistResult:
        checked = _aware(checked_at, "checked_at")
        if not _bounded_id(owner_token, 128):
            raise SpatialEvidenceWriterError("invalid owner token")
        bounded = max(1, min(limit, MAX_BATCH))
        claims: list[SpatialWorkClaim] = []
        invalidated: list[SpatialGenerationManifest] = []
        with self._lock:
            self.inspected_last_claim = 0
            while self._due and len(claims) < bounded:
                timestamp, version, key = heapq.heappop(self._due)
                self.inspected_last_claim += 1
                if timestamp > checked.timestamp():
                    heapq.heappush(self._due, (timestamp, version, key))
                    break
                if self._schedule_version.get(key) != version:
                    continue
                state = self._states.get(key)
                if state is None:
                    continue
                if state.status == "processing" and state.claim_expires_at is not None:
                    if state.claim_expires_at > checked:
                        self._schedule(key, state.claim_expires_at)
                        continue
                if state.status in {"ready", "conflict"} and (
                    state.expires_at is not None and state.expires_at <= checked
                ):
                    expectation = self._expectations.get(state.identity.lot_id)
                    if expectation is None:
                        raise SpatialEvidenceWriterError(
                            "durable manifest expectation missing for expiry invalidation"
                        )
                    state = replace(state, status="expired", last_error_code="feed_expired")
                    self._states[key] = state
                    manifest, enqueue = self._reconcile(expectation, checked)
                    if enqueue:
                        invalidated.append(manifest)
                token = f"{owner_token}:{uuid.uuid4().hex}"
                self._states[key] = replace(
                    state,
                    status="processing",
                    claim_token=token,
                    claim_expires_at=checked + CLAIM_TTL,
                    claimed_from_status=(
                        state.claimed_from_status if state.status == "processing" else state.status
                    ),
                )
                self._schedule(key, checked + CLAIM_TTL)
                claims.append(SpatialWorkClaim(state.identity, token, state.input_signature))
            return SpatialWorklistResult(tuple(claims), tuple(invalidated))

    def persist_observation_atomic(
        self,
        claim: SpatialWorkClaim,
        observation: PreparedSpatialObservation,
        expectation: SpatialManifestExpectation,
        *,
        checked_at: datetime,
    ) -> SpatialWriteResult:
        checked = _aware(checked_at, "checked_at")
        validate_manifest_expectation(expectation)
        if observation.identity != claim.identity or expectation.lot_id != claim.identity.lot_id:
            raise SpatialEvidenceWriterError("claim/observation/manifest lot mismatch")
        if claim.identity.key not in expectation.required_feed_keys[claim.identity.module]:
            raise SpatialEvidenceWriterError("feed identity is outside manifest checklist")
        if claim.input_signature != observation.source_event_signature:
            raise SpatialEvidenceWriterError("claim does not bind prepared source content")
        if observation.expires_at is not None and observation.expires_at <= checked:
            raise SpatialEvidenceWriterError("cannot persist expired spatial observation")
        payload_json = _strict_json(observation.envelope.payload)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        with self._lock:
            self._expectations[expectation.lot_id] = expectation
            state = self._governing_claim(claim)
            already_current = (
                state.current_generation_id == observation.envelope.generation_id
                and state.current_payload_hash == payload_hash
                and state.claimed_from_status
                == ("ready" if observation.envelope.status == "found" else "conflict")
            )
            evidence_id = state.current_evidence_id
            if not already_current:
                evidence_id = len(self._evidence) + 1
                evidence_status = (
                    "found" if observation.envelope.status == "found" else "conflict"
                )
                self._evidence.append(
                    SpatialEvidenceRecord(
                        evidence_id,
                        observation.identity,
                        evidence_status,
                        observation.envelope.generation_id,
                        observation.input_signature,
                        payload_hash,
                        json.loads(payload_json),
                        observation.envelope.observed_at,
                        observation.envelope.source_url,
                        observation.receipt.receipt_sha256,
                    )
                )
            next_validation = checked + REVALIDATE_AFTER
            if observation.expires_at is not None:
                next_validation = min(next_validation, observation.expires_at)
            new_state = replace(
                state,
                status="ready" if observation.envelope.status == "found" else "conflict",
                input_signature=claim.input_signature,
                current_evidence_id=evidence_id,
                current_generation_id=observation.envelope.generation_id,
                current_payload_hash=payload_hash,
                observed_at=observation.envelope.observed_at,
                expires_at=observation.expires_at,
                attempts=0,
                next_attempt_at=None,
                next_validation_at=next_validation,
                claim_token=None,
                claim_expires_at=None,
                claimed_from_status=None,
                last_error_code=None,
            )
            self._states[claim.identity.key] = new_state
            self._schedule(claim.identity.key, next_validation)
            manifest, enqueue = self._reconcile(expectation, checked)
            return SpatialWriteResult(
                "already_current" if already_current else "written",
                evidence_id,
                manifest,
                enqueue,
            )

    def persist_failure_atomic(
        self,
        claim: SpatialWorkClaim,
        failure: SpatialProcessingFailure,
        expectation: SpatialManifestExpectation,
        *,
        checked_at: datetime,
    ) -> SpatialWriteResult:
        checked = _aware(checked_at, "checked_at")
        validate_manifest_expectation(expectation)
        if expectation.lot_id != claim.identity.lot_id or (
            claim.identity.key not in expectation.required_feed_keys[claim.identity.module]
        ):
            raise SpatialEvidenceWriterError("claim is outside manifest checklist")
        if not _bounded_id(failure.code, 128) or not isinstance(failure.message, str):
            raise SpatialEvidenceWriterError("invalid failure")
        message = " ".join(failure.message.split())[:MAX_QUARANTINE_TEXT]
        with self._lock:
            self._expectations[expectation.lot_id] = expectation
            state = self._governing_claim(claim)
            attempts = min(state.attempts + 1, 100)
            evidence_id = None
            if failure.retryable:
                base_delay = 2 ** min(attempts, 14)
                jitter_window = max(1, base_delay // 4)
                jitter = int(
                    hashlib.sha256(claim.identity.key.encode()).hexdigest()[:8], 16
                ) % (jitter_window + 1)
                delay = min(
                    base_delay + jitter,
                    int(MAX_RETRY_DELAY.total_seconds()),
                )
                due = checked + timedelta(seconds=delay)
                new_state = replace(
                    state,
                    status="retryable",
                    attempts=attempts,
                    next_attempt_at=due,
                    claim_token=None,
                    claim_expires_at=None,
                    claimed_from_status=None,
                    last_error_code=failure.code,
                )
                result_status: Literal["retryable", "quarantined"] = "retryable"
                self._schedule(claim.identity.key, due)
            else:
                evidence_id = len(self._evidence) + 1
                self._evidence.append(
                    SpatialEvidenceRecord(
                        evidence_id,
                        claim.identity,
                        "quarantine",
                        None,
                        claim.input_signature,
                        None,
                        None,
                        checked,
                        None,
                        None,
                        failure.code,
                        message,
                    )
                )
                new_state = replace(
                    state,
                    status="quarantined",
                    current_evidence_id=evidence_id,
                    current_generation_id=None,
                    current_payload_hash=None,
                    attempts=attempts,
                    next_attempt_at=None,
                    next_validation_at=None,
                    claim_token=None,
                    claim_expires_at=None,
                    claimed_from_status=None,
                    last_error_code=failure.code,
                )
                result_status = "quarantined"
            self._states[claim.identity.key] = new_state
            manifest, enqueue = self._reconcile(expectation, checked)
            return SpatialWriteResult(
                result_status,
                evidence_id,
                manifest,
                enqueue,
                delay if failure.retryable else None,
            )

    def _governing_claim(self, claim: SpatialWorkClaim) -> SpatialFeedState:
        state = self._states.get(claim.identity.key)
        if (
            state is None
            or state.status != "processing"
            or state.claim_token != claim.token
            or state.input_signature != claim.input_signature
        ):
            raise SpatialEvidenceWriterError("claim is stale or not governing")
        return state

    def _schedule(self, key: str, due: datetime) -> None:
        version = self._schedule_version.get(key, 0) + 1
        self._schedule_version[key] = version
        heapq.heappush(self._due, (due.timestamp(), version, key))

    def _reconcile(
        self, expectation: SpatialManifestExpectation, checked: datetime
    ) -> tuple[SpatialGenerationManifest, bool]:
        missing: list[str] = []
        blocking: list[str] = []
        generations: dict[Module, tuple[str, ...]] = {}
        expiries: list[datetime] = []
        for module in MODULES:
            module_generations = []
            for key in sorted(expectation.required_feed_keys[module]):
                state = self._states.get(key)
                effective_status = (
                    state.claimed_from_status
                    if state is not None
                    and state.status == "processing"
                    and state.claimed_from_status is not None
                    else state.status if state is not None else None
                )
                if state is None or effective_status == "pending":
                    missing.append(key)
                    continue
                if state.identity.lot_id != expectation.lot_id or state.identity.module != module:
                    blocking.append(key)
                    continue
                if effective_status != "ready":
                    blocking.append(key)
                    continue
                if state.expires_at is not None and state.expires_at <= checked:
                    blocking.append(key)
                    continue
                if not state.current_generation_id or state.current_evidence_id is None:
                    blocking.append(key)
                    continue
                module_generations.append(state.current_generation_id)
                if state.expires_at is not None:
                    expiries.append(state.expires_at)
            generations[module] = tuple(sorted(module_generations))
        settled = not missing
        status: Literal["complete", "incomplete", "conflict"] = (
            "conflict" if blocking else "incomplete" if missing else "complete"
        )
        material = {
            "lot_id": expectation.lot_id,
            "status": status,
            "settled": settled,
            "module_generations": generations,
            "missing": sorted(missing),
            "blocking": sorted(blocking),
            "version": expectation.version,
            "writer_version": WRITER_VERSION,
        }
        manifest_hash = hashlib.sha256(_strict_json(material).encode()).hexdigest()
        previous = self._manifests.get(expectation.lot_id)
        changed = previous is None or previous.manifest_hash != manifest_hash
        manifest = SpatialGenerationManifest(
            expectation.lot_id,
            status,
            settled,
            manifest_hash,
            generations,
            tuple(sorted(missing)),
            tuple(sorted(blocking)),
            min(expiries) if expiries else None,
            expectation.version,
            checked,
        )
        if changed:
            self._manifests[expectation.lot_id] = manifest
        previous_settled = previous.settled if previous is not None else False
        enqueue = changed and (previous_settled or settled)
        return manifest, enqueue


def _generation(canonical: str, receipt: str, registry_version: str) -> str:
    if not _sha256(canonical) or not _sha256(receipt) or not _bounded_id(registry_version, 128):
        raise SpatialEvidenceWriterError("invalid generation material")
    return hashlib.sha256(f"{registry_version}:{canonical}:{receipt}".encode()).hexdigest()


def _strict_json(value: object) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SpatialEvidenceWriterError("spatial evidence is not strict JSON") from exc
    if len(rendered.encode()) > MAX_PAYLOAD_BYTES:
        raise SpatialEvidenceWriterError("spatial evidence exceeds byte budget")
    return rendered


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SpatialEvidenceWriterError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_aware(value: datetime | None, label: str) -> datetime | None:
    return None if value is None else _aware(value, label)


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bounded_id(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _validate_identity(identity: SpatialFeedIdentity) -> None:
    if not isinstance(identity, SpatialFeedIdentity) or identity.module not in MODULES:
        raise SpatialEvidenceWriterError("invalid spatial feed identity")
    if (
        not _bounded_id(identity.lot_id, 64)
        or not _bounded_id(identity.provider_id, 128)
        or not _bounded_id(identity.feed_id, 128)
    ):
        raise SpatialEvidenceWriterError("invalid spatial feed identity")


def _state_due_at(state: SpatialFeedState) -> datetime | None:
    if state.status == "pending":
        return datetime(1970, 1, 1, tzinfo=UTC)
    if state.status == "retryable":
        return state.next_attempt_at or datetime(1970, 1, 1, tzinfo=UTC)
    if state.status == "processing":
        return state.claim_expires_at or datetime(1970, 1, 1, tzinfo=UTC)
    if state.status in {"ready", "conflict", "expired"}:
        candidates = [
            value
            for value in (state.next_validation_at, state.expires_at)
            if value is not None
        ]
        return min(candidates) if candidates else None
    return None


def event_signature(
    *,
    lot_id: str,
    module: Module,
    provider_id: str,
    feed_id: str,
    source_version: str,
    content_sha256: str,
) -> str:
    """Cheap event-driven dirty signature; used before any feed parsing or GIS CPU."""
    values: Sequence[str] = (lot_id, module, provider_id, feed_id, source_version, WRITER_VERSION)
    if any(not _bounded_id(item, 300) for item in values):
        raise SpatialEvidenceWriterError("invalid spatial dirty event")
    if not _sha256(content_sha256):
        raise SpatialEvidenceWriterError("invalid spatial dirty content hash")
    return hashlib.sha256(("\x1f".join(values) + f"\x1f{content_sha256}").encode()).hexdigest()
