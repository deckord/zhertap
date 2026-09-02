from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol

from redis import Redis
from redis.exceptions import RedisError

MAX_PROVIDERS = 64
MAX_OWNER_TOKEN = 128
MAX_ERROR_TEXT = 240

PermitStatus = Literal[
    "allowed",
    "rate_limited",
    "concurrency_limited",
    "circuit_open",
    "backend_unavailable",
]


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    provider: str
    qps: float
    burst: int
    max_concurrency: int
    lease_ttl_seconds: int
    failure_threshold: int = 3
    open_base_seconds: float = 15
    open_max_seconds: float = 900
    half_open_max_calls: int = 1
    jitter_fraction: float = 0.20
    queue_warning_depth: int = 100
    queue_critical_depth: int = 500


DEFAULT_PROVIDER_POLICIES: dict[str, ProviderPolicy] = {
    "eqazyna": ProviderPolicy("eqazyna", 2.0, 4, 4, 360, queue_warning_depth=200),
    "jerler": ProviderPolicy("jerler", 1.0, 2, 2, 360),
    "egkn": ProviderPolicy("egkn", 0.8, 2, 2, 360),
    "osm_overpass": ProviderPolicy("osm_overpass", 0.25, 1, 1, 360, failure_threshold=2),
    "gov_kz": ProviderPolicy("gov_kz", 0.5, 1, 2, 360),
    "auction_documents": ProviderPolicy("auction_documents", 1.0, 2, 3, 360),
}


@dataclass(frozen=True, slots=True)
class ProviderPermit:
    provider: str
    owner_token: str
    status: PermitStatus
    retry_after_seconds: float = 0
    lease_expires_at_ms: int | None = None
    half_open_probe: bool = False

    @property
    def allowed(self) -> bool:
        return self.status == "allowed"


@dataclass(frozen=True, slots=True)
class QueueDepthAlarm:
    provider: str
    depth: int
    status: Literal["ok", "warning", "critical"]
    threshold: int
    recommendation: str


@dataclass(frozen=True, slots=True)
class ProviderMetricsSnapshot:
    provider: str
    allowed: int = 0
    rate_limited: int = 0
    concurrency_limited: int = 0
    circuit_open: int = 0
    backend_unavailable: int = 0
    successes: int = 0
    failures: int = 0
    latency_le_100ms: int = 0
    latency_le_500ms: int = 0
    latency_le_2000ms: int = 0
    latency_gt_2000ms: int = 0
    active_leases: int = 0
    queue_depth: int = 0
    queue_alarms: int = 0
    circuit_state: str = "closed"
    last_error: str | None = None
    last_error_at_ms: int | None = None
    backend_status: str = "available"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _AcquireDecision:
    status: PermitStatus
    retry_after_seconds: float
    lease_expires_at_ms: int | None
    half_open_probe: bool


class ProviderBackend(Protocol):
    def acquire(
        self,
        policy: ProviderPolicy,
        owner_token: str,
        now_ms: int,
    ) -> _AcquireDecision: ...

    def release(self, policy: ProviderPolicy, owner_token: str, now_ms: int) -> bool: ...

    def record_result(
        self,
        policy: ProviderPolicy,
        owner_token: str,
        *,
        success: bool,
        latency_ms: float,
        error: str | None,
        jitter_unit: float,
        now_ms: int,
    ) -> None: ...

    def metrics(self, policy: ProviderPolicy, now_ms: int) -> ProviderMetricsSnapshot: ...

    def record_queue_depth(
        self,
        policy: ProviderPolicy,
        depth: int,
        alarmed: bool,
        now_ms: int,
    ) -> None: ...



