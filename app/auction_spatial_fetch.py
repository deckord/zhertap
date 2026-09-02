"""Trusted bounded fetcher for configured signed/internal spatial feeds."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.auction_spatial_source_adapters import (
    SpatialSourceAdapterError,
    SpatialTrustedProvider,
    SpatialTrustedReceipt,
    canonical_spatial_authority_hash,
    canonical_spatial_feed_hash,
    parse_spatial_feed_json,
    spatial_provider_registry,
)
from app.provider_backpressure import ProviderBackpressure, ProviderPolicy

MAX_CONFIG_BYTES = 128_000
MAX_PROVIDERS = 32
MAX_FEEDS = 96
MAX_RESPONSE_BYTES = 1_000_000
MAX_REDIRECTS = 2
HTTP_TIMEOUT_SECONDS = 15
MAX_TOTAL_FETCH_SECONDS = 20
PROVIDER_LEASE_SECONDS = 60


class SpatialFetchError(RuntimeError):
    pass


class SpatialFetchDeferred(SpatialFetchError):
    def __init__(self, code: str, retry_after_seconds: float) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = max(0.1, min(float(retry_after_seconds), 21_600))


class SpatialFetchTerminal(SpatialFetchError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SpatialFeedEndpoint:
    provider_id: str
    feed_id: str
    module: str
    url_template: str
    auth_mode: str
    hmac_secret: bytes | None
    pinned_sha256: str | None
    allowed_hosts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpatialFetchRuntime:
    registry: Mapping[str, SpatialTrustedProvider]
    endpoints: Mapping[tuple[str, str], SpatialFeedEndpoint]
    policies: Mapping[str, ProviderPolicy]


@dataclass(frozen=True, slots=True)
class VerifiedSpatialFeed:
    feed: dict[str, object]
    receipt: SpatialTrustedReceipt
    canonical_feed_sha256: str
    raw_sha256: str
    final_url: str


def parse_spatial_fetch_runtime(raw_json: str) -> SpatialFetchRuntime:
    if not isinstance(raw_json, str) or not raw_json.strip():
        raise SpatialFetchTerminal("config_missing", "spatial provider config is empty")
    if len(raw_json.encode()) > MAX_CONFIG_BYTES:
        raise SpatialFetchTerminal("config_oversized", "spatial provider config is oversized")
    try:
        value = json.loads(raw_json, object_pairs_hook=_reject_duplicate_pairs)
    except (json.JSONDecodeError, SpatialFetchTerminal) as exc:
        raise SpatialFetchTerminal("config_invalid", "spatial provider config is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"providers"}:
        raise SpatialFetchTerminal("config_invalid", "expected providers config")
    providers = value["providers"]
    if not isinstance(providers, list) or not 1 <= len(providers) <= MAX_PROVIDERS:
        raise SpatialFetchTerminal("config_invalid", "invalid provider count")
    trusted: list[SpatialTrustedProvider] = []
    endpoints: dict[tuple[str, str], SpatialFeedEndpoint] = {}
    policies: dict[str, ProviderPolicy] = {}
    for item in providers:
        if not isinstance(item, dict) or set(item) != {
            "provider_id",
            "registry_version",
            "allowed_hosts",
            "authority_or_license",
            "authority_bbox",
            "restriction_layers",
            "planning_layers",
            "site_coverage",
            "qps",
            "burst",
            "max_concurrency",
            "feeds",
        }:
            raise SpatialFetchTerminal("config_invalid", "provider config fields mismatch")
        provider_id = _provider_id(item["provider_id"])
        registry_version = _identifier(item["registry_version"], 128)
        hosts = _hosts(item["allowed_hosts"])
        feeds = item["feeds"]
        if not isinstance(feeds, list) or not feeds or len(feeds) > MAX_FEEDS:
            raise SpatialFetchTerminal("config_invalid", "invalid feed list")
        feed_kinds: set[str] = set()
        for feed in feeds:
            endpoint = _endpoint(provider_id, hosts, feed)
            key = (provider_id, endpoint.feed_id)
            if key in endpoints:
                raise SpatialFetchTerminal("config_invalid", "duplicate spatial feed")
            endpoints[key] = endpoint
            feed_kinds.add(endpoint.module)
        try:
            trusted.append(
                SpatialTrustedProvider(
                    provider_id,
                    registry_version,
                    tuple(sorted(feed_kinds)),
                    hosts,
                    canonical_spatial_authority_hash(item["authority_or_license"]),
                    tuple(item["authority_bbox"]),
                    tuple(item["restriction_layers"]),
                    tuple(item["planning_layers"]),
                    tuple(item["site_coverage"]),
                )
            )
        except (SpatialSourceAdapterError, TypeError) as exc:
            raise SpatialFetchTerminal(
                "config_invalid", "invalid spatial provider authority scope"
            ) from exc
        qps = _number(item["qps"], 0.01, 20)
        burst = _integer(item["burst"], 1, 20)
        concurrency = _integer(item["max_concurrency"], 1, 10)
        policies[provider_id] = ProviderPolicy(
            provider_id,
            qps,
            burst,
            concurrency,
            PROVIDER_LEASE_SECONDS,
            queue_warning_depth=100,
            queue_critical_depth=500,
        )
    if len(endpoints) > MAX_FEEDS:
        raise SpatialFetchTerminal("config_invalid", "aggregate feed count exceeds bound")
    try:
        registry = spatial_provider_registry(trusted)
    except SpatialSourceAdapterError as exc:
        raise SpatialFetchTerminal(
            "config_invalid", "invalid spatial provider registry"
        ) from exc
    return SpatialFetchRuntime(
        registry, MappingProxyType(endpoints), MappingProxyType(policies)
    )


def _reject_duplicate_pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in items:
        if key in result:
            raise SpatialFetchTerminal("duplicate_config_key", "duplicate config key")
        result[key] = value
    return result


def _identifier(value: object, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not all(character.isalnum() or character in "_.:/-" for character in value)
    ):
        raise SpatialFetchTerminal("config_invalid", "invalid identifier")
    return value


def _provider_id(value: object) -> str:
    provider_id = _identifier(value, 80)
    if not all(character.isalnum() or character in "_-" for character in provider_id):
        raise SpatialFetchTerminal("config_invalid", "invalid provider ID")
    return provider_id


def _hosts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise SpatialFetchTerminal("config_invalid", "invalid host allowlist")
    hosts = tuple(str(item).casefold().rstrip(".") for item in value)
    if len(set(hosts)) != len(hosts) or any(not _public_hostname(host) for host in hosts):
        raise SpatialFetchTerminal("config_invalid", "unsafe host allowlist")
    return hosts


def _endpoint(
    provider_id: str,
    hosts: tuple[str, ...],
    value: object,
) -> SpatialFeedEndpoint:
    if not isinstance(value, dict) or set(value) != {
        "feed_id",
        "module",
        "url_template",
        "auth_mode",
        "hmac_secret_b64",
        "pinned_sha256",
    }:
        raise SpatialFetchTerminal("config_invalid", "feed config fields mismatch")
    feed_id = _identifier(value["feed_id"], 128)
    module = value["module"]
    if module not in {"restrictions", "site", "planning"}:
        raise SpatialFetchTerminal("config_invalid", "invalid feed module")
    template = value["url_template"]
    if not isinstance(template, str) or template.count("{lot_id}") != 1 or len(template) > 2_000:
        raise SpatialFetchTerminal("config_invalid", "invalid feed URL template")
    _validate_url(template.replace("{lot_id}", "probe"), hosts)
    mode = value["auth_mode"]
    secret = None
    pin = value["pinned_sha256"]
    if mode == "hmac_sha256":
        if value["pinned_sha256"] is not None or not isinstance(value["hmac_secret_b64"], str):
            raise SpatialFetchTerminal("config_invalid", "invalid HMAC configuration")
        try:
            secret = base64.b64decode(value["hmac_secret_b64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise SpatialFetchTerminal("config_invalid", "invalid HMAC secret") from exc
        if len(secret) < 32:
            raise SpatialFetchTerminal("config_invalid", "HMAC secret is too short")
    elif mode == "pinned_sha256":
        if value["hmac_secret_b64"] is not None or not _sha256(pin):
            raise SpatialFetchTerminal("config_invalid", "invalid pinned hash")
    else:
        raise SpatialFetchTerminal("config_invalid", "unsupported authenticity mode")
    return SpatialFeedEndpoint(
        provider_id,
        feed_id,
        module,
        template,
        mode,
        secret,
        pin,
        hosts,
    )


def fetch_verified_spatial_feed(
    endpoint: SpatialFeedEndpoint,
    *,
    source_lot_id: str,
    backpressure: ProviderBackpressure,
    owner_token: str,
    transport: httpx.BaseTransport | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> VerifiedSpatialFeed:
    if not isinstance(source_lot_id, str) or not 1 <= len(source_lot_id) <= 64:
        raise SpatialFetchTerminal("lot_invalid", "invalid source lot ID")
    permit = backpressure.acquire(endpoint.provider_id, owner_token=owner_token)
    if not permit.allowed:
        raise SpatialFetchDeferred(permit.status, permit.retry_after_seconds or 1)
    started = monotonic()
    upstream_success = True
    error = None
    record_result = True
    try:
        url = endpoint.url_template.replace("{lot_id}", quote(source_lot_id, safe=""))
        body, headers, final_url = _fetch_bytes(
            endpoint,
            url,
            transport=transport,
            deadline_at=started + MAX_TOTAL_FETCH_SECONDS,
            monotonic=monotonic,
        )
        raw_hash = hashlib.sha256(body).hexdigest()
        _verify_authenticity(endpoint, body, raw_hash, headers)
        try:
            feed = parse_spatial_feed_json(body)
            canonical = canonical_spatial_feed_hash(feed)
        except ValueError as exc:
            raise SpatialFetchTerminal("feed_invalid", "verified feed is malformed") from exc
        if (
            feed.get("provider_id") != endpoint.provider_id
            or feed.get("feed_id") != endpoint.feed_id
            or feed.get("target_lot_id") != source_lot_id
            or feed.get("feed_kind") != endpoint.module
        ):
            raise SpatialFetchTerminal("feed_identity_mismatch", "verified feed identity mismatch")
        receipt_hash = feed.get("receipt_sha256")
        if not _sha256(receipt_hash):
            raise SpatialFetchTerminal("receipt_invalid", "verified feed receipt is invalid")
        receipt = SpatialTrustedReceipt(
            endpoint.provider_id,
            endpoint.feed_id,
            receipt_hash,
            canonical,
            "signed_feed" if endpoint.auth_mode == "hmac_sha256" else "internal_fetch",
        )
        return VerifiedSpatialFeed(feed, receipt, canonical, raw_hash, final_url)
    except SpatialFetchDeferred:
        upstream_success = False
        error = "upstream_deferred"
        raise
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        upstream_success = False
        error = "network_error"
        raise SpatialFetchDeferred("network_error", 5) from exc
    except SpatialFetchTerminal:
        raise
    except BaseException:
        # Cancellation is not an upstream success or failure. Release the owner lease
        # without mutating the circuit so another worker can safely continue.
        record_result = False
        backpressure.release(permit)
        raise
    finally:
        if record_result:
            backpressure.record_result(
                permit,
                success=upstream_success,
                latency_ms=max(0, (monotonic() - started) * 1_000),
                error=error,
            )


def _fetch_bytes(
    endpoint: SpatialFeedEndpoint,
    url: str,
    *,
    transport: httpx.BaseTransport | None,
    deadline_at: float,
    monotonic: Callable[[], float],
) -> tuple[bytes, httpx.Headers, str]:
    current = _validate_url(url, endpoint.allowed_hosts)
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=5)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        verify=True,
        transport=transport,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        headers={"Accept": "application/json", "User-Agent": "ZhertapSpatial/2026.1"},
    ) as client:
        for redirect in range(MAX_REDIRECTS + 1):
            _check_deadline(monotonic, deadline_at)
            with client.stream("GET", current) as response:
                _check_deadline(monotonic, deadline_at)
                if response.is_redirect:
                    if redirect >= MAX_REDIRECTS:
                        raise SpatialFetchTerminal("redirect_limit", "redirect limit exceeded")
                    location = response.headers.get("location")
                    if not location:
                        raise SpatialFetchTerminal("redirect_invalid", "empty redirect")
                    current = _validate_url(urljoin(current, location), endpoint.allowed_hosts)
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    retry = _retry_after(response.headers.get("retry-after"))
                    raise SpatialFetchDeferred(f"http_{response.status_code}", retry or 5)
                if response.status_code >= 400:
                    raise SpatialFetchTerminal("http_client_error", "provider rejected request")
                content_type = response.headers.get("content-type", "").casefold()
                if "application/json" not in content_type:
                    raise SpatialFetchTerminal("content_type", "provider did not return JSON")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    _check_deadline(monotonic, deadline_at)
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise SpatialFetchTerminal("response_oversized", "response exceeds bound")
                    chunks.append(chunk)
                _check_deadline(monotonic, deadline_at)
                return b"".join(chunks), response.headers, current
    raise SpatialFetchTerminal("redirect_invalid", "redirect loop")


def _check_deadline(monotonic: Callable[[], float], deadline_at: float) -> None:
    if monotonic() > deadline_at:
        raise SpatialFetchDeferred("fetch_deadline", 5)


def _verify_authenticity(
    endpoint: SpatialFeedEndpoint,
    body: bytes,
    raw_hash: str,
    headers: httpx.Headers,
) -> None:
    if endpoint.auth_mode == "pinned_sha256":
        if not hmac.compare_digest(raw_hash, endpoint.pinned_sha256 or ""):
            raise SpatialFetchTerminal("pinned_hash_mismatch", "pinned feed hash mismatch")
        return
    signature = headers.get("x-zhertap-signature", "")
    expected = hmac.new(endpoint.hmac_secret or b"", body, hashlib.sha256).hexdigest()
    supplied = signature.removeprefix("sha256=").strip().casefold()
    if not _sha256(supplied) or not hmac.compare_digest(expected, supplied):
        raise SpatialFetchTerminal("signature_invalid", "feed signature verification failed")


def _validate_url(value: str, allowed_hosts: tuple[str, ...]) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SpatialFetchTerminal("url_invalid", "invalid spatial feed URL") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or host not in allowed_hosts
        or not _public_hostname(host)
        or parsed.fragment
    ):
        raise SpatialFetchTerminal("url_forbidden", "spatial feed URL is not allowlisted")
    return value


def _public_hostname(host: str) -> bool:
    if not host or "." not in host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return True


def _retry_after(value: str | None) -> float | None:
    try:
        number = float((value or "").strip())
    except ValueError:
        return None
    return number if math.isfinite(number) and 0 <= number <= 21_600 else None


def _number(value: object, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpatialFetchTerminal("config_invalid", "invalid provider number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise SpatialFetchTerminal("config_invalid", "provider number outside bounds")
    return number


def _integer(value: object, minimum: int, maximum: int) -> int:
    number = _number(value, minimum, maximum)
    if number != int(number):
        raise SpatialFetchTerminal("config_invalid", "provider integer required")
    return int(number)


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
