from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.provider_backpressure import (
    InMemoryProviderBackend,
    ProviderBackpressure,
    ProviderPermit,
    ProviderPolicy,
    create_redis_provider_backpressure,
)


class Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class OutageBackend:
    def __getattr__(self, _name: str):
        def unavailable(*_args: object, **_kwargs: object) -> object:
            raise RedisConnectionError("redis offline")

        return unavailable


def policy(**overrides: object) -> ProviderPolicy:
    values: dict[str, object] = {
        "provider": "egkn",
        "qps": 100.0,
        "burst": 100,
        "max_concurrency": 3,
        "lease_ttl_seconds": 10,
        "failure_threshold": 2,
        "open_base_seconds": 5,
        "open_max_seconds": 30,
        "half_open_max_calls": 1,
        "jitter_fraction": 0.2,
        "queue_warning_depth": 10,
        "queue_critical_depth": 20,
    }
    values.update(overrides)
    return ProviderPolicy(**values)


def guard(
    clock: Clock,
    *,
    selected_policy: ProviderPolicy | None = None,
    backend: object | None = None,
    app_env: str = "test",
    fallback: InMemoryProviderBackend | None = None,
) -> ProviderBackpressure:
    selected = selected_policy or policy()
    return ProviderBackpressure(
        {selected.provider: selected},
        backend or InMemoryProviderBackend(),
        app_env=app_env,
        fallback=fallback,
        clock=clock,
    )


def test_concurrent_acquire_never_exceeds_provider_limit() -> None:
    clock = Clock()
    limiter = guard(clock)

    with ThreadPoolExecutor(max_workers=20) as executor:
        permits = list(
            executor.map(
                lambda index: limiter.acquire("egkn", owner_token=f"worker-{index}"),
                range(20),
            )
        )

    allowed = [permit for permit in permits if permit.allowed]
    assert len(allowed) == 3
    assert all(permit.status == "concurrency_limited" for permit in permits if not permit.allowed)
    assert limiter.metrics_snapshot("egkn").active_leases == 3
    assert limiter.metrics_snapshot("egkn").concurrency_limited == 17


def test_owner_release_is_exact_and_expired_lease_recovers_capacity() -> None:
    clock = Clock()
    limiter = guard(clock, selected_policy=policy(max_concurrency=1, lease_ttl_seconds=5))
    first = limiter.acquire("egkn", owner_token="owner-a")
    assert first.allowed
    wrong = ProviderPermit("egkn", "owner-b", "allowed")
    assert limiter.release(wrong) is False
    assert limiter.acquire("egkn", owner_token="owner-b").status == "concurrency_limited"

    clock.advance(5.1)
    recovered = limiter.acquire("egkn", owner_token="owner-b")
    assert recovered.allowed
    assert limiter.release(recovered) is True
    assert limiter.release(recovered) is False
    limiter.record_result(recovered, success=False, latency_ms=10, error="late duplicate")
    assert limiter.metrics_snapshot("egkn").failures == 0


def test_token_bucket_rate_limits_then_refills() -> None:
    clock = Clock()
    limiter = guard(
        clock,
        selected_policy=policy(qps=1.0, burst=1, max_concurrency=2),
    )
    first = limiter.acquire("egkn", owner_token="one")
    assert first.allowed
    limiter.release(first)
    limited = limiter.acquire("egkn", owner_token="two")
    assert limited.status == "rate_limited"
    assert 0.9 <= limited.retry_after_seconds <= 1.0
    clock.advance(1)
    assert limiter.acquire("egkn", owner_token="two").allowed