def _validate_policy(policy: ProviderPolicy) -> ProviderPolicy:
    if (
        not isinstance(policy.provider, str)
        or not policy.provider
        or len(policy.provider) > 80
        or not all(char.isalnum() or char in "_-" for char in policy.provider)
    ):
        raise ValueError("invalid provider code")
    numeric = (
        policy.qps,
        policy.burst,
        policy.max_concurrency,
        policy.lease_ttl_seconds,
        policy.failure_threshold,
        policy.open_base_seconds,
        policy.open_max_seconds,
        policy.half_open_max_calls,
        policy.jitter_fraction,
        policy.queue_warning_depth,
        policy.queue_critical_depth,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in numeric
    ):
        raise ValueError("provider policy contains invalid numbers")
    if not (
        0.01 <= policy.qps <= 1_000
        and 1 <= policy.burst <= 10_000
        and 1 <= policy.max_concurrency <= 1_000
        and 1 <= policy.lease_ttl_seconds <= 3_600
        and 1 <= policy.failure_threshold <= 100
        and 1 <= policy.open_base_seconds <= policy.open_max_seconds <= 86_400
        and 1 <= policy.half_open_max_calls <= policy.max_concurrency
        and 0 <= policy.jitter_fraction <= 0.5
        and 1 <= policy.queue_warning_depth < policy.queue_critical_depth <= 10_000_000
    ):
        raise ValueError("provider policy is outside bounds")
    return policy


@dataclass(slots=True)
class _MemoryProviderState:
    tokens: float
    last_refill_ms: int
    leases: dict[str, int] = field(default_factory=dict)
    probes: dict[str, int] = field(default_factory=dict)
    circuit_state: str = "closed"
    failures: int = 0
    reopen_count: int = 0
    open_until_ms: int = 0
    counters: dict[str, int] = field(default_factory=dict)
    queue_depth: int = 0
    last_error: str | None = None
    last_error_at_ms: int | None = None


