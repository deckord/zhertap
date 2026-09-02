from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx


class JerlerError(RuntimeError):
    pass


class JerlerUpstreamError(JerlerError):
    """Retryable network, 429, or 5xx failure from the remote Jerler service."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class JerlerUnsafeUrlError(JerlerError):
    pass


@dataclass(slots=True)
class JerlerObjectData:
    source_url: str
    land_object_id: str | None = None
    cadastre_number: str | None = None
    land_rights: str | None = None
    lease_term_years: float | None = None
    divisible: bool | None = None
    arrests_text: str | None = None
    restrictions_text: str | None = None
    additional_payment_kzt: float | None = None
    annual_rent_kzt: float | None = None
    geometry_geojson: dict[str, object] | None = None
    cadastral_map_url: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class _PublicObjectParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.scripts: list[str] = []
        self.geometry_wkts: list[str] = []
        self._skip_depth = 0
        self._href: str | None = None
        self._link_text: list[str] = []
        self._in_script = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        wkt = attrs_dict.get("data-wkt")
        if wkt:
            self.geometry_wkts.append(wkt)
        for name in ("wkts", "wktsNeighbours", "data-wkts"):
            self.geometry_wkts.extend(_wkt_attribute_values(attrs_dict.get(name)))
        if tag in {"style", "noscript"}:
            self._skip_depth += 1
        if tag == "script":
            self._in_script = True
            self._script_parts = []
        if tag == "a":
            self._href = attrs_dict.get("href")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "script" and self._in_script:
            self.scripts.append("".join(self._script_parts))
            self._in_script = False
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._link_text)))
            self._href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_parts.append(data)
            return
        if self._skip_depth:
            return
        cleaned = _clean_text(data)
        if not cleaned:
            return
        self.text_parts.append(cleaned)
        if self._href:
            self._link_text.append(cleaned)


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" \t\r\n:;")


def _wkt_attribute_values(value: str | None) -> list[str]:
    if not value:
        return []
    cleaned = value.strip()
    if not cleaned:
        return []
    try:
        decoded = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        decoded = None
    if isinstance(decoded, list):
        return [item for item in decoded if isinstance(item, str) and item.strip()]
    if isinstance(decoded, str) and decoded.strip():
        return [decoded]
    return re.findall(
        r"(?:POLYGON|MULTIPOLYGON|POINT)\s*\([^\"']+\)",
        cleaned,
        flags=re.IGNORECASE,
    )


def _retry_after_seconds(value: str | None) -> float | None:
    """Accept bounded delta-seconds only; HTTP dates are intentionally not guessed."""
    try:
        seconds = float((value or "").strip())
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 86_400 else None


def _number(value: str | None) -> float | None:
    if not value:
        return None
    normalized = re.sub(r"[^0-9,.-]", "", value.replace("\xa0", "").replace(" ", ""))
    if not normalized:
        return None
    if normalized.count(",") == 1 and "." not in normalized:
        normalized = normalized.replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _field(text: str, labels: tuple[str, ...], *, stop_length: int = 320) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_pattern})\s*[:\-]?\s*(.{{1,{stop_length}}}?)(?="
        r"\s+(?:Идентификатор|Кадастровый|Вид |Срок |Делим|Арест|Огранич|Обремен|"
        r"Дополнитель|Ежегод|Размер |Категория|Функциональ|Целевое|Публичн)|$)",
        text,
        flags=re.IGNORECASE,
    )
    return _clean_text(match.group(1)) if match else None


def _json_geometry(scripts: list[str]) -> dict[str, object] | None:
    decoder = json.JSONDecoder()
    for script in scripts:
        for match in re.finditer(r'(?i)["\'](?:geometry|geojson)["\']\s*:\s*', script):
            try:
                value, _ = decoder.raw_decode(script[match.end() :].lstrip())
            except (json.JSONDecodeError, TypeError):
                continue
            if _valid_geometry(value):
                return value
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    continue
                if _valid_geometry(decoded):
                    return decoded
    return None


def _wkt_geometry(wkts: list[str]) -> dict[str, object] | None:
    for wkt in wkts:
        polygon = _polygon_from_wkt(wkt)
        if polygon is not None:
            return polygon
        point = _point_from_wkt(wkt)
        if point is not None:
            return point
    return None


def _coordinate_pair(value: str) -> list[float] | None:
    parts = value.strip().split()
    if len(parts) < 2:
        return None
    try:
        longitude = float(parts[0].replace(",", "."))
        latitude = float(parts[1].replace(",", "."))
    except ValueError:
        return None
    if not (40.0 <= latitude <= 56.5 and 46.0 <= longitude <= 88.5):
        return None
    return [longitude, latitude]


def _polygon_from_wkt(wkt: str) -> dict[str, object] | None:
    match = re.search(r"POLYGON\s*\(\(([^()]+)\)\)", wkt, flags=re.IGNORECASE)
    if not match:
        return None
    ring = [
        pair
        for item in match.group(1).split(",")
        if (pair := _coordinate_pair(item)) is not None
    ]
    if len(ring) < 4:
        return None
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _point_from_wkt(wkt: str) -> dict[str, object] | None:
    match = re.search(r"POINT\s*\(([^()]+)\)", wkt, flags=re.IGNORECASE)
    if not match:
        return None
    pair = _coordinate_pair(match.group(1))
    if pair is None:
        return None
    return {"type": "Point", "coordinates": pair}


def _valid_geometry(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") in {"Polygon", "MultiPolygon", "Point"}
        and isinstance(value.get("coordinates"), list)
    )


def parse_jerler_object(html: str, *, source_url: str) -> JerlerObjectData:
    parser = _PublicObjectParser()
    parser.feed(html)
    text = " ".join(parser.text_parts)
    land_id_match = re.search(
        r"(?:идентификатор(?:\s+земельного\s+участка)?(?:\s+в\s+ЕГКН)?|EGKN\s*ID|Land\s*ID)"
        r"\s*[:№\-]?\s*([0-9]{12,32})",
        text,
        flags=re.IGNORECASE,
    )
    cadastre = _field(text, ("Кадастровый номер", "Кадастрлық нөмір"), stop_length=80)
    rights = _field(
        text,
        (
            "Вид землепользования",
            "Права на землю",
            "Вид права",
            "Право на земельный участок",
        ),
    )
    lease_match = re.search(
        r"(?:срок(?:\s+права|\s+аренды|\s+землепользования)?|сроком)\s*[:\-]?\s*"
        r"(?:на\s*)?([0-9]+(?:[.,][0-9]+)?)\s*(?:лет|года|год)",
        text,
        flags=re.IGNORECASE,
    )
    divisible_match = re.search(
        r"(?:делимость|делимый[/ ]неделимый|участок)\s*[:\-]?\s*(неделимый|делимый)",
        text,
        flags=re.IGNORECASE,
    )
    additional = _field(
        text,
        (
            "Дополнительный платеж",
            "Возмещение потерь сельскохозяйственного производства",
            "Потери сельскохозяйственного производства",
        ),
        stop_length=100,
    )
    annual_rent = _field(
        text,
        ("Ежегодная арендная плата", "Годовая арендная плата", "Размер арендной платы"),
        stop_length=100,
    )
    map_url = None
    for href, link_text in parser.links:
        absolute = urljoin(source_url, href)
        combined = f"{link_text} {href}".casefold()
        if any(token in combined for token in ("кадастров", "публичн", "карта", "map")):
            map_url = absolute
            break
    return JerlerObjectData(
        source_url=source_url,
        land_object_id=land_id_match.group(1) if land_id_match else None,
        cadastre_number=cadastre,
        land_rights=rights,
        lease_term_years=_number(lease_match.group(1)) if lease_match else None,
        divisible=(divisible_match.group(1).casefold() == "делимый") if divisible_match else None,
        arrests_text=_field(text, ("Аресты", "Наличие арестов")),
        restrictions_text=_field(
            text,
            ("Ограничения", "Обременения", "Ограничения и обременения"),
        ),
        additional_payment_kzt=_number(additional),
        annual_rent_kzt=_number(annual_rent),
        geometry_geojson=_json_geometry(parser.scripts) or _wkt_geometry(parser.geometry_wkts),
        cadastral_map_url=map_url,
    )


class JerlerProvider:
    """Bounded reader for public Jerler/E-Qazyna source-object cards."""

    DEFAULT_ALLOWED_HOSTS = ("traderesources.e-qazyna.kz", "jerler.e-qazyna.kz")

    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        max_response_bytes: int = 2_000_000,
        max_redirects: int = 2,
        verify_tls: bool = True,
        allowed_hosts: tuple[str, ...] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self.max_response_bytes = max(16_384, min(int(max_response_bytes), 5_000_000))
        self.max_redirects = max(0, min(int(max_redirects), 3))
        self.verify_tls = verify_tls
        self.allowed_hosts = tuple(
            host.casefold().strip(".") for host in (allowed_hosts or self.DEFAULT_ALLOWED_HOSTS)
        )
        self.transport = transport

    def _validated_url(self, url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold().strip(".")
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise JerlerUnsafeUrlError("Jerler source URL must use public HTTPS")
        if not host or not any(host == allowed for allowed in self.allowed_hosts):
            raise JerlerUnsafeUrlError("Jerler source URL host is not allowed")
        try:
            if ipaddress.ip_address(host).is_private:
                raise JerlerUnsafeUrlError("Private network addresses are not allowed")
        except ValueError:
            pass
        return url

    def fetch_object(self, source_url: str) -> JerlerObjectData:
        current_url = self._validated_url(source_url)
        timeout = httpx.Timeout(self.timeout_seconds, connect=min(5.0, self.timeout_seconds))
        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        try:
            with httpx.Client(
                timeout=timeout,
                limits=limits,
                verify=self.verify_tls,
                follow_redirects=False,
                transport=self.transport,
                headers={
                    "User-Agent": "LandScoutKazakhstan/1.0 (+public land object monitor)",
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                },
            ) as client:
                for redirect_no in range(self.max_redirects + 1):
                    with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            if redirect_no >= self.max_redirects:
                                raise JerlerError("Jerler redirect limit exceeded")
                            location = response.headers.get("location")
                            if not location:
                                raise JerlerError("Jerler returned an empty redirect")
                            current_url = self._validated_url(urljoin(current_url, location))
                            continue
                        if response.status_code == 429 or response.status_code >= 500:
                            raise JerlerUpstreamError(
                                f"Jerler upstream returned HTTP {response.status_code}",
                                status_code=response.status_code,
                                retry_after_seconds=_retry_after_seconds(
                                    response.headers.get("retry-after")
                                ),
                            )
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").casefold()
                        allowed_types = ("text/html", "application/json", "text/plain")
                        if not any(kind in content_type for kind in allowed_types):
                            raise JerlerError("Jerler returned an unsupported content type")
                        chunks: list[bytes] = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > self.max_response_bytes:
                                raise JerlerError(
                                    "Jerler response exceeded the configured size limit"
                                )
                            chunks.append(chunk)
                        charset = response.encoding or "utf-8"
                        html = b"".join(chunks).decode(charset, errors="replace")
                        return parse_jerler_object(html, source_url=current_url)
        except httpx.RequestError as exc:
            raise JerlerUpstreamError("Jerler network request failed") from exc
        raise JerlerError("Jerler object could not be fetched")
