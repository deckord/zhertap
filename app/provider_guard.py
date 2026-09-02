"""Typed non-blocking guard for concrete outbound provider HTTP calls."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from time import monotonic, perf_counter, sleep

import httpx

from app.config import get_settings
from app.provider_backpressure import (
    ProviderBackpressure,
    create_redis_provider_backpressure,
)


class ProviderCallDeferred(RuntimeError):
    def __init__(self, provider: str, reason: str, retry_after_seconds: float) -> None:
        retry = min(max(float(retry_after_seconds), 0.1), 86_400.0)
        super().__init__(f"{provider} deferred: {reason}")
        self.provider = provider
        self.reason = reason
        self.retry_after_seconds = retry


MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024
ABSOLUTE_PROVIDER_RESPONSE_BYTES = 64 * 1024 * 1024
PROVIDER_TOTAL_DEADLINE_SECONDS = 120.0


def bounded_http_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    total_deadline_seconds: float = PROVIDER_TOTAL_DEADLINE_SECONDS,
    **kwargs: object,
) -> httpx.Response:
    """Stream one provider response under a hard byte cap and wall-clock deadline."""
    bounded_bytes = max(1, min(int(max_bytes), ABSOLUTE_PROVIDER_RESPONSE_BYTES))
    deadline = monotonic() + max(1.0, min(float(total_deadline_seconds), 120.0))
    content = bytearray()
    # Small protocol fakes used by parser tests expose only get(); production
    # httpx.Client always takes the streamed branch below.
    if not hasattr(client, "stream"):
        getter = getattr(client, method.lower())
        return getter(url, **kwargs)
    with client.stream(method, url, **kwargs) as response:
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                parsed_length = int(length)
            except ValueError:
                parsed_length = None
            if parsed_length is not None and parsed_length > bounded_bytes:
                raise ValueError("provider response exceeds byte cap")
        for chunk in response.iter_bytes():
            if monotonic() > deadline:
                raise httpx.ReadTimeout(
                    "provider total response deadline exceeded", request=response.request
                )
            if len(content) + len(chunk) > bounded_bytes:
                raise ValueError("provider response exceeds byte cap")
            content.extend(chunk)
        # ``iter_bytes`` has already applied HTTP content decoding.  Keeping the
        # upstream encoding header on the reconstructed response would make
        # httpx decode the buffered gzip/br payload a second time when callers
        # access ``content`` or ``json()``.  Content-Length likewise describes
        # the wire representation, not these decoded bytes.
        headers = httpx.Headers(response.headers)
        headers.pop("Content-Encoding", None)
        headers.pop("Content-Length", None)
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=bytes(content),
            request=response.request,
            extensions=response.extensions,
        )


@lru_cache(maxsize=4)
def configured_provider_backpressure(redis_url: str, app_env: str) -> ProviderBackpressure:
    return create_redis_provider_backpressure(redis_url, app_env=app_env)


def default_provider_backpressure() -> ProviderBackpressure:
    settings = get_settings()
    return configured_provider_backpressure(settings.redis_url, settings.app_env)


def _retry_after(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    raw = response.headers.get("Retry-After")
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and 0 <= value <= 86_400 else None


def guarded_http_call[T](
    provider: str,
    call: Callable[[], T],
    *,
    backpressure: ProviderBackpressure | None = None,
    wait_for_rate_limit_seconds: float = 0,
) -> T:
    """Execute exactly one call; transient failures defer without worker sleep."""
    limiter = backpressure or default_provider_backpressure()
    wait_budget = max(0.0, min(float(wait_for_rate_limit_seconds), 30.0))
    wait_deadline = monotonic() + wait_budget
    permit = limiter.acquire(provider)
    while (
        permit.status == "rate_limited"
        and wait_budget > 0
        and permit.retry_after_seconds <= max(0.0, wait_deadline - monotonic())
    ):
        sleep(max(0.001, permit.retry_after_seconds))
        permit = limiter.acquire(provider)
    if not permit.allowed:
        raise ProviderCallDeferred(provider, permit.status, permit.retry_after_seconds)
    started = perf_counter()
    finalized = False
    try:
        result = call()
        if isinstance(result, httpx.Response):
            status = result.status_code
            if status == 429 or status >= 500:
                latency = (perf_counter() - started) * 1_000
                limiter.record_result(
                    permit,
                    success=False,
                    latency_ms=latency,
                    error=f"HTTP {status}",
                )
                finalized = True
                delay = limiter.retry_delay_seconds(
                    provider,
                    attempt=0,
                    owner_token=permit.owner_token,
                    server_retry_after_seconds=_retry_after(result),
                )
                raise ProviderCallDeferred(provider, f"http_{status}", delay)
            if status >= 400:
                limiter.release(permit)
                finalized = True
                result.raise_for_status()
        limiter.record_result(
            permit,
            success=True,
            latency_ms=(perf_counter() - started) * 1_000,
        )
        finalized = True
        return result
    except ProviderCallDeferred:
        raise
    except (httpx.RequestError, OSError) as exc:
        limiter.record_result(
            permit,
            success=False,
            latency_ms=(perf_counter() - started) * 1_000,
            error=type(exc).__name__,
        )
        finalized = True
        delay = limiter.retry_delay_seconds(provider, attempt=0, owner_token=permit.owner_token)
        raise ProviderCallDeferred(provider, "network_error", delay) from exc
    except BaseException:
        # Parser, validation, cancellation and permanent 4xx do not poison circuit state.
        if not finalized:
            limiter.release(permit)
            finalized = True
        raise
    finally:
        if not finalized:
            limiter.release(permit)
