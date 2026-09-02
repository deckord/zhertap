from __future__ import annotations

import gzip
import json

import httpx
import pytest

from app.provider_backpressure import (
    DEFAULT_PROVIDER_POLICIES,
    InMemoryProviderBackend,
    ProviderBackpressure,
    ProviderPolicy,
)
from app.provider_guard import (
    MAX_PROVIDER_RESPONSE_BYTES,
    ProviderCallDeferred,
    bounded_http_request,
    guarded_http_call,
)


def _limiter(provider: str = "egkn") -> ProviderBackpressure:
    policy = ProviderPolicy(
        provider,
        qps=100,
        burst=100,
        max_concurrency=2,
        lease_ttl_seconds=240,
        failure_threshold=2,
        open_base_seconds=5,
        open_max_seconds=60,
    )
    return ProviderBackpressure(
        {provider: policy}, InMemoryProviderBackend(), app_env="test", clock=lambda: 100.0
    )


def _response(status: int, *, retry_after: str | None = None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return httpx.Response(status, headers=headers, request=httpx.Request("GET", "https://x"))


def test_429_uses_retry_after_records_failure_and_releases_lease() -> None:
    limiter = _limiter()
    with pytest.raises(ProviderCallDeferred) as caught:
        guarded_http_call("egkn", lambda: _response(429, retry_after="29"), backpressure=limiter)
    assert caught.value.retry_after_seconds >= 29
    metrics = limiter.metrics_snapshot("egkn")
    assert metrics.failures == 1
    assert metrics.active_leases == 0


@pytest.mark.parametrize("status", [500, 503])
def test_5xx_is_transient_and_counts_toward_circuit(status: int) -> None:
    limiter = _limiter()
    with pytest.raises(ProviderCallDeferred, match=f"http_{status}"):
        guarded_http_call("egkn", lambda: _response(status), backpressure=limiter)
    assert limiter.metrics_snapshot("egkn").failures == 1


def test_network_error_is_transient_but_4xx_is_permanent_and_not_circuit_failure() -> None:
    limiter = _limiter()

    def network() -> httpx.Response:
        raise httpx.ConnectError("offline", request=httpx.Request("GET", "https://x"))

    with pytest.raises(ProviderCallDeferred, match="network_error"):
        guarded_http_call("egkn", network, backpressure=limiter)
    assert limiter.metrics_snapshot("egkn").failures == 1

    permanent = _limiter()
    with pytest.raises(httpx.HTTPStatusError):
        guarded_http_call("egkn", lambda: _response(404), backpressure=permanent)
    metrics = permanent.metrics_snapshot("egkn")
    assert metrics.failures == 0
    assert metrics.active_leases == 0


def test_parser_failure_occurs_after_successful_network_finalize_without_lease_leak() -> None:
    limiter = _limiter()
    response = guarded_http_call("egkn", lambda: _response(200), backpressure=limiter)
    with pytest.raises(ValueError):
        response.json()
    metrics = limiter.metrics_snapshot("egkn")
    assert metrics.successes == 1
    assert metrics.failures == 0
    assert metrics.active_leases == 0


def test_callback_parser_or_cancellation_error_releases_without_poisoning_circuit() -> None:
    limiter = _limiter()

    def parser_inside() -> httpx.Response:
        raise ValueError("bad parser input")

    with pytest.raises(ValueError, match="bad parser"):
        guarded_http_call("egkn", parser_inside, backpressure=limiter)
    metrics = limiter.metrics_snapshot("egkn")
    assert metrics.failures == 0
    assert metrics.active_leases == 0


def test_optional_local_rate_limit_wait_retries_acquire() -> None:
    limiter = ProviderBackpressure(
        {
            "egkn": ProviderPolicy(
                "egkn",
                qps=10,
                burst=1,
                max_concurrency=1,
                lease_ttl_seconds=240,
            )
        },
        InMemoryProviderBackend(),
        app_env="test",
    )

    assert guarded_http_call("egkn", lambda: _response(200), backpressure=limiter).is_success
    with pytest.raises(ProviderCallDeferred, match="rate_limited"):
        guarded_http_call("egkn", lambda: _response(200), backpressure=limiter)

    response = guarded_http_call(
        "egkn",
        lambda: _response(200),
        backpressure=limiter,
        wait_for_rate_limit_seconds=1,
    )

    assert response.is_success


def test_provider_leases_outlive_maximum_http_timeouts() -> None:
    maximum_http_timeouts = {
        "eqazyna": 120,
        "egkn": 180,
        "osm_overpass": 60,
        "gov_kz": 120,
        "auction_documents": 120,
    }
    for provider, timeout_seconds in maximum_http_timeouts.items():
        assert DEFAULT_PROVIDER_POLICIES[provider].lease_ttl_seconds > timeout_seconds
        assert DEFAULT_PROVIDER_POLICIES[provider].lease_ttl_seconds > 300


def test_bounded_http_request_rejects_hostile_body_before_buffering() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Length": str(MAX_PROVIDER_RESPONSE_BYTES + 1)},
            content=b"not-read",
            request=request,
        )
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ValueError, match="byte cap"):
            bounded_http_request(client, "GET", "https://provider.test/huge")


