from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import auction_spatial_worker as worker
from app.auction_spatial_evidence_store import SqlAlchemySpatialEvidenceStore
from app.auction_spatial_evidence_writer import (
    SpatialFeedIdentity,
    SpatialManifestExpectation,
    event_signature,
    spatial_adapter_generation,
)
from app.auction_spatial_fetch import SpatialFeedEndpoint, VerifiedSpatialFeed
from app.auction_spatial_source_adapters import (
    PRODUCER_VERSION,
    SpatialAdapterResult,
    SpatialSourceEnvelope,
    SpatialTrustedProvider,
    SpatialTrustedReceipt,
)
from app.db import Base
from app.models import (
    AuctionEvidence,
    AuctionLot,
    AuctionSpatialFeedState,
    AuctionSpatialManifestExpectation,
)
from app.provider_backpressure import ProviderPolicy

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
PARCEL = {
    "type": "Polygon",
    "coordinates": [
        [[75.1, 49.1], [75.2, 49.1], [75.2, 49.2], [75.1, 49.2], [75.1, 49.1]]
    ],
}


def _setup(*, parcel: bool):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    identity = SpatialFeedIdentity("lot-1", "planning", "abay-gis", "planning-1")
    other = {
        module: SpatialFeedIdentity("lot-1", module, "abay-gis", f"{module}-1")
        for module in ("restrictions", "site")
    }
    expectation = SpatialManifestExpectation(
        "lot-1",
        {
            "restrictions": (other["restrictions"].key,),
            "site": (other["site"].key,),
            "planning": (identity.key,),
        },
        "spatial-manifest/2026.1",
    )
    with factory() as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-1",
                source="e-qazyna",
                source_lot_id="452662",
                source_url="https://example.kz/452662",
                title="Кемпинг",
                purpose="строительство кемпинга",
            )
        )
        if parcel:
            session.add(
                AuctionEvidence(
                    lot_id="lot-1",
                    evidence_type="cadastre_boundary",
                    status="found",
                    title="ЕГКН",
                    raw_payload_json=json.dumps(
                        {"geometry_geojson": PARCEL, "source_layer": "egkn"}
                    ),
                    observed_at=NOW,
                )
            )
    with factory() as session:
        SqlAlchemySpatialEvidenceStore(session).mark_pending(
            identity,
            expectation,
            input_signature="a" * 64,
            changed_at=NOW,
        )
    with factory() as session:
        claim = SqlAlchemySpatialEvidenceStore(session).claim_due(
            checked_at=NOW, limit=1, owner_token="worker"
        ).claims[0]
    return factory, identity, claim


