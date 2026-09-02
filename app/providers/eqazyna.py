from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.provider_backpressure import ProviderBackpressure
from app.provider_guard import bounded_http_request, guarded_http_call

AuctionPublishDateWindow = tuple[str, str]
CURRENT_SEARCH_STATUSES = ("ApplicationsAccept", "Pending", "Running")


class EqazynaError(RuntimeError):
    pass


@dataclass(slots=True)
class AuctionDocumentData:
    title: str
    source_url: str
    file_type: str | None = None


@dataclass(slots=True)
class AuctionLotData:
    source_lot_id: str
    source_url: str
    title: str
    source_search_status: str | None = None
    object_type: str = "land"
    auction_number: str | None = None
    auction_type: str | None = None
    status: str | None = None
    description: str | None = None
    region: str | None = None
    district: str | None = None
    locality: str | None = None
    location_text: str | None = None
    cadastre_number: str | None = None
    land_object_id: str | None = None
    area_ha: float | None = None
    land_rights: str | None = None
    lease_term_years: float | None = None
    divisible: bool | None = None
    additional_payment_kzt: float | None = None
    annual_rent_kzt: float | None = None
    functional_purpose_level2: str | None = None
    functional_purpose_level3: str | None = None
    functional_purpose_level4: str | None = None
    use_goal: str | None = None
    purpose: str | None = None
    start_price_kzt: float | None = None
    guarantee_kzt: float | None = None
    sale_price_kzt: float | None = None
    auction_starts_at: datetime | None = None
    published_at: date | None = None
    seller_name: str | None = None
    seller_bin: str | None = None
    source_object_url: str | None = None
    documents: list[AuctionDocumentData] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class AuctionUrlCrawlResult:
    urls: list[str]
    pages_scanned: int
    complete: bool
    status_counts: dict[str, int] = field(default_factory=dict)
    url_statuses: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AuctionDetailError:
    source_url: str
    source_lot_id: str | None
    message: str


@dataclass(slots=True)
class AuctionCrawlResult:
    lots: list[AuctionLotData]
    source_lot_ids: set[str]
    url_count: int
    pages_scanned: int
    complete: bool
    detail_errors: list[AuctionDetailError] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)
    url_statuses: dict[str, str] = field(default_factory=dict)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._active_href: str | None = None
        self._active_text: list[str] = []
        self._active_excluded = False
        self._div_exclusion_stack: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attr_map = dict(attrs)
        if tag == "div":
            class_name = str(attr_map.get("class") or "")
            parent_excluded = self._div_exclusion_stack[-1] if self._div_exclusion_stack else False
            self._div_exclusion_stack.append(parent_excluded or "trade-adv" in class_name.split())
            return
        if tag == "a":
            self._active_href = attr_map.get("href")
            self._active_text = []
            self._active_excluded = (
                self._div_exclusion_stack[-1] if self._div_exclusion_stack else False
            )

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "div":
            if self._div_exclusion_stack:
                self._div_exclusion_stack.pop()
            return
        if tag == "a" and self._active_href:
            if not self._active_excluded:
                self.links.append((self._active_href, _clean_text(" ".join(self._active_text))))
            self._active_href = None
            self._active_text = []
            self._active_excluded = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = _clean_text(data)
        if not cleaned:
            return
        self.text_parts.append(cleaned)
        if self._active_href is not None:
            self._active_text.append(cleaned)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _bounded_text(value: str | None, limit: int) -> str | None:
    """Fit a derived dimension to its database contract without truncating source prose."""
    if value is None:
        return None
    cleaned = _clean_text(value)
    return cleaned[:limit] if cleaned else None


def _parse_number(value: str | None) -> float | None:
    if not value:
        return None
    normalized = value.replace("\xa0", "").replace(" ", "").replace("₸", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    return float(match.group(0)) if match else None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    match = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})", value)
    if not match:
        return None
    # E-Qazyna renders a Kazakhstan civil-time wall clock without an offset.
    # Asia/Almaty preserves the UTC+6 historical offset and the nationwide
    # UTC+5 transition in 2024; interpreting this text as UTC delays deadline
    # handling by five or six hours.
    local_value = datetime.strptime(" ".join(match.groups()), "%d.%m.%Y %H:%M")
    return local_value.replace(tzinfo=ZoneInfo("Asia/Almaty")).astimezone(UTC)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    match = re.search(r"(\d{2})-(\d{2})-(\d{4})", value)
    if not match:
        return None
    day, month, year = match.groups()
    return date(int(year), int(month), int(day))