class InMemoryProviderBackend:
    """Bounded deterministic fallback for tests/development only."""

    def __init__(self, *, max_providers: int = MAX_PROVIDERS) -> None:
        self.max_providers = max(1, min(int(max_providers), MAX_PROVIDERS))
        self._states: dict[str, _MemoryProviderState] = {}
        self._lock = threading.Lock()

    def _state(self, policy: ProviderPolicy, now_ms: int) -> _MemoryProviderState:
        state = self._states.get(policy.provider)
        if state is None:
            if len(self._states) >= self.max_providers:
                raise RuntimeError("local provider fallback capacity exceeded")
            state = _MemoryProviderState(policy.burst, now_ms)
            self._states[policy.provider] = state
        return state

    @staticmethod
    def _expire(state: _MemoryProviderState, now_ms: int) -> None:
        for collection in (state.leases, state.probes):
            for token, expires_at in tuple(collection.items()):
                if expires_at <= now_ms:
                    collection.pop(token, None)

    @staticmethod
    def _increment(state: _MemoryProviderState, key: str) -> None:
        state.counters[key] = state.counters.get(key, 0) + 1

    def acquire(
        self,
        policy: ProviderPolicy,
        owner_token: str,
        now_ms: int,
    ) -> _AcquireDecision:
        with self._lock:
            state = self._state(policy, now_ms)
            self._expire(state, now_ms)
            if state.circuit_state == "open":
                if now_ms < state.open_until_ms:
                    self._increment(state, "circuit_open")
                    return _AcquireDecision(
                        "circuit_open",
                        (state.open_until_ms - now_ms) / 1_000,
                        None,
                        False,
                    )
                state.circuit_state = "half_open"
            probe = state.circuit_state == "half_open"
            if probe and len(state.probes) >= policy.half_open_max_calls:
                self._increment(state, "circuit_open")
                return _AcquireDecision("circuit_open", 1.0, None, False)
            if owner_token in state.leases:
                self._increment(state, "concurrency_limited")
                return _AcquireDecision(
                    "concurrency_limited",
                    max(0.001, (state.leases[owner_token] - now_ms) / 1_000),
                    None,
                    False,
                )
            elapsed_ms = max(0, now_ms - state.last_refill_ms)
            state.tokens = min(
                float(policy.burst),
                state.tokens + elapsed_ms * policy.qps / 1_000,
            )
            state.last_refill_ms = now_ms
            if state.tokens < 1:
                retry = (1 - state.tokens) / policy.qps
                self._increment(state, "rate_limited")
                return _AcquireDecision("rate_limited", retry, None, False)
            if len(state.leases) >= policy.max_concurrency:
                retry = max(0.001, (min(state.leases.values()) - now_ms) / 1_000)
                self._increment(state, "concurrency_limited")
                return _AcquireDecision("concurrency_limited", retry, None, False)
            expires_at = now_ms + policy.lease_ttl_seconds * 1_000
            state.tokens -= 1
            state.leases[owner_token] = expires_at
            if probe:
                state.probes[owner_token] = expires_at
            self._increment(state, "allowed")
            return _AcquireDecision("allowed", 0, expires_at, probe)

    def release(self, policy: ProviderPolicy, owner_token: str, now_ms: int) -> bool:
        with self._lock:
            state = self._state(policy, now_ms)
            self._expire(state, now_ms)
            removed = state.leases.pop(owner_token, None) is not None
            state.probes.pop(owner_token, None)
            return removed

    def record_result(
        self,
        policy: ProviderPolicy,
        owner_token: str,
        *,
        success: bool,
        latency_ms: float,
        error: str | None,
        jitter_unit: float,
        now_ms: int,
    ) -> None:
        with self._lock:
            state = self._state(policy, now_ms)
            self._expire(state, now_ms)
            if owner_token not in state.leases:
                return
            probe = owner_token in state.probes
            state.leases.pop(owner_token, None)
            state.probes.pop(owner_token, None)
            bucket = (
                "latency_le_100ms"
                if latency_ms <= 100
                else "latency_le_500ms"
                if latency_ms <= 500
                else "latency_le_2000ms"
                if latency_ms <= 2_000
                else "latency_gt_2000ms"
            )
            self._increment(state, bucket)
            if success:
                self._increment(state, "successes")
                state.failures = 0
                if probe:
                    state.circuit_state = "closed"
                    state.reopen_count = 0
                    state.open_until_ms = 0
                return
            self._increment(state, "failures")
            state.failures += 1
            state.last_error = (error or "provider call failed")[:MAX_ERROR_TEXT]
            state.last_error_at_ms = now_ms
            if probe or state.failures >= policy.failure_threshold:
                state.reopen_count += 1
                exponential = min(
                    policy.open_max_seconds,
                    policy.open_base_seconds * (2 ** min(state.reopen_count - 1, 16)),
                )
                jitter = 1 + policy.jitter_fraction * (2 * jitter_unit - 1)
                state.open_until_ms = now_ms + max(1_000, int(exponential * jitter * 1_000))
                state.circuit_state = "open"

    def metrics(self, policy: ProviderPolicy, now_ms: int) -> ProviderMetricsSnapshot:
        with self._lock:
            state = self._state(policy, now_ms)
            self._expire(state, now_ms)
            values = state.counters
            return ProviderMetricsSnapshot(
                provider=policy.provider,
                allowed=values.get("allowed", 0),
                rate_limited=values.get("rate_limited", 0),
                concurrency_limited=values.get("concurrency_limited", 0),
                circuit_open=values.get("circuit_open", 0),
                successes=values.get("successes", 0),
                failures=values.get("failures", 0),
                latency_le_100ms=values.get("latency_le_100ms", 0),
                latency_le_500ms=values.get("latency_le_500ms", 0),
                latency_le_2000ms=values.get("latency_le_2000ms", 0),
                latency_gt_2000ms=values.get("latency_gt_2000ms", 0),
                active_leases=len(state.leases),
                queue_depth=state.queue_depth,
                queue_alarms=values.get("queue_alarms", 0),
                circuit_state=state.circuit_state,
                last_error=state.last_error,
                last_error_at_ms=state.last_error_at_ms,
            )

    def record_queue_depth(
        self,
        policy: ProviderPolicy,
        depth: int,
        alarmed: bool,
        now_ms: int,
    ) -> None:
        with self._lock:
            state = self._state(policy, now_ms)
            state.queue_depth = depth
            if alarmed:
                self._increment(state, "queue_alarms")



