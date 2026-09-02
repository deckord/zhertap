from __future__ import annotations

import os
import time
import uuid

import pytest
from redis import Redis
from redis.exceptions import RedisError

from app.provider_backpressure import (
    ProviderBackpressure,
    ProviderPolicy,
    RedisProviderBackend,
)


def test_real_redis_lua_lifecycle() -> None:
    redis_url = os.getenv("PROVIDER_BACKPRESSURE_REDIS_URL")
    if not redis_url:
        pytest.skip("set PROVIDER_BACKPRESSURE_REDIS_URL to run the Redis Lua gate")
    client = Redis.from_url(
        redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
        health_check_interval=30,
    )
    try:
        client.ping()
    except RedisError as exc:
        pytest.skip(f"integration Redis is unavailable: {exc.__class__.__name__}")

    suffix = uuid.uuid4().hex[:10]
    names = {
        "concurrency": f"it_concurrency_{suffix}",
        "rate": f"it_rate_{suffix}",
        "circuit": f"it_circuit_{suffix}",
        "expiry": f"it_expiry_{suffix}",
    }
    policies = {
        names["concurrency"]: ProviderPolicy(
            names["concurrency"], 100, 10, 1, 2, jitter_fraction=0
        ),
        names["rate"]: ProviderPolicy(
            names["rate"], 1, 1, 2, 2, jitter_fraction=0
        ),
        names["circuit"]: ProviderPolicy(
            names["circuit"],
            100,
            10,
            1,
            2,
            failure_threshold=1,
            open_base_seconds=1,
            open_max_seconds=2,
            jitter_fraction=0,
        ),
        names["expiry"]: ProviderPolicy(
            names["expiry"], 100, 10, 1, 1, jitter_fraction=0
        ),
    }
    limiter = ProviderBackpressure(
        policies,
        RedisProviderBackend(client),
        app_env="production",
    )
    try:
        concurrency = names["concurrency"]
        first = limiter.acquire(concurrency, owner_token="first")
        assert first.allowed
        assert limiter.acquire(concurrency, owner_token="second").status == (
            "concurrency_limited"
        )
        assert limiter.release(first)

        rate = names["rate"]
        rate_first = limiter.acquire(rate, owner_token="rate-first")
        assert rate_first.allowed
        assert limiter.release(rate_first)
        assert limiter.acquire(rate, owner_token="rate-second").status == "rate_limited"

        circuit = names["circuit"]
        failed = limiter.acquire(circuit, owner_token="failed")
        assert failed.allowed
        limiter.record_result(failed, success=False, latency_ms=500, error="HTTP 503")
        assert limiter.acquire(circuit, owner_token="open").status == "circuit_open"
        time.sleep(1.1)
        probe = limiter.acquire(circuit, owner_token="probe")
        assert probe.allowed and probe.half_open_probe
        limiter.record_result(probe, success=True, latency_ms=20)
        normal = limiter.acquire(circuit, owner_token="normal")
        assert normal.allowed and not normal.half_open_probe
        limiter.release(normal)

        expiry = names["expiry"]
        assert limiter.acquire(expiry, owner_token="expired").allowed
        time.sleep(1.1)
        assert limiter.acquire(expiry, owner_token="recovered").allowed

        metrics = limiter.metrics_snapshot(circuit)
        assert metrics.allowed >= 3
        assert metrics.circuit_open >= 1
        assert metrics.failures == 1
        assert metrics.successes == 1
        assert metrics.circuit_state == "closed"
    finally:
        keys = []
        for provider in names.values():
            keys.extend(client.scan_iter(match=f"land-scout:provider:{provider}:*", count=100))
        if keys:
            client.delete(*keys)
