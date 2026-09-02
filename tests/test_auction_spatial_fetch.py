from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app.auction_spatial_fetch import (
    HTTP_TIMEOUT_SECONDS,
    MAX_TOTAL_FETCH_SECONDS,
    SpatialFetchDeferred,
    SpatialFetchTerminal,
    fetch_verified_spatial_feed,
    parse_spatial_fetch_runtime,
)
from app.auction_spatial_source_adapters import SCHEMA_VERSION
from app.provider_backpressure import (
    InMemoryProviderBackend,
    ProviderBackpressure,
)

SECRET = b"s" * 32


def _config(*, auth_mode: str = "hmac_sha256", pin: str | None = None) -> str:
    return json.dumps(
        {
            "providers": [
                {
                    "provider_id": "abay-gis",
                    "registry_version": "abay-gis/2026.1",
                    "allowed_hosts": ["gis.gov.kz"],
                    "authority_or_license": "ГУ Архитектуры области Абай",
                    "authority_bbox": [74.0, 48.0, 77.0, 51.0],
                    "restriction_layers": ["red_lines"],
                    "planning_layers": ["genplan:current_zoning", "pdp:future_zoning"],
                    "site_coverage": ["physical_access", "legal_access"],
                    "qps": 10,
                    "burst": 3,
                    "max_concurrency": 2,
                    "feeds": [
                        {
                            "feed_id": "planning-452662-v1",
                            "module": "planning",
                            "url_template": "https://gis.gov.kz/feed/{lot_id}",
                            "auth_mode": auth_mode,
                            "hmac_secret_b64": (
                                base64.b64encode(SECRET).decode()
                                if auth_mode == "hmac_sha256"
                                else None
                            ),
                            "pinned_sha256": pin,
                        }
                    ],
                }
            ]
        }
    )


def _body(*, amount: str = "Рекреационная зона") -> bytes:
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "feed_kind": "planning",
            "feed_id": "planning-452662-v1",
            "provider_id": "abay-gis",
            "authority_or_license": "ГУ Архитектуры области Абай",
            "document_sha256": "a" * 64,
            "receipt_sha256": "b" * 64,
            "source_url": "https://gis.gov.kz/feed/452662",
            "target_lot_id": "452662",
            "crs": "EPSG:4326",
            "bbox": [75.0, 49.0, 76.0, 50.0],
            "observed_at": "2026-08-17T10:00:00Z",
            "valid_from": "2026-08-01T00:00:00Z",
            "valid_until": None,
            "status": "found",
            "payload": {"zone": amount},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _backpressure(runtime) -> ProviderBackpressure:
    return ProviderBackpressure(
        runtime.policies,
        InMemoryProviderBackend(),
        app_env="test",
    )


def test_hmac_verified_feed_mints_bound_receipt() -> None:
    body = _body()
    signature = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    runtime = parse_spatial_fetch_runtime(_config())
    endpoint = runtime.endpoints[("abay-gis", "planning-452662-v1")]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://gis.gov.kz/feed/452662"
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "application/json",
                "x-zhertap-signature": f"sha256={signature}",
            },
        )

    result = fetch_verified_spatial_feed(
        endpoint,
        source_lot_id="452662",
        backpressure=_backpressure(runtime),
        owner_token="worker-1",
        transport=httpx.MockTransport(handler),
    )
    assert result.feed["target_lot_id"] == "452662"
    assert result.receipt.canonical_feed_sha256 == result.canonical_feed_sha256
    assert result.receipt.provenance_kind == "signed_feed"


def test_invalid_signature_never_mints_receipt() -> None:
    runtime = parse_spatial_fetch_runtime(_config())
    endpoint = runtime.endpoints[("abay-gis", "planning-452662-v1")]
    pressure = _backpressure(runtime)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=_body(),
            headers={"content-type": "application/json", "x-zhertap-signature": "0" * 64},
        )
    )
    with pytest.raises(SpatialFetchTerminal, match="signature") as error:
        fetch_verified_spatial_feed(
            endpoint,
            source_lot_id="452662",
            backpressure=pressure,
            owner_token="worker-2",
            transport=transport,
        )
    assert error.value.code == "signature_invalid"
    metrics = pressure.metrics_snapshot("abay-gis")
    assert metrics.successes == 1
    assert metrics.failures == 0
    assert metrics.active_leases == 0


@pytest.mark.parametrize(
    "location",
    ["https://evil.example/feed", "http://gis.gov.kz/feed", "https://127.0.0.1/feed"],
)
def test_hostile_redirect_is_rejected(location: str) -> None:
    runtime = parse_spatial_fetch_runtime(_config())
    endpoint = runtime.endpoints[("abay-gis", "planning-452662-v1")]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": location})
    )
    with pytest.raises(SpatialFetchTerminal) as error:
        fetch_verified_spatial_feed(
            endpoint,
            source_lot_id="452662",
            backpressure=_backpressure(runtime),
            owner_token="worker-3",
            transport=transport,
        )
    assert error.value.code == "url_forbidden"


