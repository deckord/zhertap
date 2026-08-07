from fastapi.testclient import TestClient

import app.main as main

CSP_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data: https://*.tile.openstreetmap.org; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def test_security_headers_are_present() -> None:
    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    headers = response.headers

    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("x-frame-options") == "DENY"
    assert headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert headers.get("x-xss-protection") == "0"
    assert headers.get("permissions-policy") == "geolocation=(self), camera=(), microphone=()"
    assert headers.get("content-security-policy") == CSP_POLICY
    assert "strict-transport-security" in headers
