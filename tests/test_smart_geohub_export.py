import json
from pathlib import Path
from typing import Any

from tools.smart_geohub_export import SmartGeoHubClient, export_collection
from tools.smart_geohub_export.builder import count_collection, summarize_collection


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpxClient:
    calls: list[tuple[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "FakeHttpxClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def get(self, url: str, *, params: Any) -> FakeResponse:
        self.calls.append((url, params))
        if url.endswith("/api/list"):
            continuation = _params_values(params, "continuation")
            if not continuation:
                return FakeResponse(
                    {
                        "type": "FeatureCollection",
                        "features": [_feature("1.", "Жилая зона")],
                        "continuation": [0, "1."],
                        "total": 42,
                    }
                )
            return FakeResponse(
                {
                    "type": "FeatureCollection",
                    "features": [_feature("2.", "Жилая зона")],
                    "continuation": None,
                    "total": 42,
                }
            )
        if url.endswith("/api/geometry"):
            return FakeResponse(
                {
                    "type": "MultiPolygon",
                    "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
                    "coordinates": [],
                }
            )
        raise AssertionError(url)


def _feature(feature_id: str, functional: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": feature_id,
        "collection": "gpzone",
        "properties": {"functional": functional, "usl_i32": 11010000},
        "geometry": {"bbox": [70.0, 52.0, 70.1, 52.1]},
    }


def _params_values(params: Any, key: str) -> list[str]:
    if isinstance(params, list):
        return [value for item_key, value in params if item_key == key]
    value = params.get(key)
    if isinstance(value, list):
        return value
    return [value] if value is not None else []


def test_client_uses_continuation_repeated_params(monkeypatch) -> None:
    FakeHttpxClient.calls = []
    monkeypatch.setattr("tools.smart_geohub_export.client.httpx.Client", FakeHttpxClient)
    client = SmartGeoHubClient(base_url="https://map.example.kz/")

    rows = client.features("gpzone-jil")

    assert [row["id"] for row in rows] == ["1.", "2."]
    second_params = FakeHttpxClient.calls[1][1]
    assert ("continuation", "0") in second_params
    assert ("continuation", "1.") in second_params


def test_client_sends_search_text_params(monkeypatch) -> None:
    FakeHttpxClient.calls = []
    monkeypatch.setattr("tools.smart_geohub_export.client.httpx.Client", FakeHttpxClient)
    client = SmartGeoHubClient(base_url="https://map.example.kz/")

    rows = client.features("gpzone-jil", search={"usl_i32": "11010000"}, max_features=1)

    assert rows[0]["id"] == "1."
    first_params = FakeHttpxClient.calls[0][1]
    assert ("search[usl_i32][text]", "11010000") in first_params


def test_client_count_reads_total_without_pagination(monkeypatch) -> None:
    FakeHttpxClient.calls = []
    monkeypatch.setattr("tools.smart_geohub_export.client.httpx.Client", FakeHttpxClient)
    client = SmartGeoHubClient(base_url="https://map.example.kz/")

    total = client.count("gpzone-jil", search={"usl_i32": "11010000"})

    assert total == 42
    assert len(FakeHttpxClient.calls) == 1
    first_params = FakeHttpxClient.calls[0][1]
    assert ("search[usl_i32][text]", "11010000") in first_params


def test_export_collection_writes_candidate_files(monkeypatch, tmp_path: Path) -> None:
    FakeHttpxClient.calls = []
    monkeypatch.setattr("tools.smart_geohub_export.client.httpx.Client", FakeHttpxClient)

    result = export_collection(
        base_url="https://map.example.kz/",
        collection="gpzone-jil",
        output_dir=tmp_path,
        max_features=10,
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    geojson = json.loads(result.geojson_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "smart-geohub-export/v1"
    assert manifest["feature_count"] == 2
    assert manifest["geometry_types"] == {"MultiPolygon": 2}
    assert manifest["property_counts"]["functional"] == {"Жилая зона": 2}
    assert len(geojson["features"]) == 2
    assert "crs" not in geojson["features"][0]["geometry"]


def test_summarize_collection_writes_property_counts_without_geometry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeHttpxClient.calls = []
    monkeypatch.setattr("tools.smart_geohub_export.client.httpx.Client", FakeHttpxClient)

    result = summarize_collection(
        base_url="https://map.example.kz/",
        collection="gpzone-jil",
        output_dir=tmp_path,
        max_features=10,
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.feature_count == 2
    assert manifest["schema_version"] == "smart-geohub-summary/v1"
    assert manifest["property_counts"]["usl_i32"] == {"11010000": 2}
    assert not any(url.endswith("/api/geometry") for url, _params in FakeHttpxClient.calls)


def test_count_collection_writes_count_manifest_without_geometry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeHttpxClient.calls = []
    monkeypatch.setattr("tools.smart_geohub_export.client.httpx.Client", FakeHttpxClient)

    result = count_collection(
        base_url="https://map.example.kz/",
        collection="gpzone-jil",
        output_dir=tmp_path,
        search={"usl_i32": "11010000"},
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.feature_count == 42
    assert manifest["schema_version"] == "smart-geohub-count/v1"
    assert manifest["feature_count"] == 42
    assert manifest["search"] == {"usl_i32": "11010000"}
    assert not any(url.endswith("/api/geometry") for url, _params in FakeHttpxClient.calls)
