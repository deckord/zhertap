from __future__ import annotations

# ruff: noqa: E501
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from statistics import median
from threading import Lock
from urllib.parse import quote_plus, urlparse

import httpx
from shapely.geometry import mapping
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auction_documents import (
    auction_document_key,
    deduplicate_lot_documents,
    unique_auction_documents,
)
from app.auction_service import (
    AuctionFilters,
    AuctionLotMetrics,
    AuctionSyncResult,
    auction_lot_geo_metrics,
    auction_lot_metrics,
    auction_lots_metrics,
    sync_current_auctions,
)
from app.config import settings
from app.models import (
    Account,
    AuctionCrawlRun,
    AuctionDocument,
    AuctionEvidence,
    AuctionLot,
    AuctionLotChange,
    AuctionLotGeoCheck,
    AuctionLotV2Analysis,
    AuctionMarketComparable,
    AuctionSource,
    AuctionUserLotPipeline,
    AuctionWatchlist,
    AuctionWatchlistNotification,
)
from app.providers.egkn import (
    CadastreLookupResult,
    EgknContextFeature,
    EgknProvider,
    EgknProviderError,
)
from app.providers.gov_kz import GovKzAnnouncement, GovKzError, GovKzProvider
from app.providers.osm import OsmProvider, OsmProviderError, Surroundings

PIPELINE_STAGES: tuple[tuple[str, str], ...] = (
    ("watching", "Слежу"),
    ("checking", "Проверяю"),
    ("needs_manual_check", "Нужна ручная сверка"),
    ("ready_for_official_site", "Готов к E-Qazyna/eGov"),
    ("decided_to_participate", "Буду участвовать"),
    ("application_preparing", "Готовлю заявку"),
    ("application_submitted", "Заявка подана"),
    ("guarantee_paid", "Гарантийный взнос оплачен"),
    ("admitted_to_auction", "Допущен к торгам"),
    ("auction_completed", "Торги завершены"),
    ("won", "Победа"),
    ("lost", "Не выиграл"),
    ("contract_signed", "Договор подписан"),
    ("rights_registered", "Право зарегистрировано"),
    ("development", "Освоение участка"),
    ("listed_for_sale", "Выставлен на продажу"),
    ("sold", "Сделка закрыта"),
    ("skipped", "Пропустить"),
    ("archived", "Архив"),
)

INVESTMENT_STRATEGIES: tuple[tuple[str, str], ...] = (
    ("undecided", "Стратегия не выбрана"),
    ("resale", "Перепродажа участка"),
    ("subdivision", "Разделение и продажа частями"),
    ("development", "Строительство и продажа"),
    ("rental", "Арендный доход"),
    ("agriculture", "Сельхозиспользование"),
    ("own_use", "Для собственного проекта"),
)

FIELD_INSPECTION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("not_planned", "Выезд не запланирован"),
    ("planned", "Выезд запланирован"),
    ("completed", "Участок осмотрен"),
    ("repeat_required", "Нужен повторный выезд"),
    ("rejected", "Отказ после осмотра"),
)

AUCTION_ACTIVITY_TYPES: tuple[tuple[str, str], ...] = (
    ("note", "Заметка"),
    ("task", "Задача"),
    ("decision", "Решение"),
    ("expert_request", "Вопрос специалисту"),
)

RISK_LABELS = {
    "low": "Низкий",
    "medium": "Средний",
    "high": "Высокий",
    "unknown": "Неизвестно",
}

CONFIDENCE_LABELS = {
    "high": "Высокая",
    "medium": "Средняя",
    "low": "Низкая",
}

ACTION_LABELS = {
    "prepare_official_review": "Готовить участие на официальном сайте",
    "manual_check": "Сначала ручная сверка",
    "watch_and_check": "Следить и проверить",
    "watch": "Наблюдать",
    "skip": "Пропустить",
}

DEADLINE_STATUS_LABELS = {
    "urgent": "Срочно: до 24 часов",
    "soon": "1-3 дня до торгов",
    "normal": "Есть запас",
    "unknown": "Дата неизвестна",
    "expired": "Уже начались",
}

GEO_STATUS_LABELS = {
    "coordinates_found": "Координаты есть",
    "coordinates_missing": "Нет или не подтверждены координаты",
    "osm_checked": "OSM проверен без предупреждений",
    "osm_warning": "OSM с предупреждением",
    "osm_pending": "OSM не проверен",
}

COORDINATE_STATUS_LABELS = {
    "found": "Координаты подтверждены",
    "missing": "Координаты не найдены",
    "unconfirmed": "Координаты не подтверждены",
    "unknown": "Координаты не проверены",
}

CADASTRE_STATUS_LABELS = {
    "verified": "ЕГКН подтвердил",
    "found": "Кадастр найден в лоте",
    "missing": "Кадастр не указан",
    "not_found": "ЕГКН не подтвердил",
    "unavailable": "ЕГКН не ответил",
    "unknown": "Не проверено",
}

BOUNDARY_STATUS_LABELS = {
    "verified": "Граница подтверждена",
    "warning": "Площадь расходится",
    "manual_required": "Нужна сверка границы",
    "not_found": "Граница не найдена",
    "unknown": "Граница не проверена",
}

OSM_STATUS_LABELS = {
    "checked": "OSM проверен",
    "not_checked": "OSM еще не проверен",
    "missing_coordinates": "Нет координат",
    "stale": "Нужен пересчет",
    "unavailable": "OSM не ответил",
}

ENGINEERING_STATUS_LABELS = {
    "checked": "Без явных предупреждений",
    "warning": "Есть вопросы",
    "manual_required": "Нужна ручная проверка",
    "unknown": "Не проверено",
}

URBAN_PLAN_STATUS_LABELS = {
    "checked": "Проверено",
    "warning": "Есть вопросы",
    "manual_required": "Нужна ручная сверка",
    "unknown": "Не проверено",
}

LOT_SCOPE_LABELS = {
    "active": "Активные",
    "future": "Будущие",
    "archive": "Архив",
    "all": "Все",
}

EQAZYNA_SEARCH_STATUS_LABELS = {
    "ApplicationsAccept": "Прием заявок",
    "Pending": "Ожидает начала",
    "Running": "Идут торги",
    "SuccessProtocolSigned": "Состоялся, протокол подписан",
    "FailureProtocolSigned": "Не состоялся, протокол подписан",
    "NullifyResultProtocolSigned": "Результат аннулирован",
    "CancelBeforeStart": "Отменен до начала",
}

EQAZYNA_SEARCH_STATUS_NOTES = {
    "ApplicationsAccept": "Можно готовить проверку и заявку на официальном портале.",
    "Pending": "Лот уже опубликован, но до торгов еще есть время на проверку.",
    "Running": "Торги уже идут; проверьте официальный статус перед любым действием.",
    "SuccessProtocolSigned": "Торги завершены успешно, лот нужен для истории и сравнения цены.",
    "FailureProtocolSigned": "Торги не состоялись; важно для истории участка и повторных размещений.",
    "NullifyResultProtocolSigned": "Результат аннулирован; перед выводами нужна ручная сверка.",
    "CancelBeforeStart": "Торги отменены до начала; лот хранится в архиве для истории.",
}

EQAZYNA_STATUS_FILTER_LABELS = {
    **EQAZYNA_SEARCH_STATUS_LABELS,
    "unknown": "Статус не определен",
}

AUCTION_V2_SORT_LABELS = {
    "best": "Сначала лучшие",
    "deadline_asc": "Сначала ближайшие торги",
    "price_per_sotka_asc": "Сначала дешевле за сотку",
    "start_price_asc": "Сначала дешевле старт",
    "area_desc": "Сначала больше площадь",
    "new_first": "Сначала новые",
}

SOURCE_STATUS_LABELS = {
    "ok": "Проверено",
    "warning": "Есть вопросы",
    "missing": "Не найдено",
    "manual_required": "Проверить вручную",
    "planned": "Будет подключено",
    "query_ready": "Открыть поиск",
    "external_action": "Официальный портал",
}

SOURCE_QUALITY_LABELS = {
    "live": "Работает",
    "partial_live": "Частично работает",
    "reference": "Справка",
    "planned": "Запланировано",
    "manual_required": "Нужна ручная сверка",
}

CRAWL_RUN_STATUS_LABELS = {
    "success": "Успешно",
    "warning": "Есть вопросы",
    "error": "Ошибка",
    "running": "Идет сейчас",
    "query_ready": "Поиск вручную",
    "planned": "Запланировано",
    "manual_required": "Ручная сверка",
    "missing": "Нет данных",
}

EMPTY_REASON_LABELS = {
    "no_eqazyna_run": "E-Qazyna еще не запускался",
    "eqazyna_running": "E-Qazyna сейчас обновляется",
    "eqazyna_error": "E-Qazyna вернул ошибку",
    "eqazyna_no_urls": "E-Qazyna ответил, но не дал ссылок на земельные лоты",
    "eqazyna_detail_errors": "E-Qazyna дал ссылки, но карточки не разобрались",
    "no_base_lots": "В базе пока нет лотов",
    "filters_cut_all": "Лоты есть, но текущий фильтр их отрезал",
    "unknown": "Нужна проверка источников",
}

WORKFLOW_STATUS_LABELS = {
    "done": "Закрыто в Zhertap",
    "manual": "Проверить вручную",
    "warning": "Есть вопросы",
    "missing": "Не найдено",
    "external": "Внешний портал",
}

EVIDENCE_TYPE_LABELS = {
    "official_lot": "Официальный лот",
    "official_document": "Документ E-Qazyna",
    "official_document_summary": "Документы E-Qazyna",
    "akimat_announcement": "Объявление акимата",
    "cadastre_number": "Кадастровый номер",
    "cadastre_boundary": "Граница ЕГКН",
    "egkn_context_layer": "Слой ЕГКН",
    "source_check_status": "Статус источника",
    "source_query": "Поиск источника",
    "market_query": "Поиск рыночных аналогов",
    "official_boundary": "Граница официальных действий",
}

MAX_BUILTIN_DOCUMENT_EVIDENCE = 12

EVIDENCE_STATUS_LABELS = {
    "found": "Найдено",
    "missing": "Не найдено",
    "query_ready": "Открыть вручную",
    "manual_required": "Нужна ручная сверка",
    "planned": "Запланировано",
    "external_action": "Внешний портал",
    "error": "Ошибка",
    "warning": "Есть вопросы",
}

EGKN_CONTEXT_LAYERS: tuple[dict[str, str], ...] = (
    {
        "code": "free_lands",
        "layer": "egkn:freelands_view",
        "label": "Свободные земли",
        "kind": "opportunity",
    },
    {
        "code": "pdp",
        "layer": "egkn:pdp_u_view",
        "label": "ПДП",
        "kind": "planning",
    },
    {
        "code": "functional_zones",
        "layer": "egkn:funczones_view",
        "label": "Функциональные зоны",
        "kind": "restriction",
    },
    {
        "code": "engineering",
        "layer": "egkn:eng_view",
        "label": "Инженерные зоны",
        "kind": "engineering",
    },
)

ARCHIVED_EQAZYNA_SEARCH_STATUSES = {
    "SuccessProtocolSigned",
    "FailureProtocolSigned",
    "NullifyResultProtocolSigned",
    "CancelBeforeStart",
}

CHANGE_NOTIFICATION_FIELDS: dict[str, tuple[str, str, str, int]] = {
    "auction_starts_at": ("auction_date_changed", "Изменилась дата торгов", "Дата торгов", 88),
    "start_price_kzt": ("price_changed", "Изменилась стартовая цена", "Стартовая цена", 86),
    "sale_price_kzt": ("result_changed", "Появился результат торгов", "Цена продажи", 84),
    "source_search_status": (
        "eqazyna_status_changed",
        "Изменился статус E-Qazyna",
        "Статус E-Qazyna",
        86,
    ),
    "status": ("status_changed", "Изменился статус лота", "Статус", 82),
    "documents": ("document_added", "Появился новый документ", "Документ", 80),
    "area_ha": ("lot_terms_changed", "Изменились параметры участка", "Площадь", 74),
    "land_rights": ("lot_terms_changed", "Изменилось право на участок", "Право", 74),
    "purpose": ("lot_terms_changed", "Изменилось назначение участка", "Назначение", 74),
}

DEFAULT_AUCTION_SOURCES: tuple[dict[str, object], ...] = (
    {
        "code": "eqazyna_current_lots",
        "source_type": "official_auction",
        "name": "E-Qazyna: текущие торги",
        "base_url": "https://sauda.e-qazyna.kz/ru/list?searchStatus=ApplicationsAccept",
        "region": "all",
        "parser_kind": "eqazyna_provider",
        "priority": 100,
        "crawl_interval_minutes": 30,
        "quality_status": "live",
        "legal_status": "official_public",
        "notes": "Основной официальный источник карточек лотов, сроков, документов и статусов торгов.",
    },
    {
        "code": "eqazyna_history_backfill",
        "source_type": "official_auction_archive",
        "name": "E-Qazyna: архив торгов",
        "base_url": "https://sauda.e-qazyna.kz/ru/list?objectType=Land",
        "region": "all",
        "parser_kind": "eqazyna_history_backfill",
        "priority": 98,
        "crawl_interval_minutes": 1440,
        "quality_status": "partial_live",
        "legal_status": "official_public",
        "notes": "Загружает старые опубликованные лоты и результаты торгов E-Qazyna из открытого списка. Это нужно для истории участка, повторных размещений и сравнения цен; заявки не подаются.",
    },
    {
        "code": "gov_kz_akimat_announcements",
        "source_type": "gov_announcement",
        "name": "gov.kz: объявления акиматов",
        "base_url": "https://www.gov.kz/memleket/entities",
        "region": "all",
        "parser_kind": "gov_kz_content_manager",
        "priority": 92,
        "crawl_interval_minutes": 60,
        "quality_status": "partial_live",
        "legal_status": "official_public",
        "notes": "Живой сбор доступных документов/событий gov.kz по акиматам; новости gov.kz подключаются через seed detail URL или когда публичный API выдает browser ticket.",
    },
    {
        "code": "egov_land_auction_proposal",
        "source_type": "official_service",
        "name": "eGov: предложение вынести участок на торги",
        "base_url": "https://www.gov.kz/services/5169",
        "region": "all",
        "parser_kind": "external_link",
        "priority": 85,
        "crawl_interval_minutes": 1440,
        "quality_status": "reference",
        "legal_status": "official_public",
        "notes": "Справочный официальный путь, но Zhertap не подает заявки и не подписывает ЭЦП.",
    },
    {
        "code": "egkn_public_map",
        "source_type": "cadastre",
        "name": "ЕГКН: публичная кадастровая карта",
        "base_url": "https://map.gov4c.kz/egkn/",
        "region": "all",
        "parser_kind": "egkn_cadastre_lookup",
        "priority": 82,
        "crawl_interval_minutes": 720,
        "quality_status": "partial_live",
        "legal_status": "official_public",
        "notes": "Partial live: поиск кадастрового номера в публичном WFS u_view через каталог районов; границы/слои ограничений остаются частично ручными.",
    },
    {
        "code": "egkn_wfs_layers",
        "source_type": "cadastre_layers",
        "name": "ЕГКН: WFS/слои",
        "base_url": settings.egkn_wfs_url,
        "region": "all",
        "parser_kind": "planned_wfs",
        "priority": 78,
        "crawl_interval_minutes": 720,
        "quality_status": "planned",
        "legal_status": "official_public",
        "notes": "Будущий автоматический слой для геометрии и проверок вокруг участка.",
    },
    {
        "code": "smart_geohub_genplans",
        "source_type": "urban_plan",
        "name": "Smart Geohub / ГГК: генпланы и ПДП",
        "base_url": "https://gov.ggk.kz/",
        "region": "all",
        "parser_kind": "existing_genplan_sources",
        "priority": 75,
        "crawl_interval_minutes": 1440,
        "quality_status": "partial_live",
        "legal_status": "official_public",
        "notes": "Использует уже подключенный контур генпланов и ручных слоев Zhertap.",
    },
    {
        "code": "geo_shymkent",
        "source_type": "regional_geoportal",
        "name": "Геопортал Шымкента",
        "base_url": "https://geo-shym.kz/map/?access_token=&lang=ru",
        "region": "г. Шымкент",
        "parser_kind": "planned_arcgis",
        "priority": 68,
        "crawl_interval_minutes": 1440,
        "quality_status": "planned",
        "legal_status": "official_public",
        "notes": "Региональная сверка градостроительных слоев по Шымкенту.",
    },
    {
        "code": "data_egov_open_data",
        "source_type": "open_data",
        "name": "data.egov.kz: открытые наборы по торгам",
        "base_url": "https://data.egov.kz",
        "region": "all",
        "parser_kind": "planned_dataset",
        "priority": 60,
        "crawl_interval_minutes": 1440,
        "quality_status": "planned",
        "legal_status": "official_public",
        "notes": "Дополнительная сверка списков торгов, если регион публикует наборы данных.",
    },
    {
        "code": "krisha_land_market",
        "source_type": "market",
        "name": "Krisha: рыночные аналоги",
        "base_url": "https://krisha.kz/prodazha/uchastkov/kazaxstan/",
        "region": "all",
        "parser_kind": "planned_market",
        "priority": 50,
        "crawl_interval_minutes": 360,
        "quality_status": "planned",
        "legal_status": "public_market",
        "notes": "Не источник аукционов. Используется только для сравнения стартовой цены с рыночными объявлениями.",
    },
    {
        "code": "olx_land_market",
        "source_type": "market",
        "name": "OLX: рыночные аналоги",
        "base_url": "https://www.olx.kz/nedvizhimost/zemlya/prodazha/",
        "region": "all",
        "parser_kind": "planned_market",
        "priority": 46,
        "crawl_interval_minutes": 360,
        "quality_status": "planned",
        "legal_status": "public_market",
        "notes": "Не источник аукционов. Используется только как дополнительный рыночный ориентир по цене.",
    },
    {
        "code": "osm_overpass",
        "source_type": "infrastructure",
        "name": "OpenStreetMap / Overpass",
        "base_url": settings.overpass_url,
        "region": "all",
        "parser_kind": "existing_osm",
        "priority": 44,
        "crawl_interval_minutes": 1440,
        "quality_status": "partial_live",
        "legal_status": "open_data",
        "notes": "Инфраструктура вокруг координат: дороги, школы, медицина, АЗС, ЛЭП и прочее.",
    },
)


@dataclass(slots=True)
class AuctionV2Filters:
    base: AuctionFilters
    search_query: str | None = None
    lot_scope: str = "active"
    sort_by: str = "best"
    eqazyna_status: str | None = None
    min_score: int | None = None
    risk_level: str | None = None
    confidence_level: str | None = None
    recommended_action: str | None = None
    stage: str | None = None
    deadline_status: str | None = None
    geo_status: str | None = None


@dataclass(slots=True)
class AuctionV2LotPayload:
    lot: AuctionLot
    analysis: AuctionLotV2Analysis
    metrics: AuctionLotMetrics
    geo_check: AuctionLotGeoCheck
    pipeline: AuctionUserLotPipeline | None
    map_embed_url: str | None
    osm_map_url: str | None
    readiness: list[dict[str, object]]
    risk_flags: list[dict[str, object]]
    source_statuses: list[dict[str, object]]
    official_readiness: list[dict[str, object]]
    buyer_workflow: list[dict[str, object]]
    review_steps: list[dict[str, object]]
    manual_process: list[dict[str, object]]
    manual_process_counts: dict[str, int]
    next_actions: list[dict[str, object]]
    data_quality: dict[str, object]
    cost_estimate: dict[str, object]
    investment_case: dict[str, object]
    field_inspection: dict[str, object]
    deal_room: dict[str, object]
    decision_summary: dict[str, object]
    risk_label: str
    confidence_label: str
    action_label: str
    stage_label: str | None
    deadline_label: str
    deadline_status: str
    lot_scope: str
    lot_scope_label: str
    eqazyna_status_label: str
    eqazyna_status_note: str
    coordinate_label: str
    cadastre_label: str
    boundary_label: str
    urban_plan_label: str
    osm_label: str
    engineering_label: str


@dataclass(slots=True)
class AuctionV2SyncResult:
    lots_checked: int = 0
    analyses_updated: int = 0
    infrastructure_checked: int = 0
    infrastructure_errors: int = 0
    documents_checked: int = 0
    documents_downloaded: int = 0
    document_errors: int = 0
    sources_checked: int = 0
    evidence_created: int = 0
    crawl_runs_created: int = 0
    watchlists_checked: int = 0
    watchlist_matches_seen: int = 0
    web_notifications_created: int = 0
    telegram_notifications_sent: int = 0
    notification_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "lots_checked": self.lots_checked,
            "analyses_updated": self.analyses_updated,
            "infrastructure_checked": self.infrastructure_checked,
            "infrastructure_errors": self.infrastructure_errors,
            "documents_checked": self.documents_checked,
            "documents_downloaded": self.documents_downloaded,
            "document_errors": self.document_errors,
            "sources_checked": self.sources_checked,
            "evidence_created": self.evidence_created,
            "crawl_runs_created": self.crawl_runs_created,
            "watchlists_checked": self.watchlists_checked,
            "watchlist_matches_seen": self.watchlist_matches_seen,
            "web_notifications_created": self.web_notifications_created,
            "telegram_notifications_sent": self.telegram_notifications_sent,
            "notification_errors": self.notification_errors,
        }


@dataclass(slots=True)
class AuctionV2FullSyncResult:
    lots_fetched: int = 0
    lots_created: int = 0
    lots_updated: int = 0
    lots_deactivated: int = 0
    crawl_errors: int = 0
    v2: AuctionV2SyncResult = field(default_factory=AuctionV2SyncResult)

    def as_dict(self) -> dict[str, int]:
        return {
            "lots_fetched": self.lots_fetched,
            "lots_created": self.lots_created,
            "lots_updated": self.lots_updated,
            "lots_deactivated": self.lots_deactivated,
            "crawl_errors": self.crawl_errors,
            **self.v2.as_dict(),
        }


@dataclass(slots=True)
class AuctionV2DocumentSyncResult:
    checked: int = 0
    downloaded: int = 0
    errors: int = 0


@dataclass(slots=True)
class AuctionV2WatchlistPayload:
    watchlist: AuctionWatchlist
    match_count: int
    top_score: int | None
    web_notification_count: int = 0
    filter_description: str = ""


@dataclass(slots=True)
class AuctionV2WebNotificationPayload:
    notification: AuctionWatchlistNotification
    watchlist: AuctionWatchlist
    item: AuctionV2LotPayload


@dataclass(slots=True)
class AuctionV2NotificationEvent:
    event_type: str
    event_key: str
    title: str
    detail: str
    priority: int = 50


@dataclass(slots=True)
class AuctionV2MarketStats:
    comparable_count: int = 0
    priced_count: int = 0
    average_price_per_sotka: float | None = None
    median_price_per_sotka: float | None = None
    min_price_per_sotka: float | None = None
    max_price_per_sotka: float | None = None
    source_names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AuctionV2NotificationResult:
    watchlists_checked: int = 0
    matches_seen: int = 0
    web_notifications_created: int = 0
    telegram_notifications_sent: int = 0
    errors: int = 0


def seed_auction_v2_sources(session: Session) -> list[AuctionSource]:
    existing = {
        source.code: source
        for source in session.scalars(select(AuctionSource)).all()
    }
    for spec in DEFAULT_AUCTION_SOURCES:
        code = str(spec["code"])
        source = existing.get(code)
        if source is None:
            source = AuctionSource(code=code)
            session.add(source)
            existing[code] = source
        for key, value in spec.items():
            setattr(source, key, value)
    session.flush()
    return sorted(existing.values(), key=lambda item: (-item.priority, item.name))


def configured_eqazyna_history_statuses() -> list[str]:
    values = [
        item.strip()
        for item in settings.eqazyna_history_sync_statuses.split(",")
        if item.strip()
    ]
    return values or ["SuccessProtocolSigned", "FailureProtocolSigned"]


def eqazyna_history_publish_date_windows(*, today: date | None = None) -> list[tuple[str, str]]:
    end_date = today or datetime.now(UTC).date()
    start_date = date(settings.eqazyna_history_sync_start_year, 1, 1)
    if start_date > end_date:
        start_date = end_date
    window_days = max(1, settings.eqazyna_history_sync_window_days)
    windows: list[tuple[str, str]] = []
    cursor = start_date
    while cursor <= end_date:
        window_end = min(cursor + timedelta(days=window_days - 1), end_date)
        windows.append(
            (
                cursor.strftime("%d.%m.%Y"),
                window_end.strftime("%d.%m.%Y"),
            )
        )
        cursor = window_end + timedelta(days=1)
    return windows


