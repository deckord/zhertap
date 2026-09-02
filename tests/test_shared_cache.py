from __future__ import annotations

from app.config import settings
from app.shared_cache import SharedJsonCache


def test_shared_cache_is_a_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auction_cache_enabled", False)
    cache = SharedJsonCache()

    cache.set("auctions", "lot-1", {"score": 80}, ttl_seconds=30)

    assert cache.get("auctions", "lot-1") is None
    assert not cache._local


def test_shared_cache_local_fallback_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auction_cache_enabled", True)
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(settings, "auction_cache_local_max_entries", 2)
    cache = SharedJsonCache()
    monkeypatch.setattr(cache, "_redis", lambda: None)

    cache.set("auctions", "lot-1", {"score": 10}, ttl_seconds=30)
    cache.set("auctions", "lot-2", {"score": 20}, ttl_seconds=30)
    cache.set("auctions", "lot-3", {"score": 30}, ttl_seconds=30)

    assert cache.get("auctions", "lot-1") is None
    assert cache.get("auctions", "lot-2") == {"score": 20}
    assert cache.get("auctions", "lot-3") == {"score": 30}


def test_shared_cache_build_lock_uses_owner_token(monkeypatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def set(self, key: str, value: str, **kwargs) -> bool:
            assert kwargs == {"nx": True, "ex": 15}
            if key in self.values:
                return False
            self.values[key] = value
            return True

        def eval(self, _script: str, _keys: int, key: str, token: str) -> int:
            if self.values.get(key) != token:
                return 0
            self.values.pop(key)
            return 1

    monkeypatch.setattr(settings, "auction_cache_enabled", True)
    client = FakeRedis()
    cache = SharedJsonCache()
    monkeypatch.setattr(cache, "_redis", lambda: client)

    token = cache.acquire_build_lock("map", "same-key")
    busy = cache.acquire_build_lock("map", "same-key")

    assert isinstance(token, str)
    assert busy is False
    cache.release_build_lock("map", "same-key", token)
    assert isinstance(cache.acquire_build_lock("map", "same-key"), str)
