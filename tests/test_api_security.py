from fastapi.testclient import TestClient

import app.main as main
from app.config import settings


def test_api_auctions_fail_closed_without_key_in_production(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "internal_api_key", "")

    response = TestClient(main.app).get("/api/auctions")

    assert response.status_code == 503
    assert response.json()["detail"] == "Internal API key is not configured"


def test_api_auctions_requires_configured_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "internal_api_key", "site-secret")

    response = TestClient(main.app).get("/api/auctions")

    assert response.status_code == 401


def test_api_auctions_accepts_configured_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "internal_api_key", "site-secret")
    monkeypatch.setattr(main, "list_auction_lots", lambda *_, **__: ([], 0))

    response = TestClient(main.app).get(
        "/api/auctions",
        headers={"X-API-Key": "site-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_api_auction_map_geojson_accepts_configured_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "internal_api_key", "site-secret")
    monkeypatch.setattr(
        main,
        "active_auction_lots_geojson",
        lambda *_, **__: {"type": "FeatureCollection", "features": []},
    )

    response = TestClient(main.app).get(
        "/api/auctions/map/geojson?region=Акмолинская область",
        headers={"X-API-Key": "site-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"type": "FeatureCollection", "features": []}


def test_api_allows_empty_key_only_in_development(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "internal_api_key", "")
    monkeypatch.setattr(main, "list_auction_lots", lambda *_, **__: ([], 0))

    response = TestClient(main.app).get("/api/auctions")

    assert response.status_code == 200