def _json_payload(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _latest_crawl_run_for_source(
    session: Session,
    source_code: str,
) -> tuple[AuctionCrawlRun, AuctionSource] | None:
    return session.execute(
        select(AuctionCrawlRun, AuctionSource)
        .join(AuctionSource, AuctionSource.id == AuctionCrawlRun.source_id)
        .where(AuctionSource.code == source_code)
        .order_by(AuctionCrawlRun.started_at.desc(), AuctionCrawlRun.id.desc())
        .limit(1)
    ).first()


def _auction_v2_empty_diagnostics(
    session: Session,
    *,
    lots_total: int,
    active_lots: int,
) -> dict[str, object]:
    latest = _latest_crawl_run_for_source(session, "eqazyna_current_lots")
    if latest is None:
        return {
            "reason": "no_eqazyna_run",
            "reason_label": EMPTY_REASON_LABELS["no_eqazyna_run"],
            "severity": "warning",
            "summary": "Сначала запустите обновление: система еще не знает, что вернул официальный источник.",
            "steps": [
                "Нажмите “Обновить данные”.",
                "После запуска проверьте счетчики E-Qazyna: ссылки, карточки, ошибки.",
            ],
            "source_url": "https://sauda.e-qazyna.kz/ru/list?searchStatus=ApplicationsAccept",
        }
    latest_run, source = latest
    payload = _json_payload(latest_run.raw_payload_json)
    fetched = int(payload.get("fetched") or latest_run.items_created + latest_run.items_updated or 0)
    url_count = int(payload.get("url_count") or latest_run.items_seen or 0)
    detail_errors = int(payload.get("detail_errors") or 0)
    pages_scanned = int(payload.get("pages_scanned") or 0)
    crawl_complete = bool(payload.get("crawl_complete"))
    status_counts = payload.get("status_counts")
    if not isinstance(status_counts, dict):
        status_counts = {}
    status_line = ", ".join(
        f"{status}: {count}"
        for status, count in status_counts.items()
        if isinstance(count, int) or str(count).isdigit()
    )

    reason = "unknown"
    severity = "warning"
    steps: list[str] = []
    if latest_run.status == "running":
        reason = "eqazyna_running"
        severity = "info"
        steps = ["Дождитесь завершения текущей синхронизации.", "Обновите страницу через минуту."]
    elif latest_run.status == "error":
        reason = "eqazyna_error"
        severity = "error"
        steps = [
            "Откройте служебные источники и посмотрите текст ошибки.",
            "Если ошибка повторяется, проверить доступность E-Qazyna и параметры запроса.",
        ]
    elif url_count == 0:
        reason = "eqazyna_no_urls"
        steps = [
            "Проверить официальный список E-Qazyna вручную.",
            "Если на E-Qazyna есть лоты, нужно чинить поиск ссылок/параметры статусов.",
            "Если на E-Qazyna пусто, включить архив или дождаться новых торгов.",
        ]
    elif fetched == 0 and detail_errors > 0:
        reason = "eqazyna_detail_errors"
        severity = "error"
        steps = [
            "Открыть диагностику источников и посмотреть ошибки карточек.",
            "Проверить, не изменился ли HTML карточки E-Qazyna.",
        ]
    elif lots_total == 0:
        reason = "no_base_lots"
        steps = [
            "E-Qazyna уже нашел ссылки, но лоты не попали в базу.",
            "Проверить ошибки сохранения и миграции БД.",
        ]
    else:
        reason = "filters_cut_all"
        severity = "info"
        steps = [
            "Сбросить часть фильтров: регион, район, населенный пункт, цену и индекс.",
            "Включить режим “Все лоты” или “Архив”.",
            "Если ищете кадастр, попробовать номер с дефисами и без них.",
        ]

    summary_parts = [
        f"Последний E-Qazyna: {CRAWL_RUN_STATUS_LABELS.get(latest_run.status, latest_run.status)}",
        f"ссылок {url_count}",
        f"карточек {fetched}",
        f"страниц {pages_scanned}",
    ]
    if detail_errors:
        summary_parts.append(f"ошибок карточек {detail_errors}")
    if not crawl_complete and url_count:
        summary_parts.append("обход неполный")
    if status_line:
        summary_parts.append(f"статусы: {status_line}")
    return {
        "reason": reason,
        "reason_label": EMPTY_REASON_LABELS.get(reason, EMPTY_REASON_LABELS["unknown"]),
        "severity": severity,
        "summary": "; ".join(summary_parts) + ".",
        "steps": steps,
        "last_run_at": latest_run.finished_at or latest_run.started_at,
        "last_error": latest_run.error_message or source.last_error,
        "source_url": source.base_url,
    }


def auction_v2_dashboard(session: Session) -> dict[str, object]:
    sources = seed_auction_v2_sources(session)
    source_counts: dict[str, int] = {}
    for source in sources:
        source_counts[source.quality_status] = source_counts.get(source.quality_status, 0) + 1
    last_v2_sync = session.scalar(select(func.max(AuctionCrawlRun.finished_at)))
    recent_runs = session.execute(
        select(AuctionCrawlRun, AuctionSource)
        .join(AuctionSource, AuctionSource.id == AuctionCrawlRun.source_id)
        .order_by(AuctionCrawlRun.started_at.desc())
        .limit(12)
    ).all()
    lots_total = session.scalar(select(func.count(AuctionLot.id))) or 0
    active_lots = (
        session.scalar(select(func.count(AuctionLot.id)).where(AuctionLot.active.is_(True)))
        or 0
    )
    empty_diagnostics = _auction_v2_empty_diagnostics(
        session,
        lots_total=int(lots_total),
        active_lots=int(active_lots),
    )
    return {
        "sources_total": len(sources),
        "sources_live": source_counts.get("live", 0) + source_counts.get("partial_live", 0),
        "sources_planned": source_counts.get("planned", 0),
        "sources_manual": source_counts.get("manual_required", 0),
        "lots_total": lots_total,
        "active_lots": active_lots,
        "empty_diagnostics": empty_diagnostics,
        "documents_total": session.scalar(select(func.count(AuctionDocument.id))) or 0,
        "evidence_total": session.scalar(select(func.count(AuctionEvidence.id))) or 0,
        "analysed_lots": session.scalar(select(func.count(AuctionLotV2Analysis.id))) or 0,
        "high_score_lots": session.scalar(
            select(func.count(AuctionLotV2Analysis.id)).where(
                AuctionLotV2Analysis.score >= 75
            )
        )
        or 0,
        "high_risk_lots": session.scalar(
            select(func.count(AuctionLotV2Analysis.id)).where(
                AuctionLotV2Analysis.risk_level == "high"
            )
        )
        or 0,
        "ready_lots": session.scalar(
            select(func.count(AuctionLotV2Analysis.id)).where(
                AuctionLotV2Analysis.recommended_action == "prepare_official_review"
            )
        )
        or 0,
        "sources": sources,
        "source_cards": [
            {
                "code": source.code,
                "name": source.name,
                "base_url": source.base_url,
                "source_type": source.source_type,
                "quality_status": source.quality_status,
                "quality_label": SOURCE_QUALITY_LABELS.get(
                    source.quality_status,
                    source.quality_status,
                ),
                "notes": source.notes,
            }
            for source in sources
        ],
        "source_counts": source_counts,
        "last_v2_sync": last_v2_sync,
        "crawl_runs": session.scalar(select(func.count(AuctionCrawlRun.id))) or 0,
        "recent_runs": [
            {
                "source_code": source.code,
                "source_name": source.name,
                "status": run.status,
                "status_label": CRAWL_RUN_STATUS_LABELS.get(run.status, run.status),
                "items_seen": run.items_seen,
                "items_created": run.items_created,
                "items_updated": run.items_updated,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "error_message": run.error_message,
            }
            for run, source in recent_runs
        ],
    }


_dashboard_cache_lock = Lock()
_dashboard_cache: tuple[float, dict[str, object]] | None = None


def cached_auction_v2_dashboard(session: Session, *, ttl_seconds: int = 15) -> dict[str, object]:
    """Reuse global auction statistics briefly so every page load avoids 15 count queries."""
    if settings.app_env.strip().lower() not in {"production", "prod"}:
        return auction_v2_dashboard(session)

    now = time.monotonic()
    with _dashboard_cache_lock:
        global _dashboard_cache
        if _dashboard_cache is not None and now - _dashboard_cache[0] < ttl_seconds:
            return _dashboard_cache[1]
        dashboard = auction_v2_dashboard(session)
        _dashboard_cache = (time.monotonic(), dashboard)
        return dashboard


def auction_v2_analytics_payload(
    session: Session,
    *,
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    limit: int = 80,
) -> dict[str, object]:
    rows = session.execute(
        select(AuctionLot, AuctionLotV2Analysis, AuctionLotGeoCheck)
        .outerjoin(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id)
        .outerjoin(AuctionLotGeoCheck, AuctionLotGeoCheck.lot_id == AuctionLot.id)
        .options(selectinload(AuctionLot.documents))
    ).all()
    boundary_lot_ids = {
        lot_id
        for lot_id in session.scalars(
            select(AuctionEvidence.lot_id).where(
                AuctionEvidence.evidence_type == "cadastre_boundary",
                AuctionEvidence.status == "found",
            )
        ).all()
        if lot_id
    }
    filtered_rows: list[tuple[AuctionLot, AuctionLotV2Analysis | None, AuctionLotGeoCheck | None]] = []
    for lot, analysis, geo_check in rows:
        if not _geo_values_match(lot.region, region):
            continue
        if not _geo_values_match(lot.district, district):
            continue
        if not _geo_values_match(lot.locality, locality):
            continue
        filtered_rows.append((lot, analysis, geo_check))

    totals = _new_analytics_bucket("Все лоты", href=_auction_v2_filter_href(region=region, district=district, locality=locality))
    region_buckets: dict[str, dict[str, object]] = {}
    district_buckets: dict[str, dict[str, object]] = {}
    locality_buckets: dict[str, dict[str, object]] = {}
    purpose_buckets: dict[str, dict[str, object]] = {}
    month_buckets: dict[str, dict[str, object]] = {}

    for lot, analysis, geo_check in filtered_rows:
        deadline_label, deadline_status = _deadline_payload(lot)
        scope = _map_marker_scope(lot, deadline_status)
        _add_lot_to_analytics_bucket(
            totals,
            lot=lot,
            analysis=analysis,
            geo_check=geo_check,
            scope=scope,
            boundary_lot_ids=boundary_lot_ids,
        )

        region_label = _clean_geo_display_name(lot.region, "Регион не указан")
        district_label = _clean_geo_display_name(lot.district, "Район не указан")
        locality_label = _clean_geo_display_name(lot.locality, "Населенный пункт не указан")
        purpose_label = _clean_analytics_label(
            lot.functional_purpose_level2 or lot.purpose,
            "Назначение не указано",
        )
        region_key = _analytics_bucket_key(region_label)
        district_key = "|".join([region_key, _analytics_bucket_key(district_label)])
        locality_key = "|".join(
            [
                region_key,
                _analytics_bucket_key(district_label),
                _analytics_bucket_key(locality_label),
            ]
        )
        purpose_key = _analytics_bucket_key(purpose_label)
        month_key = _lot_publication_month_key(lot)

        region_bucket = region_buckets.setdefault(
            region_key,
            _new_analytics_bucket(
                region_label,
                region=region_label,
                href=_auction_v2_filter_href(region=region_label),
                history_href=_auction_v2_filter_href(region=region_label, lot_scope="all"),
            ),
        )
        district_bucket = district_buckets.setdefault(
            district_key,
            _new_analytics_bucket(
                district_label,
                region=region_label,
                district=district_label,
                href=_auction_v2_filter_href(region=region_label, district=district_label),
                history_href=_auction_v2_filter_href(
                    region=region_label,
                    district=district_label,
                    lot_scope="all",
                ),
            ),
        )
        locality_bucket = locality_buckets.setdefault(
            locality_key,
            _new_analytics_bucket(
                locality_label,
                region=region_label,
                district=district_label,
                locality=locality_label,
                href=_auction_v2_filter_href(
                    region=region_label,
                    district=district_label,
                    locality=locality_label,
                ),
                history_href=_auction_v2_filter_href(
                    region=region_label,
                    district=district_label,
                    locality=locality_label,
                    lot_scope="all",
                ),
            ),
        )
        purpose_bucket = purpose_buckets.setdefault(
            purpose_key,
            _new_analytics_bucket(
                purpose_label,
                href=_auction_v2_filter_href(purpose=purpose_label),
                history_href=_auction_v2_filter_href(purpose=purpose_label, lot_scope="all"),
            ),
        )
        month_bucket = month_buckets.setdefault(
            month_key,
            _new_analytics_bucket(_lot_publication_month_label(lot), href=_auction_v2_filter_href(region=region, district=district, locality=locality, lot_scope="all")),
        )

        for bucket in (
            region_bucket,
            district_bucket,
            locality_bucket,
            purpose_bucket,
            month_bucket,
        ):
            _add_lot_to_analytics_bucket(
                bucket,
                lot=lot,
                analysis=analysis,
                geo_check=geo_check,
                scope=scope,
                boundary_lot_ids=boundary_lot_ids,
            )

    totals_row = _finalize_analytics_bucket(totals)
    return {
        "filters": {
            "region": region or "",
            "district": district or "",
            "locality": locality or "",
        },
        "has_filters": bool(region or district or locality),
        "totals": totals_row,
        "region_rows": _sort_analytics_rows(region_buckets.values())[:limit],
        "district_rows": _sort_analytics_rows(district_buckets.values())[:limit],
        "locality_rows": _sort_analytics_rows(locality_buckets.values())[:limit],
        "purpose_rows": _sort_analytics_rows(purpose_buckets.values())[: min(limit, 40)],
        "month_rows": [
            _finalize_analytics_bucket(bucket)
            for _key, bucket in sorted(month_buckets.items(), reverse=True)
        ][:24],
    }


def _new_analytics_bucket(
    label: str,
    *,
    region: str = "",
    district: str = "",
    locality: str = "",
    href: str = "/cabinet/auctions-v2",
    history_href: str | None = None,
) -> dict[str, object]:
    return {
        "label": label,
        "region": region,
        "district": district,
        "locality": locality,
        "href": href,
        "history_href": history_href or href,
        "total": 0,
        "active": 0,
        "future": 0,
        "archive": 0,
        "analysed": 0,
        "high_risk": 0,
        "medium_risk": 0,
        "low_risk": 0,
        "documents": 0,
        "coordinates": 0,
        "boundaries": 0,
        "_prices": [],
        "_scores": [],
    }


def _add_lot_to_analytics_bucket(
    bucket: dict[str, object],
    *,
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis | None,
    geo_check: AuctionLotGeoCheck | None,
    scope: str,
    boundary_lot_ids: set[str],
) -> None:
    bucket["total"] = int(bucket["total"]) + 1
    if scope in {"active", "future", "archive"}:
        bucket[scope] = int(bucket[scope]) + 1
    if analysis is not None:
        bucket["analysed"] = int(bucket["analysed"]) + 1
        if analysis.risk_level == "high":
            bucket["high_risk"] = int(bucket["high_risk"]) + 1
        elif analysis.risk_level == "medium":
            bucket["medium_risk"] = int(bucket["medium_risk"]) + 1
        elif analysis.risk_level == "low":
            bucket["low_risk"] = int(bucket["low_risk"]) + 1
        bucket["_scores"].append(float(analysis.score))  # type: ignore[union-attr]
    if lot.documents:
        bucket["documents"] = int(bucket["documents"]) + 1
    if (
        geo_check is not None
        and geo_check.coordinate_status == "found"
        and geo_check.latitude is not None
        and geo_check.longitude is not None
    ):
        bucket["coordinates"] = int(bucket["coordinates"]) + 1
    if lot.id in boundary_lot_ids:
        bucket["boundaries"] = int(bucket["boundaries"]) + 1
    price_per_sotka = (
        analysis.price_per_sotka
        if analysis is not None and analysis.price_per_sotka is not None
        else _lot_price_per_sotka(lot)
    )
    if price_per_sotka is not None and price_per_sotka > 0:
        bucket["_prices"].append(float(price_per_sotka))  # type: ignore[union-attr]


def _finalize_analytics_bucket(bucket: dict[str, object]) -> dict[str, object]:
    prices = [float(value) for value in bucket.pop("_prices", [])]  # type: ignore[arg-type]
    scores = [float(value) for value in bucket.pop("_scores", [])]  # type: ignore[arg-type]
    total = int(bucket["total"])
    median_price = median(prices) if prices else None
    average_price = sum(prices) / len(prices) if prices else None
    average_score = sum(scores) / len(scores) if scores else None
    bucket.update(
        {
            "median_price_per_sotka": median_price,
            "median_price_per_sotka_text": _money(median_price),
            "average_price_per_sotka": average_price,
            "average_price_per_sotka_text": _money(average_price),
            "average_score": average_score,
            "average_score_text": f"{average_score:.0f}/100" if average_score is not None else "—",
            "documents_percent_text": _plain_percent(int(bucket["documents"]), total),
            "coordinates_percent_text": _plain_percent(int(bucket["coordinates"]), total),
            "boundaries_percent_text": _plain_percent(int(bucket["boundaries"]), total),
            "risk_note": _analytics_risk_note(
                high_risk=int(bucket["high_risk"]),
                medium_risk=int(bucket["medium_risk"]),
                total=total,
            ),
            "data_note": _analytics_data_note(
                documents=int(bucket["documents"]),
                coordinates=int(bucket["coordinates"]),
                boundaries=int(bucket["boundaries"]),
                total=total,
            ),
            "price_count": len(prices),
        }
    )
    return bucket


def _sort_analytics_rows(rows: object) -> list[dict[str, object]]:
    finalized = [_finalize_analytics_bucket(dict(row)) for row in rows]  # type: ignore[arg-type]
    return sorted(
        finalized,
        key=lambda row: (
            int(row["active"]) + int(row["future"]),
            float(row["average_score"] or 0),
            int(row["total"]),
        ),
        reverse=True,
    )


def _plain_percent(value: int, total: int) -> str:
    if total <= 0:
        return "—"
    return f"{round((value / total) * 100)}%"


def _analytics_risk_note(*, high_risk: int, medium_risk: int, total: int) -> str:
    if total <= 0:
        return "Нет лотов"
    if high_risk:
        return f"{high_risk} с высоким риском"
    if medium_risk:
        return f"{medium_risk} со средним риском"
    return "Критичных рисков не видно"


def _analytics_data_note(
    *,
    documents: int,
    coordinates: int,
    boundaries: int,
    total: int,
) -> str:
    if total <= 0:
        return "Данных пока нет"
    missing: list[str] = []
    if documents < total:
        missing.append("документы")
    if coordinates < total:
        missing.append("координаты")
    if boundaries < total:
        missing.append("границы")
    if not missing:
        return "Данные собраны хорошо"
    return "Проверить: " + ", ".join(missing[:3])


def _clean_analytics_label(value: str | None, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -–—:/,") or fallback


def _clean_geo_display_name(value: str | None, fallback: str) -> str:
    text = _clean_analytics_label(value, fallback)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"^\s*УСТАРЕВШЕЕ\s*[-–—:/]*\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -–—:/,") or fallback


def _analytics_bucket_key(value: str | None) -> str:
    text = _clean_analytics_label(value, "")
    text = re.sub(r"\([^)]*\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[-–—:/,]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _geo_values_match(source_value: str | None, filter_value: str | None) -> bool:
    wanted = str(filter_value or "").strip()
    if not wanted:
        return True
    source = str(source_value or "").strip()
    if not source:
        return False
    source_variants = [source, *_geo_filter_variants(source)]
    wanted_variants = [wanted, *_geo_filter_variants(wanted)]
    source_keys = {
        _analytics_bucket_key(value)
        for value in source_variants
        if _analytics_bucket_key(value)
    }
    wanted_keys = {
        _analytics_bucket_key(value)
        for value in wanted_variants
        if _analytics_bucket_key(value)
    }
    for source_key in source_keys:
        for wanted_key in wanted_keys:
            if source_key == wanted_key:
                return True
            if len(source_key) >= 4 and len(wanted_key) >= 4 and (
                source_key in wanted_key or wanted_key in source_key
            ):
                return True
    return False


def _auction_v2_filter_href(
    *,
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    purpose: str | None = None,
    lot_scope: str = "active",
) -> str:
    params: list[tuple[str, str]] = []
    if lot_scope and lot_scope != "active":
        params.append(("lot_scope", lot_scope))
    if region:
        params.append(("region", region))
    if district:
        params.append(("district", district))
    if locality:
        params.append(("locality", locality))
    if purpose:
        params.append(("purpose", purpose))
    if not params:
        return "/cabinet/auctions-v2"
    query = "&".join(f"{key}={quote_plus(value)}" for key, value in params if value)
    return f"/cabinet/auctions-v2?{query}"


def _lot_publication_month_key(lot: AuctionLot) -> str:
    value = lot.published_at or _aware(lot.first_seen_at)
    return value.strftime("%Y-%m") if value else "unknown"


def _lot_publication_month_label(lot: AuctionLot) -> str:
    value = lot.published_at or _aware(lot.first_seen_at)
    return value.strftime("%m.%Y") if value else "Дата не указана"


def auction_v2_source_admin_payload(session: Session) -> dict[str, object]:
    sources = seed_auction_v2_sources(session)
    runs = session.execute(
        select(AuctionCrawlRun, AuctionSource)
        .join(AuctionSource, AuctionSource.id == AuctionCrawlRun.source_id)
        .order_by(AuctionCrawlRun.started_at.desc(), AuctionCrawlRun.id.desc())
        .limit(120)
    ).all()
    latest_run_by_source: dict[int, AuctionCrawlRun] = {}
    run_counts: dict[int, int] = {}
    for run, _source in runs:
        run_counts[run.source_id] = run_counts.get(run.source_id, 0) + 1
        latest_run_by_source.setdefault(run.source_id, run)
    evidence_counts = dict(
        session.execute(
            select(AuctionEvidence.source_id, func.count(AuctionEvidence.id))
            .where(AuctionEvidence.source_id.is_not(None))
            .group_by(AuctionEvidence.source_id)
        ).all()
    )
    source_rows = []
    for source in sources:
        latest_run = latest_run_by_source.get(source.id)
        status = _source_admin_status(source, latest_run)
        source_rows.append(
            {
                "source": source,
                "status": status,
                "status_label": _source_admin_status_label(status),
                "quality_label": SOURCE_QUALITY_LABELS.get(
                    source.quality_status,
                    source.quality_status,
                ),
                "latest_run": latest_run,
                "latest_run_status_label": (
                    CRAWL_RUN_STATUS_LABELS.get(latest_run.status, latest_run.status)
                    if latest_run
                    else "Нет запусков"
                ),
                "run_count": run_counts.get(source.id, 0),
                "evidence_count": evidence_counts.get(source.id, 0),
                "raw_payload_preview": _raw_payload_preview(
                    latest_run.raw_payload_json if latest_run else None
                ),
            }
        )
    return {
        "source_rows": source_rows,
        "recent_runs": [
            {
                "run": run,
                "source": source,
                "status_label": CRAWL_RUN_STATUS_LABELS.get(run.status, run.status),
                "raw_payload_preview": _raw_payload_preview(run.raw_payload_json),
            }
            for run, source in runs[:30]
        ],
        "totals": {
            "sources": len(sources),
            "active_sources": sum(1 for source in sources if source.active),
            "runs": session.scalar(select(func.count(AuctionCrawlRun.id))) or 0,
            "errors": session.scalar(
                select(func.count(AuctionCrawlRun.id)).where(
                    AuctionCrawlRun.status == "error"
                )
            )
            or 0,
            "warnings": session.scalar(
                select(func.count(AuctionCrawlRun.id)).where(
                    AuctionCrawlRun.status == "warning"
                )
            )
            or 0,
            "evidence": session.scalar(select(func.count(AuctionEvidence.id))) or 0,
        },
    }


def _source_admin_status(source: AuctionSource, latest_run: AuctionCrawlRun | None) -> str:
    if not source.active:
        return "disabled"
    if latest_run is not None and latest_run.status in {"error", "warning", "running"}:
        return latest_run.status
    if source.last_error:
        return "error"
    if latest_run is not None and latest_run.status == "missing":
        return "missing"
    if source.quality_status in {"planned", "manual_required"}:
        return source.quality_status
    if latest_run is not None and latest_run.status == "success":
        return "success"
    if source.last_success_at is not None:
        return "success"
    return "pending"


def _source_admin_status_label(status: str) -> str:
    labels = {
        "success": "Работает",
        "warning": "Есть вопросы",
        "error": "Ошибка",
        "running": "Идет сейчас",
        "missing": "Нет данных",
        "planned": "Запланировано",
        "manual_required": "Ручная сверка",
        "disabled": "Выключен",
        "pending": "Ожидает запуска",
    }
    return labels.get(status, status)


def _raw_payload_preview(raw_payload_json: str | None, limit: int = 1800) -> str:
    if not raw_payload_json:
        return ""
    try:
        parsed = json.loads(raw_payload_json)
    except json.JSONDecodeError:
        return raw_payload_json[:limit]
    return json.dumps(parsed, ensure_ascii=False, indent=2)[:limit]


def refresh_auction_v2_snapshot(session: Session, *, limit: int | None = None) -> dict[str, int]:
    limit = limit or settings.auction_v2_refresh_limit
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.auction_v2_analysis_ttl_minutes)
    lots = list(
        session.scalars(
            select(AuctionLot)
            .options(selectinload(AuctionLot.documents))
            .outerjoin(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id)
            .where(
                AuctionLot.active.is_(True),
                or_(
                    AuctionLotV2Analysis.id.is_(None),
                    AuctionLotV2Analysis.checked_at < cutoff,
                ),
            )
            .order_by(
                AuctionLotV2Analysis.id.is_not(None),
                AuctionLotV2Analysis.checked_at.is_not(None),
                AuctionLotV2Analysis.checked_at,
                AuctionLot.last_seen_at.desc(),
                AuctionLot.created_at.desc(),
            )
            .limit(limit)
        ).all()
    )
    for lot in lots:
        build_auction_v2_analysis(session, lot, force=True)
    session.flush()
    return {"checked": len(lots)}


def ensure_auction_v2_analyses_for_filters(
    session: Session,
    filters: AuctionV2Filters,
    *,
    account_id: str | None = None,
    limit: int | None = None,
    refresh_stale: bool = False,
) -> dict[str, int]:
    candidate_limit = limit or settings.auction_v2_refresh_limit
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.auction_v2_analysis_ttl_minutes)
    conditions = _preanalysis_lot_conditions(filters)
    conditions.append(
        _analysis_due_condition(cutoff)
        if refresh_stale
        else AuctionLotV2Analysis.id.is_(None)
    )
    query = (
        select(AuctionLot)
        .options(selectinload(AuctionLot.documents))
        .outerjoin(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id)
    )
    if filters.geo_status:
        query = query.outerjoin(
            AuctionLotGeoCheck,
            AuctionLotGeoCheck.lot_id == AuctionLot.id,
        )
    if filters.stage and account_id:
        query = query.join(
            AuctionUserLotPipeline,
            and_(
                AuctionUserLotPipeline.lot_id == AuctionLot.id,
                AuctionUserLotPipeline.account_id == account_id,
            ),
        )
        conditions.append(AuctionUserLotPipeline.stage == filters.stage)
    lots = list(
        session.scalars(
            query.where(and_(*conditions))
            .order_by(
                AuctionLot.active.desc(),
                AuctionLot.last_seen_at.desc(),
                AuctionLot.auction_starts_at.is_(None),
                AuctionLot.auction_starts_at,
                AuctionLot.created_at.desc(),
            )
            .limit(candidate_limit)
        ).all()
    )
    for lot in lots:
        build_auction_v2_analysis(session, lot, force=True)
    session.flush()
    return {"checked": len(lots)}


def _analysis_due_condition(cutoff: datetime):
    return or_(
        AuctionLotV2Analysis.id.is_(None),
        AuctionLotV2Analysis.checked_at.is_(None),
        AuctionLotV2Analysis.checked_at < cutoff,
    )


def _preanalysis_lot_conditions(filters: AuctionV2Filters) -> list[object]:
    conditions = _auction_filter_conditions(_base_filters_for_lot_scope(filters))
    conditions.extend(_lot_scope_conditions(filters.lot_scope))
    conditions.extend(_search_conditions(filters.search_query))
    conditions.extend(_eqazyna_status_conditions(filters.eqazyna_status))
    if filters.deadline_status:
        conditions.extend(_deadline_conditions(filters.deadline_status))
    if filters.geo_status:
        conditions.extend(_geo_status_conditions(filters.geo_status))
    return conditions


def refresh_auction_v2_infrastructure(
    session: Session,
    lot: AuctionLot,
    *,
    provider: OsmProvider | None = None,
    force: bool = False,
) -> AuctionLotGeoCheck:
    geo_check = _get_or_build_geo_check(session, lot)
    if geo_check.latitude is None or geo_check.longitude is None:
        _clear_osm_fields(geo_check, status="missing_coordinates")
        geo_check.notes = _geo_check_notes(geo_check)
        session.flush()
        return geo_check
    if not force and not _osm_check_due(geo_check):
        return geo_check
    if provider is None:
        if not _auction_v2_live_osm_enabled():
            return geo_check
        provider = OsmProvider()
    try:
        surroundings = provider.analyze_points(
            [(geo_check.latitude, geo_check.longitude)],
            radius_m=settings.auction_v2_osm_radius_m,
        )
    except OsmProviderError as exc:
        _mark_osm_unavailable(geo_check, exc)
        session.flush()
        return geo_check
    if surroundings:
        _apply_osm_surroundings(geo_check, surroundings[0])
    session.flush()
    return geo_check


def prepare_auction_v2_worklist(
    session: Session,
    *,
    limit: int | None = None,
    force: bool = True,
    send_notifications: bool = False,
) -> AuctionV2SyncResult:
    """Build the fast user-facing layer before slow external checks finish."""
    limit = limit or settings.auction_v2_refresh_limit
    sources_by_code = {source.code: source for source in seed_auction_v2_sources(session)}
    lots = list(
        session.scalars(
            select(AuctionLot)
            .options(selectinload(AuctionLot.documents))
            .outerjoin(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id)
            .where(AuctionLot.active.is_(True))
            .order_by(
                AuctionLotV2Analysis.id.is_not(None),
                AuctionLotV2Analysis.checked_at.is_not(None),
                AuctionLotV2Analysis.checked_at,
                AuctionLot.last_seen_at.desc(),
                AuctionLot.created_at.desc(),
            )
            .limit(limit)
        ).all()
    )
    evidence_before = session.scalar(select(func.count(AuctionEvidence.id))) or 0
    for lot in lots:
        build_auction_v2_analysis(session, lot, force=force)
        _sync_external_query_evidence(session, lot, sources_by_code)
    session.flush()
    evidence_after = session.scalar(select(func.count(AuctionEvidence.id))) or 0
    evidence_created = max(0, int(evidence_after) - int(evidence_before))
    notification_result = (
        dispatch_auction_v2_watchlist_notifications(session)
        if send_notifications
        else AuctionV2NotificationResult()
    )
    return AuctionV2SyncResult(
        lots_checked=len(lots),
        analyses_updated=len(lots),
        evidence_created=evidence_created,
        watchlists_checked=notification_result.watchlists_checked,
        watchlist_matches_seen=notification_result.matches_seen,
        web_notifications_created=notification_result.web_notifications_created,
        telegram_notifications_sent=notification_result.telegram_notifications_sent,
        notification_errors=notification_result.errors,
    )


def sync_auction_v2_sources(
    session: Session,
    *,
    limit: int | None = None,
    force: bool = True,
    send_notifications: bool = True,
) -> AuctionV2SyncResult:
    limit = limit or settings.auction_v2_refresh_limit
    sources = seed_auction_v2_sources(session)
    sources_by_code = {source.code: source for source in sources}
    sync_source_codes = (
        "eqazyna_current_lots",
        "gov_kz_akimat_announcements",
        "egkn_public_map",
        "smart_geohub_genplans",
        "geo_shymkent",
        "data_egov_open_data",
        "krisha_land_market",
        "olx_land_market",
        "osm_overpass",
    )
    started_at = datetime.now(UTC)
    runs: list[AuctionCrawlRun] = []
    for code in sync_source_codes:
        source = sources_by_code.get(code)
        if source is None or not source.active:
            continue
        run = AuctionCrawlRun(
            source_id=source.id,
            status="running",
            started_at=started_at,
            raw_payload_json=json.dumps(
                {"mode": "auction_v2_pre_purchase_sync", "limit": limit},
                ensure_ascii=False,
            ),
        )
        session.add(run)
        runs.append(run)
    session.flush()

    lots = list(
        session.scalars(
            select(AuctionLot)
            .options(selectinload(AuctionLot.documents))
            .outerjoin(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id)
            .where(AuctionLot.active.is_(True))
            .order_by(
                AuctionLotV2Analysis.id.is_not(None),
                AuctionLotV2Analysis.checked_at.is_not(None),
                AuctionLotV2Analysis.checked_at,
                AuctionLot.last_seen_at.desc(),
                AuctionLot.created_at.desc(),
            )
            .limit(limit)
        ).all()
    )
    evidence_before = session.scalar(select(func.count(AuctionEvidence.id))) or 0
    egkn_checked = 0
    egkn_verified = 0
    egkn_errors = 0
    egkn_context_checked = 0
    egkn_context_features = 0
    egkn_context_errors = 0
    if _auction_v2_live_egkn_enabled():
        egkn_source = sources_by_code.get("egkn_public_map")
        if egkn_source is not None and egkn_source.active:
            egkn_checked, egkn_verified, egkn_errors = _refresh_auction_v2_egkn_batch(
                session,
                lots,
                source=egkn_source,
                force=force,
            )
            (
                egkn_context_checked,
                egkn_context_features,
                egkn_context_errors,
            ) = _refresh_auction_v2_egkn_context_batch(
                session,
                lots,
                source=egkn_source,
                force=force,
            )
    infrastructure_checked = 0
    infrastructure_errors = 0
    if _auction_v2_live_osm_enabled():
        infrastructure_checked, infrastructure_errors = _refresh_auction_v2_infrastructure_batch(
            session,
            lots,
            force=force,
        )
    gov_kz_items_seen = 0
    gov_kz_matches = 0
    gov_kz_errors: list[str] = []
    if _auction_v2_live_gov_kz_enabled():
        gov_kz_source = sources_by_code.get("gov_kz_akimat_announcements")
        if gov_kz_source is not None and gov_kz_source.active:
            gov_kz_items_seen, gov_kz_matches, gov_kz_errors = (
                sync_auction_v2_gov_kz_announcements(
                    session,
                    lots=lots,
                    source=gov_kz_source,
                )
            )
    for lot in lots:
        build_auction_v2_analysis(session, lot, force=force)
        _sync_external_query_evidence(session, lot, sources_by_code)

    finished_at = datetime.now(UTC)
    runtime_source_statuses = {
        "osm_overpass": _osm_run_status(
            infrastructure_checked=infrastructure_checked,
            infrastructure_errors=infrastructure_errors,
        ),
        "gov_kz_akimat_announcements": _gov_kz_run_status(
            items_seen=gov_kz_items_seen,
            matches=gov_kz_matches,
            errors=gov_kz_errors,
        ),
        "egkn_public_map": _egkn_run_status(
            checked=egkn_checked + egkn_context_checked,
            verified=egkn_verified + egkn_context_features,
            errors=egkn_errors + egkn_context_errors,
        ),
    }
    runtime_items_seen = {
        "osm_overpass": infrastructure_checked,
        "gov_kz_akimat_announcements": gov_kz_items_seen,
        "egkn_public_map": egkn_checked + egkn_context_checked,
    }
    for run in runs:
        run_source = session.get(AuctionSource, run.source_id)
        status = runtime_source_statuses.get(run_source.code if run_source else "") or (
            _v2_source_sync_status(run_source)
        )
        run.status = status
        run.items_seen = _v2_run_items_seen(
            run_source,
            status=status,
            lots_count=len(lots),
            infrastructure_checked=infrastructure_checked,
            runtime_items_seen=runtime_items_seen,
        )
        run.items_updated = (
            gov_kz_matches
            if run_source is not None and run_source.code == "gov_kz_akimat_announcements"
            else egkn_verified + egkn_context_features
            if run_source is not None and run_source.code == "egkn_public_map"
            else run.items_seen
        )
        run.finished_at = finished_at
        if run_source is not None:
            run_source.last_checked_at = finished_at
            if status == "success":
                run_source.last_success_at = finished_at
            run_source.last_error = (
                "; ".join(gov_kz_errors)[:2000]
                if run_source.code == "gov_kz_akimat_announcements" and gov_kz_errors
                else None
            )
    session.flush()
    evidence_after = session.scalar(select(func.count(AuctionEvidence.id))) or 0
    evidence_created = max(0, int(evidence_after) - int(evidence_before))
    notification_result = (
        dispatch_auction_v2_watchlist_notifications(session)
        if send_notifications
        else AuctionV2NotificationResult()
    )
    return AuctionV2SyncResult(
        lots_checked=len(lots),
        analyses_updated=len(lots),
        infrastructure_checked=infrastructure_checked,
        infrastructure_errors=infrastructure_errors,
        sources_checked=len(runs),
        evidence_created=evidence_created,
        crawl_runs_created=len(runs),
        watchlists_checked=notification_result.watchlists_checked,
        watchlist_matches_seen=notification_result.matches_seen,
        web_notifications_created=notification_result.web_notifications_created,
        telegram_notifications_sent=notification_result.telegram_notifications_sent,
        notification_errors=notification_result.errors,
    )


def sync_auction_v2_gov_kz_announcements(
    session: Session,
    *,
    lots: list[AuctionLot],
    source: AuctionSource,
    provider: GovKzProvider | None = None,
) -> tuple[int, int, list[str]]:
    if not lots or not source.active or not _auction_v2_live_gov_kz_enabled():
        return 0, 0, []

    projects = _csv_settings(settings.auction_v2_gov_kz_projects)
    detail_urls = _csv_settings(settings.auction_v2_gov_kz_detail_urls)
    if not projects and not detail_urls:
        return 0, 0, []

    owned_provider = provider is None
    gov_provider = provider or GovKzProvider(base_url=_gov_kz_base_url(source.base_url))
    try:
        announcements = gov_provider.crawl_announcements(
            projects=projects,
            detail_urls=detail_urls,
            page_size=settings.auction_v2_gov_kz_page_size,
            max_pages=settings.auction_v2_gov_kz_max_pages,
        )
        crawl_errors = list(getattr(gov_provider, "errors", []))
    except (GovKzError, OSError) as exc:
        return 0, 0, [str(exc)]
    finally:
        if owned_provider:
            gov_provider.close()

    matches = 0
    for announcement in announcements:
        for lot in lots:
            confidence, reasons = _gov_kz_lot_match(lot, announcement)
            if confidence < 0.45:
                continue
            _upsert_evidence(
                session,
                lot=lot,
                source=source,
                evidence_type="akimat_announcement",
                title=announcement.title,
                status="found",
                value_text=_gov_kz_evidence_text(announcement, reasons),
                source_url=announcement.source_url,
                confidence=confidence,
                raw_payload_json=json.dumps(announcement.as_dict(), ensure_ascii=False),
            )
            _upsert_gov_kz_attachments(session, lot, announcement)
            matches += 1
    session.flush()
    return len(announcements), matches, crawl_errors


def sync_auction_v2_eqazyna_history_backfill(
    session: Session,
    *,
    max_pages: int | None = None,
    max_lots: int | None = None,
    statuses: list[str] | None = None,
    publish_date_windows: list[tuple[str, str]] | None = None,
) -> AuctionSyncResult:
    page_limit = max_pages or settings.eqazyna_history_sync_max_pages
    lot_limit = max_lots or settings.eqazyna_history_sync_max_lots
    history_statuses = statuses or configured_eqazyna_history_statuses()
    date_windows = publish_date_windows or eqazyna_history_publish_date_windows()
    sources_by_code = {source.code: source for source in seed_auction_v2_sources(session)}
    source = sources_by_code.get("eqazyna_history_backfill")
    source_id = source.id if source is not None else None
    run_id: int | None = None
    started_at = datetime.now(UTC)
    if source is not None and source.active:
        run = AuctionCrawlRun(
            source_id=source.id,
            status="running",
            started_at=started_at,
            raw_payload_json=json.dumps(
                {
                    "mode": "auction_v2_eqazyna_history_backfill",
                    "max_pages": page_limit,
                    "max_lots": lot_limit,
                    "statuses": history_statuses,
                    "publish_date_windows_count": len(date_windows),
                    "publish_date_window_first": date_windows[0] if date_windows else None,
                    "publish_date_window_last": date_windows[-1] if date_windows else None,
                    "deactivate_missing": False,
                    "send_notifications": False,
                },
                ensure_ascii=False,
            ),
        )
        session.add(run)
        source.last_checked_at = started_at
        session.commit()
        run_id = run.id

    try:
        result = AuctionSyncResult(
            fetched=0,
            created=0,
            updated=0,
            notifications_sent=0,
            errors=0,
            crawl_complete=True,
            status_counts={status: 0 for status in history_statuses},
        )
        for publish_date_window in date_windows:
            window_result = sync_current_auctions(
                session,
                max_pages=page_limit,
                max_lots=lot_limit,
                statuses=history_statuses,
                publish_date_windows=[publish_date_window],
                deactivate_missing=False,
                send_notifications=False,
            )
            result.fetched += window_result.fetched
            result.created += window_result.created
            result.updated += window_result.updated
            result.notifications_sent += window_result.notifications_sent
            result.errors += window_result.errors
            result.detail_errors += window_result.detail_errors
            result.deactivated += window_result.deactivated
            result.url_count += window_result.url_count
            result.pages_scanned += window_result.pages_scanned
            result.crawl_complete = result.crawl_complete and window_result.crawl_complete
            status_counts = result.status_counts or {}
            for status, count in (window_result.status_counts or {}).items():
                status_counts[status] = status_counts.get(status, 0) + count
            result.status_counts = status_counts
    except Exception as exc:
        session.rollback()
        if run_id is not None:
            finished_at = datetime.now(UTC)
            run = session.get(AuctionCrawlRun, run_id)
            source = session.get(AuctionSource, source_id) if source_id is not None else None
            if run is not None:
                run.status = "error"
                run.finished_at = finished_at
                run.error_message = str(exc)[:2000]
            if source is not None:
                source.last_checked_at = finished_at
                source.last_error = str(exc)[:2000]
            session.commit()
        raise

    if run_id is not None:
        finished_at = datetime.now(UTC)
        run = session.get(AuctionCrawlRun, run_id)
        source = session.get(AuctionSource, source_id) if source_id is not None else None
        status = (
            "warning"
            if result.errors
            else "missing"
            if result.url_count == 0 and result.fetched == 0
            else "success"
        )
        if run is not None:
            run.status = status
            run.items_seen = result.url_count or result.fetched
            run.items_created = result.created
            run.items_updated = result.updated
            run.finished_at = finished_at
            run.error_message = (
                f"Ошибки деталей: {result.detail_errors}"
                if result.detail_errors
                else None
            )
            run.raw_payload_json = json.dumps(
                {
                    "mode": "auction_v2_eqazyna_history_backfill",
                    "max_pages": page_limit,
                    "max_lots": lot_limit,
                    "statuses": history_statuses,
                    "publish_date_windows_count": len(date_windows),
                    "publish_date_window_first": date_windows[0] if date_windows else None,
                    "publish_date_window_last": date_windows[-1] if date_windows else None,
                    "fetched": result.fetched,
                    "url_count": result.url_count,
                    "pages_scanned": result.pages_scanned,
                    "crawl_complete": result.crawl_complete,
                    "detail_errors": result.detail_errors,
                    "errors": result.errors,
                    "deactivated": result.deactivated,
                    "status_counts": result.status_counts or {},
                },
                ensure_ascii=False,
            )
        if source is not None:
            source.last_checked_at = finished_at
            source.last_error = (
                f"Ошибки деталей: {result.detail_errors}"
                if result.detail_errors
                else None
            )
            if status in {"success", "missing"}:
                source.last_success_at = finished_at
        session.commit()
    prepare_auction_v2_worklist(
        session,
        limit=settings.auction_v2_refresh_limit,
        send_notifications=False,
    )
    session.commit()
    return result