def test_circuit_opens_half_opens_one_probe_and_closes_on_success() -> None:
    clock = Clock()
    limiter = guard(clock)
    for index in range(2):
        permit = limiter.acquire("egkn", owner_token=f"failure-{index}")
        assert permit.allowed
        limiter.record_result(
            permit,
            success=False,
            latency_ms=700,
            error="provider timeout",
        )
    opened = limiter.acquire("egkn", owner_token="blocked")
    assert opened.status == "circuit_open"
    assert opened.retry_after_seconds > 0

    clock.advance(7)
    probe = limiter.acquire("egkn", owner_token="probe")
    assert probe.allowed and probe.half_open_probe
    assert limiter.acquire("egkn", owner_token="second-probe").status == "circuit_open"
    limiter.record_result(probe, success=True, latency_ms=80)
    normal = limiter.acquire("egkn", owner_token="normal")
    assert normal.allowed and not normal.half_open_probe
    metrics = limiter.metrics_snapshot("egkn")
    assert metrics.circuit_state == "closed"
    assert metrics.failures == 2
    assert metrics.successes == 1
    assert metrics.latency_le_100ms == 1
    assert metrics.latency_le_500ms == 0
    assert metrics.latency_le_2000ms == 2
    assert metrics.last_error == "provider timeout"


def test_failed_half_open_probe_reopens_with_bounded_backoff_and_jitter() -> None:
    clock = Clock()
    limiter = guard(clock, selected_policy=policy(failure_threshold=1))
    initial = limiter.acquire("egkn", owner_token="initial")
    limiter.record_result(initial, success=False, latency_ms=10, error="500")
    clock.advance(7)
    probe = limiter.acquire("egkn", owner_token="probe")
    assert probe.half_open_probe
    limiter.record_result(probe, success=False, latency_ms=10, error="500 again")
    reopened = limiter.acquire("egkn", owner_token="blocked")
    # Second opening has nominal 10s duration and +/-20% deterministic jitter.
    assert 8 <= reopened.retry_after_seconds <= 12


def test_production_redis_outage_fails_closed_but_dev_uses_bounded_fallback() -> None:
    clock = Clock()
    production = guard(clock, backend=OutageBackend(), app_env="production")
    denied = production.acquire("egkn", owner_token="prod")
    assert denied.status == "backend_unavailable"
    assert denied.allowed is False
    metrics = production.metrics_snapshot("egkn")
    assert metrics.backend_status == "unavailable"
    assert metrics.backend_unavailable >= 1

    development = guard(
        clock,
        backend=OutageBackend(),
        app_env="development",
        fallback=InMemoryProviderBackend(max_providers=1),
    )
    assert development.acquire("egkn", owner_token="dev").allowed
    assert development.metrics_snapshot("egkn").backend_unavailable >= 1


def test_queue_depth_alarm_and_metrics_snapshot() -> None:
    clock = Clock()
    limiter = guard(clock)
    assert limiter.queue_depth_alarm("egkn", 9).status == "ok"
    warning = limiter.queue_depth_alarm("egkn", 10)
    critical = limiter.queue_depth_alarm("egkn", 20)
    assert warning.status == "warning"
    assert critical.status == "critical"
    metrics = limiter.metrics_snapshot("egkn")
    assert metrics.queue_depth == 20
    assert metrics.queue_alarms == 2


def test_retry_backoff_is_deterministic_jittered_and_bounded() -> None:
    clock = Clock()
    limiter = guard(clock)
    first = limiter.retry_delay_seconds("egkn", attempt=1, owner_token="job-1")
    second = limiter.retry_delay_seconds("egkn", attempt=1, owner_token="job-1")
    assert first == second
    assert 8 <= first <= 12
    assert (
        limiter.retry_delay_seconds(
            "egkn",
            attempt=16,
            owner_token="job-1",
            server_retry_after_seconds=29,
        )
        <= 30
    )


def test_redis_factory_enforces_short_timeouts_and_no_production_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    client = object()

    def from_url(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return client

    monkeypatch.setattr("app.provider_backpressure.Redis.from_url", from_url)
    limiter = create_redis_provider_backpressure(
        "redis://example/0",
        app_env="production",
        policies={"egkn": policy()},
        socket_timeout_seconds=1.25,
    )
    assert captured["socket_connect_timeout"] == 1.25
    assert captured["socket_timeout"] == 1.25
    assert limiter.fallback is None
    with pytest.raises(ValueError):
        create_redis_provider_backpressure(
            "redis://example/0",
            app_env="production",
            policies={"egkn": policy()},
            socket_timeout_seconds=4,
        )