def _after_label(text: str, label: str, stop_labels: list[str]) -> str | None:
    start = text.find(label)
    if start < 0:
        return None
    start += len(label)
    end = len(text)
    for stop in stop_labels:
        position = text.find(stop, start)
        if position >= 0:
            end = min(end, position)
    value = _clean_text(text[start:end].strip(" :;"))
    return value or None


def extract_lot_urls(html: str, base_url: str) -> list[str]:
    parser = _PageParser()
    parser.feed(html)
    seen: set[str] = set()
    urls: list[str] = []
    for href, _ in parser.links:
        match = re.fullmatch(r"/(?:ru|kz)/list/(\d+)", href.split("?", 1)[0])
        if not match:
            continue
        url = urljoin(base_url, href.split("?", 1)[0])
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_source_lot_id(source_url: str) -> str | None:
    match = re.search(r"/list/(\d+)", source_url)
    return match.group(1) if match else None


def classify_auction_object_type(*, title: str | None, description: str | None) -> str:
    """Classify the lot's own title/description, excluding unrelated page chrome/ads."""
    material = f"{title or ''} {description or ''}".casefold()
    vehicle_markers = ("автомобиль", "автокөлік", "транспорт:", "vin-код", "vin код")
    if any(marker in material for marker in vehicle_markers):
        return "vehicle"
    land_markers = (
        "земельный участок",
        "жер учаск",
        "права на землю",
        "кадастровый номер",
        "площадь земельного участка",
    )
    return "land" if any(marker in material for marker in land_markers) else "non_land"