_ACQUIRE_LUA = r"""
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local qps = tonumber(ARGV[1]); local burst = tonumber(ARGV[2])
local max_concurrency = tonumber(ARGV[3]); local ttl = tonumber(ARGV[4])
local half_max = tonumber(ARGV[5]); local owner = ARGV[6]
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
local circuit = redis.call('HGET', KEYS[1], 'circuit_state') or 'closed'
local open_until = tonumber(redis.call('HGET', KEYS[1], 'open_until_ms') or '0')
if circuit == 'open' then
  if now < open_until then
    redis.call('HINCRBY', KEYS[4], 'circuit_open', 1)
    return {'circuit_open', open_until - now, 0, 0}
  end
  circuit = 'half_open'; redis.call('HSET', KEYS[1], 'circuit_state', circuit)
end
local probe = 0
if circuit == 'half_open' then
  probe = 1
  if redis.call('ZCARD', KEYS[3]) >= half_max then
    redis.call('HINCRBY', KEYS[4], 'circuit_open', 1)
    return {'circuit_open', 1000, 0, 0}
  end
end
if redis.call('ZSCORE', KEYS[2], owner) then
  redis.call('HINCRBY', KEYS[4], 'concurrency_limited', 1)
  return {'concurrency_limited', ttl * 1000, 0, 0}
end
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens') or tostring(burst))
local last = tonumber(redis.call('HGET', KEYS[1], 'last_refill_ms') or tostring(now))
tokens = math.min(burst, tokens + math.max(0, now - last) * qps / 1000)
redis.call('HSET', KEYS[1], 'last_refill_ms', now, 'tokens', tokens)
if tokens < 1 then
  redis.call('HSET', KEYS[1], 'tokens', tokens)
  redis.call('HINCRBY', KEYS[4], 'rate_limited', 1)
  return {'rate_limited', math.ceil((1 - tokens) / qps * 1000), 0, 0}
end
if redis.call('ZCARD', KEYS[2]) >= max_concurrency then
  local first = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
  local retry = 1
  if first[2] then retry = math.max(1, tonumber(first[2]) - now) end
  redis.call('HINCRBY', KEYS[4], 'concurrency_limited', 1)
  return {'concurrency_limited', retry, 0, 0}
end
local expires = now + ttl * 1000
redis.call('HSET', KEYS[1], 'tokens', tokens - 1)
redis.call('ZADD', KEYS[2], expires, owner)
if probe == 1 then redis.call('ZADD', KEYS[3], expires, owner) end
redis.call('HINCRBY', KEYS[4], 'allowed', 1)
redis.call('PEXPIRE', KEYS[1], math.max(ttl * 2000, 86400000))
redis.call('PEXPIRE', KEYS[2], ttl * 2000); redis.call('PEXPIRE', KEYS[3], ttl * 2000)
redis.call('PEXPIRE', KEYS[4], 604800000)
return {'allowed', 0, expires, probe}
"""

_RELEASE_LUA = r"""
local removed = redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return removed
"""

