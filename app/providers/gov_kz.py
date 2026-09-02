from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from app.config import settings
from app.provider_backpressure import ProviderBackpressure
from app.provider_guard import bounded_http_request, guarded_http_call


class GovKzError(RuntimeError):
    pass


@dataclass(slots=True)
class GovKzAttachment:
    title: str
    url: str
    file_type: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(slots=True)
class GovKzAnnouncement:
    source_url: str
    source_kind: str
    project: str | None
    title: str
    body_text: str
    published_at: datetime | None = None
    lot_numbers: set[str] = field(default_factory=set)
    auction_numbers: set[str] = field(default_factory=set)
    cadastre_numbers: set[str] = field(default_factory=set)
    eqazyna_urls: set[str] = field(default_factory=set)
    attachments: list[GovKzAttachment] = field(default_factory=list)
    raw_payload: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "source_url": self.source_url,
            "source_kind": self.source_kind,
            "project": self.project,
            "title": self.title,
            "body_text": self.body_text,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "lot_numbers": sorted(self.lot_numbers),
            "auction_numbers": sorted(self.auction_numbers),
            "cadastre_numbers": sorted(self.cadastre_numbers),
            "eqazyna_urls": sorted(self.eqazyna_urls),
            "attachments": [item.as_dict() for item in self.attachments],
            "raw_payload": self.raw_payload,
        }


LAND_AUCTION_KEYWORDS = (
    "земельный аукцион",
    "земельные аукционы",
    "земельные торги",
    "земельного участка",
    "земельный участок",
    "земельных участков",
    "право аренды",
    "право частной собственности",
    "e-qazyna",
    "eqazyna",
    "gosreestr",
)

CADASTRE_RE = re.compile(r"\b\d{2}-\d{3}-\d{3}-\d{3,}\b")
LOT_NUMBER_RE = re.compile(r"(?iu)(?:№\s*лота|лот\s*№|номер\s*лота|лота\s*№)\D{0,50}(\d{3,12})")
AUCTION_NUMBER_RE = re.compile(
    r"(?iu)(?:№\s*торгов|аукцион\s*№|номер\s*аукциона)\D{0,50}([A-ZА-Я0-9-]{3,32})"
)


class _HtmlTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._active_href: str | None = None
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            attr_map = dict(attrs)
            self._active_href = attr_map.get("href")
            self._active_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a" and self._active_href:
            self.links.append((self._active_href, _clean_text(" ".join(self._active_text))))
            self._active_href = None
            self._active_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = _clean_text(data)
        if not cleaned:
            return
        self.text_parts.append(cleaned)
        if self._active_href is not None:
            self._active_text.append(cleaned)


