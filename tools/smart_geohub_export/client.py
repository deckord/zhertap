from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx


class SmartGeoHubClientError(RuntimeError):
    """Raised when a Smart GeoHub portal cannot provide a valid response."""


class SmartGeoHubClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 45,
        max_features: int = 250_000,
        retries: int = 3,
        geometry_workers: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.api_base_url = self.base_url + "api/"
        self.timeout_seconds = timeout_seconds
        self.max_features = max_features
        self.retries = retries
        self.geometry_workers = max(1, geometry_workers)
        self._client: httpx.Client | None = None

    def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
            self._client = None

    def __enter__(self) -> SmartGeoHubClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def features(
        self,
        collection: str,
        *,
        context_admterr_id: str = "kz",
        max_features: int | None = None,
        search: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        continuation: list[Any] | None = None
        limit = max_features or self.max_features
        while True:
            payload = self._list_page(
                collection,
                context_admterr_id=context_admterr_id,
                continuation=continuation,
                search=search,
            )
            features = payload.get("features")
            if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
                raise SmartGeoHubClientError(
                    f"{collection} did not return a GeoJSON FeatureCollection"
                )
            rows.extend(feature for feature in features if isinstance(feature, dict))
            if len(rows) >= limit:
                return rows[:limit]
            continuation = payload.get("continuation")
            if not continuation:
                return rows

    def count(
        self,
        collection: str,
        *,
        context_admterr_id: str = "kz",
        search: dict[str, str] | None = None,
    ) -> int:
        payload = self._list_page(
            collection,
            context_admterr_id=context_admterr_id,
            continuation=None,
            search=search,
        )
        total = payload.get("total")
        if isinstance(total, int):
            return total
        try:
            return int(str(total))
        except (TypeError, ValueError):
            features = payload.get("features")
            return len(features) if isinstance(features, list) else 0

    def features_with_geometry(
        self,
        collection: str,
        *,
        context_admterr_id: str = "kz",
        max_features: int | None = None,
        search: dict[str, str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        features = self.features(
            collection,
            context_admterr_id=context_admterr_id,
            max_features=max_features,
            search=search,
        )
        if self.geometry_workers == 1:
            for feature in features:
                row = self.feature_with_geometry(collection, feature)
                if row is not None:
                    yield row
            return
        with ThreadPoolExecutor(max_workers=self.geometry_workers) as executor:
            mapped = executor.map(
                lambda item: self.feature_with_geometry(collection, item),
                features,
            )
            for row in mapped:
                if row is not None:
                    yield row

    def feature_with_geometry(
        self,
        collection: str,
        feature: dict[str, Any],
    ) -> dict[str, Any] | None:
        feature_id = _clean(feature.get("id"))
        if not feature_id:
            return None
        sample_collection = _clean(feature.get("collection")) or collection
        geometry = self.geometry(collection, feature_id)
        if geometry is None and sample_collection != collection:
            geometry = self.geometry(sample_collection, feature_id)
        if geometry is None:
            return None
        return {
            "type": "Feature",
            "id": feature_id,
            "collection": sample_collection,
            "properties": feature.get("properties") or {},
            "geometry": _strip_crs(geometry),
        }

    def geometry(self, collection: str, feature_id: str) -> dict[str, Any] | None:
        response = self._request(
            "geometry",
            params={
                "collection": collection,
                "feature_id": feature_id,
                "lang": "ru",
            },
            allow_not_found=True,
        )
        if response is None:
            return None
        geometry = response.json()
        return geometry if isinstance(geometry, dict) else None

    def _list_page(
        self,
        collection: str,
        *,
        context_admterr_id: str,
        continuation: list[Any] | None,
        search: dict[str, str] | None,
    ) -> dict[str, Any]:
        params: list[tuple[str, str]] = [
            ("collection", collection),
            ("context[admterr_id]", context_admterr_id),
            ("lang", "ru"),
        ]
        if continuation:
            # Smart GeoHub accepts repeated continuation params. JSON string
            # serialization makes some portals return HTTP 500.
            params.extend(("continuation", str(value)) for value in continuation)
        for field, text in (search or {}).items():
            params.append((f"search[{field}][text]", text))
        response = self._request("list", params=params)
        if response is None:
            raise SmartGeoHubClientError(f"{collection} list unexpectedly returned no response")
        payload = response.json()
        if not isinstance(payload, dict):
            raise SmartGeoHubClientError(f"{collection} returned non-object JSON")
        return payload

    def _request(
        self,
        path: str,
        *,
        params: dict[str, str] | list[tuple[str, str]],
        allow_not_found: bool = False,
    ) -> httpx.Response | None:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self._get_client().get(self.api_base_url + path, params=params)
                if allow_not_found and response.status_code == 400:
                    return None
                response.raise_for_status()
                return response
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise SmartGeoHubClientError(
            f"Smart GeoHub request {path} failed after {self.retries} attempts"
        ) from last_error

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            headers = {"User-Agent": "LandScoutKZ/0.4 (Smart GeoHub genplan exporter)"}
            self._client = httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers=headers,
            )
        return self._client


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _strip_crs(geometry: dict[str, Any]) -> dict[str, Any]:
    result = dict(geometry)
    result.pop("crs", None)
    return result