def parse_lot_detail(html: str, source_url: str, base_url: str) -> AuctionLotData:
    parser = _PageParser()
    parser.feed(html)
    text = _clean_text(" ".join(parser.text_parts))
    lowered = text.casefold()
    if "превышен лимит запросов" in lowered:
        raise EqazynaError("E-Qazyna временно ограничил запросы к карточке лота")
    if "ошибка исполнения" in lowered:
        raise EqazynaError("E-Qazyna вернул страницу ошибки вместо карточки лота")
    source_lot_id = extract_source_lot_id(source_url)
    if not source_lot_id:
        raise EqazynaError("Не удалось определить идентификатор лота E-Qazyna")

    description = _after_label(
        text,
        "Объект продажи",
        ["Расположение объекта", "Продавец", "Балансодержатель"],
    )
    location = _after_label(
        text,
        "Расположение объекта",
        ["Продавец", "Балансодержатель", "Электронные документы"],
    )
    seller = _after_label(
        text,
        "Продавец",
        ["Все объявления этого продавца", "Балансодержатель", "Электронные документы"],
    )
    title = (description or "").split(", Описание:", 1)[0].strip(" ;")
    if not title:
        number_position = text.find("№")
        title = text[:number_position].rsplit("Просмотр торга", 1)[-1].strip()
    auction_number_match = re.search(r"№\s*(\d{3,})", text)
    auction_type_match = re.search(
        r"(Аукцион[^№]{3,180}?)(?=\s+\d{2,}|\s+Стартовая цена)",
        text,
    )
    status = _after_label(
        text,
        "Статус торгов:",
        ["Обязательно ознакомтесь", "Порядок", "Объект"],
    )
    land_rights = _after_label(
        text,
        "Права на землю:",
        ["Статус торгов:"],
    )

    cadastre_match = re.search(
        r"Кадастровый номер:\s*([^;]{2,64})",
        description or text,
        flags=re.IGNORECASE,
    )
    area_match = re.search(
        r"Площадь земельного участка,\s*га:\s*([0-9.,]+)",
        description or text,
        flags=re.IGNORECASE,
    )
    purpose_match = re.search(
        r"Целевое назначение земельного участка:\s*([^;]+)",
        description or text,
        flags=re.IGNORECASE,
    )
    functional_purpose_matches = {
        level: re.search(
            rf"Функциональное назначение земельного участка "
            rf"\(уровень {level}\):\s*([^;]+)",
            description or text,
            flags=re.IGNORECASE,
        )
        for level in (2, 3, 4)
    }
    use_goal_match = re.search(
        r"Цель использования:\s*([^;]+)",
        description or text,
        flags=re.IGNORECASE,
    )
    land_object_id_match = re.search(
        r"(?:идентификатор(?:\s+земельного)?\s+(?:участка|объекта)(?:\s+в\s+(?:цс\s+)?егкн)?)[^0-9]{0,40}(\d{12,32})",
        text,
        flags=re.IGNORECASE,
    )
    lease_term_match = re.search(
        r"(?:срок(?:ом)?\s+(?:аренды\s*)?(?:на\s+)?|землепользован\w*[^.;]{0,60}?)[^0-9]{0,20}(\d+(?:[.,]\d+)?)\s*(?:лет|года|год)",
        " ".join((land_rights or "", description or "", text)),
        flags=re.IGNORECASE,
    )
    divisible_match = re.search(
        r"(?:делимость|делимый\s+участок)\s*[:\-]?\s*(неделимый|делимый)",
        description or text,
        flags=re.IGNORECASE,
    )
    additional_payment_match = re.search(
        r"(?:возместить|возмещение)[^.;]{0,100}?(?:потер\w*|затрат\w*)[^0-9]{0,80}([0-9][0-9\s\u00a0]*(?:[.,]\d+)?)\s*(?:₸|тенге)",
        description or text,
        flags=re.IGNORECASE,
    )
    annual_rent_match = re.search(
        r"(?:ежегодн\w*\s+)?арендн\w*\s+плат\w*[^0-9]{0,40}([0-9][0-9\s\u00a0]*(?:[.,]\d+)?)\s*(?:₸|тенге)",
        description or text,
        flags=re.IGNORECASE,
    )
    seller_bin_match = re.search(r"(?:ИИН/БИН|БИН):\s*(\d{12})", seller or "")
    publication_match = re.search(
        r"Извещение о продаже опубликовано:.{0,300}?\b(\d{2}-\d{2}-\d{4})",
        text,
        flags=re.IGNORECASE,
    )

    documents: list[AuctionDocumentData] = []
    source_object_url = None
    for href, link_text in parser.links:
        absolute = urljoin(base_url, href)
        if "source-object-view" in href or (
            "jerler.e-qazyna.kz" in absolute
            and "/reestr/objects/list/" in absolute
            and "/view" in absolute
        ):
            source_object_url = absolute
        if "MnuFileStoreFileDownload" not in href:
            continue
        suffix = link_text.rsplit(".", 1)[-1].lower() if "." in link_text else None
        documents.append(
            AuctionDocumentData(
                title=link_text or "Документ E-Qazyna",
                source_url=absolute,
                file_type=suffix,
            )
        )
    land_object_id = land_object_id_match.group(1) if land_object_id_match else None

    region = None
    district = None
    locality = None
    if location:
        parts = [part.strip() for part in location.split(",") if part.strip()]
        region = parts[0] if parts else None
        district = parts[1] if len(parts) > 1 else None
        locality = parts[-1] if len(parts) > 2 else None

    return AuctionLotData(
        source_lot_id=source_lot_id,
        source_url=source_url,
        title=title or f"Земельный лот {source_lot_id}",
        object_type=classify_auction_object_type(title=title, description=description),
        auction_number=auction_number_match.group(1) if auction_number_match else None,
        auction_type=(
            _bounded_text(auction_type_match.group(1), 160) if auction_type_match else None
        ),
        status=status,
        description=description,
        region=_bounded_text(region, 160),
        district=_bounded_text(district, 160),
        locality=_bounded_text(locality, 160),
        location_text=location,
        cadastre_number=_clean_text(cadastre_match.group(1)) if cadastre_match else None,
        land_object_id=land_object_id,
        area_ha=_parse_number(area_match.group(1)) if area_match else None,
        land_rights=_bounded_text(land_rights, 240),
        lease_term_years=_parse_number(lease_term_match.group(1)) if lease_term_match else None,
        divisible=(divisible_match.group(1).casefold() == "делимый") if divisible_match else None,
        additional_payment_kzt=(
            _parse_number(additional_payment_match.group(1)) if additional_payment_match else None
        ),
        annual_rent_kzt=_parse_number(annual_rent_match.group(1)) if annual_rent_match else None,
        functional_purpose_level2=(
            _bounded_text(functional_purpose_matches[2].group(1), 240)
            if functional_purpose_matches[2]
            else None
        ),
        functional_purpose_level3=(
            _bounded_text(functional_purpose_matches[3].group(1), 320)
            if functional_purpose_matches[3]
            else None
        ),
        functional_purpose_level4=(
            _bounded_text(functional_purpose_matches[4].group(1), 320)
            if functional_purpose_matches[4]
            else None
        ),
        use_goal=_bounded_text(use_goal_match.group(1), 160) if use_goal_match else None,
        purpose=_clean_text(purpose_match.group(1)) if purpose_match else None,
        start_price_kzt=_parse_number(
            _after_label(text, "Стартовая цена", ["Начало торгов", "Гарантийный взнос"])
        ),
        guarantee_kzt=_parse_number(
            _after_label(text, "Гарантийный взнос", ["Цена продажи", "Права на землю"])
        ),
        sale_price_kzt=_parse_number(
            _after_label(text, "Цена продажи", ["Права на землю", "Статус торгов"])
        ),
        auction_starts_at=_parse_datetime(
            _after_label(text, "Начало торгов", ["Гарантийный взнос", "Цена продажи"])
        ),
        published_at=_parse_date(publication_match.group(1) if publication_match else None),
        seller_name=seller,
        seller_bin=seller_bin_match.group(1) if seller_bin_match else None,
        source_object_url=source_object_url,
        documents=documents,
    )


