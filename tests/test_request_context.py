from starlette.requests import Request

import app.request_context as request_context


def make_request(*, peer: str, forwarded: str = "") -> Request:
    headers = []
    if forwarded:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (peer, 1234),
        "scheme": "http",
        "server": ("testserver", 80),
        "query_string": b"",
    }
    return Request(scope)


def test_untrusted_forwarded_header_cannot_change_client_ip(monkeypatch) -> None:
    monkeypatch.setattr(request_context.settings, "trusted_proxy_networks", "")

    request = make_request(peer="10.0.0.8", forwarded="203.0.113.9")

    assert request_context.client_ip(request) == "10.0.0.8"


def test_forwarded_header_is_used_only_for_trusted_proxy(monkeypatch) -> None:
    monkeypatch.setattr(request_context.settings, "trusted_proxy_networks", "10.0.0.0/8")

    request = make_request(peer="10.0.0.8", forwarded="203.0.113.9, 10.0.0.7")

    assert request_context.client_ip(request) == "203.0.113.9"