def test_missing_parcel_defers_without_network_or_open_parse_transaction() -> None:
    factory, identity, claim = _setup(parcel=False)
    called = False

    def no_fetch(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not run without parcel")

    runtime = SimpleNamespace(
        endpoints={("abay-gis", "planning-1"): SimpleNamespace(module="planning")},
        registry={"abay-gis": SimpleNamespace(registry_version="abay/2026.1")},
    )
    result = worker.process_spatial_claim(
        factory,
        claim,
        runtime=runtime,
        backpressure=object(),
        owner_token="owner",
        checked_at=NOW,
        fetcher=no_fetch,
    )
    assert result.status == "retryable"
    assert called is False
    with factory() as session:
        state = session.scalar(
            select(AuctionSpatialFeedState).where(
                AuctionSpatialFeedState.identity_key == identity.key
            )
        )
        assert state.status == "retryable"


def test_claim_a_fetch_b_marks_new_pending_signature_and_never_persists_under_a(
    monkeypatch,
) -> None:
    factory, identity, claim = _setup(parcel=True)
    canonical = "b" * 64
    receipt_hash = "c" * 64
    registry_version = "abay/2026.1"
    receipt = SpatialTrustedReceipt(
        "abay-gis", "planning-1", receipt_hash, canonical, "signed_feed"
    )
    envelope = SpatialSourceEnvelope(
        "planning",
        {"planning_sources": [], "planning_features": []},
        "found",
        NOW,
        "https://gis.gov.kz/planning/452662",
        spatial_adapter_generation(canonical, receipt_hash, registry_version),
    )
    verified = VerifiedSpatialFeed(
        {
            "feed_kind": "planning",
            "valid_until": (NOW + timedelta(days=10)).isoformat(),
        },
        receipt,
        canonical,
        "d" * 64,
        "https://gis.gov.kz/planning/452662",
    )
    monkeypatch.setattr(
        worker,
        "_adapt",
        lambda *args, **kwargs: SpatialAdapterResult(envelope, ()),
    )
    runtime = SimpleNamespace(
        endpoints={("abay-gis", "planning-1"): SimpleNamespace(module="planning")},
        registry={
            "abay-gis": SimpleNamespace(registry_version=registry_version)
        },
    )
    result = worker.process_spatial_claim(
        factory,
        claim,
        runtime=runtime,
        backpressure=object(),
        owner_token="owner",
        checked_at=NOW,
        fetcher=lambda *args, **kwargs: verified,
    )
    expected = event_signature(
        lot_id="lot-1",
        module="planning",
        provider_id="abay-gis",
        feed_id="planning-1",
        source_version=PRODUCER_VERSION,
        content_sha256=canonical,
    )
    assert result.status == "superseded"
    with factory() as session:
        state = session.scalar(
            select(AuctionSpatialFeedState).where(
                AuctionSpatialFeedState.identity_key == identity.key
            )
        )
        assert state.status == "pending"
        assert state.input_signature == expected
        assert state.current_evidence_id is None


def test_bootstrap_is_bounded_keyset_and_covers_all_three_modules() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session, session.begin():
        for index in range(7):
            session.add(
                AuctionLot(
                    id=f"lot-{index:02d}",
                    source="e-qazyna",
                    source_lot_id=str(452662 + index),
                    source_url=f"https://example.kz/{index}",
                    title="Земля",
                )
            )
    endpoints = {}
    registry = {}
    for module in ("restrictions", "site", "planning"):
        endpoint = SpatialFeedEndpoint(
            provider_id=f"{module}-provider",
            feed_id=f"{module}-feed",
            module=module,
            url_template=f"https://gis.gov.kz/{module}/{{lot_id}}",
            auth_mode="pinned_sha256",
            hmac_secret=None,
            pinned_sha256="a" * 64,
            allowed_hosts=("gis.gov.kz",),
        )
        endpoints[(endpoint.provider_id, endpoint.feed_id)] = endpoint
        registry[endpoint.provider_id] = SpatialTrustedProvider(
            provider_id=endpoint.provider_id,
            registry_version=f"{module}/2026.1",
            allowed_feed_kinds=(module,),
            allowed_https_hosts=("gis.gov.kz",),
            authority_or_license_sha256="b" * 64,
            authority_bbox=(74.0, 48.0, 77.0, 51.0),
            allowed_restriction_layers=("red_lines",)
            if module == "restrictions"
            else (),
            allowed_planning_layers=("genplan:current_zoning",)
            if module == "planning"
            else (),
            allowed_site_coverage=("physical_access",)
            if module == "site"
            else (),
        )
    policies = {
        provider_id: ProviderPolicy(provider_id, 1, 1, 1, 60)
        for provider_id in registry
    }
    runtime = SimpleNamespace(
        endpoints=endpoints,
        registry=registry,
        policies=policies,
    )
    first = worker.seed_spatial_feed_states(
        factory,
        runtime=runtime,
        after_lot_id=None,
        high_water_lot_id=None,
        limit=5,
        checked_at=NOW,
    )
    assert first.lots_scanned == 5
    assert first.feeds_created_or_changed == 15
    assert first.has_more is True
    second = worker.seed_spatial_feed_states(
        factory,
        runtime=runtime,
        after_lot_id=first.next_after_lot_id,
        high_water_lot_id=first.high_water_lot_id,
        limit=5,
        checked_at=NOW,
    )
    assert second.lots_scanned == 2
    assert second.has_more is False
    quiescent = worker.seed_spatial_feed_states(
        factory,
        runtime=runtime,
        after_lot_id=None,
        high_water_lot_id=None,
        limit=5,
        checked_at=NOW,
    )
    assert quiescent.lots_scanned == 0
    assert quiescent.has_more is False
    with factory() as session:
        states = session.scalars(select(AuctionSpatialFeedState)).all()
    assert len(states) == 21


def test_config_reconcile_same_count_rotation_retire_and_readd_is_fail_safe() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-1",
                source="e-qazyna",
                source_lot_id="452662",
                source_url="https://example.kz/452662",
                title="Земля",
            )
        )

    def runtime(planning_feed: str, *, pin: str = "a" * 64, suffix: str = ""):
        endpoints = {}
        registry = {}
        policies = {}
        for module in ("restrictions", "site", "planning"):
            provider_id = f"{module}-provider"
            feed_id = planning_feed if module == "planning" else f"{module}-feed"
            endpoint = SpatialFeedEndpoint(
                provider_id,
                feed_id,
                module,
                (
                    f"https://gis.gov.kz/{module}{suffix}/{{lot_id}}"
                    if module == "planning"
                    else f"https://gis.gov.kz/{module}/{{lot_id}}"
                ),
                "pinned_sha256",
                None,
                pin if module == "planning" else "a" * 64,
                ("gis.gov.kz",),
            )
            endpoints[(provider_id, feed_id)] = endpoint
            registry[provider_id] = SpatialTrustedProvider(
                provider_id,
                f"{module}/2026.1",
                (module,),
                ("gis.gov.kz",),
                "b" * 64,
                (74.0, 48.0, 77.0, 51.0),
                ("red_lines",) if module == "restrictions" else (),
                ("genplan:current_zoning",) if module == "planning" else (),
                ("physical_access",) if module == "site" else (),
            )
            policies[provider_id] = ProviderPolicy(provider_id, 1, 1, 1, 60)
        return SimpleNamespace(
            endpoints=endpoints,
            registry=registry,
            policies=policies,
        )

    old = runtime("planning-c")
    worker.seed_spatial_feed_states(
        factory,
        runtime=old,
        after_lot_id=None,
        high_water_lot_id=None,
        checked_at=NOW,
    )
    new = runtime("planning-d")
    # Simulate the former multi-transaction crash: expectation says new config,
    # while the same-count state set still contains obsolete C and lacks D.
    with factory() as session, session.begin():
        model = session.get(AuctionSpatialManifestExpectation, "lot-1")
        model.version = worker._expectation_version(new)
    repaired = worker.seed_spatial_feed_states(
        factory,
        runtime=new,
        after_lot_id=None,
        high_water_lot_id=None,
        checked_at=NOW,
    )
    assert repaired.lots_scanned == 1
    with factory() as session:
        states = {
            row.feed_id: row
            for row in session.scalars(
                select(AuctionSpatialFeedState).where(
                    AuctionSpatialFeedState.lot_id == "lot-1"
                )
            )
        }
    assert states["planning-c"].status == "terminal"
    assert states["planning-c"].last_error_code == "config_retired"
    assert states["planning-d"].status == "pending"

    rotated = runtime("planning-d", pin="d" * 64, suffix="-v2")
    rotation = worker.seed_spatial_feed_states(
        factory,
        runtime=rotated,
        after_lot_id=None,
        high_water_lot_id=None,
        checked_at=NOW,
    )
    assert rotation.feeds_created_or_changed == 1

    readded = worker.seed_spatial_feed_states(
        factory,
        runtime=old,
        after_lot_id=None,
        high_water_lot_id=None,
        checked_at=NOW,
    )
    assert readded.lots_scanned == 1
    with factory() as session:
        states = {
            row.feed_id: row
            for row in session.scalars(
                select(AuctionSpatialFeedState).where(
                    AuctionSpatialFeedState.lot_id == "lot-1"
                )
            )
        }
    assert states["planning-c"].status == "pending"
    assert states["planning-d"].status == "terminal"
    with factory() as session, session.begin():
        session.get(AuctionLot, "lot-1").active = False
    with factory() as session:
        claims = SqlAlchemySpatialEvidenceStore(session).claim_due(
            checked_at=NOW, limit=20, owner_token="inactive-check"
        ).claims
    assert claims == ()
