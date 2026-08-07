from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock

from app.config import settings

logger = logging.getLogger(__name__)


try:
    import redis
except ImportError:
    redis = None


@dataclass(frozen=True)
class RateLimitState:
    allowed: bool
    retry_after_seconds: int


class _RateLimiter:
    def consume(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> RateLimitState:
        raise NotImplementedError


class _InMemoryRateLimiter(_RateLimiter):
    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def consume(self, key: str, *, limit: int, window_seconds: int) -> RateLimitState:
        if limit <= 0 or window_seconds <= 0:
            return RateLimitState(allowed=True, retry_after_seconds=0)

        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            timestamps = self._buckets[key]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= limit:
                retry_after_seconds = max(1, int((timestamps[0] + window_seconds) - now))
                return RateLimitState(allowed=False, retry_after_seconds=retry_after_seconds)

            timestamps.append(now)
            return RateLimitState(allowed=True, retry_after_seconds=0)


class _RedisRateLimiter(_RateLimiter):
    _SCRIPT = """
    local key = KEYS[1]
    local window = tonumber(ARGV[1])
    local current = redis.call("INCR", key)
    if current == 1 then
      redis.call("EXPIRE", key, window)
    end
    return current
    """

    def __init__(self, redis_url: str) -> None:
        if redis is None:
            raise RuntimeError("redis package is not available")
        self._redis = redis.Redis.from_url(redis_url)
        self._script = self._redis.register_script(self._SCRIPT)
        # Validate connectivity; fallback to in-memory if unreachable.
        self._redis.ping()

    def consume(self, key: str, *, limit: int, window_seconds: int) -> RateLimitState:
        if limit <= 0 or window_seconds <= 0:
            return RateLimitState(allowed=True, retry_after_seconds=0)
        namespaced_key = f"rl:{key}"
        try:
            current = int(self._script(keys=[namespaced_key], args=[str(window_seconds)]))
            if current <= limit:
                return RateLimitState(allowed=True, retry_after_seconds=0)
            ttl = self._redis.ttl(namespaced_key) or window_seconds
            if ttl < 0:
                ttl = window_seconds
            return RateLimitState(allowed=False, retry_after_seconds=max(1, int(ttl)))
        except Exception:
            logger.exception("Failed to use Redis for rate limiting; falling back to in-memory")
            _store = _FALLBACK_STORE
            return _store.consume(key, limit=limit, window_seconds=window_seconds)


def _build_rate_limit_store() -> _RateLimiter:
    try:
        if settings.redis_url and redis is not None:
            return _RedisRateLimiter(settings.redis_url)
        raise RuntimeError("redis not configured")
    except Exception as exc:
        logger.warning("Using in-memory rate limit fallback: %s", exc)
        return _InMemoryRateLimiter()


_FALLBACK_STORE = _InMemoryRateLimiter()
_store: _RateLimiter = _build_rate_limit_store()


def consume_rate_limit(
    key: str,
    *,
    limit: int,
    window_seconds: int,
) -> RateLimitState:
    return _store.consume(key, limit=limit, window_seconds=window_seconds)