def test_bounded_http_request_honors_provider_specific_byte_cap() -> None:
    payload = b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1024)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload, request=request)
    )

    with httpx.Client(transport=transport) as client:
        response = bounded_http_request(
            client,
            "GET",
            "https://provider.test/large-but-allowed",
            max_bytes=len(payload),
        )

    assert response.content == payload


def test_bounded_http_request_rebuilds_gzip_decoded_response_without_double_decode() -> None:
    payload = json.dumps({"items": ["лот", 452662]}).encode()
    compressed = gzip.compress(payload)

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
            },
            content=compressed,
            request=request,
        )
    )
    with httpx.Client(transport=transport) as client:
        response = bounded_http_request(client, "GET", "https://provider.test/feed")

    assert response.content == payload
    assert response.json() == {"items": ["лот", 452662]}
    assert "Content-Encoding" not in response.headers
    # httpx may add a fresh length for the decoded body; it must not retain the
    # upstream compressed-wire length.
    assert response.headers.get("Content-Length") == str(len(payload))


def test_bounded_http_request_strips_br_after_stream_decoder() -> None:
    """The reconstruction rule is codec-neutral even without optional brotli installed."""

    payload = b'{"feed":"planning"}'
    request = httpx.Request("GET", "https://provider.test/feed")

    class DecodedBrResponse:
        status_code = 200
        headers = httpx.Headers(
            {"Content-Encoding": "br", "Content-Length": "9"}
        )
        extensions: dict[str, object] = {}

        def __init__(self) -> None:
            self.request = request

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield payload

    class DecodedBrClient:
        def stream(self, *_args, **_kwargs):
            return DecodedBrResponse()

    response = bounded_http_request(
        DecodedBrClient(),  # type: ignore[arg-type]
        "GET",
        "https://provider.test/feed",
    )

    assert response.content == payload
    assert "Content-Encoding" not in response.headers
    assert response.headers.get("Content-Length") == str(len(payload))


def test_malformed_content_encoding_is_classified_as_transient_provider_failure() -> None:
    limiter = _limiter()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=b"not-a-gzip-stream",
            request=request,
        )
    )

    with httpx.Client(transport=transport) as client:
        with pytest.raises(ProviderCallDeferred, match="network_error"):
            guarded_http_call(
                "egkn",
                lambda: bounded_http_request(
                    client,
                    "GET",
                    "https://provider.test/malformed",
                ),
                backpressure=limiter,
            )

    metrics = limiter.metrics_snapshot("egkn")
    assert metrics.failures == 1
    assert metrics.active_leases == 0