def test_pinned_internal_feed_requires_exact_raw_hash() -> None:
    body = _body()
    runtime = parse_spatial_fetch_runtime(
        _config(auth_mode="pinned_sha256", pin=hashlib.sha256(body).hexdigest())
    )
    endpoint = runtime.endpoints[("abay-gis", "planning-452662-v1")]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
        )
    )
    result = fetch_verified_spatial_feed(
        endpoint,
        source_lot_id="452662",
        backpressure=_backpressure(runtime),
        owner_token="worker-4",
        transport=transport,
    )
    assert result.receipt.provenance_kind == "internal_fetch"
    assert result.raw_sha256 == hashlib.sha256(body).hexdigest()


def test_429_returns_retry_without_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = parse_spatial_fetch_runtime(_config())
    endpoint = runtime.endpoints[("abay-gis", "planning-452662-v1")]
    pressure = _backpressure(runtime)
    monkeypatch.setattr("time.sleep", lambda *_: pytest.fail("fetcher must not sleep"))
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            429,
            headers={"retry-after": "3", "content-type": "application/json"},
        )
    )
    with pytest.raises(SpatialFetchDeferred) as error:
        fetch_verified_spatial_feed(
            endpoint,
            source_lot_id="452662",
            backpressure=pressure,
            owner_token="worker-5",
            transport=transport,
        )
    assert error.value.retry_after_seconds == 3
    metrics = pressure.metrics_snapshot("abay-gis")
    assert metrics.failures == 1
    assert metrics.active_leases == 0


def test_backpressure_denial_does_not_call_transport() -> None:
    runtime = parse_spatial_fetch_runtime(_config())
    endpoint = runtime.endpoints[("abay-gis", "planning-452662-v1")]
    pressure = _backpressure(runtime)
    pressure.acquire("abay-gis", owner_token="held-1")
    pressure.acquire("abay-gis", owner_token="held-2")
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    with pytest.raises(SpatialFetchDeferred) as error:
        fetch_verified_spatial_feed(
            endpoint,
            source_lot_id="452662",
            backpressure=pressure,
            owner_token="worker-6",
            transport=httpx.MockTransport(handler),
        )
    assert error.value.code == "concurrency_limited"
    assert called is False


def test_config_rejects_duplicate_keys_and_private_or_unknown_hosts() -> None:
    with pytest.raises(SpatialFetchTerminal):
        parse_spatial_fetch_runtime('{"providers":[],"providers":[]}')
    hostile = _config().replace("gis.gov.kz", "127.0.0.1")
    with pytest.raises(SpatialFetchTerminal, match="host"):
        parse_spatial_fetch_runtime(hostile)


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        ('"target_lot_id":"452662"', '"target_lot_id":"other-lot"'),
        ('"feed_kind":"planning"', '"feed_kind":"site"'),
    ],
)
def test_signed_wrong_lot_or_module_is_rejected_before_receipt(
    replacement: str,
    expected: str,
) -> None:
    body = _body().replace(replacement.encode(), expected.encode())
    signature = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    runtime = parse_spatial_fetch_runtime(_config())
    endpoint = runtime.endpoints[("abay-gis", "planning-452662-v1")]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "application/json",
                "x-zhertap-signature": signature,
            },
        )
    )
    with pytest.raises(SpatialFetchTerminal) as error:
        fetch_verified_spatial_feed(
            endpoint,
            source_lot_id="452662",
            backpressure=_backpressure(runtime),
            owner_token="wrong-identity",
            transport=transport,
        )
    assert error.value.code == "feed_identity_mismatch"


def test_total_deadline_bounds_slow_stream_and_lease_exceeds_worst_overshoot() -> None:
    class Clock:
        value = 100.0

        def __call__(self) -> float:
            return self.value

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            clock.value += MAX_TOTAL_FETCH_SECONDS + 1
            yield _body()

    clock = Clock()
    runtime = parse_spatial_fetch_runtime(_config())
    endpoint = runtime.endpoints[("abay-gis", "planning-452662-v1")]
    policy = runtime.policies["abay-gis"]
    assert policy.lease_ttl_seconds > MAX_TOTAL_FETCH_SECONDS + HTTP_TIMEOUT_SECONDS
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            stream=SlowBody(),
            headers={"content-type": "application/json"},
        )
    )
    with pytest.raises(SpatialFetchDeferred) as error:
        fetch_verified_spatial_feed(
            endpoint,
            source_lot_id="452662",
            backpressure=_backpressure(runtime),
            owner_token="slow-stream",
            transport=transport,
            monotonic=clock,
        )
    assert error.value.code == "fetch_deadline"


def test_cancellation_releases_without_recording_circuit_result() -> None:
    runtime = parse_spatial_fetch_runtime(_config())
    endpoint = runtime.endpoints[("abay-gis", "planning-452662-v1")]
    pressure = _backpressure(runtime)

    def cancelled(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        fetch_verified_spatial_feed(
            endpoint,
            source_lot_id="452662",
            backpressure=pressure,
            owner_token="cancelled",
            transport=httpx.MockTransport(cancelled),
        )
    metrics = pressure.metrics_snapshot("abay-gis")
    assert metrics.active_leases == 0
    assert metrics.successes == metrics.failures == 0