_RESULT_LUA = r"""
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local owner = ARGV[1]; local success = tonumber(ARGV[2]); local latency = tonumber(ARGV[3])
local threshold = tonumber(ARGV[4]); local base = tonumber(ARGV[5])
local maximum = tonumber(ARGV[6]); local jitter_fraction = tonumber(ARGV[7])
local jitter_unit = tonumber(ARGV[8]); local error = ARGV[9]
if not redis.call('ZSCORE', KEYS[2], owner) then return 0 end
local probe = redis.call('ZSCORE', KEYS[3], owner) and 1 or 0
redis.call('ZREM', KEYS[2], owner); redis.call('ZREM', KEYS[3], owner)
local bucket = 'latency_gt_2000ms'
if latency <= 100 then bucket = 'latency_le_100ms'
elseif latency <= 500 then bucket = 'latency_le_500ms'
elseif latency <= 2000 then bucket = 'latency_le_2000ms' end
redis.call('HINCRBY', KEYS[4], bucket, 1)
if success == 1 then
  redis.call('HINCRBY', KEYS[4], 'successes', 1); redis.call('HSET', KEYS[1], 'failures', 0)
  if probe == 1 then
    redis.call('HSET', KEYS[1], 'circuit_state', 'closed', 'reopen_count', 0)
    redis.call('HSET', KEYS[1], 'open_until_ms', 0)
  end
  return 1
end
redis.call('HINCRBY', KEYS[4], 'failures', 1)
local failures = redis.call('HINCRBY', KEYS[1], 'failures', 1)
redis.call('HSET', KEYS[4], 'last_error', string.sub(error, 1, 240), 'last_error_at_ms', now)
if probe == 1 or failures >= threshold then
  local reopen = redis.call('HINCRBY', KEYS[1], 'reopen_count', 1)
  local duration = math.min(maximum, base * (2 ^ math.min(reopen - 1, 16)))
  duration = math.max(1, duration * (1 + jitter_fraction * (2 * jitter_unit - 1)))
  redis.call('HSET', KEYS[1], 'circuit_state', 'open', 'open_until_ms', now + duration * 1000)
end
redis.call('PEXPIRE', KEYS[1], 604800000); redis.call('PEXPIRE', KEYS[4], 604800000)
return 1
"""


