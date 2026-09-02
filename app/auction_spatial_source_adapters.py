"""Trusted, network-free adapters for signed W5/W6/W7 spatial feature feeds."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from urllib.parse import urlsplit

from pyproj import CRS, Transformer
from shapely.geometry import shape
from shapely.ops import transform

from app.auction_planning_context import (
    FEATURE_KINDS,
    KIND_LAYER_CONTRACT,
    analyze_planning_context,
)
from app.auction_restriction_context import (
    REQUIRED_RESTRICTION_LAYERS,
    analyze_restriction_context,
)
from app.auction_site_context import analyze_site_context

SCHEMA_VERSION = "signed-spatial-feature-feed/2026.1"
PRODUCER_VERSION = "spatial-source-adapters/2026.1"
MAX_FEED_BYTES = 1_000_000
MAX_FEATURES = 1_000
MAX_SOURCES = 30
MAX_VERTICES = 50_000
MAX_TEXT = 300
MAX_CLOCK_SKEW_SECONDS = 300
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/\-]{0,127}$")
_COMMON_FIELDS = {
    "schema_version",
    "feed_kind",
    "feed_id",
    "provider_id",
    "authority_or_license",
    "document_sha256",
    "receipt_sha256",
    "source_url",
    "target_lot_id",
    "crs",
    "bbox",
    "observed_at",
    "valid_from",
    "valid_until",
    "status",
    "payload",
}


class SpatialSourceAdapterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SpatialTrustedProvider:
    provider_id: str
    registry_version: str
    allowed_feed_kinds: tuple[str, ...]
    allowed_https_hosts: tuple[str, ...]
    authority_or_license_sha256: str
    authority_bbox: tuple[float, float, float, float]
    allowed_restriction_layers: tuple[str, ...] = ()
    allowed_planning_layers: tuple[str, ...] = ()
    allowed_site_coverage: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SpatialTrustedReceipt:
    provider_id: str
    feed_id: str
    receipt_sha256: str
    canonical_feed_sha256: str
    provenance_kind: str


@dataclass(frozen=True, slots=True)
class SpatialAdapterIssue:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class SpatialSourceEnvelope:
    key: str
    payload: dict[str, object]
    status: str
    observed_at: datetime
    source_url: str
    generation_id: str
    producer_version: str = PRODUCER_VERSION


@dataclass(frozen=True, slots=True)
class SpatialAdapterResult:
    envelope: SpatialSourceEnvelope | None
    issues: tuple[SpatialAdapterIssue, ...]


def parse_spatial_feed_json(value: bytes | str) -> dict[str, object]:
    """Parse a bounded JSON object and reject duplicate keys at every depth."""
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(encoded, bytes) or not encoded or len(encoded) > MAX_FEED_BYTES:
        raise SpatialSourceAdapterError("spatial feed exceeds byte bound")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise SpatialSourceAdapterError("duplicate spatial feed key")
            result[key] = item
        return result

    try:
        parsed = json.loads(encoded, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpatialSourceAdapterError("invalid spatial feed JSON") from exc
    if not isinstance(parsed, dict):
        raise SpatialSourceAdapterError("spatial feed must be an object")
    return parsed


def canonical_spatial_authority_hash(value: str) -> str:
    if not isinstance(value, str):
        raise SpatialSourceAdapterError("invalid spatial authority")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    if not 1 <= len(normalized) <= MAX_TEXT:
        raise SpatialSourceAdapterError("invalid spatial authority")
    return hashlib.sha256(normalized.casefold().encode()).hexdigest()


def canonical_spatial_feed_hash(feed: Mapping[str, object]) -> str:
    material = {key: value for key, value in feed.items() if key != "receipt_sha256"}
    try:
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SpatialSourceAdapterError("feed is not canonical JSON") from exc
    if len(encoded.encode()) > MAX_FEED_BYTES:
        raise SpatialSourceAdapterError("feed exceeds byte bound")
    return hashlib.sha256(encoded.encode()).hexdigest()


def spatial_provider_registry(
    providers: tuple[SpatialTrustedProvider, ...] | list[SpatialTrustedProvider],
) -> Mapping[str, SpatialTrustedProvider]:
    if not isinstance(providers, (tuple, list)) or not 1 <= len(providers) <= 32:
        raise SpatialSourceAdapterError("invalid spatial provider registry")
    result = {}
    for provider in providers:
        if not isinstance(provider, SpatialTrustedProvider):
            raise SpatialSourceAdapterError("invalid spatial provider registry entry")
        hosts = tuple(host.rstrip(".").casefold() for host in provider.allowed_https_hosts)
        authority_bbox = _bbox(list(provider.authority_bbox))
        restriction_layers = tuple(dict.fromkeys(provider.allowed_restriction_layers))
        planning_layers = tuple(dict.fromkeys(provider.allowed_planning_layers))
        site_coverage = tuple(dict.fromkeys(provider.allowed_site_coverage))
        valid_planning_layers = {
            f"{document}:{layer}" for document, layer in KIND_LAYER_CONTRACT.values()
        }
        if (
            not _ID.fullmatch(provider.provider_id)
            or not _ID.fullmatch(provider.registry_version)
            or not provider.allowed_feed_kinds
            or any(
                kind not in {"restrictions", "site", "planning"}
                for kind in provider.allowed_feed_kinds
            )
            or not hosts
            or any(not _public_hostname(host) for host in hosts)
            or not _SHA256.fullmatch(provider.authority_or_license_sha256)
            or authority_bbox is None
            or any(layer not in REQUIRED_RESTRICTION_LAYERS for layer in restriction_layers)
            or any(layer not in valid_planning_layers for layer in planning_layers)
            or any(
                item not in {"physical_access", "legal_access", "utilities", "hazards"}
                for item in site_coverage
            )
            or ("restrictions" in provider.allowed_feed_kinds and not restriction_layers)
            or ("planning" in provider.allowed_feed_kinds and not planning_layers)
            or ("site" in provider.allowed_feed_kinds and not site_coverage)
            or provider.provider_id in result
        ):
            raise SpatialSourceAdapterError("invalid spatial provider registry entry")
        result[provider.provider_id] = SpatialTrustedProvider(
            provider.provider_id,
            provider.registry_version,
            provider.allowed_feed_kinds,
            hosts,
            provider.authority_or_license_sha256,
            authority_bbox,
            restriction_layers,
            planning_layers,
            site_coverage,
        )
    return MappingProxyType(result)


def _public_hostname(host: str) -> bool:
    if not host or "." not in host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _url(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 1_000:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme != "https"
        or not _public_hostname(host)
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or parsed.fragment
    ):
        return None
    return value, host


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _bbox(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        return None
    bounds = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in bounds):
        return None
    minx, miny, maxx, maxy = bounds
    if not (46 <= minx < maxx <= 88 and 40 <= miny < maxy <= 56.5):
        return None
    return minx, miny, maxx, maxy


def _geometry(value: object, bbox: tuple[float, float, float, float]) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    try:
        geometry = shape(value)
    except Exception:
        return None
    if geometry.is_empty or not geometry.is_valid:
        return None
    minx, miny, maxx, maxy = geometry.bounds
    feed_minx, feed_miny, feed_maxx, feed_maxy = bbox
    if not (
        feed_minx <= minx <= maxx <= feed_maxx
        and feed_miny <= miny <= maxy <= feed_maxy
        and 46 <= minx <= maxx <= 88
        and 40 <= miny <= maxy <= 56.5
    ):
        return None
    coordinates = value.get("coordinates")

    def count(node: object, depth: int = 0) -> int:
        if depth > 10 or not isinstance(node, list):
            raise ValueError
        if node and isinstance(node[0], (int, float)) and not isinstance(node[0], bool):
            return 1
        return sum(count(item, depth + 1) for item in node)

    try:
        if count(coordinates) > MAX_VERTICES:
            return None
    except ValueError:
        return None
    return json.loads(json.dumps(value, allow_nan=False))


def _trusted_common(
    feed: object,
    *,
    expected_kind: str,
    expected_lot_id: str,
    registry: Mapping[str, SpatialTrustedProvider],
    receipts: Mapping[str, SpatialTrustedReceipt],
    now: datetime | None,
) -> tuple[dict[str, object] | None, SpatialAdapterIssue | None]:
    if not isinstance(feed, dict) or set(feed) != _COMMON_FIELDS:
        return None, SpatialAdapterIssue("schema_mismatch", "feed fields mismatch")
    if feed.get("schema_version") != SCHEMA_VERSION or feed.get("feed_kind") != expected_kind:
        return None, SpatialAdapterIssue("schema_mismatch", "feed kind/version mismatch")
    if not isinstance(expected_lot_id, str) or not 1 <= len(expected_lot_id) <= 64:
        raise SpatialSourceAdapterError("invalid expected lot id")
    if feed.get("target_lot_id") != expected_lot_id or feed.get("crs") != "EPSG:4326":
        return None, SpatialAdapterIssue("applicability_mismatch", "lot or CRS mismatch")
    feed_id, provider_id = feed.get("feed_id"), feed.get("provider_id")
    if not isinstance(feed_id, str) or not _ID.fullmatch(feed_id):
        return None, SpatialAdapterIssue("invalid_feed_id", "feed ID invalid")
    if not isinstance(provider_id, str) or not _ID.fullmatch(provider_id):
        return None, SpatialAdapterIssue("untrusted_provider", "provider ID invalid")
    provider = registry.get(provider_id)
    source = _url(feed.get("source_url"))
    authority = feed.get("authority_or_license")
    try:
        authority_hash = canonical_spatial_authority_hash(authority)  # type: ignore[arg-type]
    except SpatialSourceAdapterError:
        authority_hash = None
    if (
        provider is None
        or expected_kind not in provider.allowed_feed_kinds
        or source is None
        or source[1] not in provider.allowed_https_hosts
        or authority_hash != provider.authority_or_license_sha256
    ):
        return None, SpatialAdapterIssue("provider_registry_mismatch", "provider trust mismatch")
    document_hash, receipt_hash = feed.get("document_sha256"), feed.get("receipt_sha256")
    receipt = receipts.get(f"{provider_id}:{feed_id}")
    try:
        canonical_hash = canonical_spatial_feed_hash(feed)
    except SpatialSourceAdapterError:
        canonical_hash = None
    if (
        not isinstance(document_hash, str)
        or not _SHA256.fullmatch(document_hash)
        or not isinstance(receipt_hash, str)
        or not _SHA256.fullmatch(receipt_hash)
        or not isinstance(receipt, SpatialTrustedReceipt)
        or receipt.provider_id != provider_id
        or receipt.feed_id != feed_id
        or receipt.receipt_sha256 != receipt_hash
        or receipt.canonical_feed_sha256 != canonical_hash
        or receipt.provenance_kind not in {"signed_feed", "internal_fetch"}
    ):
        return None, SpatialAdapterIssue("trusted_receipt_mismatch", "receipt mismatch")
    observed = _timestamp(feed.get("observed_at"))
    valid_from = _timestamp(feed.get("valid_from"))
    raw_valid_until = feed.get("valid_until")
    valid_until = _timestamp(raw_valid_until)
    bounds = _bbox(feed.get("bbox"))
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise SpatialSourceAdapterError("now must be timezone-aware")
    checked_at = checked_at.astimezone(UTC)
    if (
        observed is None
        or valid_from is None
        or (raw_valid_until is not None and valid_until is None)
        or (expected_kind != "planning" and valid_until is None)
        or not valid_from <= observed
        or (valid_until is not None and observed > valid_until)
        or bounds is None
        or feed.get("status") not in {"found", "conflict"}
        or not isinstance(feed.get("payload"), dict)
    ):
        return None, SpatialAdapterIssue("invalid_validity_or_bbox", "validity/bbox invalid")
    if observed > checked_at.replace(microsecond=0) + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        return None, SpatialAdapterIssue("future_feed", "observed_at is in the future")
    if valid_until is not None and valid_until < checked_at:
        return None, SpatialAdapterIssue("expired_feed", "feed validity has expired")
    assert bounds is not None
    auth_minx, auth_miny, auth_maxx, auth_maxy = provider.authority_bbox
    minx, miny, maxx, maxy = bounds
    if not (
        auth_minx <= minx <= maxx <= auth_maxx
        and auth_miny <= miny <= maxy <= auth_maxy
    ):
        return None, SpatialAdapterIssue(
            "territory_authority_mismatch", "feed bbox exceeds provider territory"
        )
    normalized = dict(feed)
    normalized["_provider"] = provider
    normalized["_observed"] = observed
    normalized["_bbox"] = bounds
    normalized["_source_url"] = source[0]
    normalized["_canonical_hash"] = canonical_hash
    return normalized, None


def adapt_restriction_feed(
    feed: object,
    *,
    expected_lot_id: str,
    parcel_geojson: object,
    registry: Mapping[str, SpatialTrustedProvider],
    receipts: Mapping[str, SpatialTrustedReceipt],
    now: datetime | None = None,
) -> SpatialAdapterResult:
    common, issue = _trusted_common(
        feed,
        expected_kind="restrictions",
        expected_lot_id=expected_lot_id,
        registry=registry,
        receipts=receipts,
        now=now,
    )
    if issue or common is None:
        return SpatialAdapterResult(None, (issue,) if issue else ())
    raw = common["payload"]
    assert isinstance(raw, dict)
    if set(raw) != {"coverage", "features", "source_version"}:
        return _issue("payload_schema", "restriction payload")
    coverage = raw.get("coverage")
    features = raw.get("features")
    source_version = _bounded_text(raw.get("source_version"))
    provider = common["_provider"]
    assert isinstance(provider, SpatialTrustedProvider)
    authorized_layers = set(provider.allowed_restriction_layers)
    if (
        not isinstance(coverage, dict)
        or set(coverage) - authorized_layers
        or not isinstance(features, list)
        or len(features) > MAX_FEATURES
        or source_version is None
    ):
        return _issue("payload_schema", "restriction coverage/features")
    normalized_coverage = {
        layer: coverage.get(layer) is True for layer in REQUIRED_RESTRICTION_LAYERS
    }
    normalized_features = []
    seen = set()
    for item in features:
        if not isinstance(item, dict) or set(item) != {
            "feature_id",
            "layer",
            "geometry",
            "geometry_mode",
            "impact",
            "reduces_usable_area",
            "value",
        }:
            return _issue("feature_schema", "restriction feature")
        feature_id = item.get("feature_id")
        geometry = _geometry(item.get("geometry"), common["_bbox"])
        value = _optional_text(item.get("value"))
        if (
            not isinstance(feature_id, str)
            or not _ID.fullmatch(feature_id)
            or feature_id in seen
            or item.get("layer") not in REQUIRED_RESTRICTION_LAYERS
            or item.get("layer") not in authorized_layers
            or item.get("geometry_mode") not in {"area", "line_fact"}
            or item.get("impact") not in {"blocker", "warning"}
            or not isinstance(item.get("reduces_usable_area"), bool)
            or geometry is None
            or (item.get("value") is not None and value is None)
        ):
            return _issue("feature_invalid", "restriction feature")
        seen.add(feature_id)
        normalized_features.append(
            {
                "restriction_id": feature_id,
                "layer": item["layer"],
                "source_id": common["feed_id"],
                "geometry": geometry,
                "geometry_mode": item["geometry_mode"],
                "impact": item["impact"],
                "reduces_usable_area": item["reduces_usable_area"],
                "value": value,
            }
        )
    source = {
        "id": common["feed_id"],
        "version": source_version,
        "provenance": _provenance(common),
        "observed_at": common["observed_at"],
        "authoritative": True,
        "coverage": normalized_coverage,
    }
    payload = {
        "restriction_sources": [source],
        "restriction_features": normalized_features,
        "expected_layers": list(REQUIRED_RESTRICTION_LAYERS),
        "generation_id": _generation(common),
    }
    analysis = analyze_restriction_context(
        parcel_geojson,
        restriction_sources=[source],
        restriction_features=normalized_features,
    )
    if analysis.status == "error":
        return _issue("analysis_error", analysis.error_code or "restriction")
    return _result(common, "restrictions", payload)


def adapt_planning_feed(
    feed: object,
    *,
    expected_lot_id: str,
    parcel_geojson: object,
    registry: Mapping[str, SpatialTrustedProvider],
    receipts: Mapping[str, SpatialTrustedReceipt],
    now: datetime | None = None,
) -> SpatialAdapterResult:
    common, issue = _trusted_common(
        feed,
        expected_kind="planning",
        expected_lot_id=expected_lot_id,
        registry=registry,
        receipts=receipts,
        now=now,
    )
    if issue or common is None:
        return SpatialAdapterResult(None, (issue,) if issue else ())
    raw = common["payload"]
    assert isinstance(raw, dict)
    if (
        set(raw) != {"sources", "features"}
        or not isinstance(raw["sources"], list)
        or not isinstance(raw["features"], list)
    ):
        return _issue("payload_schema", "planning payload")
    if len(raw["sources"]) > MAX_SOURCES or len(raw["features"]) > MAX_FEATURES:
        return _issue("payload_bound", "planning payload")
    sources = []
    source_ids = set()
    for item in raw["sources"]:
        if not isinstance(item, dict) or set(item) != {
            "source_id",
            "document_type",
            "version",
            "coverage",
        }:
            return _issue("source_schema", "planning source")
        source_id = item.get("source_id")
        document_type = item.get("document_type")
        coverage = item.get("coverage")
        version = _bounded_text(item.get("version"))
        if (
            not isinstance(source_id, str)
            or not _ID.fullmatch(source_id)
            or source_id in source_ids
            or document_type not in {"genplan", "pdp"}
            or not isinstance(coverage, dict)
            or version is None
        ):
            return _issue("source_invalid", "planning source")
        allowed_layers = {
            layer for doc, layer in KIND_LAYER_CONTRACT.values() if doc == document_type
        }
        provider = common["_provider"]
        assert isinstance(provider, SpatialTrustedProvider)
        authorized_layers = {
            value.split(":", 1)[1]
            for value in provider.allowed_planning_layers
            if value.startswith(f"{document_type}:")
        }
        if set(coverage) - allowed_layers or set(coverage) - authorized_layers:
            return _issue("coverage_invalid", "unexpected planning coverage layer")
        normalized_coverage = {layer: coverage.get(layer) is True for layer in allowed_layers}
        source_ids.add(source_id)
        sources.append(
            {
                "id": source_id,
                "document_type": document_type,
                "version": version,
                "provenance": _provenance(common),
                "observed_at": common["observed_at"],
                "authoritative": True,
                "coverage": normalized_coverage,
            }
        )
    features = []
    seen = set()
    for item in raw["features"]:
        if not isinstance(item, dict) or set(item) != {
            "feature_id",
            "kind",
            "source_id",
            "geometry",
            "value",
            "allowed_use",
        }:
            return _issue("feature_schema", "planning feature")
        feature_id = item.get("feature_id")
        geometry = _geometry(item.get("geometry"), common["_bbox"])
        value = _optional_text(item.get("value"))
        if (
            not isinstance(feature_id, str)
            or not _ID.fullmatch(feature_id)
            or feature_id in seen
            or item.get("kind") not in FEATURE_KINDS
            or item.get("source_id") not in source_ids
            or geometry is None
            or item.get("allowed_use") not in {True, False, None}
            or (item.get("value") is not None and value is None)
        ):
            return _issue("feature_invalid", "planning feature")
        seen.add(feature_id)
        features.append(
            {
                "kind": item["kind"],
                "source_id": item["source_id"],
                "geometry": geometry,
                "value": value,
                "allowed_use": item["allowed_use"],
            }
        )
    analysis = analyze_planning_context(
        parcel_geojson,
        planning_sources=sources,
        planning_features=features,
    )
    if analysis.status == "error":
        return _issue("analysis_error", analysis.error_code or "planning")
    return _result(
        common,
        "planning",
        {
            "planning_sources": sources,
            "planning_features": features,
            "generation_id": _generation(common),
        },
    )


def adapt_site_feed(
    feed: object,
    *,
    expected_lot_id: str,
    profile: str,
    parcel_geojson: object,
    registry: Mapping[str, SpatialTrustedProvider],
    receipts: Mapping[str, SpatialTrustedReceipt],
    now: datetime | None = None,
) -> SpatialAdapterResult:
    common, issue = _trusted_common(
        feed,
        expected_kind="site",
        expected_lot_id=expected_lot_id,
        registry=registry,
        receipts=receipts,
        now=now,
    )
    if issue or common is None:
        return SpatialAdapterResult(None, (issue,) if issue else ())
    raw = common["payload"]
    assert isinstance(raw, dict)
    if set(raw) != {
        "physical_access",
        "legal_access",
        "infrastructure",
        "environment",
        "features",
        "coverage",
    }:
        return _issue("payload_schema", "site payload")
    coverage = raw.get("coverage")
    spatial_features = raw.get("features")
    required_coverage = {"physical_access", "legal_access", "utilities", "hazards"}
    provider = common["_provider"]
    assert isinstance(provider, SpatialTrustedProvider)
    if (
        not isinstance(coverage, dict)
        or set(coverage) - required_coverage
        or set(coverage) - set(provider.allowed_site_coverage)
        or not isinstance(spatial_features, list)
        or len(spatial_features) > MAX_FEATURES
    ):
        return _issue("payload_schema", "site coverage/features")
    category_sections = {
        "physical_access": raw.get("physical_access"),
        "legal_access": raw.get("legal_access"),
        "utilities": raw.get("infrastructure"),
        "hazards": raw.get("environment"),
    }
    if any(
        section is not None and category not in coverage
        for category, section in category_sections.items()
    ):
        return _issue("coverage_authority_missing", "site facts lack authorized coverage")
    feature_ids: dict[str, str] = {}
    geometries: dict[str, dict[str, object]] = {}
    for item in spatial_features:
        if not isinstance(item, dict) or set(item) != {"feature_id", "kind", "geometry"}:
            return _issue("feature_schema", "site feature")
        feature_id = item.get("feature_id")
        kind = item.get("kind")
        geometry = _geometry(item.get("geometry"), common["_bbox"])
        if (
            not isinstance(feature_id, str)
            or not _ID.fullmatch(feature_id)
            or feature_id in feature_ids
            or kind not in {"road", "access_easement", "utility", "hazard", "context"}
            or geometry is None
        ):
            return _issue("feature_invalid", "site feature")
        feature_ids[feature_id] = kind
        geometries[feature_id] = geometry

    normalized = json.loads(json.dumps(raw, allow_nan=False))
    normalized.pop("features")
    normalized.pop("coverage")
    reference_issue = _validate_site_references(normalized, feature_ids)
    if reference_issue:
        return SpatialAdapterResult(None, (reference_issue,))
    parcel_value = _geometry(parcel_geojson, common["_bbox"])
    if parcel_value is None:
        return _issue("parcel_invalid", "parcel geometry invalid or outside feed bbox")
    parcel = shape(parcel_value)
    metric_distance, metric_intersects = _metric_functions(parcel)
    physical = normalized.get("physical_access")
    if isinstance(physical, dict):
        feature_id = physical.get("feature_id")
        road = shape(geometries[str(feature_id)])
        physical["road_distance_m"] = metric_distance(road)
        if physical.get("connected") is True and not metric_intersects(road):
            return _issue("connection_geometry_conflict", "connected road does not touch parcel")
    legal = normalized.get("legal_access")
    if isinstance(legal, dict):
        legal_refs = legal.get("feature_ids")
        assert isinstance(legal_refs, list)
        if legal.get("public_road_access") is True and not any(
            feature_ids[item] == "road" and metric_intersects(shape(geometries[item]))
            for item in legal_refs
        ):
            return _issue("legal_access_geometry_conflict", "public road does not touch parcel")
        if legal.get("easement_confirmed") is True and not any(
            feature_ids[item] == "access_easement"
            and metric_intersects(shape(geometries[item]))
            for item in legal_refs
        ):
            return _issue("legal_access_geometry_conflict", "easement does not touch parcel")
    infrastructure = normalized.get("infrastructure")
    services = infrastructure.get("services") if isinstance(infrastructure, dict) else None
    if isinstance(services, dict):
        for service in services.values():
            assert isinstance(service, dict)
            feature_id = str(service["feature_id"])
            service["distance_m"] = metric_distance(shape(geometries[feature_id]))
    environment = normalized.get("environment")
    context_features = environment.get("features") if isinstance(environment, dict) else None
    if isinstance(context_features, list):
        for context in context_features:
            assert isinstance(context, dict)
            feature_id = str(context["feature_id"])
            context["distance_m"] = metric_distance(shape(geometries[feature_id]))

    provenance = _provenance(common)

    def inject(value: object) -> None:
        if isinstance(value, dict):
            if set(value) == {"coverage_key"}:
                coverage_key = value.get("coverage_key")
                value.clear()
                value.update(
                    {
                        "provenance": provenance,
                        "observed_at": common["observed_at"],
                        "coverage_complete": coverage.get(coverage_key) is True,
                        "confidence": 0.95,
                    }
                )
                return
            if "evidence" in value:
                original = value["evidence"]
                coverage_key = original.get("coverage_key") if isinstance(original, dict) else None
                complete = (
                    coverage.get(coverage_key) is True
                    if coverage_key in required_coverage
                    else False
                )
                value["evidence"] = {
                    "provenance": provenance,
                    "observed_at": common["observed_at"],
                    "coverage_complete": complete,
                    "confidence": 0.95,
                }
            for child in value.values():
                inject(child)
        elif isinstance(value, list):
            for child in value:
                inject(child)

    inject(normalized)
    _strip_site_adapter_fields(normalized)
    analysis = analyze_site_context(
        profile,
        physical_access=normalized["physical_access"],
        legal_access=normalized["legal_access"],
        infrastructure=normalized["infrastructure"],
        environment=normalized["environment"],
    )
    for dimension in (
        analysis.physical_access,
        analysis.legal_access,
        analysis.infrastructure,
        analysis.environment,
    ):
        if dimension.status == "error":
            return _issue("analysis_error", dimension.error_code or "site")
    normalized["generation_id"] = _generation(common)
    return _result(common, "site", normalized)


def _validate_site_references(
    payload: dict[str, object],
    features: dict[str, str],
) -> SpatialAdapterIssue | None:
    physical = payload.get("physical_access")
    if isinstance(physical, dict) and not _reference(
        physical.get("feature_id"), features, {"road"}
    ):
        return SpatialAdapterIssue("feature_reference", "physical road feature missing")
    legal = payload.get("legal_access")
    if isinstance(legal, dict):
        refs = legal.get("feature_ids")
        if not isinstance(refs, list) or len(refs) > 20 or any(
            not _reference(item, features, {"road", "access_easement"}) for item in refs
        ):
            return SpatialAdapterIssue("feature_reference", "legal-access feature missing")
    infrastructure = payload.get("infrastructure")
    services = infrastructure.get("services") if isinstance(infrastructure, dict) else None
    if isinstance(services, dict):
        for service in services.values():
            if not isinstance(service, dict) or not _reference(
                service.get("feature_id"), features, {"utility"}
            ):
                return SpatialAdapterIssue("feature_reference", "utility feature missing")
    environment = payload.get("environment")
    context = environment.get("features") if isinstance(environment, dict) else None
    if isinstance(context, list):
        for item in context:
            if not isinstance(item, dict) or not _reference(
                item.get("feature_id"), features, {"hazard", "context"}
            ):
                return SpatialAdapterIssue("feature_reference", "context feature missing")
    return None


def _reference(value: object, features: dict[str, str], kinds: set[str]) -> bool:
    return isinstance(value, str) and features.get(value) in kinds


def _metric_functions(parcel):
    centroid = parcel.centroid
    zone = max(1, min(60, int((centroid.x + 180) // 6) + 1))
    epsg = 32600 + zone if centroid.y >= 0 else 32700 + zone
    transformer = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_epsg(epsg), always_xy=True)
    parcel_metric = transform(transformer.transform, parcel)

    def distance(other) -> float:
        return round(float(parcel_metric.distance(transform(transformer.transform, other))), 3)

    def intersects(other) -> bool:
        projected = transform(transformer.transform, other)
        return parcel_metric.intersects(projected) or parcel_metric.distance(projected) <= 0.05

    return distance, intersects


def _strip_site_adapter_fields(value: object) -> None:
    if isinstance(value, dict):
        value.pop("feature_id", None)
        value.pop("feature_ids", None)
        for child in value.values():
            _strip_site_adapter_fields(child)
    elif isinstance(value, list):
        for child in value:
            _strip_site_adapter_fields(child)


def _bounded_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized if 1 <= len(normalized) <= MAX_TEXT else None


def _optional_text(value: object) -> str | None:
    return None if value is None else _bounded_text(value)


def _provenance(common: dict[str, object]) -> str:
    provider = common["_provider"]
    assert isinstance(provider, SpatialTrustedProvider)
    return f"signed_gis:{provider.provider_id}:{provider.registry_version}:{_generation(common)}"


def _generation(common: dict[str, object]) -> str:
    provider = common["_provider"]
    assert isinstance(provider, SpatialTrustedProvider)
    material = (
        f"{provider.registry_version}:{common['_canonical_hash']}:{common['receipt_sha256']}"
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _issue(code: str, detail: str) -> SpatialAdapterResult:
    return SpatialAdapterResult(None, (SpatialAdapterIssue(code, detail),))


def _result(
    common: dict[str, object],
    key: str,
    payload: dict[str, object],
) -> SpatialAdapterResult:
    envelope = SpatialSourceEnvelope(
        key,
        payload,
        str(common["status"]),
        common["_observed"],
        str(common["_source_url"]),
        _generation(common),
    )
    return SpatialAdapterResult(envelope, ())
