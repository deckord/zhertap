from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from typing import Any
from urllib.parse import urljoin

import httpx

MATCH_TERMS = (
    "genplan",
    "генплан",
    "генераль",
    "пдп",
    "pdp",
    "красн",
    "red",
    "redline",
    "functional",
    "функцион",
    "zoning",
    "zone",
    "зон",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover urban-plan candidate layers in GeoServer WFS and ArcGIS REST portals."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    wfs = subparsers.add_parser("wfs", help="Probe an OGC WFS/GeoServer endpoint")
    wfs.add_argument("--url", required=True, help="Base WFS URL, for example https://host/geoserver/ows")
    wfs.add_argument("--timeout", type=float, default=30.0)
    wfs.add_argument("--probe-features", action="store_true")

    arcgis = subparsers.add_parser("arcgis", help="Probe an ArcGIS REST services endpoint")
    arcgis.add_argument(
        "--url",
        required=True,
        help="ArcGIS services URL, for example https://host/server/rest/services",
    )
    arcgis.add_argument("--timeout", type=float, default=30.0)
    arcgis.add_argument("--max-services", type=int, default=200)
    arcgis.add_argument("--probe-layers", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "wfs":
            result = discover_wfs(
                args.url,
                timeout=args.timeout,
                probe_features=args.probe_features,
            )
        else:
            result = discover_arcgis(
                args.url,
                timeout=args.timeout,
                max_services=args.max_services,
                probe_layers=args.probe_layers,
            )
    except Exception as exc:
        result = {
            "platform": args.command,
            "url": args.url,
            "error": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def discover_wfs(
    url: str,
    *,
    timeout: float = 30.0,
    probe_features: bool = False,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(
            url,
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetCapabilities",
            },
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        layers = _wfs_feature_types(root)
        candidates = [row for row in layers if _matches(row["search_text"])]
        if probe_features:
            for row in candidates:
                row["sample"] = _probe_wfs_feature(client, url, row["name"])
    return {
        "platform": "wfs",
        "url": url,
        "feature_type_count": len(layers),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def discover_arcgis(
    url: str,
    *,
    timeout: float = 30.0,
    max_services: int = 200,
    probe_layers: bool = False,
) -> dict[str, Any]:
    base_url = url.rstrip("/") + "/"
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        services = _arcgis_services(client, base_url, max_services=max_services)
        candidates: list[dict[str, Any]] = []
        for service in services:
            detail = _arcgis_service_detail(client, service["url"])
            if detail is None:
                continue
            candidate_layers = [
                layer for layer in detail["layers"] if _matches(layer["search_text"])
            ]
            if probe_layers:
                for layer in candidate_layers:
                    layer["sample"] = _probe_arcgis_layer(client, layer["url"])
            if candidate_layers:
                candidates.append(
                    {
                        "name": service["name"],
                        "type": service["type"],
                        "url": service["url"],
                        "layers": candidate_layers,
                    }
                )
    return {
        "platform": "arcgis",
        "url": base_url,
        "service_count": len(services),
        "candidate_service_count": len(candidates),
        "candidates": candidates,
    }


def _wfs_feature_types(root: ET.Element) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for feature_type in root.findall(".//{*}FeatureType"):
        name = _node_text(feature_type, "Name")
        title = _node_text(feature_type, "Title")
        abstract = _node_text(feature_type, "Abstract")
        keywords = [
            (node.text or "").strip()
            for node in feature_type.findall(".//{*}Keyword")
            if (node.text or "").strip()
        ]
        result.append(
            {
                "name": name,
                "title": title,
                "abstract": abstract,
                "keywords": keywords,
                "search_text": " ".join([name, title, abstract, *keywords]).casefold(),
            }
        )
    return result


def _probe_wfs_feature(client: httpx.Client, url: str, type_name: str) -> dict[str, Any]:
    try:
        response = client.get(
            url,
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": type_name,
                "count": 1,
                "outputFormat": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}
    features = payload.get("features") if isinstance(payload, dict) else None
    sample = features[0] if isinstance(features, list) and features else {}
    properties = sample.get("properties") if isinstance(sample, dict) else {}
    geometry = sample.get("geometry") if isinstance(sample, dict) else {}
    return {
        "ok": True,
        "feature_count": payload.get("numberMatched") or payload.get("totalFeatures"),
        "sample_geometry_type": geometry.get("type") if isinstance(geometry, dict) else None,
        "sample_property_keys": sorted(properties)[:30] if isinstance(properties, dict) else [],
    }


def _arcgis_services(
    client: httpx.Client,
    base_url: str,
    *,
    max_services: int,
) -> list[dict[str, str]]:
    queue = [base_url]
    result: list[dict[str, str]] = []
    visited: set[str] = set()
    while queue and len(result) < max_services:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        payload = _arcgis_get(client, current)
        if not payload:
            continue
        for folder in payload.get("folders") or []:
            if isinstance(folder, str) and folder not in {"System", "Utilities"}:
                queue.append(urljoin(base_url, folder.strip("/") + "/"))
        for service in payload.get("services") or []:
            name = str(service.get("name") or "").strip()
            service_type = str(service.get("type") or "").strip()
            if not name or not service_type:
                continue
            result.append(
                {
                    "name": name,
                    "type": service_type,
                    "url": urljoin(base_url, f"{name}/{service_type}"),
                }
            )
            if len(result) >= max_services:
                break
    return result


def _arcgis_service_detail(client: httpx.Client, url: str) -> dict[str, Any] | None:
    payload = _arcgis_get(client, url)
    if not payload:
        return None
    layers = []
    for layer in payload.get("layers") or []:
        name = str(layer.get("name") or "").strip()
        layer_id = layer.get("id")
        if not name or layer_id is None:
            continue
        layers.append(
            {
                "id": layer_id,
                "name": name,
                "url": f"{url}/{layer_id}",
                "search_text": f"{name} {payload.get('name') or ''}".casefold(),
            }
        )
    return {"layers": layers}


def _probe_arcgis_layer(client: httpx.Client, url: str) -> dict[str, Any]:
    detail = _arcgis_get(client, url)
    if not detail:
        return {"ok": False, "error": "layer detail is unavailable"}
    fields = detail.get("fields")
    field_names = [
        str(field.get("name"))
        for field in fields or []
        if isinstance(field, dict) and field.get("name")
    ]
    sample: dict[str, Any] = {}
    try:
        response = client.get(
            url.rstrip("/") + "/query",
            params={
                "f": "pjson",
                "where": "1=1",
                "outFields": "*",
                "returnGeometry": "true",
                "resultRecordCount": 1,
            },
        )
        response.raise_for_status()
        payload = response.json()
        features = payload.get("features") if isinstance(payload, dict) else None
        feature = features[0] if isinstance(features, list) and features else {}
        attributes = feature.get("attributes") if isinstance(feature, dict) else {}
        geometry = feature.get("geometry") if isinstance(feature, dict) else {}
        attribute_keys = sorted(attributes)[:30] if isinstance(attributes, dict) else []
        geometry_keys = sorted(geometry)[:10] if isinstance(geometry, dict) else []
        sample = {
            "query_ok": True,
            "sample_attribute_keys": attribute_keys,
            "sample_geometry_keys": geometry_keys,
        }
    except Exception as exc:
        sample = {"query_ok": False, "error": str(exc)[:500]}
    return {
        "ok": True,
        "geometry_type": detail.get("geometryType"),
        "capabilities": detail.get("capabilities"),
        "field_names": field_names[:50],
        **sample,
    }


def _arcgis_get(client: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        response = client.get(url, params={"f": "pjson"})
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _node_text(parent: ET.Element, local_name: str) -> str:
    node = parent.find(f"{{*}}{local_name}")
    return (node.text or "").strip() if node is not None else ""


def _matches(text: str) -> bool:
    folded = text.casefold()
    return any(term in folded for term in MATCH_TERMS)


if __name__ == "__main__":
    raise SystemExit(main())