class RedisProviderBackend:
    """Distributed atomic backend; Lua uses Redis server time for consistency."""

    def __init__(self, client: Redis, *, namespace: str = "land-scout:provider") -> None:
        self.client = client
        self.namespace = namespace.rstrip(":")

    def _keys(self, provider: str) -> tuple[str, str, str, str]:
        prefix = f"{self.namespace}:{provider}"
        return f"{prefix}:state", f"{prefix}:leases", f"{prefix}:probes", f"{prefix}:metrics"

    def acquire(
        self,
        policy: ProviderPolicy,
        owner_token: str,
        now_ms: int,
    ) -> _AcquireDecision:
        del now_ms
        raw = self.client.eval(
            _ACQUIRE_LUA,
            4,
            *self._keys(policy.provider),
            policy.qps,
            policy.burst,
            policy.max_concurrency,
            policy.lease_ttl_seconds,
            policy.half_open_max_calls,
            owner_token,
        )
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            raise RedisError("invalid provider acquire response")
        status = raw[0].decode() if isinstance(raw[0], bytes) else str(raw[0])
        if status not in {"allowed", "rate_limited", "concurrency_limited", "circuit_open"}:
            raise RedisError("invalid provider acquire status")
        return _AcquireDecision(
            status,
            max(0, float(raw[1]) / 1_000),
            int(raw[2]) or None,
            bool(int(raw[3])),
        )

    def release(self, policy: ProviderPolicy, owner_token: str, now_ms: int) -> bool:
        del now_ms
        keys = self._keys(policy.provider)
        return bool(self.client.eval(_RELEASE_LUA, 2, keys[1], keys[2], owner_token))

    def record_result(
        self,
        policy: ProviderPolicy,
        owner_token: str,
        *,
        success: bool,
        latency_ms: float,
        error: str | None,
        jitter_unit: float,
        now_ms: int,
    ) -> None:
        del now_ms
        self.client.eval(
            _RESULT_LUA,
            4,
            *self._keys(policy.provider),
            owner_token,
            int(success),
            latency_ms,
            policy.failure_threshold,
            policy.open_base_seconds,
            policy.open_max_seconds,
            policy.jitter_fraction,
            jitter_unit,
            (error or "")[:MAX_ERROR_TEXT],
        )

    @staticmethod
    def _integer(raw: Mapping[object, object], key: str) -> int:
        value = raw.get(key, raw.get(key.encode(), 0))
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _string(raw: Mapping[object, object], key: str) -> str | None:
        value = raw.get(key, raw.get(key.encode()))
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value) if value is not None else None

    def metrics(self, policy: ProviderPolicy, now_ms: int) -> ProviderMetricsSnapshot:
        del now_ms
        keys = self._keys(policy.provider)
        seconds, microseconds = self.client.time()
        redis_now_ms = int(seconds) * 1_000 + int(microseconds) // 1_000
        pipe = self.client.pipeline(transaction=False)
        pipe.zremrangebyscore(keys[1], "-inf", redis_now_ms)
        pipe.zremrangebyscore(keys[2], "-inf", redis_now_ms)
        pipe.hgetall(keys[3])
        pipe.hgetall(keys[0])
        pipe.zcard(keys[1])
        _expired_leases, _expired_probes, metrics, state, active = pipe.execute()
        return ProviderMetricsSnapshot(
            provider=policy.provider,
            allowed=self._integer(metrics, "allowed"),
            rate_limited=self._integer(metrics, "rate_limited"),
            concurrency_limited=self._integer(metrics, "concurrency_limited"),
            circuit_open=self._integer(metrics, "circuit_open"),
            successes=self._integer(metrics, "successes"),
            failures=self._integer(metrics, "failures"),
            latency_le_100ms=self._integer(metrics, "latency_le_100ms"),
            latency_le_500ms=self._integer(metrics, "latency_le_500ms"),
            latency_le_2000ms=self._integer(metrics, "latency_le_2000ms"),
            latency_gt_2000ms=self._integer(metrics, "latency_gt_2000ms"),
            active_leases=int(active or 0),
            queue_depth=self._integer(metrics, "queue_depth"),
            queue_alarms=self._integer(metrics, "queue_alarms"),
            circuit_state=self._string(state, "circuit_state") or "closed",
            last_error=self._string(metrics, "last_error"),
            last_error_at_ms=self._integer(metrics, "last_error_at_ms") or None,
        )

    def record_queue_depth(
        self,
        policy: ProviderPolicy,
        depth: int,
        alarmed: bool,
        now_ms: int,
    ) -> None:
        del now_ms
        metrics_key = self._keys(policy.provider)[3]
        pipe = self.client.pipeline(transaction=False)
        pipe.hset(metrics_key, "queue_depth", depth)
        if alarmed:
            pipe.hincrby(metrics_key, "queue_alarms", 1)
        pipe.expire(metrics_key, 604_800)
        pipe.execute()



