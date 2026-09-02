from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


class SharedJsonCache:
    """Small Redis-backed JSON cache with a bounded process-local fallback.

    The fallback is intentionally used only outside production. Production
    requires Redis so every web worker sees the same cached value.
    """

    def __init__(self) -> None:
        self._client: Any | None = None
        self._redis_retry_after = 0.0
        self._local: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    def _redis(self) -> Any | None:
        if not settings.auction_cache_enabled:
            return None
        now = time.monotonic()
        if now < self._redis_retry_after:
            return None
        if self._client is not None:
            return self._client
        try:
            import redis

            self._client = redis.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.35,
                socket_timeout=0.35,
                health_check_interval=30,
            )
            self._client.ping()
            return self._client
        except Exception as exc:  # pragma: no cover - depends on external Redis
            self._client = None
            self._redis_retry_after = now + 15
            logger.warning("Shared cache unavailable: %s", exc.__class__.__name__)
            return None

    @staticmethod
    def _key(namespace: str, key: str) -> str:
        return f"land-scout:{namespace}:{key}"

    def get(self, namespace: str, key: str) -> Any | None:
        if not settings.auction_cache_enabled:
            return None
        cache_key = self._key(namespace, key)
        client = self._redis()
        if client is not None:
            try:
                raw = client.get(cache_key)
                return json.loads(raw) if raw else None
            except Exception as exc:  # pragma: no cover - depends on external Redis
                logger.warning("Shared cache read failed: %s", exc.__class__.__name__)
                self._client = None
                self._redis_retry_after = time.monotonic() + 15
        if settings.app_env.lower() in {"production", "prod"}:
            return None
        with self._lock:
            item = self._local.get(cache_key)
            if item is None:
                return None
            expires_at, raw = item
            if expires_at <= time.monotonic():
                self._local.pop(cache_key, None)
                return None
            self._local.move_to_end(cache_key)
            return json.loads(raw)

    def set(self, namespace: str, key: str, value: Any, *, ttl_seconds: int) -> None:
        if not settings.auction_cache_enabled:
            return
        cache_key = self._key(namespace, key)
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        client = self._redis()
        if client is not None:
            try:
                client.setex(cache_key, ttl_seconds, raw)
                return
            except Exception as exc:  # pragma: no cover - depends on external Redis
                logger.warning("Shared cache write failed: %s", exc.__class__.__name__)
                self._client = None
                self._redis_retry_after = time.monotonic() + 15
        if settings.app_env.lower() in {"production", "prod"}:
            return
        with self._lock:
            self._local[cache_key] = (time.monotonic() + ttl_seconds, raw)
            self._local.move_to_end(cache_key)
            while len(self._local) > settings.auction_cache_local_max_entries:
                self._local.popitem(last=False)

    def clear_local(self) -> None:
        with self._lock:
            self._local.clear()

    def acquire_build_lock(
        self,
        namespace: str,
        key: str,
        *,
        ttl_seconds: int = 15,
    ) -> str | bool | None:
        """Return token when acquired, False when busy, None without Redis."""
        client = self._redis()
        if client is None:
            return None
        token = uuid.uuid4().hex
        lock_key = self._key(f"{namespace}:build-lock", key)
        try:
            acquired = client.set(lock_key, token, nx=True, ex=max(2, ttl_seconds))
            return token if acquired else False
        except Exception as exc:  # pragma: no cover - depends on external Redis
            logger.warning("Shared cache lock failed: %s", exc.__class__.__name__)
            return None

    def release_build_lock(self, namespace: str, key: str, token: str) -> None:
        client = self._redis()
        if client is None:
            return
        lock_key = self._key(f"{namespace}:build-lock", key)
        script = (
            'if redis.call("get", KEYS[1]) == ARGV[1] then '
            'return redis.call("del", KEYS[1]) else return 0 end'
        )
        try:
            client.eval(script, 1, lock_key, token)
        except Exception as exc:  # pragma: no cover - depends on external Redis
            logger.warning("Shared cache unlock failed: %s", exc.__class__.__name__)

    def wait_for_value(
        self,
        namespace: str,
        key: str,
        *,
        timeout_seconds: float = 0.3,
    ) -> Any | None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while time.monotonic() < deadline:
            value = self.get(namespace, key)
            if value is not None:
                return value
            time.sleep(0.02)
        return None


shared_json_cache = SharedJsonCache()
