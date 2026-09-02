"""SQLAlchemy persistence adapter for atomic spatial evidence lifecycle."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.auction_spatial_evidence_writer import (
    CLAIM_TTL,
    MAX_BATCH,
    MAX_FEEDS_PER_LOT,
    MAX_QUARANTINE_TEXT,
    MAX_RETRY_DELAY,
    MODULES,
    REVALIDATE_AFTER,
    PreparedSpatialObservation,
    SpatialConfigReconcileResult,
    SpatialEvidenceWriterError,
    SpatialFeedIdentity,
    SpatialGenerationManifest,
    SpatialManifestExpectation,
    SpatialPendingResult,
    SpatialProcessingFailure,
    SpatialWorkClaim,
    SpatialWorklistResult,
    SpatialWriteResult,
    validate_manifest_expectation,
)
from app.models import (
    AuctionEvidence,
    AuctionLot,
    AuctionSpatialDecisionSignal,
    AuctionSpatialFeedState,
    AuctionSpatialGenerationManifest,
    AuctionSpatialManifestExpectation,
)

EVIDENCE_TYPES = {
    "restrictions": "decision_input:restriction_source",
    "site": "decision_input:site_source",
    "planning": "decision_input:planning_source",
}


class SqlAlchemySpatialEvidenceStore:
    """All public mutations commit before returning an enqueue flag."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def _lock_lot(self, lot_id: str) -> None:
        lot = self.session.scalar(
            select(AuctionLot.id).where(AuctionLot.id == lot_id).with_for_update()
        )
        if lot is None:
            raise SpatialEvidenceWriterError("auction lot does not exist")
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"auction-spatial:{lot_id}"},
            )

    @staticmethod
    def _checklist_json(expectation: SpatialManifestExpectation) -> str:
        payload = {
            module: sorted(expectation.required_feed_keys[module]) for module in MODULES
        }
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(rendered.encode()) > 16_384:
            raise SpatialEvidenceWriterError("spatial checklist exceeds DB bound")
        return rendered

    def _persist_expectation(self, expectation: SpatialManifestExpectation, now: datetime) -> None:
        validate_manifest_expectation(expectation)
        rendered = self._checklist_json(expectation)
        digest = hashlib.sha256(
            f"{expectation.version}:{rendered}".encode()
        ).hexdigest()
        model = self.session.get(AuctionSpatialManifestExpectation, expectation.lot_id)
        if model is None:
            self.session.add(
                AuctionSpatialManifestExpectation(
                    lot_id=expectation.lot_id,
                    version=expectation.version,
                    checklist_hash=digest,
                    required_feed_keys_json=rendered,
                    updated_at=now,
                )
            )
        elif model.checklist_hash != digest:
            model.version = expectation.version
            model.checklist_hash = digest
            model.required_feed_keys_json = rendered
            model.updated_at = now

    def _load_expectation(self, lot_id: str) -> SpatialManifestExpectation:
        model = self.session.get(AuctionSpatialManifestExpectation, lot_id)
        if model is None:
            raise SpatialEvidenceWriterError("durable spatial manifest expectation is missing")
        try:
            payload = json.loads(model.required_feed_keys_json)
            expectation = SpatialManifestExpectation(
                lot_id,
                {module: tuple(payload[module]) for module in MODULES},
                model.version,
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SpatialEvidenceWriterError("stored spatial checklist is invalid") from exc
        validate_manifest_expectation(expectation)
        rendered = self._checklist_json(expectation)
        digest = hashlib.sha256(f"{expectation.version}:{rendered}".encode()).hexdigest()
        if digest != model.checklist_hash:
            raise SpatialEvidenceWriterError("stored spatial checklist hash mismatch")
        return expectation

    def load_expectation(self, lot_id: str) -> SpatialManifestExpectation:
        """Read and verify a bounded durable checklist without mutating worker state."""
        return self._load_expectation(lot_id)

    @staticmethod
    def _identity(model: AuctionSpatialFeedState) -> SpatialFeedIdentity:
        return SpatialFeedIdentity(model.lot_id, model.module, model.provider_id, model.feed_id)

    def _state_for_update(
        self, identity: SpatialFeedIdentity
    ) -> AuctionSpatialFeedState | None:
        return self.session.scalar(
            select(AuctionSpatialFeedState)
            .where(AuctionSpatialFeedState.identity_key == identity.key)
            .with_for_update()
        )

    def mark_pending(
        self,
        identity: SpatialFeedIdentity,
        expectation: SpatialManifestExpectation,
        *,
        input_signature: str,
        changed_at: datetime,
    ) -> SpatialPendingResult:
        checked = self._aware(changed_at)
        if (
            checked is None
            or identity.module not in MODULES
            or len(input_signature) != 64
            or any(character not in "0123456789abcdef" for character in input_signature)
        ):
            raise SpatialEvidenceWriterError("invalid pending event")
        validate_manifest_expectation(expectation)
        if expectation.lot_id != identity.lot_id or (
            identity.key not in expectation.required_feed_keys[identity.module]
        ):
            raise SpatialEvidenceWriterError("feed identity is outside manifest checklist")
        with self.session.begin():
            self._lock_lot(identity.lot_id)
            self._persist_expectation(expectation, checked)
            state = self._state_for_update(identity)
            changed = state is None or state.input_signature != input_signature
            if state is None:
                state = AuctionSpatialFeedState(
                    lot_id=identity.lot_id,
                    module=identity.module,
                    provider_id=identity.provider_id,
                    feed_id=identity.feed_id,
                    identity_key=identity.key,
                    status="pending",
                    input_signature=input_signature,
                    attempts=0,
                    created_at=checked,
                    updated_at=checked,
                )
                self.session.add(state)
                self.session.flush()
            elif changed:
                state.status = "pending"
                state.input_signature = input_signature
                state.current_evidence_id = None
                state.current_generation_id = None
                state.current_payload_hash = None
                state.observed_at = None
                state.expires_at = None
                state.attempts = 0
                state.next_attempt_at = None
                state.next_validation_at = None
                state.claim_token = None
                state.claim_expires_at = None
                state.claimed_from_status = None
                state.last_error_code = None
                state.updated_at = checked
            manifest, enqueue = self._reconcile(expectation, checked)
        return SpatialPendingResult(changed, manifest, enqueue)

    def reconcile_configured_feeds(
        self,
        expectation: SpatialManifestExpectation,
        *,
        configured: tuple[tuple[SpatialFeedIdentity, str], ...],
        changed_at: datetime,
    ) -> SpatialConfigReconcileResult:
        """Atomically install an exact configured feed set and retire obsolete rows."""
        checked = self._aware(changed_at)
        validate_manifest_expectation(expectation)
        expected_keys = {
            key
            for module in MODULES
            for key in expectation.required_feed_keys[module]
        }
        configured_by_key = {
            identity.key: (identity, signature) for identity, signature in configured
        }
        if (
            checked is None
            or len(configured_by_key) != len(configured)
            or set(configured_by_key) != expected_keys
            or any(
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for _identity, value in configured_by_key.values()
            )
            or any(
                identity.lot_id != expectation.lot_id
                or identity.module not in MODULES
                or identity.key not in expectation.required_feed_keys[identity.module]
                for identity, _signature in configured_by_key.values()
            )
        ):
            raise SpatialEvidenceWriterError("invalid configured spatial feed set")
        changed = 0
        with self.session.begin():
            self._lock_lot(expectation.lot_id)
            rows = list(
                self.session.scalars(
                    select(AuctionSpatialFeedState)
                    .where(AuctionSpatialFeedState.lot_id == expectation.lot_id)
                    .order_by(AuctionSpatialFeedState.id)
                    .limit(101)
                    .with_for_update()
                )
            )
            if len(rows) > 100:
                raise SpatialEvidenceWriterError("spatial feed history exceeds bound")
            by_key = {row.identity_key: row for row in rows}
            for row in rows:
                if row.identity_key in expected_keys:
                    continue
                if not (
                    row.status == "terminal"
                    and row.last_error_code == "config_retired"
                ):
                    row.status = "terminal"
                    row.claim_token = None
                    row.claim_expires_at = None
                    row.claimed_from_status = None
                    row.next_attempt_at = None
                    row.next_validation_at = None
                    row.last_error_code = "config_retired"
                    row.updated_at = checked
                    changed += 1
            for module in MODULES:
                for key in expectation.required_feed_keys[module]:
                    identity, signature = configured_by_key[key]
                    row = by_key.get(key)
                    if row is None:
                        row = AuctionSpatialFeedState(
                            lot_id=identity.lot_id,
                            module=identity.module,
                            provider_id=identity.provider_id,
                            feed_id=identity.feed_id,
                            identity_key=identity.key,
                            status="pending",
                            input_signature=signature,
                            attempts=0,
                            created_at=checked,
                            updated_at=checked,
                        )
                        self.session.add(row)
                        changed += 1
                        continue
                    needs_activation = (
                        row.input_signature != signature
                        or row.status == "terminal"
                        and row.last_error_code == "config_retired"
                    )
                    if needs_activation:
                        self._reset_pending(row, signature, checked)
                        changed += 1
            self._persist_expectation(expectation, checked)
            manifest, enqueue = self._reconcile(expectation, checked)
        return SpatialConfigReconcileResult(changed, manifest, enqueue)

    @staticmethod
    def _reset_pending(
        state: AuctionSpatialFeedState, signature: str, checked: datetime
    ) -> None:
        state.status = "pending"
        state.input_signature = signature
        state.current_evidence_id = None
        state.current_generation_id = None
        state.current_payload_hash = None
        state.observed_at = None
        state.expires_at = None
        state.attempts = 0
        state.next_attempt_at = None
        state.next_validation_at = None
        state.claim_token = None
        state.claim_expires_at = None
        state.claimed_from_status = None
        state.last_error_code = None
        state.updated_at = checked

    def _candidate_ids(self, checked: datetime, limit: int) -> list[int]:
        state = AuctionSpatialFeedState
        active_lot = (
            AuctionLot.active.is_(True),
            AuctionLot.object_type == "land",
        )
        queries = (
            select(state.id)
            .join(AuctionLot, AuctionLot.id == state.lot_id)
            .where(state.status == "pending", *active_lot)
            .order_by(state.id)
            .limit(limit + 1),
            select(state.id)
            .join(AuctionLot, AuctionLot.id == state.lot_id)
            .where(
                state.status == "retryable",
                *active_lot,
                or_(state.next_attempt_at.is_(None), state.next_attempt_at <= checked),
            )
            .order_by(state.next_attempt_at, state.id)
            .limit(limit + 1),
            select(state.id)
            .join(AuctionLot, AuctionLot.id == state.lot_id)
            .where(
                state.status == "processing",
                *active_lot,
                or_(state.claim_expires_at.is_(None), state.claim_expires_at <= checked),
            )
            .order_by(state.claim_expires_at, state.id)
            .limit(limit + 1),
            select(state.id)
            .join(AuctionLot, AuctionLot.id == state.lot_id)
            .where(
                state.status.in_(("ready", "conflict")),
                *active_lot,
                or_(
                    state.next_validation_at.is_(None),
                    state.next_validation_at <= checked,
                    state.expires_at <= checked,
                ),
            )
            .order_by(state.next_validation_at, state.expires_at, state.id)
            .limit(limit + 1),
        )
        return sorted(
            {int(value) for query in queries for value in self.session.scalars(query)}
        )[:limit]

    def claim_due(
        self, *, checked_at: datetime, limit: int, owner_token: str
    ) -> SpatialWorklistResult:
        checked = self._aware(checked_at)
        if checked is None or not owner_token or len(owner_token) > 64:
            raise SpatialEvidenceWriterError("invalid spatial worklist request")
        bounded = max(1, min(int(limit), MAX_BATCH))
        claims: list[SpatialWorkClaim] = []
        invalidated: list[SpatialGenerationManifest] = []
        with self.session.begin():
            ids = self._candidate_ids(checked, bounded)
        for state_id in ids:
            # Candidate discovery is unlocked. Each short claim transaction always locks
            # lot/advisory first and only then its feed row, matching mark/persist order.
            with self.session.begin():
                lot_id = self.session.scalar(
                    select(AuctionSpatialFeedState.lot_id).where(
                        AuctionSpatialFeedState.id == state_id
                    )
                )
                if lot_id is None:
                    continue
                self._lock_lot(lot_id)
                state = self.session.scalar(
                    select(AuctionSpatialFeedState)
                    .where(AuctionSpatialFeedState.id == state_id)
                    .with_for_update(skip_locked=True)
                )
                if state is None or not self._is_due(state, checked):
                    continue
                identity = self._identity(state)
                if state.status in {"ready", "conflict"} and (
                    self._aware(state.expires_at) is not None
                    and self._aware(state.expires_at) <= checked
                ):
                    state.status = "expired"
                    state.last_error_code = "feed_expired"
                    expectation = self._load_expectation(identity.lot_id)
                    manifest, enqueue = self._reconcile(expectation, checked)
                    if enqueue:
                        invalidated.append(manifest)
                previous = (
                    state.claimed_from_status
                    if state.status == "processing"
                    else state.status
                )
                token = f"{owner_token}:{uuid.uuid4().hex}"
                state.status = "processing"
                state.claimed_from_status = previous
                state.claim_token = token
                state.claim_expires_at = checked + CLAIM_TTL
                state.updated_at = checked
                claims.append(SpatialWorkClaim(identity, token, state.input_signature))
        return SpatialWorklistResult(tuple(claims), tuple(invalidated))

    def _is_due(self, state: AuctionSpatialFeedState, checked: datetime) -> bool:
        if state.status == "pending":
            return True
        if state.status == "retryable":
            due = self._aware(state.next_attempt_at)
            return due is None or due <= checked
        if state.status == "processing":
            due = self._aware(state.claim_expires_at)
            return due is None or due <= checked
        if state.status in {"ready", "conflict"}:
            validation = self._aware(state.next_validation_at)
            expiry = self._aware(state.expires_at)
            return (
                validation is None
                or validation <= checked
                or (expiry is not None and expiry <= checked)
            )
        return False

    def _governing_state(self, claim: SpatialWorkClaim) -> AuctionSpatialFeedState:
        state = self._state_for_update(claim.identity)
        if (
            state is None
            or state.status != "processing"
            or state.claim_token != claim.token
            or state.input_signature != claim.input_signature
        ):
            raise SpatialEvidenceWriterError("claim is stale or not governing")
        return state

    def persist_observation_atomic(
        self,
        claim: SpatialWorkClaim,
        observation: PreparedSpatialObservation,
        expectation: SpatialManifestExpectation,
        *,
        checked_at: datetime,
    ) -> SpatialWriteResult:
        checked = self._aware(checked_at)
        if checked is None or claim.input_signature != observation.source_event_signature:
            raise SpatialEvidenceWriterError("claim does not bind prepared source content")
        validate_manifest_expectation(expectation)
        if observation.identity != claim.identity or (
            expectation.lot_id != claim.identity.lot_id
            or claim.identity.key not in expectation.required_feed_keys[claim.identity.module]
        ):
            raise SpatialEvidenceWriterError("claim/observation/manifest mismatch")
        if observation.expires_at is not None and observation.expires_at <= checked:
            raise SpatialEvidenceWriterError("cannot persist expired spatial observation")
        if "_spatial_writer" in observation.envelope.payload:
            raise SpatialEvidenceWriterError("reserved spatial evidence metadata key")
        persistence_payload = {
            **observation.envelope.payload,
            "_spatial_writer": {
                "writer_version": "auction-spatial-evidence-writer/2026.1",
                "provider_id": claim.identity.provider_id,
                "feed_id": claim.identity.feed_id,
                "canonical_feed_sha256": observation.canonical_feed_sha256,
                "receipt_sha256": observation.receipt.receipt_sha256,
                "receipt_provenance": observation.receipt.provenance_kind,
                "producer_version": observation.envelope.producer_version,
                "expires_at": (
                    observation.expires_at.isoformat()
                    if observation.expires_at is not None
                    else None
                ),
            },
        }
        payload_json = json.dumps(
            persistence_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(payload_json.encode()) > 256_000:
            raise SpatialEvidenceWriterError("spatial evidence exceeds byte budget")
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        with self.session.begin():
            self._lock_lot(claim.identity.lot_id)
            self._persist_expectation(expectation, checked)
            state = self._governing_state(claim)
            target_status = "ready" if observation.envelope.status == "found" else "conflict"
            already = (
                state.claimed_from_status == target_status
                and state.current_generation_id == observation.envelope.generation_id
                and state.current_payload_hash == payload_hash
            )
            evidence_id = state.current_evidence_id
            if not already:
                evidence = AuctionEvidence(
                    lot_id=claim.identity.lot_id,
                    source_id=None,
                    evidence_type=EVIDENCE_TYPES[claim.identity.module],
                    status=observation.envelope.status,
                    title=f"{claim.identity.provider_id}:{claim.identity.feed_id}"[:320],
                    value_text=observation.envelope.generation_id,
                    source_url=observation.envelope.source_url,
                    confidence=0.95,
                    raw_payload_json=payload_json,
                    observed_at=observation.envelope.observed_at,
                )
                self.session.add(evidence)
                self.session.flush()
                evidence_id = evidence.id
            state.status = target_status
            state.current_evidence_id = evidence_id
            state.current_generation_id = observation.envelope.generation_id
            state.current_payload_hash = payload_hash
            state.observed_at = observation.envelope.observed_at
            state.expires_at = observation.expires_at
            state.attempts = 0
            state.next_attempt_at = None
            validation = checked + REVALIDATE_AFTER
            state.next_validation_at = (
                min(validation, observation.expires_at)
                if observation.expires_at is not None
                else validation
            )
            state.claim_token = None
            state.claim_expires_at = None
            state.claimed_from_status = None
            state.last_error_code = None
            state.updated_at = checked
            manifest, enqueue = self._reconcile(expectation, checked)
        return SpatialWriteResult(
            "already_current" if already else "written",
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
        checked = self._aware(checked_at)
        if checked is None or not failure.code or len(failure.code) > 128:
            raise SpatialEvidenceWriterError("invalid spatial processing failure")
        validate_manifest_expectation(expectation)
        if expectation.lot_id != claim.identity.lot_id or (
            claim.identity.key not in expectation.required_feed_keys[claim.identity.module]
        ):
            raise SpatialEvidenceWriterError("claim is outside manifest checklist")
        with self.session.begin():
            self._lock_lot(claim.identity.lot_id)
            self._persist_expectation(expectation, checked)
            state = self._governing_state(claim)
            attempts = min(state.attempts + 1, 10_000)
            evidence_id = None
            retry_after = None
            if failure.retryable:
                base = 2 ** min(attempts, 14)
                jitter = int(hashlib.sha256(claim.identity.key.encode()).hexdigest()[:8], 16)
                retry_after = min(
                    base + jitter % (max(1, base // 4) + 1),
                    int(MAX_RETRY_DELAY.total_seconds()),
                )
                state.status = "retryable"
                state.next_attempt_at = checked + timedelta(seconds=retry_after)
                result_status = "retryable"
            else:
                message = " ".join(failure.message.split())[:MAX_QUARANTINE_TEXT]
                evidence = AuctionEvidence(
                    lot_id=claim.identity.lot_id,
                    source_id=None,
                    evidence_type=EVIDENCE_TYPES[claim.identity.module],
                    status="quarantine",
                    title=f"{claim.identity.provider_id}:{claim.identity.feed_id}"[:320],
                    value_text=failure.code,
                    confidence=0.0,
                    raw_payload_json=json.dumps(
                        {
                            "error_code": failure.code,
                            "error_message": message,
                            "provider_id": claim.identity.provider_id,
                            "feed_id": claim.identity.feed_id,
                            "input_signature": claim.input_signature,
                            "writer_version": "auction-spatial-evidence-writer/2026.1",
                        },
                        separators=(",", ":"),
                    ),
                    observed_at=checked,
                )
                self.session.add(evidence)
                self.session.flush()
                evidence_id = evidence.id
                state.status = "quarantined"
                state.current_evidence_id = evidence_id
                state.current_generation_id = None
                state.current_payload_hash = None
                state.next_attempt_at = None
                state.next_validation_at = None
                result_status = "quarantined"
            state.attempts = attempts
            state.claim_token = None
            state.claim_expires_at = None
            state.claimed_from_status = None
            state.last_error_code = failure.code
            state.updated_at = checked
            manifest, enqueue = self._reconcile(expectation, checked)
        return SpatialWriteResult(
            result_status,
            evidence_id,
            manifest,
            enqueue,
            retry_after,
        )

    def _reconcile(
        self, expectation: SpatialManifestExpectation, checked: datetime
    ) -> tuple[SpatialGenerationManifest, bool]:
        keys = [key for module in MODULES for key in expectation.required_feed_keys[module]]
        rows = list(
            self.session.scalars(
                select(AuctionSpatialFeedState)
                .where(AuctionSpatialFeedState.identity_key.in_(keys))
                .with_for_update()
                .limit(MAX_FEEDS_PER_LOT + 1)
            )
        )
        if len(rows) > MAX_FEEDS_PER_LOT:
            raise SpatialEvidenceWriterError("spatial state reconciliation exceeds bound")
        states = {row.identity_key: row for row in rows}
        missing: list[str] = []
        blocking: list[str] = []
        generations: dict[str, tuple[str, ...]] = {}
        expiries: list[datetime] = []
        for module in MODULES:
            module_generations = []
            for key in sorted(expectation.required_feed_keys[module]):
                state = states.get(key)
                effective = (
                    state.claimed_from_status
                    if state is not None
                    and state.status == "processing"
                    and state.claimed_from_status
                    else state.status if state is not None else None
                )
                if state is None or effective == "pending":
                    missing.append(key)
                elif state.lot_id != expectation.lot_id or state.module != module:
                    blocking.append(key)
                elif (
                    effective != "ready"
                    or not state.current_generation_id
                    or state.current_evidence_id is None
                ):
                    blocking.append(key)
                elif self._aware(state.expires_at) is not None and (
                    self._aware(state.expires_at) <= checked
                ):
                    blocking.append(key)
                else:
                    module_generations.append(state.current_generation_id)
                    expiry = self._aware(state.expires_at)
                    if expiry is not None:
                        expiries.append(expiry)
            generations[module] = tuple(sorted(module_generations))
        settled = not missing
        status = "conflict" if blocking else "incomplete" if missing else "complete"
        material = {
            "lot_id": expectation.lot_id,
            "status": status,
            "settled": settled,
            "module_generations": generations,
            "missing": sorted(missing),
            "blocking": sorted(blocking),
            "version": expectation.version,
            "writer_version": "auction-spatial-evidence-writer/2026.1",
        }
        manifest_hash = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        model = self.session.scalar(
            select(AuctionSpatialGenerationManifest)
            .where(AuctionSpatialGenerationManifest.lot_id == expectation.lot_id)
            .with_for_update()
        )
        previous_settled = model.settled if model is not None else False
        changed = model is None or model.manifest_hash != manifest_hash
        if model is None:
            watermark = 1
        elif changed:
            watermark = model.watermark + 1
        else:
            watermark = model.watermark
        generation_json = json.dumps(generations, sort_keys=True, separators=(",", ":"))
        missing_json = json.dumps(sorted(missing), separators=(",", ":"))
        blocking_json = json.dumps(sorted(blocking), separators=(",", ":"))
        if model is None:
            model = AuctionSpatialGenerationManifest(
                lot_id=expectation.lot_id,
                status=status,
                settled=settled,
                manifest_hash=manifest_hash,
                module_generations_json=generation_json,
                missing_feed_keys_json=missing_json,
                blocking_feed_keys_json=blocking_json,
                expires_at=min(expiries) if expiries else None,
                version=expectation.version,
                watermark=watermark,
                updated_at=checked,
            )
            self.session.add(model)
        elif changed:
            model.status = status
            model.settled = settled
            model.manifest_hash = manifest_hash
            model.module_generations_json = generation_json
            model.missing_feed_keys_json = missing_json
            model.blocking_feed_keys_json = blocking_json
            model.expires_at = min(expiries) if expiries else None
            model.version = expectation.version
            model.watermark = watermark
            model.updated_at = checked
        enqueue = changed and (previous_settled or settled)
        if enqueue:
            self.session.add(
                AuctionSpatialDecisionSignal(
                    lot_id=expectation.lot_id,
                    manifest_hash=manifest_hash,
                    manifest_watermark=watermark,
                    status="pending",
                    attempts=0,
                    created_at=checked,
                )
            )
        self.session.flush()
        manifest = SpatialGenerationManifest(
            expectation.lot_id,
            status,
            settled,
            manifest_hash,
            {module: tuple(values) for module, values in generations.items()},
            tuple(sorted(missing)),
            tuple(sorted(blocking)),
            min(expiries) if expiries else None,
            expectation.version,
            checked,
        )
        return manifest, enqueue