def sync_auction_v2_full_cycle(
    session: Session,
    *,
    limit: int | None = None,
    send_v1_notifications: bool = False,
) -> AuctionV2FullSyncResult:
    limit = limit or settings.auction_v2_refresh_limit
    eqazyna_fetch_limit = max(limit, settings.eqazyna_sync_max_lots)
    eqazyna_max_pages = max(1, settings.eqazyna_sync_max_pages)
    sources_by_code = {source.code: source for source in seed_auction_v2_sources(session)}
    eqazyna_source = sources_by_code.get("eqazyna_current_lots")
    eqazyna_source_id = eqazyna_source.id if eqazyna_source is not None else None
    eqazyna_run_id: int | None = None
    started_at = datetime.now(UTC)
    if eqazyna_source is not None and eqazyna_source.active:
        eqazyna_run = AuctionCrawlRun(
            source_id=eqazyna_source.id,
            status="running",
            started_at=started_at,
            raw_payload_json=json.dumps(
                {
                    "mode": "auction_v2_eqazyna_crawl",
                    "worklist_limit": limit,
                    "eqazyna_fetch_limit": eqazyna_fetch_limit,
                    "max_pages": eqazyna_max_pages,
                },
                ensure_ascii=False,
            ),
        )
        session.add(eqazyna_run)
        eqazyna_source.last_checked_at = started_at
        session.commit()
        eqazyna_run_id = eqazyna_run.id

    try:
        crawl_result = sync_current_auctions(
            session,
            max_pages=eqazyna_max_pages,
            max_lots=eqazyna_fetch_limit,
            send_notifications=send_v1_notifications,
        )
    except Exception as exc:
        session.rollback()
        if eqazyna_run_id is not None:
            finished_at = datetime.now(UTC)
            eqazyna_run = session.get(AuctionCrawlRun, eqazyna_run_id)
            eqazyna_source = (
                session.get(AuctionSource, eqazyna_source_id)
                if eqazyna_source_id is not None
                else None
            )
            if eqazyna_run is not None:
                eqazyna_run.status = "error"
                eqazyna_run.finished_at = finished_at
                eqazyna_run.error_message = str(exc)[:2000]
            if eqazyna_source is not None:
                eqazyna_source.last_checked_at = finished_at
                eqazyna_source.last_error = str(exc)[:2000]
            session.commit()
        raise

    if eqazyna_run_id is not None:
        finished_at = datetime.now(UTC)
        eqazyna_run = session.get(AuctionCrawlRun, eqazyna_run_id)
        eqazyna_source = (
            session.get(AuctionSource, eqazyna_source_id)
            if eqazyna_source_id is not None
            else None
        )
        status = (
            "warning"
            if crawl_result.errors
            else "missing"
            if crawl_result.url_count == 0 and crawl_result.fetched == 0
            else "success"
        )
        if eqazyna_run is not None:
            eqazyna_run.status = status
            eqazyna_run.items_seen = crawl_result.url_count or crawl_result.fetched
            eqazyna_run.items_created = crawl_result.created
            eqazyna_run.items_updated = crawl_result.updated
            eqazyna_run.finished_at = finished_at
            eqazyna_run.error_message = (
                f"Ошибки деталей: {crawl_result.detail_errors}"
                if crawl_result.detail_errors
                else None
            )
            eqazyna_run.raw_payload_json = json.dumps(
                {
                    "mode": "auction_v2_eqazyna_crawl",
                    "worklist_limit": limit,
                    "eqazyna_fetch_limit": eqazyna_fetch_limit,
                    "fetched": crawl_result.fetched,
                    "url_count": crawl_result.url_count,
                    "pages_scanned": crawl_result.pages_scanned,
                    "crawl_complete": crawl_result.crawl_complete,
                    "detail_errors": crawl_result.detail_errors,
                    "errors": crawl_result.errors,
                    "deactivated": crawl_result.deactivated,
                    "status_counts": crawl_result.status_counts or {},
                },
                ensure_ascii=False,
            )
        if eqazyna_source is not None:
            eqazyna_source.last_checked_at = finished_at
            eqazyna_source.last_error = (
                f"Ошибки деталей: {crawl_result.detail_errors}"
                if crawl_result.detail_errors
                else None
            )
            if status in {"success", "missing"}:
                eqazyna_source.last_success_at = finished_at
        session.commit()
    quick_result = prepare_auction_v2_worklist(
        session,
        limit=limit,
        send_notifications=False,
    )
    session.commit()
    v2_result = sync_auction_v2_sources(session, limit=limit)
    document_result = sync_auction_v2_documents(session)
    v2_result.documents_checked += document_result.checked
    v2_result.documents_downloaded += document_result.downloaded
    v2_result.document_errors += document_result.errors
    return AuctionV2FullSyncResult(
        lots_fetched=crawl_result.fetched,
        lots_created=crawl_result.created,
        lots_updated=crawl_result.updated,
        lots_deactivated=crawl_result.deactivated,
        crawl_errors=crawl_result.errors,
        v2=AuctionV2SyncResult(
            lots_checked=max(v2_result.lots_checked, quick_result.lots_checked),
            analyses_updated=max(v2_result.analyses_updated, quick_result.analyses_updated),
            infrastructure_checked=v2_result.infrastructure_checked,
            infrastructure_errors=v2_result.infrastructure_errors,
            documents_checked=v2_result.documents_checked,
            documents_downloaded=v2_result.documents_downloaded,
            document_errors=v2_result.document_errors,
            sources_checked=v2_result.sources_checked,
            evidence_created=v2_result.evidence_created + quick_result.evidence_created,
            crawl_runs_created=v2_result.crawl_runs_created,
            watchlists_checked=max(
                v2_result.watchlists_checked,
                quick_result.watchlists_checked,
            ),
            watchlist_matches_seen=max(
                v2_result.watchlist_matches_seen,
                quick_result.watchlist_matches_seen,
            ),
            web_notifications_created=(
                v2_result.web_notifications_created + quick_result.web_notifications_created
            ),
            telegram_notifications_sent=(
                v2_result.telegram_notifications_sent
                + quick_result.telegram_notifications_sent
            ),
            notification_errors=v2_result.notification_errors + quick_result.notification_errors,
        ),
    )


def sync_auction_v2_documents(
    session: Session,
    *,
    limit: int | None = None,
    enabled: bool | None = None,
    client: httpx.Client | None = None,
) -> AuctionV2DocumentSyncResult:
    should_download = (
        settings.auction_v2_document_download_enabled if enabled is None else enabled
    )
    if not should_download:
        return AuctionV2DocumentSyncResult()

    document_limit = limit or settings.auction_v2_document_download_limit
    documents = list(
        session.scalars(
            select(AuctionDocument)
            .where(
                AuctionDocument.source_url != "",
                or_(
                    AuctionDocument.storage_status.is_(None),
                    AuctionDocument.storage_status.in_(("linked", "failed")),
                ),
            )
            .order_by(
                AuctionDocument.storage_status == "failed",
                AuctionDocument.created_at.desc(),
                AuctionDocument.id.desc(),
            )
            .limit(document_limit)
        ).all()
    )
    result = AuctionV2DocumentSyncResult(checked=len(documents))
    if not documents:
        return result

    storage_root = Path(settings.auction_v2_document_storage_dir)
    storage_root.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.auction_v2_document_max_mb * 1024 * 1024
    owned_client = client is None
    http_client = client or httpx.Client(
        timeout=settings.eqazyna_timeout_seconds,
        follow_redirects=True,
    )
    try:
        for document in documents:
            try:
                response = http_client.get(document.source_url)
                response.raise_for_status()
                content = response.content
                if not content:
                    raise ValueError("empty document response")
                if len(content) > max_bytes:
                    raise ValueError(
                        f"document is larger than {settings.auction_v2_document_max_mb} MB"
                    )
                digest = hashlib.sha256(content).hexdigest()
                lot_dir = storage_root / _safe_document_path_part(document.lot_id)
                lot_dir.mkdir(parents=True, exist_ok=True)
                file_path = lot_dir / _auction_document_file_name(document, digest)
                file_path.write_bytes(content)
                document.storage_status = "downloaded"
                document.local_path = str(file_path)
                document.content_sha256 = digest
                document.downloaded_at = datetime.now(UTC)
                document.download_error = None
                result.downloaded += 1
            except Exception as exc:
                document.storage_status = "failed"
                document.download_error = str(exc)[:1000]
                result.errors += 1
        session.flush()
    finally:
        if owned_client:
            http_client.close()
    return result


def _auction_document_file_name(document: AuctionDocument, digest: str) -> str:
    suffix = _auction_document_suffix(document)
    return f"{document.id}-{digest[:16]}{suffix}"


def _auction_document_suffix(document: AuctionDocument) -> str:
    file_type = (document.file_type or "").strip().lower().lstrip(".")
    if file_type and re.fullmatch(r"[a-z0-9]{2,8}", file_type):
        return f".{file_type}"
    path_suffix = Path(urlparse(document.source_url).path).suffix.lower()
    if path_suffix and re.fullmatch(r"\.[a-z0-9]{2,8}", path_suffix):
        return path_suffix
    return ".bin"