class ProviderBackpressure:
    def __init__(
        self,
        policies: Mapping[str, ProviderPolicy],
        backend: ProviderBackend,
        *,
        app_env: str,
        fallback: InMemoryProviderBackend | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not policies or len(policies) > MAX_PROVIDERS:
            raise ValueError("provider policy count is outside bounds")
        self.policies = {key: _validate_policy(value) for key, value in policies.items()}
        if any(key != policy.provider for key, policy in self.policies.items()):
            raise ValueError("provider policy key mismatch")
        self.backend = backend
        self.app_env = app_env.strip().casefold()
        self.fallback = fallback
        self.clock = clock
        self._outage_lock = threading.Lock()
        self._outage_counts: dict[str, int] = {}

    @property
    def production(self) -> bool:
        return self.app_env in {"production", "prod"}

    def _policy(self, provider: str) -> ProviderPolicy:
        try:
            return self.policies[provider]
        except KeyError as exc:
            raise ValueError("unknown provider") from exc

    def _now_ms(self) -> int:
        value = float(self.clock())
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid clock")
        return int(value * 1_000)

    def _backend_or_fallback(self, method: str, *args: object, **kwargs: object) -> object:
        try:
            return getattr(self.backend, method)(*args, **kwargs)
        except (RedisError, OSError, TimeoutError, ConnectionError):
            provider = (
                args[0].provider if args and isinstance(args[0], ProviderPolicy) else "unknown"
            )
            with self._outage_lock:
                self._outage_counts[provider] = self._outage_counts.get(provider, 0) + 1
            if self.production or self.fallback is None:
                raise
            return getattr(self.fallback, method)(*args, **kwargs)

    @staticmethod
    def _owner_token(value: str | None) -> str:
        token = value or secrets.token_urlsafe(24)
        if (
            not isinstance(token, str)
            or not token
            or len(token) > MAX_OWNER_TOKEN
            or not all(char.isalnum() or char in "_-:." for char in token)
        ):
            raise ValueError("invalid owner token")
        return token

    def acquire(self, provider: str, *, owner_token: str | None = None) -> ProviderPermit:
        policy = self._policy(provider)
        owner = self._owner_token(owner_token)
        now_ms = self._now_ms()
        try:
            decision = self._backend_or_fallback("acquire", policy, owner, now_ms)
        except (RedisError, OSError, TimeoutError, ConnectionError):
            return ProviderPermit(provider, owner, "backend_unavailable", retry_after_seconds=3)
        if not isinstance(decision, _AcquireDecision):
            return ProviderPermit(provider, owner, "backend_unavailable", retry_after_seconds=3)
        return ProviderPermit(
            provider=provider,
            owner_token=owner,
            status=decision.status,
            retry_after_seconds=min(max(decision.retry_after_seconds, 0), 86_400),
            lease_expires_at_ms=decision.lease_expires_at_ms,
            half_open_probe=decision.half_open_probe,
        )

    def release(self, permit: ProviderPermit) -> bool:
        policy = self._policy(permit.provider)
        if not permit.allowed:
            return False
        try:
            result = self._backend_or_fallback(
                "release", policy, permit.owner_token, self._now_ms()
            )
        except (RedisError, OSError, TimeoutError, ConnectionError):
            return False
        return bool(result)

    def record_result(
        self,
        permit: ProviderPermit,
        *,
        success: bool,
        latency_ms: float,
        error: str | None = None,
    ) -> None:
        if not permit.allowed:
            raise ValueError("cannot record result without an allowed permit")
        if (
            not isinstance(success, bool)
            or isinstance(latency_ms, bool)
            or not isinstance(latency_ms, (int, float))
            or not math.isfinite(float(latency_ms))
            or not 0 <= float(latency_ms) <= 86_400_000
        ):
            raise ValueError("invalid provider result")
        if error is not None and (not isinstance(error, str) or len(error) > 2_000):
            raise ValueError("invalid provider error")
        policy = self._policy(permit.provider)
        digest = hashlib.sha256(f"{permit.provider}:{permit.owner_token}".encode()).digest()
        jitter_unit = int.from_bytes(digest[:8], "big") / (2**64 - 1)
        try:
            self._backend_or_fallback(
                "record_result",
                policy,
                permit.owner_token,
                success=success,
                latency_ms=float(latency_ms),
                error=error,
                jitter_unit=jitter_unit,
                now_ms=self._now_ms(),
            )
        except (RedisError, OSError, TimeoutError, ConnectionError):
            # The outbound call has already finished; caller must not retry it
            # merely because metrics/circuit persistence is unavailable.
            return

    def queue_depth_alarm(self, provider: str, depth: int) -> QueueDepthAlarm:
        policy = self._policy(provider)
        if not isinstance(depth, int) or isinstance(depth, bool) or not 0 <= depth <= 10_000_000:
            raise ValueError("invalid queue depth")
        if depth >= policy.queue_critical_depth:
            status, threshold = "critical", policy.queue_critical_depth
            recommendation = "Pause producer batches and scale provider workers only within QPS"
        elif depth >= policy.queue_warning_depth:
            status, threshold = "warning", policy.queue_warning_depth
            recommendation = "Reduce producer batch size and inspect provider latency"
        else:
            status, threshold = "ok", policy.queue_warning_depth
            recommendation = "No action required"
        alarm = QueueDepthAlarm(provider, depth, status, threshold, recommendation)
        try:
            self._backend_or_fallback(
                "record_queue_depth",
                policy,
                depth,
                status != "ok",
                self._now_ms(),
            )
        except (RedisError, OSError, TimeoutError, ConnectionError):
            pass
        return alarm

    def retry_delay_seconds(
        self,
        provider: str,
        *,
        attempt: int,
        owner_token: str,
        server_retry_after_seconds: float | None = None,
    ) -> float:
        """Return deterministic bounded exponential backoff with per-owner jitter."""
        policy = self._policy(provider)
        self._owner_token(owner_token)
        if not isinstance(attempt, int) or isinstance(attempt, bool) or not 0 <= attempt <= 16:
            raise ValueError("invalid retry attempt")
        if server_retry_after_seconds is not None and (
            isinstance(server_retry_after_seconds, bool)
            or not isinstance(server_retry_after_seconds, (int, float))
            or not math.isfinite(float(server_retry_after_seconds))
            or not 0 <= float(server_retry_after_seconds) <= 86_400
        ):
            raise ValueError("invalid server retry-after")
        base = min(
            policy.open_max_seconds,
            policy.open_base_seconds * (2**attempt),
        )
        digest = hashlib.sha256(f"retry:{provider}:{owner_token}:{attempt}".encode()).digest()
        unit = int.from_bytes(digest[:8], "big") / (2**64 - 1)
        jittered = base * (1 + policy.jitter_fraction * (2 * unit - 1))
        return min(
            policy.open_max_seconds,
            max(float(server_retry_after_seconds or 0), max(0.1, jittered)),
        )

    def metrics_snapshot(self, provider: str) -> ProviderMetricsSnapshot:
        policy = self._policy(provider)
        try:
            snapshot = self._backend_or_fallback("metrics", policy, self._now_ms())
        except (RedisError, OSError, TimeoutError, ConnectionError):
            snapshot = ProviderMetricsSnapshot(
                provider=provider,
                backend_status="unavailable",
            )
        if not isinstance(snapshot, ProviderMetricsSnapshot):
            snapshot = ProviderMetricsSnapshot(provider=provider, backend_status="unavailable")
        with self._outage_lock:
            outages = self._outage_counts.get(provider, 0)
        if not outages:
            return snapshot
        payload = asdict(snapshot)
        payload["backend_unavailable"] = outages
        if self.production:
            payload["backend_status"] = "unavailable"
        return ProviderMetricsSnapshot(**payload)


def create_redis_provider_backpressure(
    redis_url: str,
    *,
    app_env: str,
    policies: Mapping[str, ProviderPolicy] = DEFAULT_PROVIDER_POLICIES,
    socket_timeout_seconds: float = 1.5,
) -> ProviderBackpressure:
    if (
        isinstance(socket_timeout_seconds, bool)
        or not isinstance(socket_timeout_seconds, (int, float))
        or not 1 <= float(socket_timeout_seconds) <= 3
    ):
        raise ValueError("Redis timeout must be between 1 and 3 seconds")
    client = Redis.from_url(
        redis_url,
        socket_connect_timeout=float(socket_timeout_seconds),
        socket_timeout=float(socket_timeout_seconds),
        health_check_interval=30,
    )
    fallback = (
        None
        if app_env.strip().casefold() in {"production", "prod"}
        else InMemoryProviderBackend(max_providers=len(policies))
    )
    return ProviderBackpressure(
        policies,
        RedisProviderBackend(client),
        app_env=app_env,
        fallback=fallback,
    )
