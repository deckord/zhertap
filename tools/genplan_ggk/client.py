from __future__ import annotations

import time
from typing import Any

import httpx

DEFAULT_WFS_URL = "https://gov.ggk.kz/geoserver/ows"
WORKSPACE = "default_workspace"


class GgkClientError(RuntimeError):
    """Raised when the public AIS GGK WFS cannot provide a valid response."""


class GgkClient:
    def __init__(
        self,
        *,
        wfs_url: str = DEFAULT_WFS_URL,
        timeout_seconds: float = 90,
        page_size: int = 5000,
        max_features: int = 250_000,
        retries: int = 3,
    ) -> None:
        self.wfs_url = wfs_url
        self.timeout_seconds = timeout_seconds
        self.page_size = page_size
        self.max_features = max_features
        self.retries = retries

    def features(
        self,
        type_name: str,
        *,
        cql_filter: str | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        count = page_size or self.page_size
        start_index = 0
        rows: list[dict[str, Any]] = []
        while True:
            params: dict[str, str | int] = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": f"{WORKSPACE}:{type_name}",
                "outputFormat": "application/json",
                "srsName": "EPSG:4326",
                "sortBy": "id",
                "count": count,
                "startIndex": start_index,
            }
            if cql_filter:
                params["CQL_FILTER"] = cql_filter
            payload = self._request(params)
            features = payload.get("features")
            if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
                raise GgkClientError(f"{type_name} did not return a GeoJSON FeatureCollection")
            rows.extend(feature for feature in features if isinstance(feature, dict))
            if len(rows) > self.max_features:
                raise GgkClientError(
                    f"{type_name} exceeded the safety limit of {self.max_features} features"
                )
            if len(features) < count:
                return rows
            start_index += len(features)

    def one(self, type_name: str, *, cql_filter: str) -> dict[str, Any]:
        rows = self.features(type_name, cql_filter=cql_filter, page_size=2)
        if len(rows) != 1:
            raise GgkClientError(
                f"Expected one {type_name} feature for {cql_filter!r}, received {len(rows)}"
            )
        return rows[0]

    def _request(self, params: dict[str, str | int]) -> dict[str, Any]:
        last_error: Exception | None = None
        headers = {"User-Agent": "LandScoutKZ/0.3 (official urban-plan importer)"}
        for attempt in range(self.retries):
            try:
                with httpx.Client(
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                    headers=headers,
                ) as client:
                    response = client.get(self.wfs_url, params=params)
                    response.raise_for_status()
                    payload = response.json()
                if not isinstance(payload, dict):
                    raise GgkClientError("AIS GGK returned a non-object JSON response")
                return payload
            except (httpx.HTTPError, ValueError, GgkClientError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise GgkClientError(
            f"AIS GGK request failed after {self.retries} attempts"
        ) from last_error