def _safe_document_path_part(value: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-")
    return cleaned or "unknown-lot"


def _v2_source_sync_status(source: AuctionSource | None) -> str:
    if source is None:
        return "skipped"
    if source.code == "eqazyna_current_lots":
        return "success"
    if source.parser_kind in {"external_link", "planned_search", "planned_dataset", "planned_market"}:
        return "query_ready"
    if source.quality_status == "manual_required" or source.parser_kind in {
        "manual_or_api",
        "existing_genplan_sources",
        "existing_osm",
    }:
        return "manual_required"
    if source.quality_status == "planned" or str(source.parser_kind).startswith("planned"):
        return "planned"
    if source.quality_status == "reference":
        return "query_ready"
    return source.quality_status or "skipped"


def _v2_run_items_seen(
    source: AuctionSource | None,
    *,
    status: str,
    lots_count: int,
    infrastructure_checked: int,
    runtime_items_seen: dict[str, int] | None = None,
) -> int:
    if source is not None and runtime_items_seen and source.code in runtime_items_seen:
        return runtime_items_seen[source.code] if status in {"success", "warning"} else 0
    if source is not None and source.code == "osm_overpass":
        return infrastructure_checked if status == "success" else 0
    return lots_count if status == "success" else 0


def _gov_kz_run_status(*, items_seen: int, matches: int, errors: list[str]) -> str:
    if not _auction_v2_live_gov_kz_enabled():
        return "manual_required"
    if errors:
        return "warning"
    if items_seen or matches:
        return "success"
    return "query_ready"


def _egkn_run_status(*, checked: int, verified: int, errors: int) -> str:
    if not _auction_v2_live_egkn_enabled():
        return "manual_required"
    if errors and not verified:
        return "warning"
    if checked:
        return "success"
    return "query_ready"


def _osm_run_status(*, infrastructure_checked: int, infrastructure_errors: int) -> str:
    if infrastructure_checked:
        return "success"
    if infrastructure_errors:
        return "warning"
    if not _auction_v2_live_osm_enabled():
        return "manual_required"
    return "manual_required"


def list_auction_v2_lots(
    session: Session,
    filters: AuctionV2Filters,
    *,
    account_id: str | None = None,
    offset: int = 0,
    limit: int = 30,
    prepare_missing: bool = True,
) -> tuple[list[AuctionV2LotPayload], int]:
    if prepare_missing:
        ensure_auction_v2_analyses_for_filters(
            session,
            filters,
            account_id=account_id,
            limit=min(settings.auction_v2_refresh_limit, max(limit, offset + limit)),
        )
    conditions = _auction_filter_conditions(_base_filters_for_lot_scope(filters))
    conditions.extend(_lot_scope_conditions(filters.lot_scope))
    conditions.extend(_search_conditions(filters.search_query))
    conditions.extend(_eqazyna_status_conditions(filters.eqazyna_status))
    if filters.min_score is not None:
        conditions.append(AuctionLotV2Analysis.score >= filters.min_score)
    if filters.risk_level:
        conditions.append(AuctionLotV2Analysis.risk_level == filters.risk_level)
    if filters.confidence_level:
        conditions.append(AuctionLotV2Analysis.confidence_level == filters.confidence_level)
    if filters.recommended_action:
        conditions.append(AuctionLotV2Analysis.recommended_action == filters.recommended_action)
    if filters.deadline_status:
        conditions.extend(_deadline_conditions(filters.deadline_status))

    query = (
        select(AuctionLot, AuctionLotV2Analysis)
        .join(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id)
        .options(selectinload(AuctionLot.documents))
    )
    if filters.geo_status:
        query = query.outerjoin(
            AuctionLotGeoCheck,
            AuctionLotGeoCheck.lot_id == AuctionLot.id,
        )
        conditions.extend(_geo_status_conditions(filters.geo_status))
    if filters.stage and account_id:
        query = query.join(
            AuctionUserLotPipeline,
            and_(
                AuctionUserLotPipeline.lot_id == AuctionLot.id,
                AuctionUserLotPipeline.account_id == account_id,
            ),
        )
        conditions.append(AuctionUserLotPipeline.stage == filters.stage)
    if conditions:
        query = query.where(and_(*conditions))
    query = query.order_by(*_auction_v2_sort_order(filters.sort_by))
    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = session.execute(query.offset(offset).limit(limit)).all()
    lots = [lot for lot, _analysis in rows]
    pipelines = _pipeline_by_lot(session, account_id=account_id, lot_ids=[lot.id for lot in lots])
    metrics_by_lot = auction_lots_metrics(session, lots)
    geo_checks = _get_or_build_geo_checks(session, lots)
    payloads: list[AuctionV2LotPayload] = []
    for lot, analysis in rows:
        metrics = metrics_by_lot[lot.id]
        geo_check = geo_checks[lot.id]
        payloads.append(
            _payload_from_records(
                lot=lot,
                analysis=analysis,
                metrics=metrics,
                geo_check=geo_check,
                pipeline=pipelines.get(lot.id),
            )
        )
    return payloads, int(total)


def _auction_v2_sort_order(sort_by: str | None) -> list[object]:
    sort_value = sort_by if sort_by in AUCTION_V2_SORT_LABELS else "best"
    fallback = [
        AuctionLotV2Analysis.score.desc(),
        AuctionLot.auction_starts_at.is_(None),
        AuctionLot.auction_starts_at,
        AuctionLot.last_seen_at.desc(),
    ]
    if sort_value == "deadline_asc":
        return [
            AuctionLot.auction_starts_at.is_(None),
            AuctionLot.auction_starts_at,
            AuctionLotV2Analysis.score.desc(),
            AuctionLot.last_seen_at.desc(),
        ]
    if sort_value == "price_per_sotka_asc":
        return [
            AuctionLotV2Analysis.price_per_sotka.is_(None),
            AuctionLotV2Analysis.price_per_sotka,
            AuctionLotV2Analysis.score.desc(),
            AuctionLot.auction_starts_at.is_(None),
            AuctionLot.auction_starts_at,
        ]
    if sort_value == "start_price_asc":
        return [
            AuctionLot.start_price_kzt.is_(None),
            AuctionLot.start_price_kzt,
            AuctionLotV2Analysis.score.desc(),
            AuctionLot.auction_starts_at.is_(None),
            AuctionLot.auction_starts_at,
        ]
    if sort_value == "area_desc":
        return [
            AuctionLot.area_ha.is_(None),
            AuctionLot.area_ha.desc(),
            AuctionLotV2Analysis.score.desc(),
            AuctionLot.auction_starts_at.is_(None),
            AuctionLot.auction_starts_at,
        ]
    if sort_value == "new_first":
        return [
            AuctionLot.last_seen_at.desc(),
            AuctionLot.created_at.desc(),
            AuctionLotV2Analysis.score.desc(),
        ]
    return fallback


def list_auction_v2_map_markers(
    session: Session,
    filters: AuctionV2Filters,
    *,
    account_id: str | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    marker_limit = limit or settings.auction_v2_map_limit
    payloads, total = list_auction_v2_lots(
        session,
        filters,
        account_id=account_id,
        offset=0,
        limit=marker_limit,
    )
    markers: list[dict[str, object]] = []
    district_groups: dict[tuple[str, str, str], dict[str, object]] = {}
    without_coordinates = 0
    with_boundaries = 0
    risk_counts = {"low": 0, "medium": 0, "high": 0, "unknown": 0}
    scope_counts = {"active": 0, "future": 0, "archive": 0}
    boundary_by_lot = _cadastre_boundary_by_lot(
        session,
        [item.lot.id for item in payloads],
    )
    context_layers = _egkn_context_layers_for_lots(
        session,
        [item.lot.id for item in payloads],
    )
    for item in payloads:
        lot = item.lot
        analysis = item.analysis
        geo_check = item.geo_check
        latitude = geo_check.latitude
        longitude = geo_check.longitude
        marker_scope = _map_marker_scope(lot, item.deadline_status)
        district_key = (
            (lot.region or "").strip(),
            (lot.district or "").strip(),
            (lot.locality or "").strip(),
        )
        district_label = district_key[1] or district_key[2] or district_key[0] or "Район не указан"
        group = district_groups.setdefault(
            district_key,
            {
                "id": "district:" + "|".join(district_key),
                "label": district_label,
                "region": district_key[0],
                "district": district_key[1],
                "locality": district_key[2],
                "count": 0,
                "mapped": 0,
                "lot_ids": [],
                "latitude_sum": 0.0,
                "longitude_sum": 0.0,
            },
        )
        group["count"] = int(group["count"]) + 1
        group["lot_ids"].append(lot.id)
        if latitude is None or longitude is None:
            without_coordinates += 1
            continue
        group["mapped"] = int(group["mapped"]) + 1
        group["latitude_sum"] = float(group["latitude_sum"]) + float(latitude)
        group["longitude_sum"] = float(group["longitude_sum"]) + float(longitude)
        if marker_scope in scope_counts:
            scope_counts[marker_scope] += 1
        risk_key = analysis.risk_level if analysis.risk_level in risk_counts else "unknown"
        risk_counts[risk_key] += 1
        boundary = boundary_by_lot.get(lot.id)
        if boundary:
            with_boundaries += 1
        markers.append(
            {
                "id": lot.id,
                "url": f"/cabinet/auctions-v2/{lot.id}",
                "title": lot.title,
                "region": lot.region or "",
                "district": lot.district or "",
                "locality": lot.locality or "",
                "cadastre": lot.cadastre_number or "",
                "latitude": round(float(latitude), 7),
                "longitude": round(float(longitude), 7),
                "score": int(analysis.score),
                "risk": analysis.risk_level,
                "risk_label": RISK_LABELS.get(analysis.risk_level, analysis.risk_level),
                "confidence": analysis.confidence_level,
                "confidence_label": item.confidence_label,
                "recommended_action": analysis.recommended_action,
                "action_label": item.action_label,
                "deadline_status": item.deadline_status,
                "deadline_label": item.deadline_label,
                "scope": marker_scope,
                "scope_label": LOT_SCOPE_LABELS.get(marker_scope, marker_scope),
                "stage": item.pipeline.stage if item.pipeline else "",
                "stage_label": item.stage_label or "Не в pipeline",
                "price_text": _money(lot.start_price_kzt),
                "area_text": _area_text(lot.area_ha),
                "price_per_sotka_text": _money(analysis.price_per_sotka),
                "egkn_url": geo_check.egkn_url or "",
                "google_maps_url": geo_check.google_maps_url or "",
                "osm_map_url": item.osm_map_url or "",
                "source_url": lot.source_url or "",
                "boundary": boundary,
            }
        )
    serialized_district_groups: list[dict[str, object]] = []
    for group in district_groups.values():
        mapped = int(group["mapped"])
        latitude_sum = float(group.pop("latitude_sum"))
        longitude_sum = float(group.pop("longitude_sum"))
        group["without_coordinates"] = int(group["count"]) - mapped
        group["latitude"] = round(latitude_sum / mapped, 7) if mapped else None
        group["longitude"] = round(longitude_sum / mapped, 7) if mapped else None
        serialized_district_groups.append(group)
    serialized_district_groups.sort(key=lambda row: (-int(row["count"]), str(row["label"])))
    return {
        "markers": markers,
        "total": int(total),
        "loaded": len(payloads),
        "mapped": len(markers),
        "without_coordinates": without_coordinates,
        "with_boundaries": with_boundaries,
        "egkn_layers": context_layers["features"],
        "egkn_layer_counts": context_layers["counts"],
        "egkn_layer_total": len(context_layers["features"]),
        "limit": marker_limit,
        "risk_counts": risk_counts,
        "scope_counts": scope_counts,
        "district_groups": serialized_district_groups,
    }


def _cadastre_boundary_by_lot(
    session: Session,
    lot_ids: list[str],
) -> dict[str, dict[str, object]]:
    if not lot_ids:
        return {}
    rows = session.execute(
        select(AuctionEvidence.lot_id, AuctionEvidence.raw_payload_json)
        .where(
            AuctionEvidence.lot_id.in_(lot_ids),
            AuctionEvidence.evidence_type == "cadastre_boundary",
            AuctionEvidence.status == "found",
        )
        .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
    ).all()
    boundaries: dict[str, dict[str, object]] = {}
    for lot_id, raw_payload_json in rows:
        if lot_id in boundaries:
            continue
        geometry = _safe_geojson_geometry(raw_payload_json)
        if geometry:
            boundaries[lot_id] = geometry
    return boundaries


def _safe_geojson_geometry(raw_payload_json: str | None) -> dict[str, object] | None:
    if not raw_payload_json:
        return None
    try:
        payload = json.loads(raw_payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    geometry = payload.get("geometry_geojson")
    if not isinstance(geometry, dict):
        return None
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type not in {"Polygon", "MultiPolygon"} or not isinstance(coordinates, list):
        return None
    return {
        "type": geometry_type,
        "coordinates": coordinates,
        "source": "ЕГКН",
    }


def _egkn_context_layers_for_lots(
    session: Session,
    lot_ids: list[str],
) -> dict[str, object]:
    counts = {layer["code"]: 0 for layer in EGKN_CONTEXT_LAYERS}
    if not lot_ids:
        return {"features": [], "counts": counts}
    rows = session.execute(
        select(AuctionEvidence.lot_id, AuctionEvidence.raw_payload_json)
        .where(
            AuctionEvidence.lot_id.in_(lot_ids),
            AuctionEvidence.evidence_type == "egkn_context_layer",
            AuctionEvidence.status == "found",
        )
        .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
    ).all()
    features: list[dict[str, object]] = []
    seen: set[str] = set()
    lot_id_set = set(lot_ids)
    for lot_id, raw_payload_json in rows:
        if lot_id not in lot_id_set or not raw_payload_json:
            continue
        try:
            payload = json.loads(raw_payload_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        layer_code = str(payload.get("layer_code") or "")
        layer_label = str(payload.get("label") or layer_code or "ЕГКН")
        layer_kind = str(payload.get("kind") or "context")
        payload_features = payload.get("features")
        if not isinstance(payload_features, list):
            continue
        for feature in payload_features:
            if not isinstance(feature, dict):
                continue
            geometry = feature.get("geometry")
            if not _is_context_geometry(geometry):
                continue
            feature_id = str(feature.get("id") or "")
            feature_label = str(feature.get("label") or layer_label)
            key = f"{layer_code}:{feature_id}:{json.dumps(geometry, sort_keys=True)[:240]}"
            if key in seen:
                continue
            seen.add(key)
            counts[layer_code] = counts.get(layer_code, 0) + 1
            features.append(
                {
                    "lot_id": lot_id,
                    "layer_code": layer_code,
                    "layer_label": layer_label,
                    "kind": layer_kind,
                    "feature_id": feature_id,
                    "feature_label": feature_label,
                    "geometry": geometry,
                    "source": "ЕГКН",
                }
            )
    return {"features": features[:300], "counts": counts}


def _is_context_geometry(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    geometry_type = value.get("type")
    coordinates = value.get("coordinates")
    return geometry_type in {
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
    } and isinstance(coordinates, list)


def get_auction_v2_payload(
    session: Session,
    lot_id: str,
    *,
    account_id: str | None = None,
    force: bool = False,
) -> AuctionV2LotPayload | None:
    lot = session.scalar(
        select(AuctionLot)
        .options(selectinload(AuctionLot.documents))
        .where(AuctionLot.id == lot_id)
    )
    if lot is None:
        return None
    analysis = build_auction_v2_analysis(session, lot, force=force)
    metrics = auction_lot_metrics(session, lot)
    geo_check = _get_or_build_geo_check(session, lot)
    pipeline = get_auction_v2_pipeline(session, account_id, lot.id) if account_id else None
    return _payload_from_records(
        lot=lot,
        analysis=analysis,
        metrics=metrics,
        geo_check=geo_check,
        pipeline=pipeline,
    )


def build_auction_v2_dossier_text(
    session: Session,
    lot_id: str,
    *,
    account_id: str | None = None,
) -> str | None:
    payload = get_auction_v2_payload(
        session,
        lot_id,
        account_id=account_id,
        force=True,
    )
    if payload is None:
        return None
    lot = payload.lot
    analysis = payload.analysis
    evidence = list(
        session.scalars(
            select(AuctionEvidence)
            .where(AuctionEvidence.lot_id == lot.id)
            .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
            .limit(80)
        ).all()
    )
    market_comparables = list_auction_v2_market_comparables(session, lot.id)
    lines = [
        f"ZHERTAP AUCTIONS V2 DOSSIER · {datetime.now(UTC).strftime('%d.%m.%Y %H:%M')}",
        "",
        "ГРАНИЦА СИСТЕМЫ",
        "Zhertap доводит только до решения перед покупкой. Заявка, ЭЦП, гарантийный взнос и участие в торгах выполняются пользователем только на официальных порталах E-Qazyna/eGov.",
        "",
        "ЛОТ",
        f"Номер: {lot.auction_number or lot.source_lot_id or lot.id}",
        f"Название: {lot.title}",
        f"Регион: {_text(lot.region)} / {_text(lot.district)} / {_text(lot.locality)}",
        f"Кадастр: {_text(lot.cadastre_number)}",
        f"Площадь: {_area_text(lot.area_ha)}",
        f"Назначение: {_text(lot.functional_purpose_level2 or lot.purpose)}",
        f"Право: {_text(lot.land_rights)}",
        f"Стартовая цена: {_money(lot.start_price_kzt)}",
        f"Гарантийный взнос: {_money(lot.guarantee_kzt)}",
        f"Дата торгов: {_datetime_text(lot.auction_starts_at)}",
        f"Продавец: {_text(lot.seller_name)}",
        f"БИН продавца: {_text(lot.seller_bin)}",
        f"E-Qazyna: {_text(lot.source_url)}",
        "",
        "РЕШЕНИЕ",
        f"Индекс преимущества: {analysis.score}/100",
        f"Риск: {RISK_LABELS.get(analysis.risk_level, analysis.risk_level)}",
        f"Уверенность: {CONFIDENCE_LABELS.get(analysis.confidence_level, analysis.confidence_level)}",
        f"Рекомендация: {ACTION_LABELS.get(analysis.recommended_action, analysis.recommended_action)}",
        f"Вывод: {analysis.summary}",
        "",
        "ЭКОНОМИКА СДЕЛКИ",
        f"Стратегия: {payload.investment_case['strategy_label']}",
        f"Плановая покупка: {_money(payload.investment_case['acquisition_cost_kzt'])}",
        f"Полный бюджет: {_money(payload.investment_case['all_in_cost_kzt'])}",
        f"Ожидаемая цена выхода: {_money(payload.investment_case['expected_exit_value_kzt'])}",
        f"Ожидаемая прибыль: {_money(payload.investment_case['expected_profit_kzt'])}",
        f"ROI: {_percent(payload.investment_case['roi_percent'])}",
        f"До участия — гарантийный взнос: {_money(payload.cost_estimate['cash_before_auction_kzt'])}",
        f"После победы: {_money(payload.cost_estimate['cash_after_win_kzt'])}",
        f"Вывод по экономике: {payload.investment_case['verdict']}",
        "",
        "ПОЛЕВОЙ ОСМОТР",
        f"Статус: {payload.field_inspection['status_label']}",
        f"Проверено на месте: {payload.field_inspection['checked_count']} из {payload.field_inspection['total_checks']}",
        f"Вывод: {_text(payload.field_inspection['data'].get('conclusion'))}",
        "",
        "КОМНАТА СДЕЛКИ",
        *(
            [
                f"{row['created_at'][:16].replace('T', ' ')} · {row['kind_label']}: {row['body']}"
                for row in payload.deal_room["rows"][:20]
            ]
            or ["Записей пока нет"]
        ),
        "",
        "РАБОЧИЙ ПРОЦЕСС ДО УЧАСТИЯ",
        *_dossier_workflow_lines(payload.buyer_workflow),
        "",
        "ЦЕНА И ЛИМИТЫ",
        f"Цена за сотку: {_money(analysis.price_per_sotka)}",
        f"Районный ориентир за сотку: {_money(analysis.district_average_price_per_sotka)}",
        f"Отклонение от района: {_percent(analysis.district_difference_percent)}",
        f"Лимит осторожно: {_money(analysis.max_bid_conservative_kzt)}",
        f"Лимит рынок: {_money(analysis.max_bid_market_kzt)}",
        f"Лимит агрессивно: {_money(analysis.max_bid_aggressive_kzt)}",
        "",
        "РЫНОЧНЫЕ АНАЛОГИ",
        *_dossier_market_comparable_lines(market_comparables),
        "",
        "ГЕО",
        f"Кадастр статус: {payload.geo_check.cadastre_status}",
        f"Координаты статус: {payload.geo_check.coordinate_status}",
        f"Широта/долгота: {_coordinate_text(payload.geo_check.latitude, payload.geo_check.longitude)}",
        f"ЕГКН: {_text(payload.geo_check.egkn_url)}",
        f"Карта: {_text(payload.geo_check.google_maps_url)}",
        f"OSM статус: {_text(payload.geo_check.osm_status)}",
        f"OSM проверен: {_datetime_text(payload.geo_check.osm_checked_at)}",
        f"Дорога: {_format_distance_m(payload.geo_check.road_distance_m)}",
        f"Энергия/ЛЭП: {_format_distance_m(payload.geo_check.power_distance_m)}",
        f"Вода: {_format_distance_m(payload.geo_check.water_distance_m)}",
        f"Открытая вода: {_format_distance_m(payload.geo_check.open_water_distance_m)}",
        f"Кладбище: {_format_distance_m(payload.geo_check.cemetery_distance_m)}",
        f"Ближайший объект: {_format_distance_m(payload.geo_check.object_distance_m)}",
        f"Заметка: {_text(payload.geo_check.notes)}",
        "",
        "ЧТО ПРОВЕРИТЬ ДО ОФИЦИАЛЬНОЙ ЗАЯВКИ",
        *_dossier_check_lines(payload.readiness),
        "",
        "ПЕРЕД ПЕРЕХОДОМ НА E-QAZYNA",
        *_dossier_check_lines(payload.official_readiness),
        "",
        "РИСКИ",
        *_dossier_risk_lines(payload.risk_flags),
        "",
        "ИСТОЧНИКИ",
        *_dossier_source_lines(payload.source_statuses),
        "",
        "ПРИЛОЖЕННЫЕ ДОКУМЕНТЫ",
        *_dossier_document_lines(lot.documents),
        "",
        "СЛЕДЫ ПРОВЕРОК",
        *_dossier_evidence_lines(evidence),
    ]
    return "\n".join(lines).strip() + "\n"


def build_auction_v2_analysis(
    session: Session,
    lot: AuctionLot,
    *,
    force: bool = False,
) -> AuctionLotV2Analysis:
    seed_auction_v2_sources(session)
    analysis = session.scalar(
        select(AuctionLotV2Analysis).where(AuctionLotV2Analysis.lot_id == lot.id)
    )
    if analysis is not None and not force:
        cutoff = datetime.now(UTC) - timedelta(minutes=settings.auction_v2_analysis_ttl_minutes)
        if _aware(analysis.checked_at) >= cutoff:
            return analysis

    metrics = auction_lot_metrics(session, lot)
    geo_metrics = auction_lot_geo_metrics(lot)
    geo_check = _get_or_build_geo_check(session, lot)
    market_stats = _market_comparable_stats(session, lot)
    source_statuses = _source_statuses(session, lot, metrics, geo_check, market_stats)
    risk_flags = _risk_flags(lot, metrics, geo_check, market_stats)
    score = _v2_score(lot, metrics, geo_check, market_stats, risk_flags)
    confidence_level = _confidence_level(lot, metrics, geo_check, market_stats)
    risk_level = _risk_level(score, risk_flags)
    recommended_action = _recommended_action(
        score=score,
        risk_level=risk_level,
        confidence_level=confidence_level,
        risk_flags=risk_flags,
    )
    readiness = _readiness(lot, metrics, geo_check, source_statuses)
    bid_limits = _bid_limits(
        lot,
        metrics,
        market_stats,
        score=score,
        risk_level=risk_level,
    )

    if analysis is None:
        analysis = AuctionLotV2Analysis(lot_id=lot.id)
        session.add(analysis)

    now = datetime.now(UTC)
    analysis.score = score
    analysis.risk_level = risk_level
    analysis.confidence_level = confidence_level
    analysis.recommended_action = recommended_action
    analysis.summary = _summary(
        score,
        risk_level,
        confidence_level,
        metrics,
        geo_check,
        market_stats,
    )
    analysis.readiness_json = json.dumps(readiness, ensure_ascii=False)
    analysis.risk_flags_json = json.dumps(risk_flags, ensure_ascii=False)
    analysis.source_status_json = json.dumps(source_statuses, ensure_ascii=False)
    analysis.max_bid_conservative_kzt = bid_limits["conservative"]
    analysis.max_bid_market_kzt = bid_limits["market"]
    analysis.max_bid_aggressive_kzt = bid_limits["aggressive"]
    analysis.price_per_sotka = metrics.price_per_sotka
    analysis.district_average_price_per_sotka = metrics.district_average_price_per_sotka
    analysis.district_difference_percent = metrics.district_difference_percent
    analysis.checked_at = now
    analysis.updated_at = now

    _sync_builtin_evidence(session, lot, source_statuses)
    sources_by_code = {source.code: source for source in session.scalars(select(AuctionSource)).all()}
    _sync_external_query_evidence(session, lot, sources_by_code)
    if geo_metrics.latitude is not None and geo_check.latitude is None:
        geo_check.latitude = geo_metrics.latitude
        geo_check.longitude = geo_metrics.longitude
    session.flush()
    return analysis


def get_auction_v2_pipeline(
    session: Session,
    account_id: str | None,
    lot_id: str,
    *,
    create: bool = False,
) -> AuctionUserLotPipeline | None:
    if not account_id:
        return None
    pipeline = session.scalar(
        select(AuctionUserLotPipeline).where(
            AuctionUserLotPipeline.account_id == account_id,
            AuctionUserLotPipeline.lot_id == lot_id,
        )
    )
    if pipeline is None and create:
        pipeline = AuctionUserLotPipeline(account_id=account_id, lot_id=lot_id)
        session.add(pipeline)
        session.flush()
    return pipeline


def update_auction_v2_pipeline(
    session: Session,
    *,
    account_id: str,
    lot_id: str,
    stage: str,
    max_bid_kzt: float | None,
    notes: str | None,
    reminder_at: datetime | None = None,
    pinned: bool = False,
    costs: dict[str, float | int | str | None] | None = None,
    investment: dict[str, float | int | str | None] | None = None,
    inspection: dict[str, str | bool | None] | None = None,
) -> AuctionUserLotPipeline:
    if stage not in {value for value, _label in PIPELINE_STAGES}:
        raise ValueError("unknown pipeline stage")
    pipeline = get_auction_v2_pipeline(session, account_id, lot_id, create=True)
    if pipeline is None:
        raise ValueError("account is required")
    now = datetime.now(UTC)
    previous_stage = pipeline.stage
    pipeline.stage = stage
    pipeline.max_bid_kzt = max_bid_kzt
    if max_bid_kzt is not None and max_bid_kzt < 0:
        raise ValueError("Личный лимит не может быть отрицательным")
    if costs is not None:
        pipeline.costs_json = json.dumps(_normalize_pipeline_costs(costs), ensure_ascii=False)
    if investment is not None:
        pipeline.investment_json = json.dumps(
            _normalize_investment_inputs(investment), ensure_ascii=False
        )
    if inspection is not None:
        pipeline.inspection_json = json.dumps(
            _normalize_field_inspection(inspection), ensure_ascii=False
        )
    pipeline.notes = (notes or "").strip() or None
    pipeline.reminder_at = _aware(reminder_at)
    pipeline.pinned = pinned
    pipeline.updated_at = now
    participation_stages = {
        "decided_to_participate",
        "application_preparing",
        "application_submitted",
        "guarantee_paid",
        "admitted_to_auction",
        "auction_completed",
        "won",
        "lost",
        "contract_signed",
        "rights_registered",
        "development",
        "listed_for_sale",
        "sold",
    }
    if stage in participation_stages | {"skipped"} and previous_stage != stage:
        pipeline.decided_at = now
        pipeline.decision = "participate" if stage in participation_stages else "skip"
    elif stage not in participation_stages | {"skipped"}:
        pipeline.decision = None
        pipeline.decided_at = None
    session.flush()
    return pipeline


def auction_v2_calendar_payload(
    session: Session,
    *,
    account_id: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Build the entrepreneur's action calendar from tracked auction lots."""
    current = _aware(now) or datetime.now(UTC)
    pipelines = list(
        session.scalars(
            select(AuctionUserLotPipeline)
            .where(AuctionUserLotPipeline.account_id == account_id)
            .options(selectinload(AuctionUserLotPipeline.lot))
            .order_by(AuctionUserLotPipeline.updated_at.desc())
        ).all()
    )
    events: list[dict[str, object]] = []

    def append_event(
        *,
        pipeline: AuctionUserLotPipeline,
        event_at: datetime | None,
        kind: str,
        title: str,
        detail: str,
    ) -> None:
        aware_at = _aware(event_at)
        lot = pipeline.lot
        if aware_at is None or lot is None:
            return
        seconds = (aware_at - current).total_seconds()
        if seconds < 0:
            status = "overdue"
        elif aware_at.date() == current.date():
            status = "today"
        elif seconds <= 7 * 86400:
            status = "soon"
        else:
            status = "upcoming"
        events.append(
            {
                "at": aware_at,
                "kind": kind,
                "status": status,
                "title": title,
                "detail": detail,
                "lot": lot,
                "pipeline": pipeline,
                "stage_label": _stage_label(pipeline.stage) or "В работе",
                "url": f"/cabinet/auctions-v2/{lot.id}",
            }
        )

    for pipeline in pipelines:
        lot = pipeline.lot
        if lot is None or pipeline.stage in {"sold", "lost", "skipped", "archived"}:
            continue
        append_event(
            pipeline=pipeline,
            event_at=pipeline.reminder_at,
            kind="reminder",
            title="Контрольная дата",
            detail="Личный срок по решению, документам или следующему действию.",
        )
        append_event(
            pipeline=pipeline,
            event_at=lot.auction_starts_at,
            kind="auction",
            title="Начало торгов",
            detail="Проверьте допуск, лимит ставки и переход на официальный портал.",
        )
        inspection = _load_pipeline_json(pipeline, "inspection_json")
        planned_at = inspection.get("planned_at")
        if planned_at:
            try:
                parsed_inspection_at = datetime.fromisoformat(str(planned_at))
            except ValueError:
                parsed_inspection_at = None
            if parsed_inspection_at is not None and parsed_inspection_at.tzinfo is None:
                parsed_inspection_at = parsed_inspection_at.replace(
                    tzinfo=timezone(timedelta(hours=5))
                )
            append_event(
                pipeline=pipeline,
                event_at=parsed_inspection_at,
                kind="inspection",
                title="Выезд на участок",
                detail="Зафиксируйте подъезд, сети, рельеф, окружение и границы.",
            )

    events.sort(key=lambda row: row["at"])
    overdue = [row for row in events if row["status"] == "overdue"]
    upcoming = [row for row in events if row["status"] != "overdue"]
    return {
        "events": events,
        "overdue": overdue,
        "upcoming": upcoming,
        "totals": {
            "tracked_lots": len(pipelines),
            "overdue": len(overdue),
            "today": sum(row["status"] == "today" for row in events),
            "next_7_days": sum(
                0 <= (row["at"] - current).total_seconds() <= 7 * 86400
                for row in events
            ),
        },
    }


PIPELINE_COST_FIELDS: tuple[tuple[str, str], ...] = (
    ("road", "Подъезд и земляные работы"),
    ("utilities", "Подключение сетей"),
    ("project", "Проектирование и изыскания"),
    ("registration", "Оформление и регистрация"),
    ("taxes", "Налоги и сборы"),
    ("other", "Прочие расходы"),
)

INVESTMENT_NUMBER_FIELDS = {
    "planned_purchase_price_kzt",
    "expected_exit_value_kzt",
    "expected_monthly_income_kzt",
    "holding_months",
    "financing_cost_kzt",
    "contingency_percent",
}

INSPECTION_BOOLEAN_FIELDS = {
    "access_ok",
    "power_visible",
    "water_visible",
    "flat_terrain",
    "no_flood_signs",
    "boundaries_visible",
}


def _normalize_pipeline_costs(value: dict[str, float | int | str | None]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for key, _label in PIPELINE_COST_FIELDS:
        raw = value.get(key)
        if raw in (None, ""):
            continue
        try:
            amount = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Расходы должны быть числами") from exc
        if amount < 0:
            raise ValueError("Расходы не могут быть отрицательными")
        normalized[key] = round(amount, 2)
    return normalized


def _pipeline_costs(pipeline: AuctionUserLotPipeline | None) -> dict[str, float]:
    if pipeline is None or not pipeline.costs_json:
        return {}
    try:
        raw = json.loads(pipeline.costs_json)
    except json.JSONDecodeError:
        return {}
    return _normalize_pipeline_costs(raw) if isinstance(raw, dict) else {}


def _load_pipeline_json(pipeline: AuctionUserLotPipeline | None, attribute: str) -> dict:
    if pipeline is None:
        return {}
    value = getattr(pipeline, attribute, None)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_investment_inputs(
    value: dict[str, float | int | str | None],
) -> dict[str, float | str]:
    allowed_strategies = {key for key, _label in INVESTMENT_STRATEGIES}
    strategy = str(value.get("strategy") or "undecided").strip()
    if strategy not in allowed_strategies:
        raise ValueError("Неизвестная инвестиционная стратегия")
    normalized: dict[str, float | str] = {"strategy": strategy}
    for key in INVESTMENT_NUMBER_FIELDS:
        raw = value.get(key)
        if raw in (None, ""):
            continue
        try:
            amount = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Параметры инвестиционного сценария должны быть числами") from exc
        if amount < 0:
            raise ValueError("Параметры инвестиционного сценария не могут быть отрицательными")
        if key == "contingency_percent" and amount > 100:
            raise ValueError("Резерв не может превышать 100%")
        normalized[key] = round(amount, 2)
    return normalized


def _normalize_field_inspection(
    value: dict[str, str | bool | None],
) -> dict[str, str | bool]:
    statuses = {key for key, _label in FIELD_INSPECTION_OPTIONS}
    status = str(value.get("status") or "not_planned").strip()
    if status not in statuses:
        raise ValueError("Неизвестный статус выезда")
    normalized: dict[str, str | bool] = {"status": status}
    for key in INSPECTION_BOOLEAN_FIELDS:
        normalized[key] = value.get(key) in {True, "true", "1", "on", "yes"}
    for key in ("planned_at", "inspected_at", "road_note", "utilities_note", "terrain_note", "surroundings_note", "conclusion"):
        text = str(value.get(key) or "").strip()
        if text:
            normalized[key] = text[:2000]
    return normalized


def _investment_case(
    lot: AuctionLot,
    pipeline: AuctionUserLotPipeline | None,
    *,
    known_extra_costs_kzt: float,
) -> dict[str, object]:
    raw = _load_pipeline_json(pipeline, "investment_json")
    inputs = _normalize_investment_inputs(raw) if raw else {"strategy": "undecided"}
    strategy = str(inputs.get("strategy") or "undecided")
    strategy_labels = dict(INVESTMENT_STRATEGIES)
    acquisition = float(
        inputs.get("planned_purchase_price_kzt")
        or (pipeline.max_bid_kzt if pipeline and pipeline.max_bid_kzt is not None else 0)
        or lot.start_price_kzt
        or 0
    )
    financing = float(inputs.get("financing_cost_kzt") or 0)
    reserve_percent = float(inputs.get("contingency_percent") or 0)
    reserve = round(known_extra_costs_kzt * reserve_percent / 100, 2)
    all_in = round(acquisition + known_extra_costs_kzt + financing + reserve, 2)
    exit_value = float(inputs.get("expected_exit_value_kzt") or 0)
    monthly_income = float(inputs.get("expected_monthly_income_kzt") or 0)
    profit = round(exit_value - all_in, 2) if exit_value and all_in else None
    roi = round(profit / all_in * 100, 1) if profit is not None and all_in else None
    margin = round(profit / exit_value * 100, 1) if profit is not None and exit_value else None
    payback_months = round(all_in / monthly_income, 1) if monthly_income and all_in else None
    holding_months = inputs.get("holding_months")
    if profit is None:
        verdict = "Нужно заполнить цену выхода"
        verdict_status = "incomplete"
    elif profit <= 0:
        verdict = "Экономика отрицательная"
        verdict_status = "negative"
    elif roi is not None and roi < 15:
        verdict = "Доходность ниже запаса риска"
        verdict_status = "warning"
    else:
        verdict = "Экономика требует проверки рисков" if roi is not None and roi < 25 else "Сценарий выглядит привлекательным"
        verdict_status = "positive"
    return {
        "inputs": inputs,
        "strategy": strategy,
        "strategy_label": strategy_labels.get(strategy, strategy),
        "acquisition_cost_kzt": acquisition or None,
        "known_extra_costs_kzt": known_extra_costs_kzt or None,
        "financing_cost_kzt": financing or None,
        "reserve_percent": reserve_percent,
        "reserve_kzt": reserve or None,
        "all_in_cost_kzt": all_in or None,
        "expected_exit_value_kzt": exit_value or None,
        "expected_monthly_income_kzt": monthly_income or None,
        "holding_months": holding_months,
        "expected_profit_kzt": profit,
        "roi_percent": roi,
        "margin_percent": margin,
        "payback_months": payback_months,
        "verdict": verdict,
        "verdict_status": verdict_status,
        "has_user_inputs": len(inputs) > 1 or strategy != "undecided",
    }


def _field_inspection(pipeline: AuctionUserLotPipeline | None) -> dict[str, object]:
    raw = _load_pipeline_json(pipeline, "inspection_json")
    data = _normalize_field_inspection(raw) if raw else {"status": "not_planned"}
    status = str(data.get("status") or "not_planned")
    checked = sum(bool(data.get(key)) for key in INSPECTION_BOOLEAN_FIELDS)
    return {
        "data": data,
        "status": status,
        "status_label": dict(FIELD_INSPECTION_OPTIONS).get(status, status),
        "checked_count": checked,
        "total_checks": len(INSPECTION_BOOLEAN_FIELDS),
        "completed": status == "completed",
    }


def _pipeline_activity(pipeline: AuctionUserLotPipeline | None) -> list[dict[str, str]]:
    if pipeline is None or not pipeline.activity_json:
        return []
    try:
        raw = json.loads(pipeline.activity_json)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    allowed = {value for value, _label in AUCTION_ACTIVITY_TYPES}
    labels = dict(AUCTION_ACTIVITY_TYPES)
    rows: list[dict[str, str]] = []
    for item in raw[-100:]:
        if not isinstance(item, dict):
            continue
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        kind = str(item.get("kind") or "note")
        if kind not in allowed:
            kind = "note"
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "kind": kind,
                "kind_label": labels[kind],
                "body": body[:3000],
                "created_at": str(item.get("created_at") or ""),
                "actor_account_id": str(item.get("actor_account_id") or ""),
                "actor_label": str(item.get("actor_label") or "")[:80],
            }
        )
    return list(reversed(rows))


def add_auction_v2_activity(
    session: Session,
    *,
    account_id: str,
    lot_id: str,
    kind: str,
    body: str,
    actor_account_id: str | None = None,
    actor_label: str | None = None,
) -> AuctionUserLotPipeline:
    allowed = {value for value, _label in AUCTION_ACTIVITY_TYPES}
    if kind not in allowed:
        raise ValueError("Неизвестный тип записи")
    clean_body = body.strip()
    if not clean_body:
        raise ValueError("Введите текст записи")
    if len(clean_body) > 3000:
        raise ValueError("Запись не должна превышать 3000 символов")
    pipeline = get_auction_v2_pipeline(session, account_id, lot_id, create=True)
    if pipeline is None:
        raise ValueError("account is required")
    existing = list(reversed(_pipeline_activity(pipeline)))
    existing.append(
        {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "body": clean_body,
            "created_at": datetime.now(UTC).isoformat(),
            "actor_account_id": actor_account_id or account_id,
            "actor_label": (actor_label or "").strip()[:80],
        }
    )
    pipeline.activity_json = json.dumps(existing[-100:], ensure_ascii=False)
    pipeline.updated_at = datetime.now(UTC)
    session.flush()
    return pipeline


def _deal_room(pipeline: AuctionUserLotPipeline | None) -> dict[str, object]:
    rows = _pipeline_activity(pipeline)
    return {
        "rows": rows,
        "count": len(rows),
        "types": [
            {"value": value, "label": label}
            for value, label in AUCTION_ACTIVITY_TYPES
        ],
        "has_expert_requests": any(row["kind"] == "expert_request" for row in rows),
    }


def auction_v2_portfolio_payload(
    session: Session,
    *,
    account_id: str,
) -> dict[str, object]:
    pipelines = list(
        session.scalars(
            select(AuctionUserLotPipeline)
            .where(AuctionUserLotPipeline.account_id == account_id)
            .order_by(
                AuctionUserLotPipeline.pinned.desc(),
                AuctionUserLotPipeline.updated_at.desc(),
            )
        ).all()
    )
    rows: list[dict[str, object]] = []
    stage_counts: dict[str, int] = {}
    total_budget = 0.0
    blocked_capital = 0.0
    expected_profit = 0.0
    profit_count = 0
    for pipeline in pipelines:
        payload = get_auction_v2_payload(
            session,
            pipeline.lot_id,
            account_id=account_id,
        )
        if payload is None:
            continue
        case = payload.investment_case
        budget = float(case.get("all_in_cost_kzt") or 0)
        profit = case.get("expected_profit_kzt")
        guarantee = float(payload.cost_estimate.get("cash_before_auction_kzt") or 0)
        total_budget += budget
        blocked_capital += guarantee
        if profit is not None:
            expected_profit += float(profit)
            profit_count += 1
        stage_counts[pipeline.stage] = stage_counts.get(pipeline.stage, 0) + 1
        rows.append(
            {
                "payload": payload,
                "lot": payload.lot,
                "pipeline": pipeline,
                "investment": case,
                "inspection": payload.field_inspection,
                "stage_label": _stage_label(pipeline.stage),
            }
        )
    return {
        "rows": rows,
        "totals": {
            "tracked": len(rows),
            "total_budget_kzt": round(total_budget, 2) or None,
            "blocked_capital_kzt": round(blocked_capital, 2) or None,
            "expected_profit_kzt": round(expected_profit, 2) if profit_count else None,
            "with_economics": profit_count,
            "won": sum(stage_counts.get(stage, 0) for stage in {"won", "contract_signed", "rights_registered", "development", "listed_for_sale", "sold"}),
            "closed": stage_counts.get("sold", 0),
        },
        "stages": [
            {"value": value, "label": label, "count": stage_counts.get(value, 0)}
            for value, label in PIPELINE_STAGES
            if stage_counts.get(value, 0)
        ],
    }


def _cost_estimate(
    lot: AuctionLot,
    pipeline: AuctionUserLotPipeline | None,
) -> dict[str, object]:
    costs = _pipeline_costs(pipeline)
    known_extra = round(sum(costs.values()), 2)
    start_price = float(lot.start_price_kzt or 0)
    guarantee = float(lot.guarantee_kzt or 0)
    unknown = [label for key, label in PIPELINE_COST_FIELDS if key not in costs]
    acquisition = float(
        (_load_pipeline_json(pipeline, "investment_json").get("planned_purchase_price_kzt"))
        or (pipeline.max_bid_kzt if pipeline and pipeline.max_bid_kzt is not None else 0)
        or start_price
    )
    return {
        "start_price_kzt": start_price or None,
        "guarantee_kzt": guarantee or None,
        "known_extra_costs_kzt": known_extra or None,
        "planned_purchase_price_kzt": acquisition or None,
        "known_total_cost_kzt": round(acquisition + known_extra, 2) or None,
        "cash_before_auction_kzt": guarantee or None,
        "cash_after_win_kzt": round(max(acquisition - guarantee, 0) + known_extra, 2) or None,
        "items": [
            {
                "code": key,
                "label": label,
                "value_kzt": costs.get(key),
                "status": "entered" if key in costs else "unknown",
            }
            for key, label in PIPELINE_COST_FIELDS
        ],
        "unknown_items": unknown,
        "has_user_inputs": bool(costs),
        "disclaimer": "Гарантийный взнос показан как временно заблокированный капитал и не прибавляется к стоимости земли. Неизвестные расходы нужно подтвердить до участия.",
    }


def list_auction_v2_market_comparables(
    session: Session,
    lot_id: str,
) -> list[AuctionMarketComparable]:
    return list(
        session.scalars(
            select(AuctionMarketComparable)
            .where(AuctionMarketComparable.lot_id == lot_id)
            .order_by(
                AuctionMarketComparable.observed_at.desc(),
                AuctionMarketComparable.id.desc(),
            )
        ).all()
    )


def create_auction_v2_market_comparable(
    session: Session,
    *,
    lot_id: str,
    source_name: str,
    source_url: str,
    title: str,
    area_ha: float,
    price_kzt: float,
    listing_status: str = "active",
) -> AuctionMarketComparable:
    lot = session.get(AuctionLot, lot_id)
    if lot is None:
        raise ValueError("Лот не найден")
    source_name = (source_name or "").strip()[:80]
    source_url = (source_url or "").strip()
    title = (title or "").strip()[:320] or "Рыночный аналог"
    listing_status = (listing_status or "active").strip()[:32] or "active"
    if not source_name:
        raise ValueError("Укажите источник аналога")
    if not source_url:
        raise ValueError("Укажите ссылку на аналог")
    if area_ha <= 0:
        raise ValueError("Площадь аналога должна быть больше нуля")
    if price_kzt <= 0:
        raise ValueError("Цена аналога должна быть больше нуля")
    comparable = AuctionMarketComparable(
        lot_id=lot.id,
        source_name=source_name,
        source_url=source_url,
        title=title,
        region=lot.region,
        district=lot.district,
        locality=lot.locality,
        area_ha=area_ha,
        price_kzt=price_kzt,
        price_per_sotka=price_kzt / (area_ha * 100),
        listing_status=listing_status,
        raw_payload_json=json.dumps(
            {
                "mode": "manual_auction_v2_market_comparable",
                "lot_id": lot.id,
                "source_name": source_name,
            },
            ensure_ascii=False,
        ),
        observed_at=datetime.now(UTC),
    )
    session.add(comparable)
    session.flush()
    build_auction_v2_analysis(session, lot, force=True)
    return comparable


def ensure_default_auction_v2_watchlist(
    session: Session,
    account_id: str,
) -> AuctionWatchlist:
    existing = session.scalar(
        select(AuctionWatchlist)
        .where(AuctionWatchlist.account_id == account_id)
        .order_by(AuctionWatchlist.id)
        .limit(1)
    )
    if existing is not None:
        return existing
    watchlist = AuctionWatchlist(
        account_id=account_id,
        name="Сильные лоты 70+",
        min_score=70,
        notify_channels_json=json.dumps(["web", "telegram"], ensure_ascii=False),
        active=True,
    )
    session.add(watchlist)
    session.flush()
    return watchlist


def create_auction_v2_watchlist(
    session: Session,
    *,
    account_id: str,
    name: str,
    filters: AuctionV2Filters,
) -> AuctionWatchlist:
    watchlist = AuctionWatchlist(
        account_id=account_id,
        name=(name.strip() or "Мониторинг v2")[:160],
        region=filters.base.region,
        district=filters.base.district,
        locality=filters.base.locality,
        purpose_query=filters.base.purpose_query,
        lot_scope=filters.lot_scope or "active",
        eqazyna_status=filters.eqazyna_status,
        min_score=filters.min_score,
        min_price_kzt=filters.base.min_price_kzt,
        max_price_kzt=filters.base.max_price_kzt,
        min_area_ha=filters.base.min_area_ha,
        max_area_ha=filters.base.max_area_ha,
        risk_level=filters.risk_level,
        confidence_level=filters.confidence_level,
        stage=filters.stage,
        deadline_status=filters.deadline_status,
        geo_status=filters.geo_status,
        notify_channels_json=json.dumps(["web", "telegram"], ensure_ascii=False),
        active=True,
    )
    session.add(watchlist)
    session.flush()
    return watchlist


def set_auction_v2_watchlist_active(
    session: Session,
    *,
    account_id: str,
    watchlist_id: int,
    active: bool,
) -> AuctionWatchlist | None:
    watchlist = session.scalar(
        select(AuctionWatchlist).where(
            AuctionWatchlist.id == watchlist_id,
            AuctionWatchlist.account_id == account_id,
        )
    )
    if watchlist is None:
        return None
    watchlist.active = active
    watchlist.updated_at = datetime.now(UTC)
    session.flush()
    return watchlist


def list_auction_v2_watchlists(
    session: Session,
    account_id: str,
) -> list[AuctionV2WatchlistPayload]:
    watchlists = list(
        session.scalars(
            select(AuctionWatchlist)
            .where(AuctionWatchlist.account_id == account_id)
            .order_by(AuctionWatchlist.active.desc(), AuctionWatchlist.created_at.desc())
        ).all()
    )
    payloads: list[AuctionV2WatchlistPayload] = []
    for watchlist in watchlists:
        conditions = _watchlist_conditions(watchlist)
        count_query = _apply_watchlist_joins(
            select(func.count(AuctionLot.id))
            .select_from(AuctionLot)
            .join(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id),
            watchlist,
        )
        match_count = (
            session.scalar(count_query.where(and_(*conditions)))
            or 0
        )
        top_score_query = _apply_watchlist_joins(
            select(func.max(AuctionLotV2Analysis.score))
            .select_from(AuctionLot)
            .join(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id),
            watchlist,
        )
        top_score = session.scalar(top_score_query.where(and_(*conditions)))
        web_notification_count = (
            session.scalar(
                select(func.count(AuctionWatchlistNotification.id)).where(
                    AuctionWatchlistNotification.watchlist_id == watchlist.id,
                    AuctionWatchlistNotification.channel == "web",
                    AuctionWatchlistNotification.status == "ready",
                )
            )
            or 0
        )
        payloads.append(
            AuctionV2WatchlistPayload(
                watchlist=watchlist,
                match_count=int(match_count),
                top_score=int(top_score) if top_score is not None else None,
                web_notification_count=int(web_notification_count),
                filter_description=_watchlist_filter_description(watchlist),
            )
        )
    return payloads


def list_auction_v2_web_notifications(
    session: Session,
    *,
    account_id: str,
    limit: int = 8,
) -> list[AuctionV2WebNotificationPayload]:
    rows = list(
        session.execute(
            select(
                AuctionWatchlistNotification,
                AuctionWatchlist,
                AuctionLot,
                AuctionLotV2Analysis,
            )
            .join(
                AuctionWatchlist,
                AuctionWatchlist.id == AuctionWatchlistNotification.watchlist_id,
            )
            .join(AuctionLot, AuctionLot.id == AuctionWatchlistNotification.lot_id)
            .join(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id)
            .options(selectinload(AuctionLot.documents))
            .where(
                AuctionWatchlist.account_id == account_id,
                AuctionWatchlistNotification.channel == "web",
                AuctionWatchlistNotification.status == "ready",
            )
            .order_by(
                AuctionWatchlistNotification.created_at.desc(),
                AuctionLotV2Analysis.score.desc(),
            )
            .limit(limit)
        ).all()
    )
    if not rows:
        return []
    lots = [lot for _notification, _watchlist, lot, _analysis in rows]
    metrics_by_lot = auction_lots_metrics(session, lots)
    geo_checks = _get_or_build_geo_checks(session, lots)
    pipelines = _pipeline_by_lot(
        session,
        account_id=account_id,
        lot_ids=[lot.id for lot in lots],
    )
    payloads: list[AuctionV2WebNotificationPayload] = []
    for notification, watchlist, lot, analysis in rows:
        pipeline = pipelines.get(lot.id)
        if pipeline and pipeline.stage in {"skipped", "archived"}:
            continue
        payloads.append(
            AuctionV2WebNotificationPayload(
                notification=notification,
                watchlist=watchlist,
                item=_payload_from_records(
                    lot=lot,
                    analysis=analysis,
                    metrics=metrics_by_lot[lot.id],
                    geo_check=geo_checks[lot.id],
                    pipeline=pipeline,
                ),
            )
        )
    return payloads


def mark_auction_v2_web_notifications_seen(
    session: Session,
    *,
    account_id: str,
    lot_id: str | None = None,
) -> int:
    conditions = [
        AuctionWatchlist.account_id == account_id,
        AuctionWatchlistNotification.channel == "web",
        AuctionWatchlistNotification.status == "ready",
    ]
    if lot_id:
        conditions.append(AuctionWatchlistNotification.lot_id == lot_id)
    notifications = list(
        session.scalars(
            select(AuctionWatchlistNotification)
            .join(
                AuctionWatchlist,
                AuctionWatchlist.id == AuctionWatchlistNotification.watchlist_id,
            )
            .where(*conditions)
        ).all()
    )
    if not notifications:
        return 0
    now = datetime.now(UTC)
    for notification in notifications:
        notification.status = "opened"
        notification.seen_at = now
        notification.updated_at = now
    session.flush()
    return len(notifications)


def auction_v2_watchlist_matches(
    session: Session,
    *,
    account_id: str,
    limit: int = 8,
) -> list[AuctionV2LotPayload]:
    watchlists = list(
        session.scalars(
            select(AuctionWatchlist).where(
                AuctionWatchlist.account_id == account_id,
                AuctionWatchlist.active.is_(True),
            )
        ).all()
    )
    if not watchlists:
        return []
    rows: list[tuple[AuctionLot, AuctionLotV2Analysis]] = []
    seen_lot_ids: set[str] = set()
    for watchlist in watchlists:
        query = _apply_watchlist_joins(
            select(AuctionLot, AuctionLotV2Analysis)
            .join(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id)
            .options(selectinload(AuctionLot.documents)),
            watchlist,
        )
        query = (
            query
            .where(and_(*_watchlist_conditions(watchlist)))
            .order_by(
                AuctionLotV2Analysis.score.desc(),
                AuctionLot.auction_starts_at.is_(None),
                AuctionLot.auction_starts_at,
                AuctionLot.last_seen_at.desc(),
            )
            .limit(max(limit * 3, limit))
        )
        for lot, analysis in session.execute(query).all():
            if lot.id in seen_lot_ids:
                continue
            seen_lot_ids.add(lot.id)
            rows.append((lot, analysis))
    rows.sort(
        key=lambda item: (
            -item[1].score,
            _aware(item[0].auction_starts_at) or datetime.max.replace(tzinfo=UTC),
        )
    )
    selected_rows = rows[: max(limit * 2, limit)]
    selected_lots = [lot for lot, _analysis in selected_rows]
    metrics_by_lot = auction_lots_metrics(session, selected_lots)
    geo_checks = _get_or_build_geo_checks(session, selected_lots)
    pipelines = _pipeline_by_lot(
        session,
        account_id=account_id,
        lot_ids=[lot.id for lot in selected_lots],
    )
    payloads: list[AuctionV2LotPayload] = []
    for lot, analysis in selected_rows:
        pipeline = pipelines.get(lot.id)
        if pipeline and pipeline.stage in {"skipped", "archived"}:
            continue
        payloads.append(
            _payload_from_records(
                lot=lot,
                analysis=analysis,
                metrics=metrics_by_lot[lot.id],
                geo_check=geo_checks[lot.id],
                pipeline=pipeline,
            )
        )
        if len(payloads) >= limit:
            break
    return payloads


def dispatch_auction_v2_watchlist_notifications(
    session: Session,
    *,
    limit_per_watchlist: int = 5,
) -> AuctionV2NotificationResult:
    watchlist_rows = list(
        session.execute(
            select(AuctionWatchlist, Account)
            .join(Account, Account.id == AuctionWatchlist.account_id)
            .where(AuctionWatchlist.active.is_(True))
            .order_by(AuctionWatchlist.updated_at.desc(), AuctionWatchlist.id)
        ).all()
    )
    result = AuctionV2NotificationResult(watchlists_checked=len(watchlist_rows))
    for watchlist, account in watchlist_rows:
        channels = _watchlist_channels(watchlist)
        if not channels:
            continue
        matches = _watchlist_lot_rows(
            session,
            watchlist,
            limit=max(limit_per_watchlist * 8, limit_per_watchlist),
        )
        result.matches_seen += len(matches)
        pipelines = _pipeline_by_lot(
            session,
            account_id=account.id,
            lot_ids=[lot.id for lot, _analysis in matches],
        )
        lots_notified = 0
        for lot, analysis in matches:
            if lots_notified >= limit_per_watchlist:
                break
            pipeline = pipelines.get(lot.id)
            if pipeline and pipeline.stage in {"skipped", "archived"}:
                continue
            created_events_for_lot = 0
            for event in _auction_v2_notification_events(session, lot, analysis):
                if created_events_for_lot >= settings.auction_v2_events_per_lot_limit:
                    break
                created_for_event = False
                if "web" in channels:
                    notification = _create_watchlist_notification(
                        session,
                        watchlist=watchlist,
                        lot=lot,
                        channel="web",
                        status="ready",
                        sent_at=datetime.now(UTC),
                        event=event,
                    )
                    if notification is not None:
                        result.web_notifications_created += 1
                        created_for_event = True
                if "telegram" in channels and account.telegram_chat_id:
                    notification = _create_watchlist_notification(
                        session,
                        watchlist=watchlist,
                        lot=lot,
                        channel="telegram",
                        status="queued",
                        sent_at=None,
                        event=event,
                    )
                    if notification is not None:
                        created_for_event = True
                        try:
                            _send_watchlist_telegram_notification(
                                account=account,
                                watchlist=watchlist,
                                lot=lot,
                                analysis=analysis,
                                event=event,
                            )
                            notification.status = "sent"
                            notification.sent_at = datetime.now(UTC)
                            notification.error_message = None
                            result.telegram_notifications_sent += 1
                        except Exception as exc:
                            notification.status = "error"
                            notification.error_message = str(exc)[:2000]
                            result.errors += 1
                if created_for_event:
                    created_events_for_lot += 1
            if created_events_for_lot:
                lots_notified += 1
    session.flush()
    return result


def _watchlist_lot_rows(
    session: Session,
    watchlist: AuctionWatchlist,
    *,
    limit: int,
) -> list[tuple[AuctionLot, AuctionLotV2Analysis]]:
    return list(
        session.execute(
            _apply_watchlist_joins(
                select(AuctionLot, AuctionLotV2Analysis)
                .join(AuctionLotV2Analysis, AuctionLotV2Analysis.lot_id == AuctionLot.id)
                .options(selectinload(AuctionLot.documents)),
                watchlist,
            )
            .where(and_(*_watchlist_conditions(watchlist)))
            .order_by(
                AuctionLotV2Analysis.score.desc(),
                AuctionLot.auction_starts_at.is_(None),
                AuctionLot.auction_starts_at,
                AuctionLot.last_seen_at.desc(),
            )
            .limit(limit)
        ).all()
    )


def _auction_v2_notification_events(
    session: Session,
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis,
) -> list[AuctionV2NotificationEvent]:
    changes = _recent_lot_changes(session, lot)
    events = [_new_lot_notification_event(lot, analysis)]
    events.extend(_change_notification_events(changes))
    events.extend(_evidence_notification_events(session, lot))
    deadline_event = _deadline_notification_event(lot)
    if deadline_event is not None:
        events.append(deadline_event)
    ready_event = _ready_notification_event(analysis, changes)
    if ready_event is not None:
        events.append(ready_event)
    events.extend(_risk_notification_events(analysis))
    unique_events: dict[str, AuctionV2NotificationEvent] = {}
    for event in events:
        unique_events[event.event_key] = event
    return sorted(
        unique_events.values(),
        key=lambda item: (-item.priority, item.event_type, item.event_key),
    )


def _new_lot_notification_event(
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis,
) -> AuctionV2NotificationEvent:
    deadline_label, _deadline_status = _deadline_payload(lot)
    parts = [
        f"индекс {analysis.score}/100",
        deadline_label,
        f"старт {_money(lot.start_price_kzt)}",
    ]
    if lot.guarantee_kzt:
        parts.append(f"гарантия {_money(lot.guarantee_kzt)}")
    if analysis.recommended_action:
        parts.append(ACTION_LABELS.get(analysis.recommended_action, analysis.recommended_action))
    return AuctionV2NotificationEvent(
        event_type="new_lot",
        event_key="new_lot",
        title=f"Новый лот: {lot.auction_number or lot.source_lot_id or lot.id}",
        detail=" · ".join(parts),
        priority=100,
    )


def _recent_lot_changes(session: Session, lot: AuctionLot) -> list[AuctionLotChange]:
    cutoff = datetime.now(UTC) - timedelta(hours=settings.auction_v2_event_lookback_hours)
    return list(
        session.scalars(
            select(AuctionLotChange)
            .where(
                AuctionLotChange.lot_id == lot.id,
                AuctionLotChange.changed_at >= cutoff,
            )
            .order_by(AuctionLotChange.changed_at.desc(), AuctionLotChange.id.desc())
            .limit(30)
        ).all()
    )


def _change_notification_events(
    changes: list[AuctionLotChange],
) -> list[AuctionV2NotificationEvent]:
    events: list[AuctionV2NotificationEvent] = []
    for change in changes:
        spec = CHANGE_NOTIFICATION_FIELDS.get(change.field_name)
        if spec is None:
            continue
        event_type, title, field_label, priority = spec
        old_value = _change_value_text(change.field_name, change.old_value)
        new_value = _change_value_text(change.field_name, change.new_value)
        if change.field_name == "documents":
            detail = f"{field_label}: {new_value}"
        else:
            detail = f"{field_label}: {old_value} -> {new_value}"
        events.append(
            AuctionV2NotificationEvent(
                event_type=event_type,
                event_key=f"change:{change.id}",
                title=title,
                detail=detail,
                priority=priority,
            )
        )
    return events


def _evidence_notification_events(
    session: Session,
    lot: AuctionLot,
) -> list[AuctionV2NotificationEvent]:
    cutoff = datetime.now(UTC) - timedelta(hours=settings.auction_v2_event_lookback_hours)
    rows = list(
        session.scalars(
            select(AuctionEvidence)
            .join(AuctionSource, AuctionSource.id == AuctionEvidence.source_id)
            .where(
                AuctionEvidence.lot_id == lot.id,
                AuctionSource.code == "gov_kz_akimat_announcements",
                AuctionEvidence.evidence_type == "akimat_announcement",
                AuctionEvidence.status == "found",
                AuctionEvidence.observed_at >= cutoff,
            )
            .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
            .limit(10)
        ).all()
    )
    return [
        AuctionV2NotificationEvent(
            event_type="akimat_announcement_found",
            event_key=f"evidence:akimat:{evidence.id}",
            title="Найдено объявление акимата",
            detail=_compact_text(
                f"{evidence.title}. {evidence.value_text or ''}",
                500,
            ),
            priority=83,
        )
        for evidence in rows
    ]


def _change_value_text(field_name: str, value: str | None) -> str:
    if value is None or not str(value).strip():
        return "—"
    raw_value = str(value).strip()
    if field_name in {"start_price_kzt", "sale_price_kzt"}:
        try:
            return _money(float(raw_value))
        except ValueError:
            return raw_value
    if field_name == "area_ha":
        try:
            return _area_text(float(raw_value))
        except ValueError:
            return raw_value
    if field_name == "auction_starts_at":
        try:
            return _datetime_text(datetime.fromisoformat(raw_value))
        except ValueError:
            return raw_value
    if field_name == "source_search_status":
        return EQAZYNA_SEARCH_STATUS_LABELS.get(raw_value, raw_value)
    return raw_value


def _deadline_notification_event(lot: AuctionLot) -> AuctionV2NotificationEvent | None:
    starts_at = _aware(lot.auction_starts_at)
    if starts_at is None:
        return None
    seconds_left = (starts_at - datetime.now(UTC)).total_seconds()
    if seconds_left <= 0:
        return None
    deadline_key = starts_at.isoformat()
    if seconds_left <= 3600:
        return AuctionV2NotificationEvent(
            event_type="auction_room_1h",
            event_key=f"deadline:auction_room_1h:{deadline_key}",
            title="Через час торги по лоту",
            detail="Проверьте официальный кабинет E-Qazyna: вход в торги и все юридические действия выполняются там.",
            priority=98,
        )
    if seconds_left <= 7200:
        return AuctionV2NotificationEvent(
            event_type="auction_2h",
            event_key=f"deadline:auction_2h:{deadline_key}",
            title="До торгов осталось 2 часа",
            detail="Проверьте статус приема заявок, гарантийный взнос и доступ к официальной карточке E-Qazyna.",
            priority=96,
        )
    if seconds_left <= 86400:
        return AuctionV2NotificationEvent(
            event_type="registration_24h",
            event_key=f"deadline:registration_24h:{deadline_key}",
            title="Меньше 24 часов до торгов",
            detail="Если планируете участвовать, проверьте дедлайн регистрации и документы на официальном портале.",
            priority=90,
        )
    return None


def _ready_notification_event(
    analysis: AuctionLotV2Analysis,
    changes: list[AuctionLotChange],
) -> AuctionV2NotificationEvent | None:
    readiness_change_fields = {"status", "documents", "auction_starts_at"}
    if analysis.recommended_action != "prepare_official_review" or not any(
        change.field_name in readiness_change_fields for change in changes
    ):
        return None
    return AuctionV2NotificationEvent(
        event_type="ready_for_official_site",
        event_key="ready_for_official_site",
        title="Лот готов к официальной проверке",
        detail="Система собрала достаточно данных для решения. Заявка, ЭЦП и торги остаются только на E-Qazyna/eGov.",
        priority=78,
    )


def _risk_notification_events(analysis: AuctionLotV2Analysis) -> list[AuctionV2NotificationEvent]:
    events: list[AuctionV2NotificationEvent] = []
    for flag in _json_list(analysis.risk_flags_json):
        if str(flag.get("level") or "").lower() != "high":
            continue
        code = str(flag.get("code") or "unknown")[:80]
        label = str(flag.get("label") or "Высокий риск")
        detail = str(flag.get("detail") or "Нужна ручная проверка перед переходом на официальный портал.")
        events.append(
            AuctionV2NotificationEvent(
                event_type="high_risk",
                event_key=f"risk:{code}",
                title=f"Найден высокий риск: {label}",
                detail=detail,
                priority=76,
            )
        )
    return events


def _create_watchlist_notification(
    session: Session,
    *,
    watchlist: AuctionWatchlist,
    lot: AuctionLot,
    channel: str,
    status: str,
    sent_at: datetime | None,
    event: AuctionV2NotificationEvent,
) -> AuctionWatchlistNotification | None:
    existing = session.scalar(
        select(AuctionWatchlistNotification.id).where(
            AuctionWatchlistNotification.watchlist_id == watchlist.id,
            AuctionWatchlistNotification.lot_id == lot.id,
            AuctionWatchlistNotification.channel == channel,
            AuctionWatchlistNotification.event_key == event.event_key,
        )
    )
    if existing:
        return None
    try:
        with session.begin_nested():
            notification = AuctionWatchlistNotification(
                watchlist_id=watchlist.id,
                lot_id=lot.id,
                channel=channel,
                event_type=event.event_type,
                event_key=event.event_key,
                title=event.title[:240],
                detail=event.detail,
                status=status,
                sent_at=sent_at,
                updated_at=datetime.now(UTC),
            )
            session.add(notification)
            session.flush()
    except IntegrityError:
        return None
    return notification


def _watchlist_channels(watchlist: AuctionWatchlist) -> set[str]:
    try:
        raw_channels = json.loads(watchlist.notify_channels_json or "[]")
    except json.JSONDecodeError:
        raw_channels = ["web"]
    if not isinstance(raw_channels, list):
        raw_channels = ["web"]
    channels = {
        str(channel).strip().lower()
        for channel in raw_channels
        if str(channel).strip().lower() in {"web", "telegram"}
    }
    return channels or {"web"}


def _send_watchlist_telegram_notification(
    *,
    account: Account,
    watchlist: AuctionWatchlist,
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis,
    event: AuctionV2NotificationEvent,
) -> None:
    from app.auction_service import format_auction_card
    from app.services import telegram_request

    if not account.telegram_chat_id:
        return
    text = "\n\n".join(
        [
            (
                f"🔔 <b>{escape(event.title)}</b>\n"
                f"{escape(watchlist.name)} · индекс {analysis.score}/100"
            ),
            escape(event.detail),
            escape(analysis.summary),
            format_auction_card(lot, "ru", compact=True),
            "Zhertap доводит только до решения. Заявка, ЭЦП, гарантийный взнос и торги выполняются на официальных порталах.",
        ]
    )
    buttons = [
        [
            {
                "text": "Открыть v2",
                "url": _auction_v2_lot_url(lot),
            }
        ]
    ]
    if lot.source_url:
        buttons.append([{"text": "E-Qazyna ↗", "url": lot.source_url}])
    telegram_request(
        "sendMessage",
        {
            "chat_id": account.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons},
        },
    )


def _auction_v2_lot_url(lot: AuctionLot) -> str:
    return settings.app_base_url.rstrip("/") + f"/cabinet/auctions-v2/{lot.id}"


def auction_v2_search_diagnostics(
    session: Session,
    filters: AuctionV2Filters,
    *,
    current_total: int,
    sample_limit: int = 3,
) -> dict[str, object] | None:
    query = (filters.search_query or "").strip()
    if not query or current_total > 0:
        return None
    search_conditions = _search_conditions(query)
    if not search_conditions:
        return None

    active_count = _search_scope_count(session, search_conditions, "active")
    archive_count = _search_scope_count(session, search_conditions, "archive")
    future_count = _search_scope_count(session, search_conditions, "future")
    all_count = _search_scope_count(session, search_conditions, "all")
    counts = {
        "active": active_count,
        "archive": archive_count,
        "future": future_count,
        "all": all_count,
    }
    digits = _digits_only(query)
    query_kind = "number" if len(digits) >= 5 else "text"
    current_scope = filters.lot_scope if filters.lot_scope in LOT_SCOPE_LABELS else "active"

    if all_count == 0:
        reason = "not_in_v2"
        title = "В базе v2 такого номера нет"
        summary = (
            "Система проверила активные, будущие и архивные лоты v2. Совпадений нет. "
            "Возможные причины: E-Qazyna еще не синхронизировал этот лот, это не земельный аукцион, "
            "номер относится к старому разделу или он есть только на официальной кадастровой карте."
        )
    elif active_count > 0 and current_scope == "active":
        reason = "filters_hide_active"
        title = "Номер есть в активных, но его скрыли фильтры"
        summary = (
            "По этому запросу активные лоты есть, но текущий регион, район, цена, площадь, индекс, "
            "риск или другой фильтр отрезал их из выдачи. Уберите часть условий или оставьте только поиск."
        )
    elif archive_count > 0 and active_count == 0:
        reason = "archive_only"
        title = "Номер найден не в активных, а в архиве"
        summary = (
            "В активных торгах совпадений нет. Найденные записи относятся к архиву, завершенным "
            "или неактивным лотам. Откройте архив или режим “Все”."
        )
    elif future_count > 0 and active_count == 0:
        reason = "future_only"
        title = "Номер найден в будущих торгах"
        summary = (
            "В текущем списке совпадений нет, но есть будущие торги. Откройте список будущих или режим “Все”."
        )
    elif active_count > 0:
        reason = "active_elsewhere"
        title = "Номер найден в активных"
        summary = "Совпадение есть в активных торгах. Вернитесь в список “Активные” или сбросьте фильтры."
    else:
        reason = "other_scope"
        title = "Номер найден в другом списке"
        summary = "Совпадения есть в v2, но не в текущем отборе. Откройте режим “Все” или сбросьте фильтры."

    return {
        "query": query,
        "query_kind": query_kind,
        "reason": reason,
        "title": title,
        "summary": summary,
        "counts": counts,
        "samples": _search_match_samples(
            session,
            search_conditions,
            sample_limit=max(1, sample_limit),
        ),
    }


def _search_scope_count(
    session: Session,
    search_conditions: list[object],
    scope: str,
) -> int:
    conditions = list(search_conditions)
    conditions.extend(_lot_scope_conditions(scope))
    query = select(func.count(AuctionLot.id)).select_from(AuctionLot)
    if conditions:
        query = query.where(and_(*conditions))
    return int(session.scalar(query) or 0)


def _search_match_samples(
    session: Session,
    search_conditions: list[object],
    *,
    sample_limit: int,
) -> list[dict[str, object]]:
    rows = list(
        session.scalars(
            select(AuctionLot)
            .where(and_(*search_conditions))
            .order_by(
                AuctionLot.active.desc(),
                AuctionLot.auction_starts_at.is_(None),
                AuctionLot.auction_starts_at,
                AuctionLot.last_seen_at.desc(),
                AuctionLot.created_at.desc(),
            )
            .limit(sample_limit)
        ).all()
    )
    return [_search_match_sample_payload(lot) for lot in rows]


def _search_match_sample_payload(lot: AuctionLot) -> dict[str, object]:
    scope = _lot_search_scope(lot)
    return {
        "id": lot.id,
        "title": lot.title,
        "number": lot.auction_number or lot.source_lot_id or "",
        "cadastre": lot.cadastre_number or "",
        "region": lot.region or "",
        "district": lot.district or "",
        "locality": lot.locality or "",
        "scope": scope,
        "scope_label": LOT_SCOPE_LABELS.get(scope, scope),
        "starts_at": lot.auction_starts_at,
        "url": f"/cabinet/auctions-v2/{lot.id}",
    }


def _lot_search_scope(lot: AuctionLot) -> str:
    now = datetime.now(UTC)
    starts_at = _aware(lot.auction_starts_at)
    if not lot.active or (starts_at is not None and starts_at < now):
        return "archive"
    if starts_at is not None and starts_at >= now:
        return "future"
    return "active"


def _eqazyna_status_label(lot: AuctionLot) -> str:
    source_status = (lot.source_search_status or "").strip()
    if source_status in EQAZYNA_SEARCH_STATUS_LABELS:
        return EQAZYNA_SEARCH_STATUS_LABELS[source_status]
    if lot.status:
        return lot.status
    if lot.active:
        return "Активный лот"
    return "Статус не определен"


def _eqazyna_status_note(lot: AuctionLot) -> str:
    source_status = (lot.source_search_status or "").strip()
    if source_status in EQAZYNA_SEARCH_STATUS_NOTES:
        return EQAZYNA_SEARCH_STATUS_NOTES[source_status]
    if lot.status:
        return "Статус взят из официальной карточки E-Qazyna; перед участием его нужно сверить на портале."
    if lot.active:
        return "Лот считается активным в базе Zhertap; официальный статус нужно сверить перед заявкой."
    return "Официальный статус не распознан, нужна ручная проверка E-Qazyna."


def pipeline_stage_options() -> list[dict[str, str]]:
    return [{"value": value, "label": label} for value, label in PIPELINE_STAGES]


def _base_filters_for_lot_scope(filters: AuctionV2Filters) -> AuctionFilters:
    if filters.lot_scope not in {"archive", "all"}:
        return filters.base
    return AuctionFilters(
        region=filters.base.region,
        district=filters.base.district,
        locality=filters.base.locality,
        purpose_query=filters.base.purpose_query,
        min_price_kzt=filters.base.min_price_kzt,
        max_price_kzt=filters.base.max_price_kzt,
        min_area_ha=filters.base.min_area_ha,
        max_area_ha=filters.base.max_area_ha,
        active_only=False,
    )


def _lot_scope_conditions(scope: str | None) -> list[object]:
    now = datetime.now(UTC)
    if scope == "all":
        return []
    if scope == "archive":
        return [
            or_(
                AuctionLot.active.is_(False),
                AuctionLot.auction_starts_at < now,
                AuctionLot.source_search_status.in_(ARCHIVED_EQAZYNA_SEARCH_STATUSES),
            )
        ]
    if scope == "future":
        return [
            AuctionLot.active.is_(True),
            AuctionLot.auction_starts_at >= now,
        ]
    return [AuctionLot.active.is_(True)]


def _eqazyna_status_conditions(status: str | None) -> list[object]:
    status_value = (status or "").strip()
    if not status_value:
        return []
    if status_value == "unknown":
        return [
            or_(
                AuctionLot.source_search_status.is_(None),
                AuctionLot.source_search_status == "",
            )
        ]
    if status_value in EQAZYNA_SEARCH_STATUS_LABELS:
        return [AuctionLot.source_search_status == status_value]
    return []


def _search_conditions(search_query: str | None) -> list[object]:
    query = (search_query or "").strip()
    if not query:
        return []
    pattern = f"%{query}%"
    comparisons = [
        AuctionLot.auction_number.ilike(pattern),
        AuctionLot.source_lot_id.ilike(pattern),
        AuctionLot.cadastre_number.ilike(pattern),
        AuctionLot.title.ilike(pattern),
        AuctionLot.region.ilike(pattern),
        AuctionLot.district.ilike(pattern),
        AuctionLot.locality.ilike(pattern),
        AuctionLot.purpose.ilike(pattern),
        AuctionLot.functional_purpose_level2.ilike(pattern),
        AuctionLot.seller_name.ilike(pattern),
        AuctionLot.source_search_status.ilike(pattern),
    ]
    digit_query = _digits_only(query)
    if len(digit_query) >= 5:
        digit_pattern = f"%{digit_query}%"
        comparisons.extend(
            [
                _sql_digits_only(AuctionLot.cadastre_number).like(digit_pattern),
                _sql_digits_only(AuctionLot.source_lot_id).like(digit_pattern),
                _sql_digits_only(AuctionLot.auction_number).like(digit_pattern),
            ]
        )
    return [or_(*comparisons)]


def _digits_only(value: str | None) -> str:
    return "".join(re.findall(r"\d+", value or ""))


def _sql_digits_only(column):
    result = func.coalesce(column, "")
    for symbol in ("-", ":", " ", "/", ".", "\\", "_", "№", "#", "–", "—"):
        result = func.replace(result, symbol, "")
    return result


_GEO_FILTER_STOP_WORDS = {
    "область",
    "облысы",
    "район",
    "ауданы",
    "город",
    "қаласы",
    "устаревшее",
    "устаревший",
    "устар",
}


def _geo_filter_condition(column, value: str | None):
    text = str(value or "").strip()
    if not text:
        return None
    variants = _geo_filter_variants(text)
    comparisons = [column == text]
    for variant in variants:
        comparisons.append(column.ilike(f"%{_escape_like(variant)}%", escape="\\"))
    return or_(*comparisons)


def _geo_filter_variants(value: str) -> list[str]:
    cleaned = re.sub(r"\([^)]*\)", " ", value, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bустаревш\w*\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bр-н\.?\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bг\.?\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[-–—:/,]+", " ", cleaned)
    tokens = [
        token
        for token in re.findall(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі-]{4,}", cleaned)
        if token.lower() not in _GEO_FILTER_STOP_WORDS
    ]
    variants: list[str] = []
    stripped_cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if stripped_cleaned and stripped_cleaned != value:
        variants.append(stripped_cleaned)
    variants.extend(tokens)
    seen: set[str] = set()
    result: list[str] = []
    for variant in variants:
        key = variant.lower()
        if key and key not in seen:
            seen.add(key)
            result.append(variant)
    return result


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _auction_filter_conditions(filters: AuctionFilters) -> list[object]:
    conditions: list[object] = []
    if filters.active_only:
        conditions.append(AuctionLot.active.is_(True))
    if filters.region:
        condition = _geo_filter_condition(AuctionLot.region, filters.region)
        if condition is not None:
            conditions.append(condition)
    if filters.district:
        condition = _geo_filter_condition(AuctionLot.district, filters.district)
        if condition is not None:
            conditions.append(condition)
    if filters.locality:
        condition = _geo_filter_condition(AuctionLot.locality, filters.locality)
        if condition is not None:
            conditions.append(condition)
    if filters.purpose_query:
        conditions.append(AuctionLot.functional_purpose_level2 == filters.purpose_query)
    if filters.min_price_kzt is not None:
        conditions.append(AuctionLot.start_price_kzt >= filters.min_price_kzt)
    if filters.max_price_kzt is not None:
        conditions.append(AuctionLot.start_price_kzt <= filters.max_price_kzt)
    if filters.min_area_ha is not None:
        conditions.append(AuctionLot.area_ha >= filters.min_area_ha)
    if filters.max_area_ha is not None:
        conditions.append(AuctionLot.area_ha <= filters.max_area_ha)
    return conditions


def _apply_watchlist_joins(query, watchlist: AuctionWatchlist):
    if watchlist.geo_status:
        query = query.outerjoin(
            AuctionLotGeoCheck,
            AuctionLotGeoCheck.lot_id == AuctionLot.id,
        )
    if watchlist.stage:
        query = query.join(
            AuctionUserLotPipeline,
            and_(
                AuctionUserLotPipeline.lot_id == AuctionLot.id,
                AuctionUserLotPipeline.account_id == watchlist.account_id,
            ),
        )
    return query


def _watchlist_filter_description(watchlist: AuctionWatchlist) -> str:
    parts: list[str] = []
    scope = watchlist.lot_scope or "active"
    parts.append(LOT_SCOPE_LABELS.get(scope, scope))
    if watchlist.region:
        parts.append(watchlist.region)
    if watchlist.district:
        parts.append(watchlist.district)
    if watchlist.locality:
        parts.append(watchlist.locality)
    if watchlist.purpose_query:
        parts.append(watchlist.purpose_query)
    if watchlist.eqazyna_status:
        parts.append(
            "E-Qazyna: "
            + EQAZYNA_STATUS_FILTER_LABELS.get(
                watchlist.eqazyna_status,
                watchlist.eqazyna_status,
            ).lower()
        )
    if watchlist.min_price_kzt is not None:
        parts.append(f"цена от {_money(watchlist.min_price_kzt)}")
    if watchlist.max_price_kzt is not None:
        parts.append(f"цена до {_money(watchlist.max_price_kzt)}")
    if watchlist.min_area_ha is not None:
        parts.append(f"площадь от {_area_text(watchlist.min_area_ha)}")
    if watchlist.max_area_ha is not None:
        parts.append(f"площадь до {_area_text(watchlist.max_area_ha)}")
    if watchlist.min_score is not None:
        parts.append(f"индекс от {watchlist.min_score}")
    if watchlist.risk_level:
        parts.append(f"риск: {RISK_LABELS.get(watchlist.risk_level, watchlist.risk_level).lower()}")
    if watchlist.confidence_level:
        parts.append(
            "уверенность: "
            + CONFIDENCE_LABELS.get(
                watchlist.confidence_level,
                watchlist.confidence_level,
            ).lower()
        )
    if watchlist.deadline_status:
        parts.append(
            "срок: "
            + DEADLINE_STATUS_LABELS.get(
                watchlist.deadline_status,
                watchlist.deadline_status,
            ).lower()
        )
    if watchlist.geo_status:
        parts.append(
            "гео: "
            + GEO_STATUS_LABELS.get(watchlist.geo_status, watchlist.geo_status).lower()
        )
    if watchlist.stage:
        parts.append(f"статус: {_stage_label(watchlist.stage).lower()}")
    return " · ".join(parts)


def _watchlist_conditions(watchlist: AuctionWatchlist) -> list[object]:
    lot_scope = watchlist.lot_scope or "active"
    conditions = _auction_filter_conditions(
        AuctionFilters(
            region=watchlist.region,
            district=watchlist.district,
            locality=watchlist.locality,
            purpose_query=watchlist.purpose_query,
            min_price_kzt=watchlist.min_price_kzt,
            max_price_kzt=watchlist.max_price_kzt,
            min_area_ha=watchlist.min_area_ha,
            max_area_ha=watchlist.max_area_ha,
            active_only=False,
        )
    )
    conditions.extend(_lot_scope_conditions(lot_scope))
    conditions.extend(_eqazyna_status_conditions(watchlist.eqazyna_status))
    if watchlist.min_score is not None:
        conditions.append(AuctionLotV2Analysis.score >= watchlist.min_score)
    if watchlist.risk_level:
        conditions.append(AuctionLotV2Analysis.risk_level == watchlist.risk_level)
    if watchlist.confidence_level:
        conditions.append(
            AuctionLotV2Analysis.confidence_level == watchlist.confidence_level
        )
    if watchlist.deadline_status:
        conditions.extend(_deadline_conditions(watchlist.deadline_status))
    if watchlist.geo_status:
        conditions.extend(_geo_status_conditions(watchlist.geo_status))
    if watchlist.stage:
        conditions.append(AuctionUserLotPipeline.stage == watchlist.stage)
    return conditions


def _payload_from_records(
    *,
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    pipeline: AuctionUserLotPipeline | None,
) -> AuctionV2LotPayload:
    deadline_label, deadline_status = _deadline_payload(lot)
    readiness = _json_list(analysis.readiness_json)
    source_statuses = _json_list(analysis.source_status_json)
    official_readiness = _official_readiness(
        lot=lot,
        analysis=analysis,
        metrics=metrics,
        geo_check=geo_check,
        pipeline=pipeline,
        source_statuses=source_statuses,
    )
    risk_flags = _json_list(analysis.risk_flags_json)
    buyer_workflow = _buyer_workflow(
        lot=lot,
        analysis=analysis,
        metrics=metrics,
        geo_check=geo_check,
        pipeline=pipeline,
        source_statuses=source_statuses,
        official_readiness=official_readiness,
    )
    review_steps = _lot_review_steps(
        lot=lot,
        analysis=analysis,
        metrics=metrics,
        geo_check=geo_check,
        pipeline=pipeline,
        readiness=readiness,
        risk_flags=risk_flags,
        source_statuses=source_statuses,
        official_readiness=official_readiness,
    )
    manual_process = _manual_process_map(
        lot=lot,
        analysis=analysis,
        metrics=metrics,
        geo_check=geo_check,
        pipeline=pipeline,
        source_statuses=source_statuses,
        review_steps=review_steps,
    )
    next_actions = _lot_next_actions(review_steps)
    data_quality = _data_quality_summary(
        lot=lot,
        analysis=analysis,
        metrics=metrics,
        geo_check=geo_check,
    )
    cost_estimate = _cost_estimate(lot, pipeline)
    investment_case = _investment_case(
        lot,
        pipeline,
        known_extra_costs_kzt=float(cost_estimate["known_extra_costs_kzt"] or 0),
    )
    field_inspection = _field_inspection(pipeline)
    deal_room = _deal_room(pipeline)
    lot_scope = _lot_search_scope(lot)
    return AuctionV2LotPayload(
        lot=lot,
        analysis=analysis,
        metrics=metrics,
        geo_check=geo_check,
        pipeline=pipeline,
        map_embed_url=_osm_embed_url(geo_check),
        osm_map_url=_osm_map_url(geo_check),
        readiness=readiness,
        risk_flags=risk_flags,
        source_statuses=source_statuses,
        official_readiness=official_readiness,
        buyer_workflow=buyer_workflow,
        review_steps=review_steps,
        manual_process=manual_process,
        manual_process_counts=_manual_process_counts(manual_process),
        next_actions=next_actions,
        data_quality=data_quality,
        cost_estimate=cost_estimate,
        investment_case=investment_case,
        field_inspection=field_inspection,
        deal_room=deal_room,
        decision_summary=_lot_decision_summary(
            lot=lot,
            analysis=analysis,
            metrics=metrics,
            geo_check=geo_check,
            pipeline=pipeline,
            review_steps=review_steps,
            risk_flags=risk_flags,
            next_actions=next_actions,
        ),
        risk_label=RISK_LABELS.get(analysis.risk_level, analysis.risk_level),
        confidence_label=CONFIDENCE_LABELS.get(
            analysis.confidence_level,
            analysis.confidence_level,
        ),
        action_label=ACTION_LABELS.get(analysis.recommended_action, analysis.recommended_action),
        stage_label=_stage_label(pipeline.stage) if pipeline else None,
        deadline_label=deadline_label,
        deadline_status=deadline_status,
        lot_scope=lot_scope,
        lot_scope_label=LOT_SCOPE_LABELS.get(lot_scope, "Активные"),
        eqazyna_status_label=_eqazyna_status_label(lot),
        eqazyna_status_note=_eqazyna_status_note(lot),
        coordinate_label=COORDINATE_STATUS_LABELS.get(
            geo_check.coordinate_status,
            geo_check.coordinate_status,
        ),
        cadastre_label=CADASTRE_STATUS_LABELS.get(
            geo_check.cadastre_status,
            geo_check.cadastre_status,
        ),
        boundary_label=BOUNDARY_STATUS_LABELS.get(
            geo_check.boundary_status,
            geo_check.boundary_status,
        ),
        urban_plan_label=URBAN_PLAN_STATUS_LABELS.get(
            geo_check.urban_plan_status,
            geo_check.urban_plan_status,
        ),
        osm_label=OSM_STATUS_LABELS.get(geo_check.osm_status, geo_check.osm_status),
        engineering_label=ENGINEERING_STATUS_LABELS.get(
            geo_check.engineering_status,
            geo_check.engineering_status,
        ),
    )


def _json_list(value: str | None) -> list[dict[str, object]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _pipeline_by_lot(
    session: Session,
    *,
    account_id: str | None,
    lot_ids: list[str],
) -> dict[str, AuctionUserLotPipeline]:
    if not account_id or not lot_ids:
        return {}
    return {
        pipeline.lot_id: pipeline
        for pipeline in session.scalars(
            select(AuctionUserLotPipeline).where(
                AuctionUserLotPipeline.account_id == account_id,
                AuctionUserLotPipeline.lot_id.in_(lot_ids),
            )
        ).all()
    }


def _stage_label(stage: str | None) -> str | None:
    labels = dict(PIPELINE_STAGES)
    return labels.get(stage or "")


def _deadline_payload(lot: AuctionLot) -> tuple[str, str]:
    starts_at = _aware(lot.auction_starts_at)
    if starts_at is None:
        return "Дата торгов неизвестна", "unknown"
    seconds_left = (starts_at - datetime.now(UTC)).total_seconds()
    if seconds_left < 0:
        return "Торги уже начались", "expired"
    hours_left = seconds_left / 3600
    if hours_left < 1:
        minutes = max(1, int(round(seconds_left / 60)))
        return f"{minutes} мин. до торгов", "urgent"
    if hours_left <= 24:
        return f"{int(round(hours_left))} ч. до торгов", "urgent"
    days_left = int(hours_left // 24)
    hours_remainder = int(round(hours_left % 24))
    if hours_left <= 72:
        if hours_remainder:
            return f"{days_left} д. {hours_remainder} ч.", "soon"
        return f"{days_left} д. до торгов", "soon"
    return f"{max(1, int(round(hours_left / 24)))} д. до торгов", "normal"


def _map_marker_scope(lot: AuctionLot, deadline_status: str) -> str:
    if (
        not lot.active
        or deadline_status == "expired"
        or lot.source_search_status in ARCHIVED_EQAZYNA_SEARCH_STATUSES
    ):
        return "archive"
    starts_at = _aware(lot.auction_starts_at)
    if starts_at is not None and starts_at >= datetime.now(UTC):
        return "future"
    return "active"


def _deadline_conditions(status: str) -> list[object]:
    now = datetime.now(UTC)
    one_day = now + timedelta(days=1)
    three_days = now + timedelta(days=3)
    if status == "urgent":
        return [AuctionLot.auction_starts_at >= now, AuctionLot.auction_starts_at <= one_day]
    if status == "soon":
        return [
            AuctionLot.auction_starts_at > one_day,
            AuctionLot.auction_starts_at <= three_days,
        ]
    if status == "normal":
        return [AuctionLot.auction_starts_at > three_days]
    if status == "unknown":
        return [AuctionLot.auction_starts_at.is_(None)]
    if status == "expired":
        return [AuctionLot.auction_starts_at < now]
    return []


def _geo_status_conditions(status: str) -> list[object]:
    if status == "coordinates_found":
        return [AuctionLotGeoCheck.coordinate_status == "found"]
    if status == "coordinates_missing":
        return [
            or_(
                AuctionLotGeoCheck.id.is_(None),
                AuctionLotGeoCheck.coordinate_status != "found",
            )
        ]
    if status == "osm_checked":
        return [
            AuctionLotGeoCheck.osm_status == "checked",
            AuctionLotGeoCheck.engineering_status == "checked",
        ]
    if status == "osm_warning":
        return [
            or_(
                AuctionLotGeoCheck.osm_status.in_(["unavailable", "stale"]),
                and_(
                    AuctionLotGeoCheck.osm_status == "checked",
                    AuctionLotGeoCheck.engineering_status == "warning",
                ),
            )
        ]
    if status == "osm_pending":
        return [
            or_(
                AuctionLotGeoCheck.id.is_(None),
                AuctionLotGeoCheck.osm_status.is_(None),
                AuctionLotGeoCheck.osm_status.in_(
                    ["not_checked", "missing_coordinates"],
                ),
            )
        ]
    return []


def _valid_kazakhstan_coordinates(latitude: float | None, longitude: float | None) -> bool:
    if latitude is None or longitude is None:
        return False
    return 40.0 <= float(latitude) <= 56.5 and 46.0 <= float(longitude) <= 88.5


def _get_or_build_geo_check(session: Session, lot: AuctionLot) -> AuctionLotGeoCheck:
    geo_check = session.scalar(
        select(AuctionLotGeoCheck).where(AuctionLotGeoCheck.lot_id == lot.id)
    )
    if geo_check is None:
        geo_check = AuctionLotGeoCheck(lot_id=lot.id)
        session.add(geo_check)
    _refresh_geo_check(session, lot, geo_check)
    session.flush()
    return geo_check


def _data_quality_summary(
    *,
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
) -> dict[str, object]:
    """Return the small, user-facing completeness layer for a lot.

    The detailed review checklist remains available below the fold. This layer
    answers the first question a buyer has: which decision inputs are already
    confirmed and which ones still need a manual check.
    """

    def row(
        code: str,
        title: str,
        value: str,
        detail: str,
        *,
        status: str,
        anchor: str | None = None,
    ) -> dict[str, object]:
        labels = {
            "done": "Подтверждено",
            "manual": "Проверить",
            "missing": "Не найдено",
        }
        return {
            "code": code,
            "title": title,
            "value": value,
            "detail": detail,
            "status": status,
            "status_label": labels.get(status, "Проверить"),
            "anchor": anchor,
        }

    rows = [
        row(
            "official_lot",
            "Официальный лот",
            "Есть" if lot.source_url else "Нет",
            "Ссылка на карточку E-Qazyna"
            if lot.source_url
            else "Нужна официальная ссылка на лот",
            status="done" if lot.source_url else "missing",
            anchor="#auction-v2-decision-form",
        ),
        row(
            "documents",
            "Документы",
            str(metrics.document_count),
            "Приложения и условия доступны в карточке"
            if metrics.document_count
            else "Нужно открыть приложения на E-Qazyna",
            status="done" if metrics.document_count else "missing",
            anchor="#auction-v2-documents",
        ),
        row(
            "cadastre",
            "Кадастровый номер",
            "Сверено"
            if geo_check.cadastre_status == "verified"
            else "Проверить",
            "Номер и координаты подтверждены ЕГКН"
            if geo_check.cadastre_status == "verified"
            else "Сверить номер, границу и площадь в ЕГКН",
            status=(
                "done"
                if geo_check.cadastre_status == "verified"
                else "manual"
            ),
            anchor="#auction-v2-map-panel",
        ),
        row(
            "boundary",
            "Граница и площадь",
            "Подтверждена"
            if geo_check.boundary_status == "verified"
            else "Расхождение"
            if geo_check.boundary_status == "warning"
            else "Проверить",
            (
                f"ЕГКН: {geo_check.boundary_area_ha:.4f} га; отличие от лота "
                f"{abs(geo_check.boundary_difference_percent or 0):.1f}%"
                if geo_check.boundary_status == "warning"
                else "Геометрия и площадь подтверждены источником"
                if geo_check.boundary_status == "verified"
                else "Сверить схему, площадь и поворотные точки в ЕГКН"
            ),
            status=(
                "done"
                if geo_check.boundary_status == "verified"
                else "missing"
                if geo_check.boundary_status == "not_found"
                else "manual"
            ),
            anchor="#auction-v2-map-panel",
        ),
        row(
            "restrictions",
            "Ограничения",
            "Проверено" if geo_check.urban_plan_status == "checked" else "Ручная проверка",
            "Генплан, ПДП и функциональные зоны"
            if geo_check.urban_plan_status == "checked"
            else "Проверить генплан, ПДП, красные линии и зоны",
            status="done" if geo_check.urban_plan_status == "checked" else "manual",
            anchor="#auction-v2-review-board",
        ),
        row(
            "infrastructure",
            "Подъезд и сети",
            "Проверено" if geo_check.osm_status == "checked" else "Ручная проверка",
            "Открытые данные по окружению собраны"
            if geo_check.osm_status == "checked"
            else "Проверить реальный подъезд и технические условия",
            status="done" if geo_check.osm_status == "checked" else "manual",
            anchor="#auction-v2-map-panel",
        ),
        row(
            "price",
            "Цена и лимит",
            _money(analysis.max_bid_market_kzt)
            if analysis.max_bid_market_kzt is not None
            else "Нет ориентира",
            "Есть районный или рыночный ориентир"
            if analysis.max_bid_market_kzt is not None
            else "Нужны сопоставимые цены или история торгов",
            status="done" if analysis.max_bid_market_kzt is not None else "manual",
            anchor="#auction-v2-district-context",
        ),
    ]
    counts = {
        "done": sum(item["status"] == "done" for item in rows),
        "manual": sum(item["status"] == "manual" for item in rows),
        "missing": sum(item["status"] == "missing" for item in rows),
        "total": len(rows),
    }
    return {
        "rows": rows,
        "counts": counts,
        "label": f"{counts['done']} из {counts['total']} ключевых блоков подтверждено",
    }


def _get_or_build_geo_checks(
    session: Session,
    lots: list[AuctionLot],
) -> dict[str, AuctionLotGeoCheck]:
    if not lots:
        return {}
    lot_ids = [lot.id for lot in lots]
    checks = {
        geo_check.lot_id: geo_check
        for geo_check in session.scalars(
            select(AuctionLotGeoCheck).where(AuctionLotGeoCheck.lot_id.in_(lot_ids))
        ).all()
    }
    for lot in lots:
        geo_check = checks.get(lot.id)
        if geo_check is None:
            geo_check = AuctionLotGeoCheck(lot_id=lot.id)
            session.add(geo_check)
            checks[lot.id] = geo_check
        _refresh_geo_check(session, lot, geo_check)
    session.flush()
    return checks


def _refresh_geo_check(
    session: Session,
    lot: AuctionLot,
    geo_check: AuctionLotGeoCheck,
) -> None:
    geo_metrics = auction_lot_geo_metrics(lot)
    previous_latitude = geo_check.latitude
    previous_longitude = geo_check.longitude
    now = datetime.now(UTC)
    if not lot.cadastre_number:
        geo_check.cadastre_status = "missing"
    elif geo_check.cadastre_status not in {"verified", "not_found", "unavailable"}:
        geo_check.cadastre_status = "found"

    if geo_metrics.latitude is not None and geo_metrics.longitude is not None:
        if not _valid_kazakhstan_coordinates(geo_metrics.latitude, geo_metrics.longitude):
            geo_check.coordinate_status = "unconfirmed"
            geo_check.latitude = None
            geo_check.longitude = None
        else:
            geo_check.coordinate_status = "found"
            geo_check.latitude = geo_metrics.latitude
            geo_check.longitude = geo_metrics.longitude
    elif geo_check.latitude is not None and geo_check.longitude is not None:
        if not _valid_kazakhstan_coordinates(geo_check.latitude, geo_check.longitude):
            geo_check.coordinate_status = "unconfirmed"
            geo_check.latitude = None
            geo_check.longitude = None
        else:
            geo_check.coordinate_status = "found"
    else:
        geo_check.coordinate_status = "missing"
        geo_check.latitude = None
        geo_check.longitude = None

    if geo_check.coordinate_status == "found":
        pass
    elif geo_check.coordinate_status == "unconfirmed":
        geo_check.latitude = None
        geo_check.longitude = None
    else:
        geo_check.coordinate_status = "missing"
        geo_check.latitude = None
        geo_check.longitude = None

    geo_check.egkn_url = _egkn_lot_url(lot)
    geo_check.google_maps_url = _google_maps_url(lot, geo_check.latitude, geo_check.longitude)
    geo_check.market_status = "pending"
    if not geo_check.osm_status:
        geo_check.osm_status = "not_checked"
    if geo_check.coordinate_status == "found":
        geo_check.notes = "Координаты найдены в карточке/описании лота; градостроительные ограничения требуют сверки по официальным слоям."
    elif geo_check.coordinate_status == "unconfirmed":
        geo_check.notes = "Координаты из источника не подтверждены как точка в Казахстане; маркер скрыт до ручной сверки по ЕГКН, адресу или документам."
    else:
        geo_check.notes = "Координаты не найдены автоматически; нужен ручной поиск по кадастру, адресу или приложенным документам."
    if geo_check.latitude is None or geo_check.longitude is None:
        _clear_osm_fields(geo_check, status="missing_coordinates")
    elif _coordinates_changed(
        previous_latitude,
        previous_longitude,
        geo_check.latitude,
        geo_check.longitude,
    ):
        _clear_osm_fields(
            geo_check,
            status="stale" if geo_check.osm_checked_at is not None else "not_checked",
        )
    geo_check.notes = _geo_check_notes(geo_check)
    geo_check.checked_at = now
    geo_check.updated_at = now


def _refresh_auction_v2_infrastructure_batch(
    session: Session,
    lots: list[AuctionLot],
    *,
    provider: OsmProvider | None = None,
    force: bool = False,
) -> tuple[int, int]:
    candidates: list[AuctionLotGeoCheck] = []
    for lot in lots:
        geo_check = _get_or_build_geo_check(session, lot)
        if geo_check.latitude is None or geo_check.longitude is None:
            continue
        if force or _osm_check_due(geo_check):
            candidates.append(geo_check)
    if not candidates:
        return 0, 0

    osm_provider = provider or OsmProvider()
    points = [(row.latitude, row.longitude) for row in candidates]
    try:
        surroundings = osm_provider.analyze_points(
            points,
            radius_m=settings.auction_v2_osm_radius_m,
        )
    except OsmProviderError as exc:
        for geo_check in candidates:
            _mark_osm_unavailable(geo_check, exc)
        session.flush()
        return 0, len(candidates)

    checked = 0
    errors = 0
    now = datetime.now(UTC)
    for geo_check, row in zip(candidates, surroundings, strict=False):
        if row.checked:
            _apply_osm_surroundings(geo_check, row, checked_at=now)
            checked += 1
        else:
            geo_check.osm_status = "not_checked"
            geo_check.engineering_status = "manual_required"
            geo_check.notes = _geo_check_notes(geo_check)
            geo_check.updated_at = now
            errors += 1
    session.flush()
    return checked, errors


def _refresh_auction_v2_egkn_batch(
    session: Session,
    lots: list[AuctionLot],
    *,
    source: AuctionSource,
    provider: EgknProvider | None = None,
    force: bool = False,
) -> tuple[int, int, int]:
    candidates: list[tuple[AuctionLot, AuctionLotGeoCheck]] = []
    for lot in lots[: settings.auction_v2_egkn_batch_size]:
        if not lot.cadastre_number:
            continue
        geo_check = _get_or_build_geo_check(session, lot)
        if force or _egkn_check_due(geo_check):
            candidates.append((lot, geo_check))
    if not candidates:
        return 0, 0, 0

    egkn_provider = provider or EgknProvider()
    checked = 0
    verified = 0
    errors = 0
    for lot, geo_check in candidates:
        checked += 1
        try:
            result = egkn_provider.lookup_cadastre(
                lot.cadastre_number or "",
                region=lot.region,
                district=lot.district,
                locality=lot.locality,
            )
        except (EgknProviderError, OSError) as exc:
            _mark_egkn_unavailable(geo_check, exc)
            _upsert_evidence(
                session,
                lot=lot,
                source=source,
                evidence_type="cadastre_boundary",
                title=f"ЕГКН: {lot.cadastre_number}",
                status="unavailable",
                value_text=str(exc)[:1000],
                source_url=geo_check.egkn_url or source.base_url,
                confidence=0.2,
            )
            errors += 1
            continue
        _apply_egkn_lookup_result(session, lot, geo_check, source, result)
        if result.found:
            verified += 1
    session.flush()
    return checked, verified, errors


def _refresh_auction_v2_egkn_context_batch(
    session: Session,
    lots: list[AuctionLot],
    *,
    source: AuctionSource,
    provider: EgknProvider | None = None,
    force: bool = False,
) -> tuple[int, int, int]:
    if not _auction_v2_egkn_context_enabled():
        return 0, 0, 0
    candidates: list[tuple[AuctionLot, AuctionLotGeoCheck]] = []
    for lot in lots[: settings.auction_v2_egkn_context_batch_size]:
        geo_check = _get_or_build_geo_check(session, lot)
        if geo_check.latitude is None or geo_check.longitude is None:
            continue
        if force or _egkn_context_check_due(session, lot):
            candidates.append((lot, geo_check))
    if not candidates:
        return 0, 0, 0

    egkn_provider = provider or EgknProvider()
    checked = 0
    features_seen = 0
    errors = 0
    for lot, geo_check in candidates:
        checked += 1
        for layer_meta in EGKN_CONTEXT_LAYERS:
            try:
                features = egkn_provider.features_around(
                    layer=layer_meta["layer"],
                    latitude=float(geo_check.latitude),
                    longitude=float(geo_check.longitude),
                    radius_m=settings.auction_v2_egkn_context_radius_m,
                    max_features=settings.auction_v2_egkn_context_max_features_per_layer,
                )
            except (EgknProviderError, OSError) as exc:
                errors += 1
                _upsert_egkn_context_layer_evidence(
                    session,
                    lot=lot,
                    source=source,
                    layer_meta=layer_meta,
                    status="unavailable",
                    features=[],
                    message=f"ЕГКН слой временно недоступен: {exc}",
                )
                continue
            features_seen += len(features)
            _upsert_egkn_context_layer_evidence(
                session,
                lot=lot,
                source=source,
                layer_meta=layer_meta,
                status="found" if features else "missing",
                features=features,
                message=None,
            )
    session.flush()
    return checked, features_seen, errors


def _auction_v2_live_osm_enabled() -> bool:
    return settings.auction_v2_live_osm_enabled and settings.enable_live_osm


def _auction_v2_live_gov_kz_enabled() -> bool:
    return settings.auction_v2_live_gov_kz_enabled


def _auction_v2_live_egkn_enabled() -> bool:
    return settings.auction_v2_live_egkn_enabled


def _auction_v2_egkn_context_enabled() -> bool:
    return settings.auction_v2_egkn_context_enabled


def _egkn_check_due(geo_check: AuctionLotGeoCheck) -> bool:
    if geo_check.cadastre_status in {"unknown", "found", "missing", "unavailable"}:
        return True
    checked_at = _aware(geo_check.checked_at)
    if checked_at is None:
        return True
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.auction_v2_egkn_ttl_minutes)
    return checked_at < cutoff


def _egkn_context_check_due(session: Session, lot: AuctionLot) -> bool:
    latest = session.scalar(
        select(AuctionEvidence.observed_at)
        .where(
            AuctionEvidence.lot_id == lot.id,
            AuctionEvidence.evidence_type == "egkn_context_layer",
        )
        .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
        .limit(1)
    )
    if latest is None:
        return True
    cutoff = datetime.now(UTC) - timedelta(
        minutes=settings.auction_v2_egkn_context_ttl_minutes
    )
    return _aware(latest) < cutoff


def _upsert_egkn_context_layer_evidence(
    session: Session,
    *,
    lot: AuctionLot,
    source: AuctionSource,
    layer_meta: dict[str, str],
    status: str,
    features: list[EgknContextFeature],
    message: str | None,
) -> AuctionEvidence:
    feature_count = len(features)
    if message:
        value_text = message
    elif feature_count:
        value_text = (
            f"{layer_meta['label']}: найдено объектов {feature_count} "
            f"в радиусе {settings.auction_v2_egkn_context_radius_m} м."
        )
    else:
        value_text = (
            f"{layer_meta['label']}: объектов в радиусе "
            f"{settings.auction_v2_egkn_context_radius_m} м не найдено."
        )
    confidence = 0.72 if status == "found" else 0.45 if status == "missing" else 0.2
    return _upsert_evidence(
        session,
        lot=lot,
        source=source,
        evidence_type="egkn_context_layer",
        title=f"ЕГКН слой: {layer_meta['label']}",
        status=status,
        value_text=value_text,
        source_url=_egkn_lot_url(lot),
        confidence=confidence,
        raw_payload_json=json.dumps(
            _egkn_context_layer_payload(layer_meta, features, message),
            ensure_ascii=False,
        ),
    )


def _egkn_context_layer_payload(
    layer_meta: dict[str, str],
    features: list[EgknContextFeature],
    message: str | None,
) -> dict[str, object]:
    return {
        "layer_code": layer_meta["code"],
        "layer": layer_meta["layer"],
        "label": layer_meta["label"],
        "kind": layer_meta["kind"],
        "radius_m": settings.auction_v2_egkn_context_radius_m,
        "feature_count": len(features),
        "message": message,
        "features": [_egkn_context_feature_payload(feature) for feature in features],
    }


def _egkn_context_feature_payload(feature: EgknContextFeature) -> dict[str, object]:
    return {
        "id": feature.feature_id,
        "layer": feature.layer,
        "label": _egkn_feature_label(feature.properties),
        "geometry": _safe_context_geometry(feature.geometry),
        "properties": _compact_properties(feature.properties),
    }


def _egkn_feature_label(properties: dict[str, object]) -> str:
    for key in (
        "lot_number",
        "kad_nomer",
        "name",
        "category",
        "function",
        "rent_condition_rus",
        "address_ru",
        "type_id",
        "gid",
        "id",
    ):
        value = str(properties.get(key) or "").strip()
        if value:
            return value[:180]
    return "Объект ЕГКН"


def _safe_context_geometry(geometry: dict[str, object]) -> dict[str, object] | None:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)):
        return None
    if geometry_type not in {
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
    }:
        return None
    return {"type": geometry_type, "coordinates": coordinates}


def _compact_properties(properties: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in properties.items():
        if value is None:
            continue
        text = str(value)
        if len(text) > 240:
            text = text[:237] + "..."
        result[str(key)[:80]] = text
        if len(result) >= 18:
            break
    return result


def _mark_egkn_unavailable(geo_check: AuctionLotGeoCheck, exc: Exception) -> None:
    geo_check.cadastre_status = "unavailable"
    geo_check.notes = _geo_check_notes(geo_check, extra=f"ЕГКН временно недоступен: {exc}")
    geo_check.checked_at = datetime.now(UTC)
    geo_check.updated_at = datetime.now(UTC)


def _apply_egkn_lookup_result(
    session: Session,
    lot: AuctionLot,
    geo_check: AuctionLotGeoCheck,
    source: AuctionSource,
    result: CadastreLookupResult,
) -> None:
    now = datetime.now(UTC)
    geo_check.checked_at = now
    geo_check.updated_at = now
    geo_check.egkn_url = _egkn_lot_url(lot)
    if result.found:
        previous_latitude = geo_check.latitude
        previous_longitude = geo_check.longitude
        geo_check.cadastre_status = "verified"
        geo_check.boundary_source = result.source_layer
        if result.area_m2 is not None and result.area_m2 > 0:
            geo_check.boundary_area_ha = result.area_m2 / 10_000
        if result.geometry is not None:
            geo_check.boundary_status = "verified"
        else:
            geo_check.boundary_status = "manual_required"
        if lot.area_ha and geo_check.boundary_area_ha:
            geo_check.boundary_difference_percent = (
                (geo_check.boundary_area_ha - lot.area_ha) / lot.area_ha
            ) * 100
            if abs(geo_check.boundary_difference_percent) > 10:
                geo_check.boundary_status = "warning"
        if result.latitude is not None and result.longitude is not None:
            geo_check.coordinate_status = "found"
            geo_check.latitude = result.latitude
            geo_check.longitude = result.longitude
            geo_check.google_maps_url = _google_maps_url(lot, result.latitude, result.longitude)
            if _coordinates_changed(
                previous_latitude,
                previous_longitude,
                result.latitude,
                result.longitude,
            ):
                _clear_osm_fields(
                    geo_check,
                    status="stale" if geo_check.osm_checked_at is not None else "not_checked",
                )
        geo_check.notes = _geo_check_notes(
            geo_check,
            extra=_egkn_result_note(result),
        )
        _upsert_evidence(
            session,
            lot=lot,
            source=source,
            evidence_type="cadastre_boundary",
            title=f"ЕГКН: {result.cadastre}",
            status="found",
            value_text=_egkn_result_note(result),
            source_url=geo_check.egkn_url,
            confidence=0.9,
            raw_payload_json=json.dumps(_egkn_result_payload(result), ensure_ascii=False),
        )
        return

    geo_check.cadastre_status = "not_found"
    geo_check.boundary_status = "not_found"
    geo_check.boundary_source = result.source_layer
    geo_check.notes = _geo_check_notes(geo_check, extra=result.message)
    _upsert_evidence(
        session,
        lot=lot,
        source=source,
        evidence_type="cadastre_boundary",
        title=f"ЕГКН: {lot.cadastre_number}",
        status="missing",
        value_text=result.message,
        source_url=geo_check.egkn_url,
        confidence=0.35,
        raw_payload_json=json.dumps(_egkn_result_payload(result), ensure_ascii=False),
    )


def _egkn_result_note(result: CadastreLookupResult) -> str:
    if not result.found:
        return result.message or "Кадастровый номер не найден в публичном слое ЕГКН."
    parts = [f"кадастр подтвержден в {result.source_layer}"]
    if result.district:
        parts.append(result.district.display_name)
    if result.address:
        parts.append(result.address)
    if result.land_use:
        parts.append(result.land_use)
    if result.area_m2:
        parts.append(f"площадь {result.area_m2:.0f} м2")
    if result.latitude is not None and result.longitude is not None:
        parts.append(f"центр {result.latitude:.6f}, {result.longitude:.6f}")
    return "; ".join(parts)


def _egkn_result_payload(result: CadastreLookupResult) -> dict[str, object]:
    return {
        "found": result.found,
        "cadastre": result.cadastre,
        "source_layer": result.source_layer,
        "district": result.district.display_name if result.district else None,
        "district_id": result.district.id if result.district else None,
        "address": result.address,
        "land_use": result.land_use,
        "area_m2": result.area_m2,
        "category_id": result.category_id,
        "right_type_id": result.right_type_id,
        "latitude": result.latitude,
        "longitude": result.longitude,
        "message": result.message,
        "geometry_srs": "EPSG:4326" if result.geometry is not None else None,
        "geometry_geojson": _egkn_geometry_geojson(result),
        "properties": result.raw_properties or {},
    }


def _egkn_geometry_geojson(result: CadastreLookupResult) -> dict[str, object] | None:
    if result.geometry is None:
        return None
    try:
        geometry = mapping(result.geometry)
    except Exception:
        return None
    if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        return None
    return dict(geometry)


def _egkn_lot_url(lot: AuctionLot) -> str:
    if lot.cadastre_number:
        return "https://map.gov4c.kz/egkn/?cadastre=" + quote_plus(lot.cadastre_number)
    return "https://map.gov4c.kz/egkn/"


def _csv_settings(value: str | None) -> list[str]:
    if not value:
        return []
    return [
        item.strip()
        for item in str(value).replace(";", ",").replace("\n", ",").split(",")
        if item.strip()
    ]


def _gov_kz_base_url(value: str | None) -> str:
    parsed = urlparse(value or "")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://www.gov.kz"


def _gov_kz_lot_match(
    lot: AuctionLot,
    announcement: GovKzAnnouncement,
) -> tuple[float, list[str]]:
    text = " ".join(
        [
            announcement.title,
            announcement.body_text,
            announcement.source_url,
            *announcement.eqazyna_urls,
        ]
    ).casefold()
    score = 0.0
    strong_match = False
    reasons: list[str] = []

    lot_tokens = _gov_kz_lot_tokens(lot)
    announcement_tokens = {
        token.casefold()
        for token in (
            *announcement.lot_numbers,
            *announcement.auction_numbers,
        )
        if token
    }
    for token in lot_tokens:
        token_key = token.casefold()
        matched = token_key in announcement_tokens or any(
            token_key in url.casefold() for url in announcement.eqazyna_urls
        )
        if not matched and not token_key.isdigit() and token_key in text:
            matched = True
        if matched:
            score += 0.45
            strong_match = True
            reasons.append(f"номер {token}")
            break

    cadastre = (lot.cadastre_number or "").strip()
    if cadastre:
        cadastre_key = cadastre.casefold()
        if cadastre_key in {item.casefold() for item in announcement.cadastre_numbers} or cadastre_key in text:
            score += 0.55
            strong_match = True
            reasons.append(f"кадастр {cadastre}")

    if lot.source_url:
        lot_url = lot.source_url.rstrip("/").casefold()
        lot_path = urlparse(lot.source_url).path.rstrip("/").casefold()
        for url in announcement.eqazyna_urls:
            url_key = url.rstrip("/").casefold()
            url_path = urlparse(url).path.rstrip("/").casefold()
            if lot_url == url_key or (lot_path and lot_path == url_path):
                score += 0.8
                strong_match = True
                reasons.append("ссылка E-Qazyna")
                break

    location_hits = [
        value
        for value in (lot.region, lot.district, lot.locality)
        if value and len(value.strip()) >= 3 and value.casefold() in text
    ]
    if location_hits:
        score += min(0.15, len(location_hits) * 0.05)
        reasons.append("локация " + "/".join(location_hits[:3]))

    if not strong_match:
        return min(score, 0.35), reasons
    return min(score, 0.98), reasons


def _gov_kz_lot_tokens(lot: AuctionLot) -> set[str]:
    tokens: set[str] = set()
    for value in (lot.auction_number, lot.source_lot_id, lot.source_url):
        text = str(value or "").strip()
        if not text:
            continue
        if len(text) >= 3:
            tokens.add(text)
        tokens.update(item for item in re.findall(r"\d{3,12}", text) if len(item) >= 3)
    return tokens


def _gov_kz_evidence_text(
    announcement: GovKzAnnouncement,
    reasons: list[str],
) -> str:
    parts = [
        f"Источник: {announcement.source_kind}",
        f"Проект: {announcement.project or 'gov.kz'}",
    ]
    if reasons:
        parts.append("Совпадение: " + ", ".join(reasons))
    if announcement.lot_numbers:
        parts.append("Лоты: " + ", ".join(sorted(announcement.lot_numbers)[:5]))
    if announcement.cadastre_numbers:
        parts.append("Кадастр: " + ", ".join(sorted(announcement.cadastre_numbers)[:5]))
    if announcement.attachments:
        parts.append(f"Файлы: {len(announcement.attachments)}")
    if announcement.body_text:
        parts.append(_compact_text(announcement.body_text, 320))
    return " · ".join(parts)


def _upsert_gov_kz_attachments(
    session: Session,
    lot: AuctionLot,
    announcement: GovKzAnnouncement,
) -> int:
    saved = 0
    removed_urls = deduplicate_lot_documents(lot)
    existing_by_key = {
        auction_document_key(document): document
        for document in lot.documents
        if auction_document_key(document)
    }
    existing_urls = {document.source_url for document in lot.documents}
    for attachment in announcement.attachments:
        if not attachment.url:
            continue
        title = f"gov.kz: {attachment.title or announcement.title}"[:320]
        key = auction_document_key(attachment.url, title)
        existing = existing_by_key.get(key)
        if existing is None:
            document = AuctionDocument(
                title=title,
                source_url=attachment.url,
                file_type=attachment.file_type,
            )
            lot.documents.append(document)
            if key:
                existing_by_key[key] = document
            existing_urls.add(attachment.url)
            saved += 1
            continue
        existing.title = title
        existing.file_type = attachment.file_type or existing.file_type
        if (
            attachment.url
            and attachment.url != existing.source_url
            and attachment.url not in existing_urls
            and attachment.url not in removed_urls
        ):
            existing.source_url = attachment.url
            existing_urls.add(attachment.url)
        saved += 1
    return saved


def _coordinates_changed(
    previous_latitude: float | None,
    previous_longitude: float | None,
    latitude: float | None,
    longitude: float | None,
) -> bool:
    if previous_latitude is None or previous_longitude is None:
        return False
    if latitude is None or longitude is None:
        return True
    return (
        abs(previous_latitude - latitude) > 0.000001
        or abs(previous_longitude - longitude) > 0.000001
    )


def _osm_check_due(geo_check: AuctionLotGeoCheck) -> bool:
    if geo_check.osm_status in {None, "", "not_checked", "stale", "unavailable"}:
        return True
    checked_at = _aware(geo_check.osm_checked_at)
    if checked_at is None:
        return True
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.auction_v2_osm_ttl_minutes)
    return checked_at < cutoff


def _clear_osm_fields(geo_check: AuctionLotGeoCheck, *, status: str) -> None:
    geo_check.osm_status = status
    geo_check.osm_checked_at = None
    geo_check.road_distance_m = None
    geo_check.power_distance_m = None
    geo_check.water_distance_m = None
    geo_check.open_water_distance_m = None
    geo_check.cemetery_distance_m = None
    geo_check.object_distance_m = None
    geo_check.object_kind = None
    geo_check.engineering_status = "manual_required"


def _mark_osm_unavailable(geo_check: AuctionLotGeoCheck, exc: Exception) -> None:
    now = datetime.now(UTC)
    geo_check.osm_status = "unavailable"
    geo_check.engineering_status = "manual_required"
    geo_check.osm_checked_at = now
    geo_check.updated_at = now
    geo_check.notes = _geo_check_notes(geo_check, extra=str(exc))


def _apply_osm_surroundings(
    geo_check: AuctionLotGeoCheck,
    surroundings: Surroundings,
    *,
    checked_at: datetime | None = None,
) -> None:
    now = checked_at or datetime.now(UTC)
    geo_check.osm_status = "checked" if surroundings.checked else "not_checked"
    geo_check.osm_checked_at = now if surroundings.checked else None
    geo_check.road_distance_m = surroundings.road_distance_m
    geo_check.power_distance_m = surroundings.power_distance_m
    geo_check.water_distance_m = surroundings.water_distance_m
    geo_check.open_water_distance_m = surroundings.open_water_distance_m
    geo_check.cemetery_distance_m = surroundings.cemetery_distance_m
    geo_check.object_distance_m = surroundings.object_distance_m
    geo_check.object_kind = surroundings.object_kind
    geo_check.engineering_status = _engineering_status_from_osm(geo_check)
    geo_check.notes = _geo_check_notes(geo_check)
    geo_check.updated_at = now


def _engineering_status_from_osm(geo_check: AuctionLotGeoCheck) -> str:
    if geo_check.osm_status != "checked":
        return "manual_required"
    if (
        geo_check.open_water_distance_m is not None
        and geo_check.open_water_distance_m <= settings.osm_open_water_clearance_m
    ):
        return "warning"
    if (
        geo_check.object_distance_m is not None
        and geo_check.object_distance_m <= settings.auction_v2_object_clearance_m
    ):
        return "warning"
    if (
        geo_check.power_distance_m is not None
        and geo_check.power_distance_m <= settings.auction_v2_power_clearance_m
    ):
        return "warning"
    return "checked"


def _geo_check_notes(geo_check: AuctionLotGeoCheck, *, extra: str | None = None) -> str:
    if geo_check.coordinate_status == "unconfirmed":
        parts = [
            "Координаты из источника не подтверждены как точка в Казахстане; маркер скрыт до ручной сверки по ЕГКН, адресу или документам."
        ]
    elif geo_check.coordinate_status != "found":
        parts = [
            "Координаты не найдены автоматически; нужен ручной поиск по кадастру, адресу или приложенным документам."
        ]
    else:
        parts = [
            "Координаты найдены в карточке/описании лота; градостроительные ограничения требуют сверки по официальным слоям."
        ]
    if geo_check.osm_status == "checked":
        parts.append(
            "OSM проверил окружение: "
            f"дорога {_format_distance_m(geo_check.road_distance_m)}, "
            f"энергия {_format_distance_m(geo_check.power_distance_m)}, "
            f"вода {_format_distance_m(geo_check.water_distance_m)}, "
            f"открытая вода {_format_distance_m(geo_check.open_water_distance_m)}."
        )
    elif geo_check.osm_status == "stale":
        parts.append("OSM-метрики устарели после изменения координат и должны быть пересчитаны.")
    elif geo_check.osm_status == "unavailable":
        parts.append("OSM/Overpass не ответил; инфраструктура требует ручной сверки.")
    elif geo_check.osm_status == "not_checked" and geo_check.coordinate_status == "found":
        parts.append("OSM-инфраструктура еще не проверена синхронизацией v2.")
    if extra:
        parts.append(f"Техническая деталь: {extra[:240]}.")
    return " ".join(parts)


def _format_distance_m(value: float | None) -> str:
    if value is None:
        return "нет данных"
    if value >= 1000:
        return f"{value / 1000:.1f} км"
    return f"{value:.0f} м"


def _market_comparable_stats(
    session: Session,
    lot: AuctionLot,
) -> AuctionV2MarketStats:
    comparables = list_auction_v2_market_comparables(session, lot.id)
    active_comparables = [
        item for item in comparables if item.listing_status != "removed"
    ]
    prices = [
        item.price_per_sotka
        for item in active_comparables
        if item.price_per_sotka is not None
        and item.price_per_sotka > 0
    ]
    source_names = sorted(
        {
            item.source_name
            for item in active_comparables
            if item.source_name
        }
    )
    if not prices:
        return AuctionV2MarketStats(
            comparable_count=len(active_comparables),
            source_names=source_names,
        )
    return AuctionV2MarketStats(
        comparable_count=len(active_comparables),
        priced_count=len(prices),
        average_price_per_sotka=sum(prices) / len(prices),
        median_price_per_sotka=median(prices),
        min_price_per_sotka=min(prices),
        max_price_per_sotka=max(prices),
        source_names=source_names,
    )


def _market_status_detail(market_stats: AuctionV2MarketStats) -> str:
    if not market_stats.comparable_count:
        return "Рыночные аналоги еще не добавлены."
    parts = [f"Добавлено аналогов: {market_stats.comparable_count}."]
    if market_stats.average_price_per_sotka is not None:
        parts.append(
            f"Средняя цена аналогов: {_money(market_stats.average_price_per_sotka)} за сотку."
        )
    if market_stats.median_price_per_sotka is not None:
        parts.append(
            f"Медиана: {_money(market_stats.median_price_per_sotka)} за сотку."
        )
    if market_stats.source_names:
        parts.append("Источники: " + ", ".join(market_stats.source_names[:4]) + ".")
    return " ".join(parts)


def _market_difference_percent(
    lot: AuctionLot,
    market_stats: AuctionV2MarketStats,
) -> float | None:
    lot_price = _lot_price_per_sotka(lot)
    return _market_difference_percent_from_price(
        lot_price,
        market_stats.average_price_per_sotka,
    )


def _market_difference_percent_from_price(
    lot_price: float | None,
    average: float | None,
) -> float | None:
    if lot_price is None or average is None or average <= 0:
        return None
    return ((lot_price - average) / average) * 100


def _lot_price_per_sotka(lot: AuctionLot) -> float | None:
    if lot.start_price_kzt is None or lot.area_ha is None or lot.area_ha <= 0:
        return None
    return lot.start_price_kzt / (lot.area_ha * 100)


def _source_statuses(
    session: Session,
    lot: AuctionLot,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    market_stats: AuctionV2MarketStats | None = None,
) -> list[dict[str, object]]:
    market_stats = market_stats or _market_comparable_stats(session, lot)
    statuses = [
        {
            "code": "eqazyna_current_lots",
            "name": "E-Qazyna",
            "group": "Официальный лот",
            "status": "ok",
            "label": SOURCE_STATUS_LABELS["ok"],
            "detail": "Лот загружен из официальной карточки торгов.",
            "url": lot.source_url,
        },
        {
            "code": "eqazyna_documents",
            "name": "Документы E-Qazyna",
            "group": "Официальный лот",
            "status": "ok" if metrics.document_count else "missing",
            "label": SOURCE_STATUS_LABELS["ok" if metrics.document_count else "missing"],
            "detail": (
                f"Найдено документов: {metrics.document_count}."
                if metrics.document_count
                else "Документы не найдены в карточке; нужен ручной запрос/сверка."
            ),
            "url": lot.source_url,
        },
        {
            "code": "gov_kz_akimat_announcements",
            "name": "gov.kz",
            "group": "Объявления акиматов",
            "status": (
                "ok"
                if _latest_lot_evidence(
                    session,
                    lot.id,
                    "gov_kz_akimat_announcements",
                    "akimat_announcement",
                )
                else "query_ready"
            ),
            "label": SOURCE_STATUS_LABELS[
                "ok"
                if _latest_lot_evidence(
                    session,
                    lot.id,
                    "gov_kz_akimat_announcements",
                    "akimat_announcement",
                )
                else "query_ready"
            ],
            "detail": _gov_kz_status_detail(session, lot),
            "url": _gov_kz_status_url(session, lot),
        },
        {
            "code": "egkn_public_map",
            "name": "ЕГКН",
            "group": "Кадастр",
            "status": _status_from_geo_check(geo_check),
            "label": SOURCE_STATUS_LABELS[_status_from_geo_check(geo_check)],
            "detail": _egkn_status_detail(lot, geo_check),
            "url": geo_check.egkn_url,
        },
        {
            "code": "smart_geohub_genplans",
            "name": "Генплан/ПДП",
            "group": "Градостроительные ограничения",
            "status": "manual_required",
            "label": SOURCE_STATUS_LABELS["manual_required"],
            "detail": "Нужно сопоставить участок со слоями генплана, красными линиями и ПДП.",
            "url": "https://gov.ggk.kz/",
        },
        {
            "code": "regional_geoportals",
            "name": "Региональные геопорталы",
            "group": "Геопроверка",
            "status": "manual_required",
            "label": SOURCE_STATUS_LABELS["manual_required"],
            "detail": "Региональные порталы дают локальные слои, которые не всегда есть в ЕГКН.",
            "url": "https://geo-shym.kz/map/?access_token=&lang=ru",
        },
        {
            "code": "krisha_land_market",
            "name": "Krisha: рынок для цены",
            "group": "Рыночные аналоги, не аукционы",
            "status": "ok" if market_stats.comparable_count else "query_ready",
            "label": SOURCE_STATUS_LABELS["ok" if market_stats.comparable_count else "query_ready"],
            "detail": (
                _market_status_detail(market_stats)
                if market_stats.comparable_count
                else "Krisha не является источником государственных аукционов. Ссылка нужна только для ручного сравнения стартовой цены с похожими участками в продаже."
            ),
            "url": "https://krisha.kz/prodazha/uchastkov/kazaxstan/",
        },
        {
            "code": "olx_land_market",
            "name": "OLX: рынок для цены",
            "group": "Рыночные аналоги, не аукционы",
            "status": "query_ready",
            "label": SOURCE_STATUS_LABELS["query_ready"],
            "detail": "OLX не является источником государственных аукционов. Это дополнительная ручная проверка рынка, если нужно уточнить цену.",
            "url": "https://www.olx.kz/nedvizhimost/zemlya/prodazha/",
        },
        {
            "code": "osm_overpass",
            "name": "OSM / карты",
            "group": "Инфраструктура",
            "status": _osm_source_status(geo_check),
            "label": SOURCE_STATUS_LABELS[_osm_source_status(geo_check)],
            "detail": _osm_status_detail(geo_check),
            "url": geo_check.google_maps_url,
        },
        {
            "code": "egov_official_actions",
            "name": "eGov / E-Qazyna действия",
            "group": "Официальное участие",
            "status": "external_action",
            "label": SOURCE_STATUS_LABELS["external_action"],
            "detail": "Zhertap доводит до решения; подача заявки, ЭЦП, гарантийный взнос и торги выполняются только на официальных порталах.",
            "url": "https://www.gov.kz/services/5169",
        },
    ]
    return statuses


def _gov_kz_status_detail(session: Session, lot: AuctionLot) -> str:
    evidence = _latest_lot_evidence(
        session,
        lot.id,
        "gov_kz_akimat_announcements",
        "akimat_announcement",
    )
    if evidence is not None:
        return _compact_text(
            f"Найдено объявление акимата: {evidence.title}. {evidence.value_text or ''}",
            360,
        )
    return (
        "Zhertap ищет объявления акиматов по номеру лота, кадастру, E-Qazyna-ссылке "
        "и локации. Если совпадение не найдено автоматически, откройте поиск gov.kz "
        "или добавьте ссылку вручную для сверки."
    )


def _gov_kz_status_url(session: Session, lot: AuctionLot) -> str:
    evidence = _latest_lot_evidence(
        session,
        lot.id,
        "gov_kz_akimat_announcements",
        "akimat_announcement",
    )
    return evidence.source_url if evidence is not None and evidence.source_url else "https://www.gov.kz/memleket/entities?lang=ru"


def _latest_lot_evidence(
    session: Session,
    lot_id: str,
    source_code: str,
    evidence_type: str,
) -> AuctionEvidence | None:
    return session.scalar(
        select(AuctionEvidence)
        .join(AuctionSource, AuctionSource.id == AuctionEvidence.source_id)
        .where(
            AuctionEvidence.lot_id == lot_id,
            AuctionSource.code == source_code,
            AuctionEvidence.evidence_type == evidence_type,
        )
        .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
        .limit(1)
    )


def _osm_source_status(geo_check: AuctionLotGeoCheck) -> str:
    if geo_check.coordinate_status != "found":
        return "manual_required"
    if geo_check.osm_status == "checked":
        return "ok" if geo_check.engineering_status == "checked" else "warning"
    if geo_check.osm_status in {"unavailable", "stale"}:
        return "warning"
    return "manual_required"


def _osm_status_detail(geo_check: AuctionLotGeoCheck) -> str:
    if geo_check.coordinate_status == "unconfirmed":
        return "Координаты не подтверждены как точка в Казахстане; OSM-проверка отключена до ручной геосверки."
    if geo_check.coordinate_status != "found":
        return "Координаты не найдены, OSM-проверка дорог, воды, энергии и объектов требует ручного поиска."
    if geo_check.osm_status == "checked":
        return (
            "OSM проверен: "
            f"дорога {_format_distance_m(geo_check.road_distance_m)}, "
            f"энергия {_format_distance_m(geo_check.power_distance_m)}, "
            f"вода {_format_distance_m(geo_check.water_distance_m)}, "
            f"открытая вода {_format_distance_m(geo_check.open_water_distance_m)}, "
            f"объект {_format_distance_m(geo_check.object_distance_m)}."
        )
    if geo_check.osm_status == "unavailable":
        return "Overpass/OSM не ответил во время синхронизации; инфраструктуру надо сверить вручную."
    if geo_check.osm_status == "stale":
        return "Координаты изменились после последней OSM-проверки; нужен повторный пересчет."
    return "Координаты есть, но OSM-инфраструктура еще не проверена синхронизацией v2."


def _status_from_geo_check(geo_check: AuctionLotGeoCheck) -> str:
    if geo_check.cadastre_status == "verified" and geo_check.coordinate_status == "found":
        return "ok"
    if geo_check.cadastre_status in {"not_found", "unavailable"}:
        return "warning"
    if geo_check.cadastre_status in {"found", "verified"} or geo_check.coordinate_status == "found":
        return "manual_required"
    return "missing"


def _egkn_status_detail(lot: AuctionLot, geo_check: AuctionLotGeoCheck) -> str:
    parts = []
    if geo_check.cadastre_status == "verified" and lot.cadastre_number:
        parts.append(f"ЕГКН подтвердил кадастр {lot.cadastre_number}")
    elif geo_check.cadastre_status == "not_found" and lot.cadastre_number:
        parts.append(f"кадастр {lot.cadastre_number} не найден в публичном слое ЕГКН")
    elif geo_check.cadastre_status == "unavailable" and lot.cadastre_number:
        parts.append(f"кадастр {lot.cadastre_number}: ЕГКН временно недоступен")
    elif lot.cadastre_number:
        parts.append(f"кадастр {lot.cadastre_number}")
    else:
        parts.append("кадастровый номер не найден")
    if geo_check.coordinate_status == "unconfirmed":
        parts.append("координаты не подтверждены как точка в Казахстане")
    elif geo_check.latitude is not None and geo_check.longitude is not None:
        parts.append(f"координаты {geo_check.latitude:.6f}, {geo_check.longitude:.6f}")
    else:
        parts.append("координаты не найдены автоматически")
    return "; ".join(parts) + "."


def _risk_flags(
    lot: AuctionLot,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    market_stats: AuctionV2MarketStats,
) -> list[dict[str, object]]:
    flags: list[dict[str, object]] = []
    if not lot.cadastre_number:
        flags.append(
            {
                "code": "no_cadastre",
                "level": "medium",
                "label": "Кадастр не указан в карточке E-Qazyna",
                "detail": "Это пробел в данных источника, а не риск участка. Перед заявкой уточните номер в документах или на публичной кадастровой карте.",
            }
        )
    elif geo_check.cadastre_status == "not_found":
        flags.append(
            {
                "code": "cadastre_not_confirmed_egkn",
                "level": "medium",
                "label": "ЕГКН не подтвердил кадастр автоматически",
                "detail": "Это не означает проблему с участком: номер мог отсутствовать в источнике или отличаться по формату. Сверьте его с документами перед заявкой.",
            }
        )
    elif geo_check.cadastre_status == "unavailable":
        flags.append(
            {
                "code": "egkn_unavailable",
                "level": "medium",
                "label": "ЕГКН временно недоступен",
                "detail": "Не удалось автоматически подтвердить кадастровую геометрию; нужно повторить синхронизацию или проверить публичную кадастровую карту вручную.",
            }
        )
    if geo_check.boundary_status == "warning":
        difference = geo_check.boundary_difference_percent
        flags.append(
            {
                "code": "boundary_area_mismatch",
                "level": "high",
                "label": "Площадь границы отличается",
                "detail": (
                    f"ЕГКН и карточка лота расходятся примерно на {abs(difference or 0):.1f}%. "
                    "До решения нужно сверить схему, координаты и документ-основание."
                ),
            }
        )
    elif geo_check.boundary_status == "manual_required":
        flags.append(
            {
                "code": "boundary_not_confirmed",
                "level": "high",
                "label": "Граница участка не подтверждена",
                "detail": "Номер найден или указан, но геометрия участка еще не подтверждена в доступном источнике.",
            }
        )
    if geo_check.coordinate_status == "unconfirmed":
        flags.append(
            {
                "code": "coordinates_unconfirmed",
                "level": "high",
                "label": "Координаты не подтверждены",
                "detail": "Источник вернул подозрительную точку вне Казахстана или старые координаты; карта скрывает маркер до ручной проверки.",
            }
        )
    elif geo_check.coordinate_status != "found":
        flags.append(
            {
                "code": "no_coordinates",
                "level": "high",
                "label": "Нет координат",
                "detail": "Нельзя автоматически оценить окружение, дороги, красные линии и фактическое положение.",
            }
        )
    if geo_check.osm_status == "unavailable":
        flags.append(
            {
                "code": "osm_unavailable",
                "level": "medium",
                "label": "OSM не ответил",
                "detail": "Не удалось автоматически сверить дороги, воду, энергию и ближайшие объекты; нужна ручная проверка карты.",
            }
        )
    if (
        geo_check.osm_status == "checked"
        and geo_check.open_water_distance_m is not None
        and geo_check.open_water_distance_m <= settings.osm_open_water_clearance_m
    ):
        flags.append(
            {
                "code": "open_water_nearby",
                "level": "medium",
                "label": "Рядом открытая вода",
                "detail": f"OSM показывает открытую воду примерно в {_format_distance_m(geo_check.open_water_distance_m)}; проверьте ограничения, подтопление и санитарные зоны.",
            }
        )
    if (
        geo_check.osm_status == "checked"
        and geo_check.object_distance_m is not None
        and geo_check.object_distance_m <= settings.auction_v2_object_clearance_m
    ):
        flags.append(
            {
                "code": "mapped_object_nearby",
                "level": "medium",
                "label": "Рядом размеченный объект",
                "detail": f"Ближайший объект OSM примерно в {_format_distance_m(geo_check.object_distance_m)}; нужно сверить фактические границы и сервитуты.",
            }
        )
    if (
        geo_check.osm_status == "checked"
        and geo_check.power_distance_m is not None
        and geo_check.power_distance_m <= settings.auction_v2_power_clearance_m
    ):
        flags.append(
            {
                "code": "power_nearby",
                "level": "medium",
                "label": "Рядом энергетическая инфраструктура",
                "detail": f"OSM показывает энергетическую инфраструктуру примерно в {_format_distance_m(geo_check.power_distance_m)}; проверьте охранные зоны и красные линии.",
            }
        )
    if (
        geo_check.osm_status == "checked"
        and geo_check.road_distance_m is None
    ):
        flags.append(
            {
                "code": "no_road_nearby_osm",
                "level": "medium",
                "label": "Дорога не найдена рядом",
                "detail": "В радиусе OSM-проверки не найден подъезд; надо проверить доступ к участку и фактические дороги.",
            }
        )
    if metrics.document_count == 0:
        flags.append(
            {
                "code": "no_documents",
                "level": "medium",
                "label": "Нет приложенных документов",
                "detail": "Нужна ручная сверка извещения, условий торгов и схемы участка.",
            }
        )
    if not lot.land_rights:
        flags.append(
            {
                "code": "unknown_land_rights",
                "level": "medium",
                "label": "Не указано право на землю",
                "detail": "Нужно понять: продажа участка, аренда, срок аренды и ограничения использования.",
            }
        )
    if metrics.district_difference_percent is not None and metrics.district_difference_percent >= 35:
        flags.append(
            {
                "code": "price_above_history",
                "level": "medium",
                "label": "Старт выше истории района",
                "detail": f"Цена за сотку выше районного ориентира примерно на {metrics.district_difference_percent:.0f}%.",
            }
        )
    market_difference = _market_difference_percent(lot, market_stats)
    if market_difference is not None and market_difference >= 25:
        flags.append(
            {
                "code": "price_above_market_comparables",
                "level": "medium",
                "label": "Старт выше рыночных аналогов",
                "detail": f"Добавленные аналоги показывают старт примерно на {market_difference:.0f}% выше средней цены рынка; лимит должен быть осторожным.",
            }
        )
    elif market_difference is not None and market_difference <= -25:
        flags.append(
            {
                "code": "price_below_market_comparables",
                "level": "low",
                "label": "Старт ниже рыночных аналогов",
                "detail": f"Добавленные аналоги показывают старт примерно на {abs(market_difference):.0f}% ниже средней цены рынка; это потенциальное преимущество после проверки документов.",
            }
        )
    starts_at = _aware(lot.auction_starts_at)
    if starts_at is None:
        flags.append(
            {
                "code": "unknown_deadline",
                "level": "medium",
                "label": "Нет даты торгов",
                "detail": "Нельзя оценить запас времени на гарантийный взнос и официальную заявку.",
            }
        )
    else:
        hours_left = (starts_at - datetime.now(UTC)).total_seconds() / 3600
        if hours_left < 0:
            flags.append(
                {
                    "code": "auction_started_or_finished",
                    "level": "high",
                    "label": "Торги уже начались или прошли",
                    "detail": "Лот нужно проверить по официальному статусу перед дальнейшей работой.",
                }
            )
        elif hours_left <= 24:
            flags.append(
                {
                    "code": "urgent_deadline",
                    "level": "medium",
                    "label": "Меньше суток до торгов",
                    "detail": "Мало времени на документы, гарантийный взнос и ручную проверку.",
                }
            )
    if geo_check.urban_plan_status == "manual_required":
        flags.append(
            {
                "code": "manual_genplan_required",
                "level": "medium",
                "label": "Нужна сверка генплана",
                "detail": "Перед участием нужно проверить функциональную зону, ПДП и красные линии.",
            }
        )
    return flags


def _readiness(
    lot: AuctionLot,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    source_statuses: list[dict[str, object]],
) -> list[dict[str, object]]:
    source_ok = sum(1 for item in source_statuses if item.get("status") == "ok")
    return [
        {
            "code": "official_card",
            "label": "Официальная карточка найдена",
            "status": "done" if lot.source_url else "missing",
            "detail": "Есть ссылка на лот E-Qazyna." if lot.source_url else "Нет ссылки на официальный лот.",
            "url": lot.source_url,
        },
        {
            "code": "documents",
            "label": "Документы и условия торгов",
            "status": "done" if metrics.document_count else "manual",
            "detail": (
                f"Документов в карточке: {metrics.document_count}."
                if metrics.document_count
                else "Нужно открыть официальный лот и проверить приложения."
            ),
        },
        {
            "code": "cadastre",
            "label": "Кадастр и границы",
            "status": (
                "done"
                if geo_check.cadastre_status == "verified"
                and geo_check.coordinate_status == "found"
                else "manual"
            ),
            "detail": _egkn_status_detail(lot, geo_check),
            "url": geo_check.egkn_url,
        },
        {
            "code": "urban_plan",
            "label": "Генплан, ПДП, красные линии",
            "status": "manual",
            "detail": "Автоматический контур частично готов; нужна сверка официальных слоев перед участием.",
        },
        {
            "code": "market",
            "label": "Цена относительно истории",
            "status": "done" if metrics.price_per_sotka is not None else "manual",
            "detail": _price_position_text(metrics),
        },
        {
            "code": "official_action_boundary",
            "label": "Граница официальных действий",
            "status": "external",
            "detail": "Заявка, ЭЦП, гарантийный взнос и торги выполняются пользователем на E-Qazyna/eGov.",
        },
        {
            "code": "source_coverage",
            "label": "Покрытие источников",
            "status": "done" if source_ok >= 3 else "manual",
            "detail": f"Проверено автоматически: {source_ok} источника(ов); остальные отмечены для ручной или будущей интеграции.",
        },
    ]


def _official_readiness(
    *,
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    pipeline: AuctionUserLotPipeline | None,
    source_statuses: list[dict[str, object]],
) -> list[dict[str, object]]:
    deadline_label, deadline_status = _deadline_payload(lot)
    personal_limit_saved = pipeline is not None and pipeline.max_bid_kzt is not None
    ready_stage = pipeline is not None and pipeline.stage in {
        "ready_for_official_site",
        "decided_to_participate",
        "skipped",
    }
    return [
        {
            "code": "external_ecp",
            "label": "ЭЦП готова",
            "status": "external",
            "detail": "Проверить срок действия ЭЦП вне Zhertap. Сервис не хранит ключи и пароли.",
            "url": "https://pki.gov.kz/",
        },
        {
            "code": "external_ncalayer",
            "label": "NCALayer проверен",
            "status": "external",
            "detail": "Запустить NCALayer и проверить подписание на официальном контуре до подачи заявки.",
            "url": "https://ncl.pki.gov.kz/",
        },
        {
            "code": "external_bank_details",
            "label": "Реквизиты для возврата готовы",
            "status": "external",
            "detail": "Банковские реквизиты вводятся только на официальной площадке; Zhertap их не хранит.",
        },
        {
            "code": "official_lot",
            "label": "Официальный лот открыт",
            "status": "done" if lot.source_url else "manual",
            "detail": "Есть ссылка на карточку E-Qazyna." if lot.source_url else "Нужна ссылка на официальный лот E-Qazyna.",
            "url": lot.source_url,
        },
        {
            "code": "deadline",
            "label": "Срок до торгов понятен",
            "status": _official_deadline_status(deadline_status),
            "detail": deadline_label,
        },
        {
            "code": "guarantee",
            "label": "Гарантийный взнос известен",
            "status": "done" if lot.guarantee_kzt is not None else "manual",
            "detail": (
                f"Сумма: {_money(lot.guarantee_kzt)}."
                if lot.guarantee_kzt is not None
                else "В карточке нет суммы гарантийного взноса; проверьте официальный лот."
            ),
        },
        {
            "code": "documents",
            "label": "Документы просмотрены",
            "status": "done" if metrics.document_count else "manual",
            "detail": (
                f"В карточке найдено документов: {metrics.document_count}."
                if metrics.document_count
                else "Документы не найдены автоматически; открыть официальный лот и проверить приложения."
            ),
        },
        {
            "code": "cadastre_boundaries",
            "label": "Кадастр и координаты сверены",
            "status": (
                "done"
                if geo_check.cadastre_status == "verified"
                and geo_check.coordinate_status == "found"
                else "manual"
            ),
            "detail": _egkn_status_detail(lot, geo_check),
            "url": geo_check.egkn_url,
        },
        {
            "code": "urban_plan",
            "label": "Генплан и ограничения отмечены",
            "status": "done" if geo_check.urban_plan_status == "checked" else "manual",
            "detail": "Автоматическая проверка генплана отмечена как выполненная." if geo_check.urban_plan_status == "checked" else "Нужна ручная сверка генплана, ПДП, красных линий и ограничений.",
            "url": "https://gov.ggk.kz/",
        },
        {
            "code": "market_limit",
            "label": "Рыночный потолок рассчитан",
            "status": "done" if analysis.max_bid_market_kzt is not None else "manual",
            "detail": (
                f"Ориентир рынка: {_money(analysis.max_bid_market_kzt)}."
                if analysis.max_bid_market_kzt is not None
                else "Не хватает цены, площади или рыночных ориентиров."
            ),
        },
        {
            "code": "personal_limit",
            "label": "Личный максимум сохранен",
            "status": "done" if personal_limit_saved else "manual",
            "detail": (
                f"Ваш лимит: {_money(pipeline.max_bid_kzt)}."
                if personal_limit_saved and pipeline is not None
                else "Заполните поле “Мой лимит” в pipeline до перехода на официальный портал."
            ),
        },
        {
            "code": "decision",
            "label": "Решение по лоту зафиксировано",
            "status": "done" if ready_stage else "manual",
            "detail": (
                f"Статус: {_stage_label(pipeline.stage)}."
                if ready_stage and pipeline is not None
                else "Выберите рабочий статус: готов к E-Qazyna/eGov, буду участвовать или пропустить."
            ),
        },
        {
            "code": "official_boundary",
            "label": "Граница действий понятна",
            "status": "external",
            "detail": "Дальше только внешний портал: заявка, ЭЦП, гарантийный взнос и торги выполняются пользователем на E-Qazyna/eGov.",
            "url": "https://www.gov.kz/services/5169",
        },
    ]


def _official_deadline_status(deadline_status: str) -> str:
    if deadline_status in {"normal", "soon"}:
        return "done"
    if deadline_status == "urgent":
        return "warning"
    return "manual"


def _buyer_workflow(
    *,
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    pipeline: AuctionUserLotPipeline | None,
    source_statuses: list[dict[str, object]],
    official_readiness: list[dict[str, object]],
) -> list[dict[str, object]]:
    sources = {str(item.get("code") or ""): item for item in source_statuses}
    official = {str(item.get("code") or ""): item for item in official_readiness}
    decision_done = pipeline is not None and pipeline.stage in {
        "ready_for_official_site",
        "decided_to_participate",
        "skipped",
    }
    personal_limit_done = pipeline is not None and pipeline.max_bid_kzt is not None

    return [
        _workflow_step(
            code="find_lot",
            title="1. Найти официальный лот",
            source="E-Qazyna",
            status="done" if lot.source_url else "manual",
            system_result=(
                f"Лот {lot.auction_number or lot.source_lot_id} загружен в единый список."
                if lot.source_url
                else "Официальная ссылка на лот пока не найдена."
            ),
            manual_action="Открыть E-Qazyna и сверить номер лота, статус приема заявок, продавца и дату торгов.",
            url=lot.source_url,
            url_label="Открыть E-Qazyna",
        ),
        _workflow_step(
            code="read_documents",
            title="2. Разобрать документы и условия",
            source="E-Qazyna / приложения",
            status="done" if metrics.document_count else "manual",
            system_result=(
                f"Документов найдено: {metrics.document_count}."
                if metrics.document_count
                else "Документы не извлечены автоматически."
            ),
            manual_action="Проверить PDF, схему участка, условия, гарантийный взнос, ограничения и сроки регистрации.",
            url=lot.source_url,
            url_label="Документы E-Qazyna",
        ),
        _workflow_step_from_source(
            sources.get("gov_kz_akimat_announcements"),
            code="akimat_check",
            title="3. Найти ранние публикации акимата",
            fallback_source="gov.kz / сайты акиматов",
            fallback_result="Zhertap ищет совпадения по номеру лота, кадастру, ссылке и локации.",
            manual_action="Открыть gov.kz или сайт акимата и проверить, не было ли ранних объявлений, изменений или документов.",
            fallback_url="https://www.gov.kz/memleket/entities?lang=ru",
            fallback_url_label="Открыть gov.kz",
        ),
        _workflow_step(
            code="cadastre_check",
            title="4. Сверить кадастр и границы",
            source="ЕГКН / публичная кадастровая карта",
            status=(
                "done"
                if geo_check.cadastre_status == "verified"
                and geo_check.coordinate_status == "found"
                else "manual"
            ),
            system_result=_egkn_status_detail(lot, geo_check),
            manual_action="Проверить кадастровый номер, границы, координаты и совпадение участка с документами лота.",
            url=geo_check.egkn_url,
            url_label="Открыть ЕГКН",
        ),
        _workflow_step_from_source(
            sources.get("smart_geohub_genplans"),
            code="urban_plan_check",
            title="5. Проверить генплан, ПДП и ограничения",
            fallback_source="Smart Geohub / ГГК / геопорталы",
            fallback_result=(
                "Градостроительные ограничения требуют ручной сверки по официальным слоям."
                if geo_check.urban_plan_status != "checked"
                else "Проверка градостроительных слоев отмечена как выполненная."
            ),
            manual_action="Сверить функциональную зону, ПДП, красные линии, санитарные и инженерные ограничения.",
            fallback_url="https://gov.ggk.kz/",
            fallback_url_label="Открыть генплан",
            override_status="done" if geo_check.urban_plan_status == "checked" else None,
        ),
        _workflow_step_from_source(
            sources.get("osm_overpass"),
            code="infrastructure_check",
            title="6. Проверить окружение и инфраструктуру",
            fallback_source="OpenStreetMap / Google Maps",
            fallback_result=_osm_status_detail(geo_check),
            manual_action="Посмотреть подъезд, ЛЭП, воду, близкие объекты, санитарные риски и реальное окружение на карте.",
            fallback_url=geo_check.google_maps_url,
            fallback_url_label="Открыть карту",
        ),
        _workflow_step(
            code="market_check",
            title="7. Сравнить рынок и цену за сотку",
            source="История торгов / Krisha / OLX",
            status="done" if analysis.max_bid_market_kzt is not None else "manual",
            system_result=(
                f"Рыночный ориентир рассчитан: {_money(analysis.max_bid_market_kzt)}."
                if analysis.max_bid_market_kzt is not None
                else "Не хватает истории, площади, цены или добавленных рыночных аналогов."
            ),
            manual_action="Открыть Krisha/OLX, найти похожие участки рядом и добавить 1-3 аналога в карточку.",
            url=(sources.get("krisha_land_market") or {}).get("url"),
            url_label="Открыть Krisha",
        ),
        _workflow_step(
            code="decision_limit",
            title="8. Зафиксировать решение и лимит",
            source="Личный pipeline Zhertap",
            status="done" if decision_done and personal_limit_done else "manual",
            system_result=(
                f"Статус: {_stage_label(pipeline.stage)}; лимит: {_money(pipeline.max_bid_kzt)}."
                if decision_done and personal_limit_done and pipeline is not None
                else "Решение или личный максимум еще не зафиксированы."
            ),
            manual_action="Выбрать рабочий статус, сохранить личный максимум ставки и заметку перед официальным переходом.",
        ),
        _workflow_step(
            code="official_handoff",
            title="9. Перейти к юридически значимому действию",
            source="E-Qazyna / eGov",
            status="external",
            system_result=(official.get("official_boundary") or {}).get(
                "detail",
                "Zhertap доводит до решения и не выполняет юридически значимые действия.",
            ),
            manual_action="Подача заявки, ЭЦП, гарантийный взнос, торги и подписание выполняются только пользователем на официальном портале.",
            url=lot.source_url or "https://www.gov.kz/services/5169",
            url_label="Официальный портал",
        ),
    ]


def _workflow_step(
    *,
    code: str,
    title: str,
    source: str,
    status: str,
    system_result: object,
    manual_action: str,
    url: object | None = None,
    url_label: str | None = None,
) -> dict[str, object]:
    status_value = status if status in WORKFLOW_STATUS_LABELS else "manual"
    return {
        "code": code,
        "title": title,
        "source": source,
        "status": status_value,
        "status_label": WORKFLOW_STATUS_LABELS[status_value],
        "system_result": str(system_result or "Нет данных."),
        "manual_action": manual_action,
        "url": str(url) if url else None,
        "url_label": url_label or "Открыть источник",
    }


def _workflow_step_from_source(
    source_status: dict[str, object] | None,
    *,
    code: str,
    title: str,
    fallback_source: str,
    fallback_result: str,
    manual_action: str,
    fallback_url: object | None = None,
    fallback_url_label: str | None = None,
    override_status: str | None = None,
) -> dict[str, object]:
    raw_status = str((source_status or {}).get("status") or "")
    status = override_status or _workflow_status_from_source_status(raw_status)
    return _workflow_step(
        code=code,
        title=title,
        source=str(
            (source_status or {}).get("name")
            or (source_status or {}).get("group")
            or fallback_source
        ),
        status=status,
        system_result=(source_status or {}).get("detail") or fallback_result,
        manual_action=manual_action,
        url=(source_status or {}).get("url") or fallback_url,
        url_label=fallback_url_label,
    )


def _workflow_status_from_source_status(status: str) -> str:
    if status == "ok":
        return "done"
    if status == "warning":
        return "warning"
    if status == "missing":
        return "missing"
    if status == "external_action":
        return "external"
    return "manual"


def _lot_review_steps(
    *,
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    pipeline: AuctionUserLotPipeline | None,
    readiness: list[dict[str, object]],
    risk_flags: list[dict[str, object]],
    source_statuses: list[dict[str, object]],
    official_readiness: list[dict[str, object]],
) -> list[dict[str, object]]:
    sources = {str(item.get("code") or ""): item for item in source_statuses}
    readiness_by_code = {str(item.get("code") or ""): item for item in readiness}
    official_by_code = {str(item.get("code") or ""): item for item in official_readiness}
    gov_source = sources.get("gov_kz_akimat_announcements")
    urban_source = sources.get("smart_geohub_genplans")
    osm_source = sources.get("osm_overpass")
    market_source = sources.get("krisha_land_market")
    ready_for_portal = analysis.recommended_action == "prepare_official_review"
    decision_saved = pipeline is not None and pipeline.stage in {
        "ready_for_official_site",
        "decided_to_participate",
        "skipped",
    }
    return [
        _review_step(
            code="official_lot",
            title="Официальный лот и сроки",
            source="E-Qazyna / Gosreestr",
            status="done" if lot.source_url else "missing",
            system_result=(
                f"Лот найден: {lot.auction_number or lot.source_lot_id}. {official_by_code.get('deadline', {}).get('detail') or ''}"
                if lot.source_url
                else "Официальная карточка лота пока не найдена."
            ),
            manual_action="Открыть официальный лот, сверить статус приема заявок, дату торгов, продавца, стартовую цену и гарантийный взнос.",
            impact="Без официальной карточки нельзя переходить к заявке.",
            url=lot.source_url,
            url_label="Открыть официальный лот",
            anchor="#auction-v2-official-checklist",
        ),
        _review_step(
            code="documents",
            title="Документы и условия торгов",
            source="PDF / приложения E-Qazyna / gov.kz",
            status="done" if metrics.document_count else "missing",
            system_result=(
                f"Найдено документов: {metrics.document_count}."
                if metrics.document_count
                else "Документы не найдены автоматически."
            ),
            manual_action="Открыть PDF, схему, условия, сроки регистрации, ограничения, размер гарантии и порядок участия.",
            impact="Документы часто содержат ограничения, которых нет в короткой карточке лота.",
            url=lot.source_url,
            url_label="Открыть документы",
            anchor="#auction-v2-documents",
        ),
        _review_step(
            code="akimat",
            title="Объявление акимата и ранние публикации",
            source="gov.kz / сайты акиматов",
            status=_review_status_from_source(gov_source),
            system_result=(gov_source or {}).get("detail")
            or "Zhertap ищет совпадения по номеру лота, кадастру, ссылке E-Qazyna и локации.",
            manual_action="Проверить объявление акимата: условия, список участков, сроки регистрации, изменения и вложенные файлы.",
            impact="Акимат может опубликовать условия или изменения раньше, чем пользователь увидит их в торгах.",
            url=(gov_source or {}).get("url") or "https://www.gov.kz/memleket/entities?lang=ru",
            url_label="Открыть gov.kz",
            anchor="#auction-v2-source-checks",
        ),
        _review_step(
            code="cadastre",
            title="Кадастр, координаты и границы",
            source="ЕГКН / АИС ГЗК / публичная кадастровая карта",
            status=(
                "done"
                if geo_check.cadastre_status == "verified"
                and geo_check.coordinate_status == "found"
                else "manual"
            ),
            system_result=_egkn_status_detail(lot, geo_check),
            manual_action="Сверить кадастровый номер, координаты, границы участка и совпадение с PDF/схемой лота.",
            impact="Ошибка по кадастру или координатам может полностью изменить смысл участка.",
            url=geo_check.egkn_url,
            url_label="Открыть ЕГКН",
            anchor="#auction-v2-map-panel",
        ),
        _review_step(
            code="urban_plan",
            title="Генплан, ПДП, красные линии",
            source="ГГК / Smart Geohub / геопорталы",
            status=(
                "done"
                if geo_check.urban_plan_status == "checked"
                else _review_status_from_source(urban_source)
            ),
            system_result=(urban_source or {}).get("detail")
            or (readiness_by_code.get("urban_plan") or {}).get("detail")
            or "Градостроительные ограничения требуют ручной сверки.",
            manual_action="Проверить функциональную зону, ПДП, красные линии, будущие дороги, санитарные и инженерные ограничения.",
            impact="Участок может быть дешевым из-за ограничений, которые видны только на градостроительных слоях.",
            url=(urban_source or {}).get("url") or "https://gov.ggk.kz/",
            url_label="Открыть генплан",
            anchor="#auction-v2-source-checks",
        ),
        _review_step(
            code="surroundings",
            title="Окружение, подъезд и инженерия",
            source="OSM / Google Maps / спутник",
            status=_review_status_from_source(osm_source),
            system_result=(osm_source or {}).get("detail") or _osm_status_detail(geo_check),
            manual_action="Открыть карту и проверить дороги, ЛЭП, воду, близкие объекты, санитарные риски и реальное окружение.",
            impact="Инфраструктура и окружение часто важнее красивой стартовой цены.",
            url=geo_check.google_maps_url,
            url_label="Открыть карту",
            anchor="#auction-v2-map-panel",
        ),
        _review_step(
            code="price_history",
            title="Цена, история района и лимит",
            source="История торгов / районная аналитика / рынок",
            status="done" if analysis.price_per_sotka is not None else "manual",
            system_result=(
                f"Цена за сотку: {_money(analysis.price_per_sotka)}. Районный ориентир: {_money(analysis.district_average_price_per_sotka)}."
                if analysis.price_per_sotka is not None
                else "Цена за сотку не рассчитана: не хватает цены или площади."
            ),
            manual_action="Сравнить с историей района, добавить рыночные аналоги только для ориентира и сохранить личный максимум ставки.",
            impact="Преимущество появляется до торгов, когда понятен потолок цены.",
            url=(market_source or {}).get("url"),
            url_label="Открыть рынок для сравнения",
            anchor="#auction-v2-district-context",
        ),
        _review_step(
            code="risks",
            title="Риски и ручные вопросы",
            source="Zhertap analysis",
            status="warning" if risk_flags else "done",
            system_result=(
                f"Найдено рисков: {len(risk_flags)}. Главный: {risk_flags[0].get('label')}."
                if risk_flags
                else "Критичных рисков не найдено, остается стандартная ручная сверка."
            ),
            manual_action="Открыть список рисков и закрыть каждый вопрос перед официальным переходом.",
            impact="Высокий риск не всегда означает плохой лот, но означает запрет на слепое участие.",
            anchor="#auction-v2-risk-panel",
        ),
        _review_step(
            code="official_handoff",
            title="Можно ли идти на официальный портал",
            source="E-Qazyna / eGov",
            status="done" if ready_for_portal else ("warning" if analysis.risk_level == "high" else "manual"),
            system_result=f"Рекомендация: {ACTION_LABELS.get(analysis.recommended_action, analysis.recommended_action)}. Решение сохранено: {'да' if decision_saved else 'нет'}.",
            manual_action=(
                "Открыть E-Qazyna/eGov и выполнять заявку, ЭЦП, гарантийный взнос и торги только на официальном портале."
                if ready_for_portal
                else "Сначала закрыть ручные проверки, сохранить личный лимит и рабочий статус."
            ),
            impact="Zhertap не подает заявки, не хранит ЭЦП и не участвует в торгах.",
            url=lot.source_url or "https://www.gov.kz/services/5169",
            url_label="Перейти официально",
            anchor="#auction-v2-official-checklist",
        ),
    ]


def _review_step(
    *,
    code: str,
    title: str,
    source: str,
    status: str,
    system_result: object,
    manual_action: str,
    impact: str,
    url: object | None = None,
    url_label: str | None = None,
    anchor: str | None = None,
) -> dict[str, object]:
    status_value = status if status in WORKFLOW_STATUS_LABELS else "manual"
    return {
        "code": code,
        "title": title,
        "source": source,
        "status": status_value,
        "status_label": WORKFLOW_STATUS_LABELS[status_value],
        "system_result": str(system_result or "Данных пока нет."),
        "manual_action": manual_action,
        "impact": impact,
        "url": str(url) if url else None,
        "url_label": url_label or "Открыть источник",
        "anchor": anchor,
    }


def _review_status_from_source(source_status: dict[str, object] | None) -> str:
    if source_status is None:
        return "manual"
    return _workflow_status_from_source_status(str(source_status.get("status") or ""))


def _manual_process_map(
    *,
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    pipeline: AuctionUserLotPipeline | None,
    source_statuses: list[dict[str, object]],
    review_steps: list[dict[str, object]],
) -> list[dict[str, object]]:
    sources = {str(item.get("code") or ""): item for item in source_statuses}
    reviews = {str(item.get("code") or ""): item for item in review_steps}
    gov_source = sources.get("gov_kz_akimat_announcements")
    urban_source = sources.get("smart_geohub_genplans")
    osm_source = sources.get("osm_overpass")
    market_source = sources.get("krisha_land_market") or sources.get("olx_land_market")
    market_comparison_ready = analysis.max_bid_market_kzt is not None
    decision_saved = pipeline is not None and pipeline.stage in {
        "ready_for_official_site",
        "decided_to_participate",
        "skipped",
    }
    personal_limit_saved = pipeline is not None and pipeline.max_bid_kzt is not None

    return [
        _manual_process_row(
            code="eqazyna",
            site="E-Qazyna",
            role="Официальный источник торгов",
            importance="required",
            status=str((reviews.get("official_lot") or {}).get("status") or ("done" if lot.source_url else "missing")),
            system_result=(reviews.get("official_lot") or {}).get("system_result")
            or (f"Лот {lot.auction_number or lot.source_lot_id} найден." if lot.source_url else "Официальная карточка пока не найдена."),
            manual_action="Сверить статус приема заявок, дату торгов, продавца, цену, гарантийный взнос и открыть официальный путь участия.",
            url=lot.source_url,
            url_label="Открыть лот",
            note="Это источник аукциона. Zhertap не подает заявку и не участвует в торгах.",
        ),
        _manual_process_row(
            code="documents",
            site="PDF и приложения",
            role="Условия участия и схема участка",
            importance="required",
            status=str((reviews.get("documents") or {}).get("status") or ("done" if metrics.document_count else "missing")),
            system_result=f"Найдено документов: {metrics.document_count}." if metrics.document_count else "Документы пока не извлечены автоматически.",
            manual_action="Открыть файлы, проверить схему, назначение, ограничения, сроки регистрации и требования к участнику.",
            url=lot.source_url,
            url_label="Открыть документы",
            anchor="#auction-v2-documents",
            note="Без документов нельзя считать лот готовым к участию.",
        ),
        _manual_process_row(
            code="akimat",
            site="gov.kz и сайты акиматов",
            role="Ранние объявления, изменения, вложения",
            importance="required",
            status=str((reviews.get("akimat") or {}).get("status") or _review_status_from_source(gov_source)),
            system_result=(gov_source or {}).get("detail")
            or "Zhertap ищет совпадения по номеру лота, кадастру, ссылке, региону и названию.",
            manual_action="Проверить извещения, протоколы, изменения сроков и дополнительные документы акимата.",
            url=(gov_source or {}).get("url") or "https://www.gov.kz/memleket/entities?lang=ru",
            url_label="Открыть gov.kz",
            anchor="#auction-v2-source-checks",
            note="Это не площадка торгов, а источник условий и ранних публикаций.",
        ),
        _manual_process_row(
            code="egkn",
            site="ЕГКН / публичная кадастровая карта",
            role="Кадастр, координаты, границы",
            importance="required",
            status=str((reviews.get("cadastre") or {}).get("status") or "manual"),
            system_result=_egkn_status_detail(lot, geo_check),
            manual_action="Сверить кадастровый номер, границу участка и совпадение с PDF/схемой.",
            url=geo_check.egkn_url,
            url_label="Открыть ЕГКН",
            anchor="#auction-v2-map-panel",
            note="Кадастр нужен, чтобы не анализировать участок только по тексту лота.",
        ),
        _manual_process_row(
            code="urban_plan",
            site="ГГК / Smart Geohub / геопорталы",
            role="Генплан, ПДП, красные линии, ограничения",
            importance="required",
            status=str((reviews.get("urban_plan") or {}).get("status") or _review_status_from_source(urban_source)),
            system_result=(urban_source or {}).get("detail")
            or (
                "Проверка градостроительных слоев отмечена как выполненная."
                if geo_check.urban_plan_status == "checked"
                else "Нужна ручная сверка градостроительных ограничений."
            ),
            manual_action="Проверить функциональную зону, ПДП, будущие дороги, санитарные и инженерные ограничения.",
            url=(urban_source or {}).get("url") or "https://gov.ggk.kz/",
            url_label="Открыть геопортал",
            anchor="#auction-v2-source-checks",
            note="Именно здесь часто находится причина, почему дешевый участок нельзя брать вслепую.",
        ),
        _manual_process_row(
            code="maps",
            site="OSM / Google Maps / спутник",
            role="Окружение и инфраструктура",
            importance="support",
            status=str((reviews.get("surroundings") or {}).get("status") or _review_status_from_source(osm_source)),
            system_result=(osm_source or {}).get("detail") or _osm_status_detail(geo_check),
            manual_action="Посмотреть подъезд, воду, ЛЭП, соседние объекты, санитарные риски и реальное окружение.",
            url=geo_check.google_maps_url,
            url_label="Открыть карту",
            anchor="#auction-v2-map-panel",
            note="Это помогает оценить участок, но не заменяет официальные документы и кадастр.",
        ),
        _manual_process_row(
            code="zhertap_history",
            site="История торгов Zhertap",
            role="Цена района и прошлые публикации",
            importance="required",
            status="done" if analysis.price_per_sotka is not None else "manual",
            system_result=(
                f"Цена за сотку: {_money(analysis.price_per_sotka)}. Районный ориентир: {_money(analysis.district_average_price_per_sotka)}."
                if analysis.price_per_sotka is not None
                else "Нужна цена и площадь, чтобы посчитать цену за сотку."
            ),
            manual_action="Сравнить старт с историей района и понять, есть ли ценовое преимущество.",
            anchor="#auction-v2-district-context",
            note="Это внутренний слой Zhertap, который заменяет ручное сравнение таблиц и старых лотов.",
        ),
        _manual_process_row(
            code="market_comparison",
            site="Krisha / OLX / рыночные объявления",
            role="Только сравнение цены",
            importance="optional",
            status="done" if market_comparison_ready else "manual",
            system_result=(
                f"Рыночный ориентир рассчитан: {_money(analysis.max_bid_market_kzt)}."
                if market_comparison_ready
                else "Рыночные сайты не подключены как источник аукционов; они нужны только для ручных аналогов цены."
            ),
            manual_action="Если нужно уточнить потолок ставки, открыть похожие участки рядом и добавить 1-3 аналога в карточку.",
            url=(market_source or {}).get("url"),
            url_label="Открыть рынок",
            note="Это не источник аукционов. Лоты торгов ищутся в официальных источниках, а рынок нужен только для ориентира цены.",
        ),
        _manual_process_row(
            code="decision",
            site="Личный pipeline Zhertap",
            role="Решение, лимит, заметки",
            importance="required",
            status="done" if decision_saved and personal_limit_saved else "manual",
            system_result=(
                f"Статус: {_stage_label(pipeline.stage)}; лимит: {_money(pipeline.max_bid_kzt)}."
                if decision_saved and personal_limit_saved and pipeline is not None
                else "Решение или личный максимум еще не сохранены."
            ),
            manual_action="Сохранить максимум ставки, статус работы и заметку до перехода на официальный портал.",
            anchor="#auction-v2-decision-form",
            note="Это рабочая отметка внутри Zhertap, не официальная заявка.",
        ),
        _manual_process_row(
            code="official_handoff",
            site="E-Qazyna / eGov",
            role="Юридически значимый финальный шаг",
            importance="external",
            status="external",
            system_result="Zhertap доводит до решения и не выполняет юридически значимые действия.",
            manual_action="Подача заявки, ЭЦП, гарантийный взнос, торги и подписание выполняются только пользователем на официальном портале.",
            url=lot.source_url or "https://www.gov.kz/services/5169",
            url_label="Официальный портал",
            note="Это граница продукта: дальше пользователь действует сам на государственном портале.",
        ),
    ]


def _manual_process_row(
    *,
    code: str,
    site: str,
    role: str,
    importance: str,
    status: str,
    system_result: object,
    manual_action: str,
    url: object | None = None,
    url_label: str | None = None,
    anchor: str | None = None,
    note: str | None = None,
) -> dict[str, object]:
    status_value = status if status in WORKFLOW_STATUS_LABELS else "manual"
    importance_labels = {
        "required": "Обязательно",
        "support": "Для оценки",
        "optional": "По желанию",
        "external": "Официальный шаг",
    }
    return {
        "code": code,
        "site": site,
        "role": role,
        "importance": importance,
        "importance_label": importance_labels.get(importance, "Проверка"),
        "required": importance == "required",
        "status": status_value,
        "status_label": WORKFLOW_STATUS_LABELS[status_value],
        "system_result": str(system_result or "Данных пока нет."),
        "manual_action": manual_action,
        "url": str(url) if url else None,
        "url_label": url_label or "Открыть источник",
        "anchor": anchor,
        "note": note or "",
    }


def _manual_process_counts(rows: list[dict[str, object]]) -> dict[str, int]:
    required_rows = [row for row in rows if bool(row.get("required"))]
    return {
        "total": len(rows),
        "required": len(required_rows),
        "required_done": sum(1 for row in required_rows if row.get("status") == "done"),
        "required_open": sum(
            1
            for row in required_rows
            if row.get("status") in {"manual", "warning"}
        ),
        "required_missing": sum(
            1 for row in required_rows if row.get("status") == "missing"
        ),
        "external": sum(1 for row in rows if row.get("status") == "external"),
        "optional": sum(1 for row in rows if not bool(row.get("required"))),
    }


def _lot_next_actions(review_steps: list[dict[str, object]]) -> list[dict[str, object]]:
    priority = {"missing": 0, "warning": 1, "manual": 2}
    unresolved = [
        (index, step)
        for index, step in enumerate(review_steps)
        if str(step.get("status") or "") in priority
    ]
    unresolved.sort(
        key=lambda item: (
            priority[str(item[1].get("status") or "")],
            item[0],
        )
    )
    selected = [step for _index, step in unresolved[:5]]
    if not selected:
        handoff = next(
            (
                step
                for step in review_steps
                if step.get("code") == "official_handoff"
            ),
            None,
        )
        return [
            _next_action_from_review_step(
                handoff or {
                    "title": "Проверка закрыта",
                    "status": "external",
                    "status_label": WORKFLOW_STATUS_LABELS["external"],
                    "manual_action": "Перейти на официальный портал и выполнять юридические действия там.",
                    "impact": "Zhertap не подает заявки и не участвует в торгах.",
                },
                title="Можно переходить к официальному порталу",
            )
        ]
    return [_next_action_from_review_step(step) for step in selected]


def _lot_decision_summary(
    *,
    lot: AuctionLot,
    analysis: AuctionLotV2Analysis,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    pipeline: AuctionUserLotPipeline | None,
    review_steps: list[dict[str, object]],
    risk_flags: list[dict[str, object]],
    next_actions: list[dict[str, object]],
) -> dict[str, object]:
    status_counts = {
        "done": 0,
        "manual": 0,
        "warning": 0,
        "missing": 0,
        "external": 0,
    }
    for step in review_steps:
        status = str(step.get("status") or "manual")
        status_counts[status if status in status_counts else "manual"] += 1
    unresolved = [
        step
        for step in review_steps
        if str(step.get("status") or "") in {"missing", "warning", "manual"}
    ]
    missing_count = status_counts["missing"]
    warning_count = status_counts["warning"]
    ready_for_portal = analysis.recommended_action == "prepare_official_review"
    decision_saved = pipeline is not None and pipeline.stage in {
        "ready_for_official_site",
        "decided_to_participate",
        "skipped",
    }
    personal_limit_saved = pipeline is not None and pipeline.max_bid_kzt is not None
    hard_blocker_codes = {
        "no_coordinates",
        "coordinates_unconfirmed",
        "boundary_area_mismatch",
        "boundary_not_confirmed",
        "no_documents",
        "auction_started_or_finished",
    }
    blockers = [
        item for item in risk_flags if str(item.get("code") or "") in hard_blocker_codes
    ]

    if blockers:
        status = "blocked"
        title = "Решение заблокировано до проверки"
        detail = (
            f"Есть критические пробелы: {len(blockers)}. "
            "Система не считает участок подходящим, пока не подтверждены границы, документы и ключевые условия."
        )
    elif missing_count:
        status = "blocked"
        title = "Пока нельзя идти к участию"
        detail = (
            f"Не найдено обязательных блоков: {missing_count}. "
            "Сначала откройте карточку лота, документы или кадастр и закройте пробелы."
        )
    elif warning_count or analysis.risk_level == "high":
        status = "warning"
        title = "Сначала закрыть ручную проверку"
        detail = (
            "Лот может быть интересным, но есть вопросы по рискам, источникам или окружению. "
            "Идти к заявке без сверки нельзя."
        )
    elif ready_for_portal and decision_saved and personal_limit_saved:
        status = "ready"
        title = "Можно готовиться к официальному переходу"
        detail = (
            "Основные проверки закрыты, решение и личный лимит сохранены. "
            "Дальше только официальный портал."
        )
    elif ready_for_portal:
        status = "checking"
        title = "Лот близок к официальному переходу"
        detail = "Осталось зафиксировать решение, личный лимит или одну из ручных проверок."
    else:
        status = "checking"
        title = "Лот на проверке"
        detail = "Система собрала основу, но до заявки нужно закрыть ручные действия ниже."

    next_action = next_actions[0] if next_actions else None
    primary_url = lot.source_url if status == "ready" and lot.source_url else None
    primary_url_label = "Перейти на E-Qazyna" if primary_url else ""
    return {
        "status": status,
        "fit_status": "blocked" if blockers else "not_confirmed" if status != "ready" else "ready",
        "blockers": blockers[:6],
        "title": title,
        "detail": detail,
        "primary_url": primary_url,
        "primary_url_label": primary_url_label,
        "next_title": (next_action or {}).get("title") or "Проверить лот",
        "next_detail": (next_action or {}).get("detail") or "Открыть список проверок ниже.",
        "next_anchor": (next_action or {}).get("anchor") or "#auction-v2-review-board",
        "counts": [
            {"label": "Закрыто", "value": status_counts["done"]},
            {
                "label": "Проверить",
                "value": status_counts["manual"] + status_counts["warning"],
            },
            {"label": "Не найдено", "value": status_counts["missing"]},
        ],
        "facts": [
            {
                "label": "Официальный лот",
                "value": "есть" if lot.source_url else "нет",
                "status": "done" if lot.source_url else "missing",
            },
            {
                "label": "Документы",
                "value": str(metrics.document_count),
                "status": "done" if metrics.document_count else "missing",
            },
            {
                "label": "Кадастр и координаты",
                "value": (
                    "сверены"
                    if geo_check.cadastre_status == "verified"
                    and geo_check.coordinate_status == "found"
                    else "проверить"
                ),
                "status": (
                    "done"
                    if geo_check.cadastre_status == "verified"
                    and geo_check.coordinate_status == "found"
                    else "manual"
                ),
            },
            {
                "label": "Личный лимит",
                "value": _money(pipeline.max_bid_kzt)
                if personal_limit_saved and pipeline is not None
                else "не сохранен",
                "status": "done" if personal_limit_saved else "manual",
            },
            {
                "label": "Решение",
                "value": _stage_label(pipeline.stage)
                if decision_saved and pipeline is not None
                else "не выбрано",
                "status": "done" if decision_saved else "manual",
            },
        ],
        "remaining": unresolved[:4],
    }


def _next_action_from_review_step(
    step: dict[str, object],
    *,
    title: str | None = None,
) -> dict[str, object]:
    return {
        "code": step.get("code"),
        "title": title or step.get("title") or "Проверить лот",
        "status": step.get("status") or "manual",
        "status_label": step.get("status_label") or WORKFLOW_STATUS_LABELS["manual"],
        "detail": step.get("manual_action") or "Открыть источник и сверить данные вручную.",
        "reason": step.get("impact") or "Это влияет на решение до официальной заявки.",
        "url": step.get("url"),
        "url_label": step.get("url_label") or "Открыть источник",
        "anchor": step.get("anchor"),
    }


def _v2_score(
    lot: AuctionLot,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    market_stats: AuctionV2MarketStats,
    risk_flags: list[dict[str, object]],
) -> int:
    score = metrics.rating
    score += 8 if lot.cadastre_number else -5
    score += 6 if geo_check.coordinate_status == "found" else -10
    if geo_check.osm_status == "checked":
        score += 4
        if geo_check.engineering_status == "warning":
            score -= 6
    elif geo_check.coordinate_status == "found":
        score -= 2
    score += 8 if metrics.document_count else -8
    score += 4 if (lot.functional_purpose_level2 or lot.purpose) else -3
    score += 2 if lot.guarantee_kzt is not None else 0

    starts_at = _aware(lot.auction_starts_at)
    if starts_at is None:
        score -= 5
    else:
        hours_left = (starts_at - datetime.now(UTC)).total_seconds() / 3600
        if hours_left < 0:
            score -= 20
        elif hours_left <= 24:
            score -= 5
        elif hours_left >= 168:
            score += 4

    if metrics.district_difference_percent is not None:
        if metrics.district_difference_percent <= -30:
            score += 8
        elif metrics.district_difference_percent <= -15:
            score += 4
        elif metrics.district_difference_percent >= 35:
            score -= 12

    market_difference = _market_difference_percent(lot, market_stats)
    if market_difference is not None:
        if market_difference <= -30:
            score += 8
        elif market_difference <= -10:
            score += 4
        elif market_difference >= 30:
            score -= 10
        elif market_difference >= 15:
            score -= 4

    high_risks = sum(1 for item in risk_flags if item.get("level") == "high")
    medium_risks = sum(1 for item in risk_flags if item.get("level") == "medium")
    score -= high_risks * 8
    score -= medium_risks * 3
    return max(0, min(100, int(round(score))))


def _confidence_level(
    lot: AuctionLot,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    market_stats: AuctionV2MarketStats,
) -> str:
    points = 20
    if lot.source_url:
        points += 20
    if metrics.document_count:
        points += 15
    if lot.cadastre_number:
        points += 15
    if geo_check.coordinate_status == "found":
        points += 15
    if geo_check.osm_status == "checked":
        points += 5
    if metrics.district_average_price_per_sotka is not None:
        points += 10
    if market_stats.priced_count >= 2:
        points += 10
    elif market_stats.priced_count == 1:
        points += 5
    if metrics.publication_count > 1 or metrics.district_lot_count >= 3:
        points += 5
    if points >= 75:
        return "high"
    if points >= 50:
        return "medium"
    return "low"


def _risk_level(score: int, risk_flags: list[dict[str, object]]) -> str:
    if any(item.get("level") == "high" for item in risk_flags) or score < 45:
        return "high"
    if any(item.get("level") == "medium" for item in risk_flags) or score < 68:
        return "medium"
    return "low"


def _recommended_action(
    *,
    score: int,
    risk_level: str,
    confidence_level: str,
    risk_flags: list[dict[str, object]],
) -> str:
    if risk_level == "high" and score < 50:
        return "skip"
    if confidence_level == "low" or any(
        item.get("code") in {"no_cadastre", "no_coordinates", "coordinates_unconfirmed"}
        for item in risk_flags
    ):
        return "manual_check"
    if score >= 75 and risk_level != "high":
        return "prepare_official_review"
    if score >= 55:
        return "watch_and_check"
    return "watch"


def _bid_limits(
    lot: AuctionLot,
    metrics: AuctionLotMetrics,
    market_stats: AuctionV2MarketStats,
    *,
    score: int,
    risk_level: str,
) -> dict[str, float | None]:
    if not lot.start_price_kzt:
        return {"conservative": None, "market": None, "aggressive": None}
    market_anchor = None
    if market_stats.median_price_per_sotka and lot.area_ha:
        market_anchor = market_stats.median_price_per_sotka * lot.area_ha * 100
    elif (
        metrics.district_average_price_per_sotka
        and metrics.district_lot_count >= 3
        and lot.area_ha
    ):
        market_anchor = metrics.district_average_price_per_sotka * lot.area_ha * 100

    base = float(lot.start_price_kzt)
    if risk_level == "high":
        conservative_multiplier = 1.0
        aggressive_multiplier = 1.03
    elif score >= 75:
        conservative_multiplier = 1.08
        aggressive_multiplier = 1.18
    else:
        conservative_multiplier = 1.04
        aggressive_multiplier = 1.10

    conservative = base * conservative_multiplier
    market = market_anchor
    aggressive = max(base, market_anchor) * aggressive_multiplier if market_anchor is not None else None
    if market_anchor is not None:
        conservative = min(conservative, max(base, market_anchor * 0.9))
        aggressive = min(aggressive, max(base, market_anchor * 1.08)) if aggressive is not None else None
    return {
        "conservative": round(conservative),
        "market": round(market) if market is not None else None,
        "aggressive": round(aggressive) if aggressive is not None else None,
    }


def _summary(
    score: int,
    risk_level: str,
    confidence_level: str,
    metrics: AuctionLotMetrics,
    geo_check: AuctionLotGeoCheck,
    market_stats: AuctionV2MarketStats,
) -> str:
    parts = [
        f"Индекс преимущества {score}/100, риск: {RISK_LABELS.get(risk_level, risk_level).lower()}, уверенность: {CONFIDENCE_LABELS.get(confidence_level, confidence_level).lower()}."
    ]
    if metrics.district_difference_percent is not None:
        parts.append(
            f"Стартовая цена за сотку отличается от истории района на {metrics.district_difference_percent:+.0f}%."
        )
    elif metrics.price_per_sotka is not None:
        parts.append("Цена за сотку посчитана, но районного ориентира пока недостаточно.")
    else:
        parts.append("Для ценовой оценки не хватает цены или площади.")
    if geo_check.coordinate_status == "found":
        parts.append("Координаты найдены; можно быстро открыть карту и сверить окружение.")
    elif geo_check.coordinate_status == "unconfirmed":
        parts.append("Координаты не подтверждены как точка в Казахстане, поэтому маркер скрыт до ручной геосверки.")
    else:
        parts.append("Координаты не найдены, поэтому перед решением нужна ручная геосверка.")
    if geo_check.osm_status == "checked":
        parts.append(
            "OSM-инфраструктура проверена: "
            f"дорога {_format_distance_m(geo_check.road_distance_m)}, "
            f"вода {_format_distance_m(geo_check.water_distance_m)}, "
            f"энергия {_format_distance_m(geo_check.power_distance_m)}."
        )
    elif geo_check.osm_status == "unavailable":
        parts.append("OSM не ответил, поэтому инфраструктуру надо проверить вручную.")
    elif geo_check.coordinate_status == "found":
        parts.append("OSM-инфраструктура еще не проверена синхронизацией v2.")
    if market_stats.average_price_per_sotka is not None:
        market_difference = _market_difference_percent_from_price(
            metrics.price_per_sotka,
            market_stats.average_price_per_sotka,
        )
        if market_difference is not None:
            parts.append(
                f"Рыночные аналоги: {market_stats.priced_count}, средняя цена {_money(market_stats.average_price_per_sotka)} за сотку, отклонение старта {market_difference:+.0f}%."
            )
        else:
            parts.append(
                f"Рыночные аналоги: {market_stats.priced_count}, средняя цена {_money(market_stats.average_price_per_sotka)} за сотку."
            )
    elif market_stats.comparable_count:
        parts.append("Рыночные аналоги добавлены, но в них нет цены за сотку для расчета.")
    return " ".join(parts)


def _price_position_text(metrics: AuctionLotMetrics) -> str:
    if metrics.price_per_sotka is None:
        return "Не хватает стартовой цены или площади для расчета цены за сотку."
    if metrics.district_difference_percent is None:
        return "Цена за сотку рассчитана, но в районе пока мало истории для сравнения."
    if metrics.district_difference_percent <= -20:
        return "Старт выглядит заметно ниже районной истории E-Qazyna."
    if metrics.district_difference_percent >= 25:
        return "Старт выглядит выше районной истории, нужен осторожный лимит."
    return "Старт близок к районной истории E-Qazyna."


def _text(value: object) -> str:
    text = str(value or "").strip()
    return text if text else "—"


def _money(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ") + " ₸"


def _percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.0f}%"


def _area_text(value: float | None) -> str:
    if value is None:
        return "—"
    hectares = f"{value:.4f}".rstrip("0").rstrip(".")
    sotka = f"{value * 100:.2f}".rstrip("0").rstrip(".")
    return f"{hectares} га / {sotka} сот."


def _datetime_text(value: datetime | None) -> str:
    aware = _aware(value)
    return aware.strftime("%d.%m.%Y %H:%M") if aware else "—"


def format_auction_v2_telegram_card(payload: AuctionV2LotPayload) -> str:
    """Format a short decision-first lot card for Telegram."""
    lot = payload.lot
    quality = payload.data_quality
    counts = quality.get("counts") if isinstance(quality, dict) else {}
    rows = quality.get("rows") if isinstance(quality, dict) else []
    rows = rows if isinstance(rows, list) else []
    open_checks = [
        str(item.get("title") or "Проверка")
        for item in rows
        if isinstance(item, dict) and item.get("status") in {"manual", "missing"}
    ]
    open_checks_text = ", ".join(open_checks[:3]) or "критичных пробелов не найдено"
    location = " · ".join(
        str(value) for value in (lot.region, lot.district, lot.locality) if value
    ) or "местоположение не указано"
    decision = payload.decision_summary
    summary = str(decision.get("title") or payload.action_label)
    detail = str(decision.get("detail") or "")
    if len(detail) > 300:
        detail = detail[:297].rstrip() + "..."
    done_count = counts.get("done", 0) if isinstance(counts, dict) else 0
    total_count = counts.get("total", 0) if isinstance(counts, dict) else 0
    parts = [
        f"<b>Лот №{escape(str(lot.auction_number or lot.source_lot_id))}</b>",
        escape(location),
        "",
        f"<b>Решение:</b> {escape(summary)}",
        f"<b>Риск:</b> {escape(payload.risk_label)} · <b>данные:</b> {escape(payload.confidence_label)}",
        f"<b>Срок:</b> {escape(payload.deadline_label)}",
        f"<b>Старт:</b> {_money(lot.start_price_kzt)} · <b>площадь:</b> {_area_text(lot.area_ha)}",
        f"<b>Кадастр:</b> {escape(lot.cadastre_number or 'не указан')}",
        f"<b>Данные:</b> {done_count} из {total_count} ключевых блоков подтверждено",
        f"<b>Сейчас проверить:</b> {escape(open_checks_text)}",
        "",
        escape(detail),
    ]
    return "\n".join(parts).strip()


def _coordinate_text(latitude: float | None, longitude: float | None) -> str:
    if latitude is None or longitude is None:
        return "—"
    return f"{latitude:.6f}, {longitude:.6f}"


def _osm_map_url(geo_check: AuctionLotGeoCheck) -> str | None:
    if geo_check.latitude is None or geo_check.longitude is None:
        return None
    return (
        "https://www.openstreetmap.org/"
        f"?mlat={geo_check.latitude:.6f}&mlon={geo_check.longitude:.6f}"
        f"#map=17/{geo_check.latitude:.6f}/{geo_check.longitude:.6f}"
    )


def _osm_embed_url(geo_check: AuctionLotGeoCheck) -> str | None:
    if geo_check.latitude is None or geo_check.longitude is None:
        return None
    latitude = geo_check.latitude
    longitude = geo_check.longitude
    delta = 0.004
    bbox = (
        f"{longitude - delta:.6f},{latitude - delta:.6f},"
        f"{longitude + delta:.6f},{latitude + delta:.6f}"
    )
    return (
        "https://www.openstreetmap.org/export/embed.html"
        f"?bbox={bbox}&layer=mapnik&marker={latitude:.6f},{longitude:.6f}"
    )


def _dossier_check_lines(readiness: list[dict[str, object]]) -> list[str]:
    if not readiness:
        return ["- Нет чеклиста готовности."]
    return [
        _clean_line(
            "- "
            f"[{item.get('status') or 'unknown'}] "
            f"{item.get('label') or 'Проверка'} — {item.get('detail') or ''}"
            f"{' · ' + str(item.get('url')) if item.get('url') else ''}"
        )
        for item in readiness
    ]


def _dossier_workflow_lines(workflow: list[dict[str, object]]) -> list[str]:
    if not workflow:
        return ["- Рабочий процесс пока не рассчитан."]
    return [
        _clean_line(
            "- "
            f"[{item.get('status_label') or item.get('status') or 'unknown'}] "
            f"{item.get('title') or 'Шаг'} / {item.get('source') or 'Источник'} — "
            f"Zhertap: {item.get('system_result') or ''} "
            f"Проверить: {item.get('manual_action') or ''}"
            f"{' · ' + str(item.get('url')) if item.get('url') else ''}"
        )
        for item in workflow
    ]


def _dossier_risk_lines(risk_flags: list[dict[str, object]]) -> list[str]:
    if not risk_flags:
        return ["- Критичных рисков не найдено; остается ручная сверка официальных документов."]
    return [
        _clean_line(
            "- "
            f"[{item.get('level') or 'unknown'}] "
            f"{item.get('label') or 'Риск'} — {item.get('detail') or ''}"
        )
        for item in risk_flags
    ]


def _dossier_source_lines(source_statuses: list[dict[str, object]]) -> list[str]:
    if not source_statuses:
        return ["- Нет статусов источников."]
    return [
        _clean_line(
            "- "
            f"[{item.get('status') or 'unknown'}] "
            f"{item.get('group') or 'Источник'} / {item.get('name') or ''}: "
            f"{item.get('detail') or ''}"
            f"{' · ' + str(item.get('url')) if item.get('url') else ''}"
        )
        for item in source_statuses
    ]


def _dossier_document_lines(documents: list[object]) -> list[str]:
    documents = unique_auction_documents(documents)
    if not documents:
        return ["- Документы не найдены в карточке источника."]
    return [
        _clean_line(
            "- "
            f"{getattr(document, 'title', '') or 'Документ'} "
            f"({getattr(document, 'file_type', None) or 'файл'}) · "
            f"{getattr(document, 'source_url', '') or '—'}"
        )
        for document in documents
    ]


def _dossier_market_comparable_lines(
    comparables: list[AuctionMarketComparable],
) -> list[str]:
    if not comparables:
        return ["- Рыночные аналоги еще не добавлены; используйте ссылки Krisha/OLX в источниках и внесите подходящие объявления вручную."]
    return [
        _clean_line(
            "- "
            f"{item.source_name}: {item.title or 'аналог'} — "
            f"{_money(item.price_kzt)}, {_area_text(item.area_ha)}, "
            f"{_money(item.price_per_sotka)} за сотку"
            f"{' · ' + item.source_url if item.source_url else ''}"
        )
        for item in comparables
    ]


def _dossier_evidence_lines(evidence: list[AuctionEvidence]) -> list[str]:
    if not evidence:
        return ["- Следы проверок появятся после синхронизации v2."]
    return [
        _clean_line(
            "- "
            f"[{item.status}] {item.evidence_type}: {item.title}"
            f"{' — ' + item.value_text if item.value_text else ''}"
            f"{' · ' + item.source_url if item.source_url else ''}"
        )
        for item in evidence
    ]


def _clean_line(value: str) -> str:
    return " ".join(value.split())


def _sync_external_query_evidence(
    session: Session,
    lot: AuctionLot,
    sources_by_code: dict[str, AuctionSource],
) -> None:
    query = _lot_identity_query(lot)
    market_query = _lot_market_query(lot)
    specs = [
        (
            "gov_kz_akimat_announcements",
            "source_query",
            "Поиск ранних объявлений акимата",
            "query_ready",
            f"Искать: {query}",
            "https://www.gov.kz/memleket/entities?lang=ru",
            0.35,
        ),
        (
            "egkn_public_map",
            "source_query",
            "Сверка публичной кадастровой карты",
            "query_ready" if lot.cadastre_number else "manual_required",
            f"Кадастр/адрес: {lot.cadastre_number or query}",
            "https://map.gov4c.kz/egkn/",
            0.45 if lot.cadastre_number else 0.25,
        ),
        (
            "smart_geohub_genplans",
            "source_query",
            "Сверка генплана и ПДП",
            "manual_required",
            f"Искать район/точку: {query}",
            "https://gov.ggk.kz/",
            0.25,
        ),
        (
            "data_egov_open_data",
            "source_query",
            "Сверка открытых наборов по торгам",
            "query_ready",
            f"Искать наборы и записи: {query}",
            "https://data.egov.kz",
            0.25,
        ),
        (
            "krisha_land_market",
            "market_query",
            "Рыночные аналоги Krisha",
            "query_ready",
            f"Искать аналоги: {market_query}",
            "https://krisha.kz/prodazha/uchastkov/kazaxstan/",
            0.25,
        ),
        (
            "olx_land_market",
            "market_query",
            "Рыночные аналоги OLX",
            "query_ready",
            f"Искать аналоги: {market_query}",
            "https://www.olx.kz/nedvizhimost/zemlya/prodazha/",
            0.25,
        ),
        (
            "egov_land_auction_proposal",
            "official_boundary",
            "Граница официальных действий",
            "external_action",
            "Заявка, ЭЦП, гарантийный взнос и торги выполняются только пользователем на официальном портале.",
            "https://www.gov.kz/services/5169",
            0.9,
        ),
    ]
    if _is_shymkent_lot(lot):
        specs.append(
            (
                "geo_shymkent",
                "source_query",
                "Региональный геопортал Шымкента",
                "manual_required",
                f"Искать участок/слои: {query}",
                "https://geo-shym.kz/map/?access_token=&lang=ru",
                0.25,
            )
        )
    for (
        source_code,
        evidence_type,
        title,
        status,
        value_text,
        source_url,
        confidence,
    ) in specs:
        source = sources_by_code.get(source_code)
        if source is None:
            continue
        _upsert_evidence(
            session,
            lot=lot,
            source=source,
            evidence_type=evidence_type,
            title=title,
            status=status,
            value_text=value_text,
            source_url=source_url,
            confidence=confidence,
        )


def _lot_identity_query(lot: AuctionLot) -> str:
    return _query_text(
        lot.auction_number,
        lot.source_lot_id,
        lot.cadastre_number,
        lot.region,
        lot.district,
        lot.locality,
        lot.functional_purpose_level2,
        lot.purpose,
        "земельный аукцион",
    )


def _lot_market_query(lot: AuctionLot) -> str:
    area_text = None
    if lot.area_ha is not None:
        area_text = f"{lot.area_ha:g} га"
    return _query_text(
        "участок",
        lot.region,
        lot.district,
        lot.locality,
        lot.functional_purpose_level2,
        lot.purpose,
        area_text,
    )


def _query_text(*parts: object) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return _compact_text(" ".join(result), 240) or "земельный аукцион"


def _compact_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _is_shymkent_lot(lot: AuctionLot) -> bool:
    text = " ".join(
        part.casefold()
        for part in (lot.region, lot.district, lot.locality, lot.location_text)
        if part
    )
    return "шымкент" in text or "shymkent" in text


def _sync_builtin_evidence(
    session: Session,
    lot: AuctionLot,
    source_statuses: list[dict[str, object]],
) -> None:
    sources = {source.code: source for source in session.scalars(select(AuctionSource)).all()}
    eqazyna = sources.get("eqazyna_current_lots")
    if eqazyna is not None:
        _upsert_evidence(
            session,
            lot=lot,
            source=eqazyna,
            evidence_type="official_lot",
            title=f"E-Qazyna lot {lot.auction_number or lot.source_lot_id}",
            status="found",
            value_text=lot.title,
            source_url=lot.source_url,
            confidence=0.95,
        )
        unique_documents = unique_auction_documents(lot.documents)
        documents = _document_evidence_sample(unique_documents)
        _delete_extra_document_evidence(session, lot, eqazyna, documents)
        for document in documents:
            _upsert_evidence(
                session,
                lot=lot,
                source=eqazyna,
                evidence_type="official_document",
                title=document.title[:320],
                status="found",
                value_text=document.file_type,
                source_url=document.source_url,
                confidence=0.9,
            )
        if len(unique_documents) > len(documents):
            _upsert_evidence(
                session,
                lot=lot,
                source=eqazyna,
                evidence_type="official_document_summary",
                title="Документы E-Qazyna",
                status="found",
                value_text=(
                    f"Всего документов: {len(unique_documents)}. "
                    f"В следах проверки сохранены первые {len(documents)}; "
                    "полный список доступен в блоке документов карточки."
                ),
                source_url=lot.source_url,
                confidence=0.85,
            )
    egkn = sources.get("egkn_public_map")
    if egkn is not None and lot.cadastre_number:
        _upsert_evidence(
            session,
            lot=lot,
            source=egkn,
            evidence_type="cadastre_number",
            title="Кадастровый номер",
            status="found",
            value_text=lot.cadastre_number,
            source_url="https://map.gov4c.kz/egkn/",
            confidence=0.75,
        )
    for item in source_statuses:
        if item.get("status") not in {"manual_required", "planned"}:
            continue
        source = sources.get(str(item.get("code") or ""))
        if source is None:
            continue
        _upsert_evidence(
            session,
            lot=lot,
            source=source,
            evidence_type="source_check_status",
            title=str(item.get("name") or source.name)[:320],
            status=str(item.get("status") or "planned"),
            value_text=str(item.get("detail") or ""),
            source_url=str(item.get("url") or source.base_url),
            confidence=0.2,
        )


def _document_evidence_sample(documents: list[AuctionDocument]) -> list[AuctionDocument]:
    result: list[AuctionDocument] = []
    seen: set[tuple[str, str]] = set()
    for document in documents:
        key = ((document.source_url or "").strip(), (document.title or "").strip())
        if key in seen:
            continue
        seen.add(key)
        result.append(document)
        if len(result) >= MAX_BUILTIN_DOCUMENT_EVIDENCE:
            break
    return result


def _delete_extra_document_evidence(
    session: Session,
    lot: AuctionLot,
    source: AuctionSource,
    documents: list[AuctionDocument],
) -> None:
    allowed_urls = [document.source_url for document in documents if document.source_url]
    conditions = [
        AuctionEvidence.lot_id == lot.id,
        AuctionEvidence.source_id == source.id,
        AuctionEvidence.evidence_type == "official_document",
    ]
    if allowed_urls:
        conditions.append(
            or_(
                AuctionEvidence.source_url.is_(None),
                AuctionEvidence.source_url.not_in(allowed_urls),
            )
        )
    session.execute(delete(AuctionEvidence).where(and_(*conditions)))


def _upsert_evidence(
    session: Session,
    *,
    lot: AuctionLot,
    source: AuctionSource,
    evidence_type: str,
    title: str,
    status: str,
    value_text: str | None,
    source_url: str | None,
    confidence: float,
    raw_payload_json: str | None = None,
) -> AuctionEvidence:
    evidence = session.scalar(
        select(AuctionEvidence)
        .where(
            AuctionEvidence.lot_id == lot.id,
            AuctionEvidence.source_id == source.id,
            AuctionEvidence.evidence_type == evidence_type,
            AuctionEvidence.title == title[:320],
            AuctionEvidence.source_url == source_url,
        )
        .order_by(AuctionEvidence.id.desc())
        .limit(1)
    )
    if evidence is None:
        evidence = AuctionEvidence(
            lot_id=lot.id,
            source_id=source.id,
            evidence_type=evidence_type,
            title=title[:320],
        )
        session.add(evidence)
    evidence.status = status
    evidence.value_text = value_text
    evidence.source_url = source_url
    evidence.confidence = confidence
    if raw_payload_json is not None:
        evidence.raw_payload_json = raw_payload_json
    evidence.observed_at = datetime.now(UTC)
    return evidence


def _google_maps_url(lot: AuctionLot, latitude: float | None, longitude: float | None) -> str:
    if latitude is not None and longitude is not None:
        return f"https://www.google.com/maps?q={latitude:.6f},{longitude:.6f}"
    query = " ".join(
        part
        for part in (
            lot.region,
            lot.district,
            lot.locality,
            lot.location_text,
            lot.cadastre_number,
        )
        if part
    )
    return "https://www.google.com/maps/search/" + quote_plus(query or lot.title)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
