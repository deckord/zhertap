from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.auction_spatial_evidence_writer import (
    InMemorySpatialEvidenceStore,
    PreparedSpatialObservation,
    SpatialEvidenceWriterError,
    SpatialFeedIdentity,
    SpatialFeedState,
    SpatialManifestExpectation,
    SpatialProcessingFailure,
    event_signature,
    prepare_spatial_observation,
    spatial_adapter_generation,
)
from app.auction_spatial_source_adapters import (
    PRODUCER_VERSION,
    SpatialSourceEnvelope,
    SpatialTrustedReceipt,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
REGISTRY_VERSION = "abay-gis/2026.1"


def identity(module: str, *, lot_id: str = "452662") -> SpatialFeedIdentity:
    return SpatialFeedIdentity(lot_id, module, "abay-gis", f"{module}-feed")


def expectation(lot_id: str = "452662") -> SpatialManifestExpectation:
    return SpatialManifestExpectation(
        lot_id,
        {
            "restrictions": (identity("restrictions", lot_id=lot_id).key,),
            "site": (identity("site", lot_id=lot_id).key,),
            "planning": (identity("planning", lot_id=lot_id).key,),
        },
        "spatial-manifest/2026.1",
    )


def prepared(
    module: str,
    marker: str,
    *,
    status: str = "found",
    lot_id: str = "452662",
    expires_at: datetime | None = None,
) -> PreparedSpatialObservation:
    canonical = marker * 64
    receipt_hash = chr(ord(marker) + 1) * 64
    feed_id = f"{module}-feed"
    receipt = SpatialTrustedReceipt(
        "abay-gis",
        feed_id,
        receipt_hash,
        canonical,
        "signed_feed",
    )
    generation = spatial_adapter_generation(canonical, receipt_hash, REGISTRY_VERSION)
    envelope = SpatialSourceEnvelope(
        module,
        {"generation_id": generation, "marker": marker},
        status,
        NOW - timedelta(hours=1),
        f"https://gis.gov.kz/{module}/{feed_id}",
        generation,
        PRODUCER_VERSION,
    )
    return prepare_spatial_observation(
        identity=identity(module, lot_id=lot_id),
        envelope=envelope,
        canonical_feed_sha256=canonical,
        receipt=receipt,
        registry_version=REGISTRY_VERSION,
        expires_at=expires_at or NOW + timedelta(days=7),
        prepared_at=NOW,
    )


def dirty(item: PreparedSpatialObservation) -> str:
    return event_signature(
        lot_id=item.identity.lot_id,
        module=item.identity.module,
        provider_id=item.identity.provider_id,
        feed_id=item.identity.feed_id,
        source_version=item.envelope.producer_version,
        content_sha256=item.canonical_feed_sha256,
    )


def write(
    store: InMemorySpatialEvidenceStore,
    item: PreparedSpatialObservation,
    expected: SpatialManifestExpectation,
):
    assert store.mark_pending(
        item.identity,
        expected,
        input_signature=dirty(item),
        changed_at=NOW,
    ).changed
    claims = store.claim_due(checked_at=NOW, limit=10, owner_token="worker-1")
    claim = next(value for value in claims.claims if value.identity == item.identity)
    return store.persist_observation_atomic(claim, item, expected, checked_at=NOW)


def test_atomic_trio_manifest_enqueues_w14_only_when_complete_and_changed() -> None:
    store = InMemorySpatialEvidenceStore()
    expected = expectation()
    first = write(store, prepared("restrictions", "a"), expected)
    second = write(store, prepared("site", "c"), expected)
    third_item = prepared("planning", "e")
    third = write(store, third_item, expected)
    assert first.manifest.status == "incomplete" and not first.enqueue_w14
    assert second.manifest.status == "incomplete" and not second.enqueue_w14
    assert third.manifest.status == "complete" and third.enqueue_w14
    assert set(third.manifest.module_generations) == {"restrictions", "site", "planning"}

    # Scheduled validation of byte-identical evidence does not append or enqueue.
    claim = store.claim_due(
        checked_at=NOW + timedelta(hours=25), limit=10, owner_token="validator"
    )
    planning_claim = next(value for value in claim.claims if value.identity.module == "planning")
    repeated = store.persist_observation_atomic(
        planning_claim,
        third_item,
        expected,
        checked_at=NOW + timedelta(hours=25),
    )
    assert repeated.status == "already_current"
    assert repeated.enqueue_w14 is False
    assert len(store.evidence) == 3

    changed_planning = prepared("planning", "7")
    pending = store.mark_pending(
        changed_planning.identity,
        expected,
        input_signature=dirty(changed_planning),
        changed_at=NOW + timedelta(hours=26),
    )
    assert pending.changed is True
    assert pending.manifest.status == "incomplete"
    assert pending.enqueue_w14 is True

    changed_claim = next(
        value
        for value in store.claim_due(
            checked_at=NOW + timedelta(hours=26), limit=10, owner_token="updater"
        ).claims
        if value.identity.module == "planning"
    )
    conflict_item = prepared("planning", "7", status="conflict")
    conflict = store.persist_observation_atomic(
        changed_claim,
        conflict_item,
        expected,
        checked_at=NOW + timedelta(hours=26),
    )
    assert conflict.manifest.status == "conflict"
    assert conflict.manifest.settled is True
    assert conflict.enqueue_w14 is True


def test_a_to_b_to_a_appends_reactivation_then_identical_event_is_noop() -> None:
    store = InMemorySpatialEvidenceStore()
    expected = expectation()
    a = prepared("restrictions", "a")
    b = prepared("restrictions", "c", status="conflict")
    first = write(store, a, expected)
    second = write(store, b, expected)
    third = write(store, a, expected)
    assert first.status == second.status == third.status == "written"
    rows = [row for row in store.evidence if row.identity.module == "restrictions"]
    assert [row.status for row in rows] == ["found", "conflict", "found"]
    assert third.manifest.status == "incomplete"
    assert store.mark_pending(
        a.identity,
        expected,
        input_signature=dirty(a),
        changed_at=NOW,
    ).changed is False


def test_newest_conflict_and_terminal_quarantine_block_old_found() -> None:
    store = InMemorySpatialEvidenceStore()
    expected = expectation()
    write(store, prepared("restrictions", "a"), expected)
    conflict = write(store, prepared("restrictions", "c", status="conflict"), expected)
    assert conflict.manifest.status == "conflict"
    assert identity("restrictions").key in conflict.manifest.blocking_feed_keys

    site = prepared("site", "e")
    assert store.mark_pending(
        site.identity,
        expected,
        input_signature=dirty(site),
        changed_at=NOW,
    ).changed
    claim = store.claim_due(checked_at=NOW, limit=10, owner_token="worker").claims[0]
    failed = store.persist_failure_atomic(
        claim,
        SpatialProcessingFailure("invalid_geometry", "x" * 2_000, retryable=False),
        expected,
        checked_at=NOW,
    )
    assert failed.status == "quarantined"
    assert failed.manifest.status == "conflict"
    assert store.evidence[-1].status == "quarantine"
    assert len(store.evidence[-1].error_message or "") == 500


def test_initial_partial_to_all_settled_conflict_enqueues_null_decision() -> None:
    store = InMemorySpatialEvidenceStore()
    expected = expectation()
    write(store, prepared("restrictions", "a"), expected)
    write(store, prepared("site", "c"), expected)
    settled = write(store, prepared("planning", "e", status="conflict"), expected)
    assert settled.manifest.status == "conflict"
    assert settled.manifest.settled is True
    assert settled.enqueue_w14 is True


@pytest.mark.parametrize("retryable", [True, False])
def test_complete_to_retry_or_quarantine_always_enqueues_invalidation(
    retryable: bool,
) -> None:
    store = InMemorySpatialEvidenceStore()
    expected = expectation()
    for module, marker in (("restrictions", "a"), ("site", "c"), ("planning", "e")):
        write(store, prepared(module, marker), expected)
    changed = prepared("planning", "7")
    pending = store.mark_pending(
        changed.identity,
        expected,
        input_signature=dirty(changed),
        changed_at=NOW,
    )
    assert pending.enqueue_w14 is True
    claim = store.claim_due(checked_at=NOW, limit=10, owner_token="failure").claims[0]
    failed = store.persist_failure_atomic(
        claim,
        SpatialProcessingFailure("upstream_failure", "failed", retryable),
        expected,
        checked_at=NOW,
    )
    assert failed.manifest.status == "conflict"
    assert failed.manifest.settled is True
    assert failed.enqueue_w14 is True


def test_expiry_invalidates_complete_manifest_before_refetch_network_work() -> None:
    store = InMemorySpatialEvidenceStore()
    expected = expectation()
    write(store, prepared("restrictions", "a"), expected)
    write(store, prepared("site", "c", expires_at=NOW + timedelta(hours=2)), expected)
    completed = write(store, prepared("planning", "e"), expected)
    assert completed.manifest.status == "complete"

    due = store.claim_due(
        checked_at=NOW + timedelta(hours=2), limit=10, owner_token="expiry-worker"
    )
    assert any(claim.identity.module == "site" for claim in due.claims)
    assert len(due.invalidated_manifests) == 1
    invalidated = due.invalidated_manifests[0]
    assert invalidated.status == "conflict"
    assert invalidated.settled is True
    assert store.manifest("452662") == invalidated


def test_claim_for_content_a_rejects_prepared_content_b() -> None:
    store = InMemorySpatialEvidenceStore()
    expected = expectation()
    source_a = prepared("planning", "a")
    source_b = prepared("planning", "c")
    store.mark_pending(
        source_a.identity,
        expected,
        input_signature=dirty(source_a),
        changed_at=NOW,
    )
    claim = store.claim_due(checked_at=NOW, limit=1, owner_token="worker").claims[0]
    with pytest.raises(SpatialEvidenceWriterError, match="does not bind"):
        store.persist_observation_atomic(claim, source_b, expected, checked_at=NOW)


def test_retryable_failure_has_bounded_backoff_and_expired_claim_recovers() -> None:
    store = InMemorySpatialEvidenceStore()
    expected = expectation()
    item = prepared("planning", "a")
    signature = dirty(item)
    store.mark_pending(item.identity, expected, input_signature=signature, changed_at=NOW)
    claim = store.claim_due(checked_at=NOW, limit=1, owner_token="worker").claims[0]
    failed = store.persist_failure_atomic(
        claim,
        SpatialProcessingFailure("upstream_503", "temporary", retryable=True),
        expected,
        checked_at=NOW,
    )
    assert failed.retry_after_seconds is not None
    assert 2 <= failed.retry_after_seconds <= 3
    assert failed.manifest.status == "conflict"
    assert store.claim_due(
        checked_at=NOW + timedelta(seconds=1), limit=1, owner_token="early"
    ).claims == ()
    assert store.claim_due(
        checked_at=NOW + timedelta(seconds=failed.retry_after_seconds),
        limit=1,
        owner_token="retry",
    ).claims

    other = prepared("site", "c")
    store.mark_pending(other.identity, expected, input_signature=dirty(other), changed_at=NOW)
    original = store.claim_due(
        checked_at=NOW, limit=1, owner_token="dead-worker"
    ).claims[0]
    assert original.identity == other.identity
    recovered = store.claim_due(
        checked_at=NOW + timedelta(minutes=10), limit=1, owner_token="recovery"
    ).claims[0]
    assert recovered.identity == other.identity
    assert recovered.token != original.token


def test_expired_input_and_untrusted_receipt_fail_before_persistence() -> None:
    with pytest.raises(SpatialEvidenceWriterError, match="expired"):
        prepared("planning", "a", expires_at=NOW - timedelta(minutes=1))

    good = prepared("planning", "a")
    wrong = SpatialTrustedReceipt(
        "other-provider",
        good.identity.feed_id,
        good.receipt.receipt_sha256,
        good.canonical_feed_sha256,
        "signed_feed",
    )
    with pytest.raises(SpatialEvidenceWriterError, match="identity mismatch"):
        prepare_spatial_observation(
            identity=good.identity,
            envelope=good.envelope,
            canonical_feed_sha256=good.canonical_feed_sha256,
            receipt=wrong,
            registry_version=REGISTRY_VERSION,
            expires_at=good.expires_at,
            prepared_at=NOW,
        )


def test_10k_quiescent_states_have_constant_small_due_scan() -> None:
    future = NOW + timedelta(days=1)
    states = []
    for index in range(10_000):
        item_identity = SpatialFeedIdentity(
            f"lot-{index}", "planning", "abay-gis", "planning-feed"
        )
        states.append(
            SpatialFeedState(
                item_identity,
                "ready",
                "a" * 64,
                current_evidence_id=index + 1,
                current_generation_id="b" * 64,
                current_payload_hash="c" * 64,
                observed_at=NOW,
                next_validation_at=future,
            )
        )
    store = InMemorySpatialEvidenceStore(states)
    assert store.claim_due(checked_at=NOW, limit=50, owner_token="worker").claims == ()
    assert store.inspected_last_claim == 1


def test_worklist_is_bounded_and_claim_owner_is_enforced() -> None:
    store = InMemorySpatialEvidenceStore()
    for index in range(100):
        item_identity = SpatialFeedIdentity(
            f"lot-{index}", "planning", "abay-gis", "planning-feed"
        )
        item_expectation = SpatialManifestExpectation(
            item_identity.lot_id,
            {
                "restrictions": (identity("restrictions", lot_id=item_identity.lot_id).key,),
                "site": (identity("site", lot_id=item_identity.lot_id).key,),
                "planning": (item_identity.key,),
            },
            "v1",
        )
        store.mark_pending(
            item_identity,
            item_expectation,
            input_signature="a" * 64,
            changed_at=NOW,
        )
    claims = store.claim_due(checked_at=NOW, limit=5_000, owner_token="worker")
    assert len(claims.claims) == 50
    wrong = replace_claim_token(claims.claims[0])
    with pytest.raises(SpatialEvidenceWriterError, match="stale"):
        store.persist_failure_atomic(
            wrong,
            SpatialProcessingFailure("x", "x", False),
            SpatialManifestExpectation(
                wrong.identity.lot_id,
                {
                    "restrictions": (identity("restrictions", lot_id=wrong.identity.lot_id).key,),
                    "site": (identity("site", lot_id=wrong.identity.lot_id).key,),
                    "planning": (wrong.identity.key,),
                },
                "v1",
            ),
            checked_at=NOW,
        )


def replace_claim_token(claim):
    return type(claim)(claim.identity, "wrong-token", claim.input_signature)