def configured_search_statuses() -> list[str]:
    configured = [
        item.strip() for item in settings.eqazyna_sync_statuses.split(",") if item.strip()
    ]
    allowed = set(CURRENT_SEARCH_STATUSES)
    # Current-catalogue completeness is the deactivation safety gate. Historical
    # result statuses have their own bounded/year-window crawl; allowing them here
    # can spend the current run's detail cap on old terminal rows and make every
    # freshness pass incomplete.
    values = list(dict.fromkeys(item for item in configured if item in allowed))
    return values or list(CURRENT_SEARCH_STATUSES)


class EqazynaProvider:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: int | None = None,
        verify_tls: bool | None = None,
        transport: httpx.BaseTransport | None = None,
        backpressure: ProviderBackpressure | None = None,
    ) -> None:
        self.base_url = (base_url or settings.eqazyna_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.eqazyna_timeout_seconds
        self.verify_tls = settings.eqazyna_verify_tls if verify_tls is None else verify_tls
        self.transport = transport
        self.backpressure = backpressure

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout_seconds,
            verify=self.verify_tls,
            follow_redirects=True,
            transport=self.transport,
            headers={
                "User-Agent": "LandScoutKazakhstan/1.0 (+public auction monitor)",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        )

    def lot_url_page(
        self,
        *,
        search_status: str,
        page: int,
        publish_date_window: AuctionPublishDateWindow | None = None,
    ) -> list[str]:
        """Fetch and parse exactly one resumable list-page request."""
        if not search_status or not 1 <= page <= 1_000:
            raise ValueError("invalid E-Qazyna page unit")
        params: list[tuple[str, str]] = [
            ("objectType", "Land"),
            ("searchStatus", search_status),
        ]
        if publish_date_window is not None:
            from_inclusive, to_inclusive = publish_date_window
            params.extend(
                [
                    ("moreFilters", "on"),
                    ("publishDateFromInclusive", from_inclusive),
                    ("publishDateToInclusive", to_inclusive),
                ]
            )
        params.append(("p", str(page)))
        with self._client() as client:
            try:
                response = guarded_http_call(
                    "eqazyna",
                    lambda: bounded_http_request(
                        client, "GET", f"{self.base_url}/ru/list", params=params
                    ),
                    backpressure=self.backpressure,
                )
            except httpx.HTTPError as exc:
                raise EqazynaError(f"E-Qazyna list-page request failed: {exc}") from exc
        return extract_lot_urls(response.text, self.base_url)

    def current_lot_url_crawl(
        self,
        *,
        max_pages: int | None = None,
        statuses: list[str] | None = None,
        publish_date_windows: list[AuctionPublishDateWindow] | None = None,
    ) -> AuctionUrlCrawlResult:
        page_limit = max_pages or settings.eqazyna_sync_max_pages
        collected: list[str] = []
        seen: set[str] = set()
        pages_scanned = 0
        complete = True
        status_counts: dict[str, int] = {}
        url_statuses: dict[str, str] = {}
        search_statuses = statuses or configured_search_statuses()
        date_windows: list[AuctionPublishDateWindow | None] = publish_date_windows or [None]
        search_queries = [
            (search_status, publish_date_window)
            for search_status in search_statuses
            for publish_date_window in date_windows
        ]
        with self._client() as client:
            for search_status, publish_date_window in search_queries:
                status_count = status_counts.get(search_status, 0)
                status_complete = False
                for page in range(1, page_limit + 1):
                    pages_scanned += 1
                    params = [
                        ("objectType", "Land"),
                        ("searchStatus", search_status),
                    ]
                    if publish_date_window is not None:
                        from_inclusive, to_inclusive = publish_date_window
                        params.extend(
                            [
                                ("moreFilters", "on"),
                                ("publishDateFromInclusive", from_inclusive),
                                ("publishDateToInclusive", to_inclusive),
                            ]
                        )
                    params.append(("p", str(page)))
                    try:
                        response = guarded_http_call(
                            "eqazyna",
                            lambda request_params=params: bounded_http_request(
                                client,
                                "GET",
                                f"{self.base_url}/ru/list",
                                params=request_params,
                            ),
                            backpressure=self.backpressure,
                        )
                    except httpx.HTTPError as exc:
                        raise EqazynaError(
                            f"E-Qazyna не ответил при загрузке списка {search_status}: {exc}"
                        ) from exc
                    page_urls = extract_lot_urls(response.text, self.base_url)
                    new_urls = [url for url in page_urls if url not in seen]
                    if not new_urls:
                        status_complete = True
                        break
                    for url in new_urls:
                        seen.add(url)
                        collected.append(url)
                        url_statuses[url] = search_status
                        status_count += 1
                status_counts[search_status] = status_count
                complete = complete and status_complete
        return AuctionUrlCrawlResult(
            urls=collected,
            pages_scanned=pages_scanned,
            complete=complete,
            status_counts=status_counts,
            url_statuses=url_statuses,
        )

    def current_lot_urls(
        self,
        *,
        max_pages: int | None = None,
        statuses: list[str] | None = None,
        publish_date_windows: list[AuctionPublishDateWindow] | None = None,
    ) -> list[str]:
        return self.current_lot_url_crawl(
            max_pages=max_pages,
            statuses=statuses,
            publish_date_windows=publish_date_windows,
        ).urls

    def lot_detail(self, source_url: str) -> AuctionLotData:
        with self._client() as client:
            try:
                response = guarded_http_call(
                    "eqazyna",
                    lambda: bounded_http_request(client, "GET", source_url),
                    backpressure=self.backpressure,
                )
            except httpx.HTTPError as exc:
                raise EqazynaError(f"E-Qazyna не ответил при загрузке лота: {exc}") from exc
        return parse_lot_detail(response.text, source_url, self.base_url)

    def current_lots(
        self,
        *,
        max_pages: int | None = None,
        max_lots: int | None = None,
        statuses: list[str] | None = None,
        publish_date_windows: list[AuctionPublishDateWindow] | None = None,
    ) -> list[AuctionLotData]:
        return self.current_lots_with_report(
            max_pages=max_pages,
            max_lots=max_lots,
            statuses=statuses,
            publish_date_windows=publish_date_windows,
        ).lots

    def current_lots_with_report(
        self,
        *,
        max_pages: int | None = None,
        max_lots: int | None = None,
        statuses: list[str] | None = None,
        publish_date_windows: list[AuctionPublishDateWindow] | None = None,
    ) -> AuctionCrawlResult:
        limit = max_lots or settings.eqazyna_sync_max_lots
        url_crawl = self.current_lot_url_crawl(
            max_pages=max_pages,
            statuses=statuses,
            publish_date_windows=publish_date_windows,
        )
        urls = url_crawl.urls
        search_statuses = statuses or configured_search_statuses()
        urls_by_status: dict[str, list[str]] = {status: [] for status in search_statuses}
        for url in urls:
            status = url_crawl.url_statuses.get(url) or "unknown"
            urls_by_status.setdefault(status, []).append(url)

        selected_by_status: dict[str, int] = {}
        limited_urls: list[str] = []
        offsets = {status: 0 for status in urls_by_status}
        while len(limited_urls) < limit:
            appended = False
            for status in search_statuses:
                bucket = urls_by_status.get(status) or []
                offset = offsets.get(status, 0)
                if offset >= len(bucket):
                    continue
                url = bucket[offset]
                offsets[status] = offset + 1
                selected_by_status[status] = selected_by_status.get(status, 0) + 1
                limited_urls.append(url)
                appended = True
                if len(limited_urls) >= limit:
                    break
            if not appended:
                break
        lots: list[AuctionLotData] = []
        source_lot_ids: set[str] = set()
        detail_errors: list[AuctionDetailError] = []
        for source_url in limited_urls:
            source_lot_id = extract_source_lot_id(source_url)
            if source_lot_id:
                source_lot_ids.add(source_lot_id)
            try:
                lot = self.lot_detail(source_url)
                lot.source_search_status = url_crawl.url_statuses.get(source_url)
                lots.append(lot)
            except EqazynaError as exc:
                detail_errors.append(
                    AuctionDetailError(
                        source_url=source_url,
                        source_lot_id=source_lot_id,
                        message=str(exc),
                    )
                )
        return AuctionCrawlResult(
            lots=lots,
            source_lot_ids=source_lot_ids,
            url_count=len(urls),
            pages_scanned=url_crawl.pages_scanned,
            complete=(url_crawl.complete and len(limited_urls) == len(urls) and not detail_errors),
            detail_errors=detail_errors,
            status_counts=url_crawl.status_counts,
            url_statuses=url_crawl.url_statuses,
        )
