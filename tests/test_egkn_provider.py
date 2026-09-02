import pytest

from app.provider_backpressure import InMemoryProviderBackend, ProviderBackpressure, ProviderPolicy
from app.providers import egkn as egkn_module
from app.providers.egkn import EGKN_MAX_RESPONSE_BYTES, EgknProvider, EgknProviderError


def _limiter() -> ProviderBackpressure:
    policy = ProviderPolicy(
        "egkn",
        qps=100,
        burst=100,
        max_concurrency=2,
        lease_ttl_seconds=240,
    )
    return ProviderBackpressure(
        {"egkn": policy}, InMemoryProviderBackend(), app_env="test", clock=lambda: 100.0
    )


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_egkn_uses_larger_response_cap_and_classifies_oversized_layers(monkeypatch) -> None:
    seen: dict[str, int] = {}

    def fake_bounded_http_request(_client, _method, _url, **kwargs):
        seen["max_bytes"] = kwargs["max_bytes"]
        raise ValueError("provider response exceeds byte cap")

    monkeypatch.setattr(egkn_module, "bounded_http_request", fake_bounded_http_request)
    monkeypatch.setattr(EgknProvider, "_client", lambda self: _FakeClient())

    provider = EgknProvider(backpressure=_limiter())

    with pytest.raises(EgknProviderError, match="превысил лимит объектов"):
        provider._get("https://egkn.test/ows", params={})

    assert seen["max_bytes"] == EGKN_MAX_RESPONSE_BYTES