class GovKzProvider:
    def __init__(
        self,
        *,
        base_url: str = "https://www.gov.kz",
        timeout_seconds: int | None = None,
        verify_tls: bool | None = None,
        backpressure: ProviderBackpressure | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.gov_kz_timeout_seconds
        self.verify_tls = settings.gov_kz_verify_tls if verify_tls is None else verify_tls
        self.backpressure = backpressure
        self.errors: list[str] = []
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            verify=self.verify_tls,
            follow_redirects=True,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru",
                "User-Agent": "LandScoutKZ/0.4 (pre-purchase land auction monitoring)",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GovKzProvider:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def crawl_announcements(
        self,
        *,
        projects: list[str],
        detail_urls: list[str] | None = None,
        page_size: int | None = None,
        max_pages: int | None = None,
    ) -> list[GovKzAnnouncement]:
        page_size = page_size or settings.auction_v2_gov_kz_page_size
        max_pages = max_pages or settings.auction_v2_gov_kz_max_pages
        announcements: list[GovKzAnnouncement] = []
        seen_urls: set[str] = set()

        content_kinds = ["documents", "events"]
        if settings.auction_v2_gov_kz_include_news:
            content_kinds.append("news")

        for project in projects:
            for kind in content_kinds:
                for page in range(max_pages):
                    try:
                        items = self._list_items(
                            kind=kind,
                            project=project,
                            page=page,
                            size=page_size,
                        )
                    except GovKzError as exc:
                        if kind != "news":
                            self.errors.append(f"{project}:{kind}: {exc}")
                        break
                    if not items:
                        break
                    for item in items:
                        announcement = self._announcement_from_item(
                            item,
                            kind=kind,
                            project=project,
                        )
                        if announcement is None or announcement.source_url in seen_urls:
                            continue
                        seen_urls.add(announcement.source_url)
                        announcements.append(announcement)

        for url in detail_urls or []:
            try:
                announcement = self.fetch_detail_url(url)
            except GovKzError:
                continue
            if announcement.source_url in seen_urls:
                continue
            seen_urls.add(announcement.source_url)
            announcements.append(announcement)

        return announcements

    def fetch_detail_url(self, url: str) -> GovKzAnnouncement:
        response = guarded_http_call(
            "gov_kz",
            lambda: bounded_http_request(
                self._client, "GET", _absolute_url(self.base_url, url)
            ),
            backpressure=self.backpressure,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GovKzError(f"gov.kz detail request failed: {exc}") from exc
        text, links = parse_html_text(response.text)
        title = _html_title(response.text) or _compact(text, 160) or "gov.kz"
        announcement = _build_announcement(
            source_url=str(response.url),
            source_kind=_source_kind_from_url(str(response.url)),
            project=_project_from_url(str(response.url)),
            title=title,
            body_text=text,
            links=links,
            raw_payload={"url": str(response.url), "source": "html_detail"},
        )
        if not is_land_auction_text(f"{announcement.title} {announcement.body_text}"):
            raise GovKzError("gov.kz detail is not a land auction announcement")
        return announcement

    def _list_items(
        self,
        *,
        kind: str,
        project: str,
        page: int,
        size: int,
        request_headers: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        params = {
            "projects": project,
            "page": page,
            "size": size,
            "sort-by": _sort_for_kind(kind),
        }
        response = guarded_http_call(
            "gov_kz",
            lambda: bounded_http_request(
                self._client,
                "GET",
                f"/api/v1/public/content-manager/{kind}",
                params=params,
                headers=(
                    request_headers
                    if request_headers is not None
                    else self._special_headers(kind)
                ),
            ),
            backpressure=self.backpressure,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GovKzError(f"gov.kz {kind} list request failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise GovKzError(f"gov.kz {kind} list returned invalid JSON") from exc
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def list_items_page(
        self,
        *,
        kind: str,
        project: str,
        page: int,
        size: int,
        request_headers: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        """Fetch one parsed gov.kz page; news headers are a separate workflow unit."""
        if kind not in {"documents", "events", "news"} or not 0 <= page <= 1_000:
            raise ValueError("invalid gov.kz page unit")
        return self._list_items(
            kind=kind,
            project=project,
            page=page,
            size=size,
            request_headers=request_headers,
        )

    def news_headers_unit(self) -> dict[str, str]:
        """Fetch the news challenge in one request; malformed data is terminal for the unit."""
        response = guarded_http_call(
            "gov_kz",
            lambda: bounded_http_request(
                self._client, "POST", "/api/v2/_/c/k6", json={}
            ),
            backpressure=self.backpressure,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise GovKzError("gov.kz news headers returned invalid payload")
        result = {str(key): str(value) for key, value in payload.items() if value is not None}
        if len(result) > 32 or sum(len(k) + len(v) for k, v in result.items()) > 4000:
            raise GovKzError("gov.kz news headers exceed bounds")
        return result

    def _special_headers(self, kind: str) -> dict[str, str]:
        if kind != "news":
            return {}
        try:
            response = guarded_http_call(
                "gov_kz",
                lambda: bounded_http_request(
                    self._client, "POST", "/api/v2/_/c/k6", json={}
                ),
                backpressure=self.backpressure,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items() if value is not None}

    def _announcement_from_item(
        self,
        item: dict[str, object],
        *,
        kind: str,
        project: str,
    ) -> GovKzAnnouncement | None:
        title = _string(item.get("title")) or _string(item.get("hint")) or "gov.kz"
        body_html = " ".join(
            value
            for value in (
                _text_value(item.get("content")),
                _text_value(item.get("body")),
                _text_value(item.get("full_text")),
                _text_value(item.get("hint")),
            )
            if value
        )
        body_text, links = parse_html_text(body_html)
        combined_text = f"{title} {body_text}"
        if not is_land_auction_text(combined_text):
            return None
        source_url = self._item_url(item, kind=kind, project=project)
        return _build_announcement(
            source_url=source_url,
            source_kind=kind,
            project=_string(item.get("projects")) or project,
            title=title,
            body_text=body_text,
            links=links,
            raw_payload=_json_ready(item),
            published_at=_item_datetime(item, kind=kind),
            attachments=_attachments_from_item(item, base_url=self.base_url),
        )

    def _item_url(self, item: dict[str, object], *, kind: str, project: str) -> str:
        item_id = _string(item.get("id"))
        if not item_id:
            return f"{self.base_url}/memleket/entities/{project}?lang=ru"
        if kind == "events":
            return (
                f"{self.base_url}/memleket/entities/{project}/press/events/details/{item_id}"
                "?lang=ru"
            )
        if kind == "news":
            return (
                f"{self.base_url}/memleket/entities/{project}/press/news/details/{item_id}?lang=ru"
            )
        return f"{self.base_url}/memleket/entities/{project}/documents/details/{item_id}?lang=ru"


def parse_html_text(html: str) -> tuple[str, list[tuple[str, str]]]:
    parser = _HtmlTextParser()
    parser.feed(html or "")
    return _clean_text(" ".join(parser.text_parts)), parser.links


def is_land_auction_text(text: str) -> bool:
    lowered = (text or "").casefold()
    if not lowered:
        return False
    has_land = "зем" in lowered or "жер" in lowered or "участ" in lowered
    has_auction = "аукцион" in lowered or "торг" in lowered or "e-qazyna" in lowered
    return has_land and (
        has_auction or any(keyword in lowered for keyword in LAND_AUCTION_KEYWORDS)
    )


def _build_announcement(
    *,
    source_url: str,
    source_kind: str,
    project: str | None,
    title: str,
    body_text: str,
    links: list[tuple[str, str]],
    raw_payload: dict[str, object],
    published_at: datetime | None = None,
    attachments: list[GovKzAttachment] | None = None,
) -> GovKzAnnouncement:
    combined_text = f"{title} {body_text}"
    normalized_links = {
        _absolute_url("https://www.gov.kz", href)
        for href, _label in links
        if href and not href.startswith("#")
    }
    eqazyna_urls = {
        url
        for url in normalized_links
        if "e-qazyna.kz" in urlparse(url).netloc or "gosreestr.kz" in urlparse(url).netloc
    }
    link_attachments = [
        GovKzAttachment(
            title=label or _filename_from_url(url),
            url=url,
            file_type=_file_type_from_url(url),
        )
        for url, label in (
            (_absolute_url("https://www.gov.kz", href), text) for href, text in links
        )
        if _looks_like_file_url(url)
    ]
    return GovKzAnnouncement(
        source_url=source_url,
        source_kind=source_kind,
        project=project,
        title=_compact(title, 320) or "gov.kz",
        body_text=_compact(body_text, 4000) or "",
        published_at=published_at,
        lot_numbers=set(LOT_NUMBER_RE.findall(combined_text)),
        auction_numbers=set(AUCTION_NUMBER_RE.findall(combined_text)),
        cadastre_numbers=set(CADASTRE_RE.findall(combined_text)),
        eqazyna_urls=eqazyna_urls,
        attachments=[*(attachments or []), *link_attachments],
        raw_payload=raw_payload,
    )


def _attachments_from_item(
    item: dict[str, object],
    *,
    base_url: str,
) -> list[GovKzAttachment]:
    attachments: list[GovKzAttachment] = []
    for key in ("other_files", "files", "attachments", "file", "download_files"):
        raw = item.get(key)
        values = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
        for value in values:
            if not isinstance(value, dict):
                continue
            url = _string(
                value.get("url")
                or value.get("file")
                or value.get("path")
                or value.get("download_url")
                or value.get("source_url")
            )
            if not url:
                continue
            full_url = _absolute_url(base_url, url)
            title = (
                _string(value.get("title"))
                or _string(value.get("name"))
                or _string(value.get("file_name"))
                or _filename_from_url(full_url)
            )
            attachments.append(
                GovKzAttachment(
                    title=title,
                    url=full_url,
                    file_type=_string(value.get("file_type")) or _file_type_from_url(full_url),
                )
            )
    return attachments


def _item_datetime(item: dict[str, object], *, kind: str) -> datetime | None:
    keys = (
        ("event_date", "event_date_end", "created_date", "updated_date")
        if kind == "events"
        else ("created_date", "publication_date", "updated_date", "date")
    )
    for key in keys:
        parsed = _parse_datetime(_string(item.get(key)))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _sort_for_kind(kind: str) -> str:
    if kind == "events":
        return "event_date:desc"
    return "created_date:desc"


def _source_kind_from_url(url: str) -> str:
    lowered = url.casefold()
    if "/press/news/" in lowered:
        return "news"
    if "/press/events/" in lowered:
        return "events"
    if "/documents/" in lowered:
        return "documents"
    return "detail"


def _project_from_url(url: str) -> str | None:
    match = re.search(r"/memleket/entities/([^/?#]+)", url)
    return match.group(1) if match else None


def _html_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return _clean_text(re.sub(r"<[^>]+>", " ", match.group(1)))


def _text_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text_value(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_text_value(item) for item in value.values())
    return str(value)


def _json_ready(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    ready: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, (str, int, float, bool)) or item is None:
            ready[str(key)] = item
        elif isinstance(item, list):
            ready[str(key)] = item[:20]
        elif isinstance(item, dict):
            ready[str(key)] = item
        else:
            ready[str(key)] = str(item)
    return ready


def _looks_like_file_url(url: str) -> bool:
    lowered = url.casefold()
    file_suffixes = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png")
    return any(lowered.endswith(suffix) for suffix in file_suffixes) or "/uploads/" in lowered


def _file_type_from_url(url: str) -> str | None:
    path = urlparse(url).path.casefold()
    for suffix in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".jpg", ".jpeg", ".png"):
        if path.endswith(suffix):
            return suffix.lstrip(".")
    return None


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    filename = path.rsplit("/", 1)[-1]
    return filename or "gov.kz file"


def _absolute_url(base_url: str, url: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", url)


def _compact(value: str, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
