from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Land Scout Kazakhstan"
    app_env: str = "development"
    app_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./land_scout.db"
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=20, ge=0, le=500)
    db_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    db_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    redis_url: str = "redis://localhost:6379/0"
    run_tasks_inline: bool = True
    client_funnel_version: Literal["v1", "v2"] = "v2"
    enable_standard_lph_10: bool = False

    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_admin_chat_id: str = ""
    telegram_admin_user_ids: str = ""
    analytics_excluded_telegram_user_ids: str = "70557953"

    platform_access_price_kzt: int = Field(default=1990, ge=0, le=10_000_000)
    platform_access_months: int = Field(default=1, ge=1, le=24)
    trial_access_enabled: bool = True
    trial_access_days: int = Field(default=1, ge=0, le=30)
    search_price_kzt: int = Field(default=1990, ge=0, le=10_000_000)
    free_preview_enabled: bool = True
    free_preview_plot_limit: int = Field(default=3, ge=1, le=10)
    paid_search_enabled: bool = True
    payment_recipient: str = ""
    payment_bank_name: str = ""
    payment_card_number: str = ""
    payment_url: str = "https://pay.kaspi.kz/pay/l31wvjsj"
    apipay_enabled: bool = False
    apipay_api_key: str = ""
    apipay_webhook_secret: str = ""
    apipay_base_url: str = "https://api.apipay.kz/api/v1"
    apipay_timeout_seconds: int = Field(default=30, ge=5, le=60)
    apipay_polling_enabled: bool = True
    apipay_poll_interval_seconds: int = Field(default=20, ge=10, le=300)
    apipay_poll_attempts: int = Field(default=30, ge=1, le=180)

    smsc_enabled: bool = False
    smsc_login: str = ""
    smsc_password: str = ""
    smsc_base_url: str = "https://smsc.kz/sys/send.php"
    smsc_sender: str = ""
    smsc_timeout_seconds: int = Field(default=15, ge=5, le=60)

    service_provider_name: str = ""
    service_provider_status: str = ""
    service_provider_id: str = ""
    service_provider_address: str = ""
    service_provider_contact: str = ""
    data_storage_location: str = "Республика Казахстан"
    data_retention_months: int = Field(default=12, ge=1, le=120)

    admin_username: str = "admin"
    admin_password: str = "change-me-now"
    admin_web_phones: str = "+77026669475"
    internal_api_key: str = ""

    auctions_enabled: bool = True
    auction_access_price_kzt: int = Field(default=1990, ge=0, le=10_000_000)
    auction_free_preview_lots: int = Field(default=1, ge=0, le=10)
    eqazyna_base_url: str = "https://sauda.e-qazyna.kz"
    eqazyna_sync_statuses: str = (
        "ApplicationsAccept,Pending,Running,SuccessProtocolSigned,"
        "FailureProtocolSigned,NullifyResultProtocolSigned,CancelBeforeStart"
    )
    eqazyna_sync_interval_minutes: int = Field(default=30, ge=5, le=1440)
    eqazyna_sync_max_pages: int = Field(default=10, ge=1, le=100)
    eqazyna_sync_max_lots: int = Field(default=100, ge=1, le=1000)
    eqazyna_history_sync_statuses: str = (
        "ApplicationsAccept,Pending,Running,SuccessProtocolSigned,"
        "FailureProtocolSigned,NullifyResultProtocolSigned,CancelBeforeStart"
    )
    eqazyna_history_sync_max_pages: int = Field(default=100, ge=1, le=1000)
    eqazyna_history_sync_max_lots: int = Field(default=1000, ge=1, le=20000)
    eqazyna_timeout_seconds: int = Field(default=30, ge=5, le=120)
    eqazyna_verify_tls: bool = True
    auction_v2_full_cycle_interval_minutes: int = Field(default=15, ge=5, le=1440)
    auction_v2_refresh_limit: int = Field(default=80, ge=1, le=1000)
    auction_v2_analysis_ttl_minutes: int = Field(default=360, ge=5, le=10080)
    auction_v2_live_osm_enabled: bool = True
    auction_v2_osm_radius_m: int = Field(default=1200, ge=100, le=5000)
    auction_v2_osm_ttl_minutes: int = Field(default=1440, ge=5, le=10080)
    auction_v2_object_clearance_m: int = Field(default=25, ge=0, le=500)
    auction_v2_power_clearance_m: int = Field(default=30, ge=0, le=500)
    auction_v2_event_lookback_hours: int = Field(default=72, ge=1, le=720)
    auction_v2_events_per_lot_limit: int = Field(default=4, ge=1, le=20)
    auction_v2_document_download_enabled: bool = False
    auction_v2_document_storage_dir: str = "var/auction-documents"
    auction_v2_document_download_limit: int = Field(default=25, ge=1, le=500)
    auction_v2_document_max_mb: int = Field(default=25, ge=1, le=100)
    auction_v2_live_gov_kz_enabled: bool = True
    auction_v2_gov_kz_projects: str = (
        "vko-altai,vko-shemonaiha,astana-saulet,almaty-zher,almobl-zher,"
        "karaganda-zher,shymkent-zher,abay-zher,akmola-zher,aktobe-zher,"
        "atyrau-zher,zko-zher,kostanay-zher,kyzylorda-zher,mangystau-zher,"
        "pavlodar-zher,sko-zher,turkestan-zher,zhambyl-zher,jetisu-zher,ulytau-zher"
    )
    auction_v2_gov_kz_detail_urls: str = ""
    auction_v2_gov_kz_include_news: bool = False
    auction_v2_gov_kz_page_size: int = Field(default=20, ge=1, le=100)
    auction_v2_gov_kz_max_pages: int = Field(default=1, ge=1, le=10)
    gov_kz_timeout_seconds: int = Field(default=20, ge=5, le=120)
    gov_kz_verify_tls: bool = True
    auction_v2_live_egkn_enabled: bool = True
    auction_v2_egkn_ttl_minutes: int = Field(default=1440, ge=5, le=10080)
    auction_v2_egkn_batch_size: int = Field(default=20, ge=1, le=100)
    auction_v2_egkn_context_enabled: bool = True
    auction_v2_egkn_context_ttl_minutes: int = Field(default=1440, ge=5, le=10080)
    auction_v2_egkn_context_batch_size: int = Field(default=12, ge=1, le=100)
    auction_v2_egkn_context_radius_m: int = Field(default=1200, ge=100, le=5000)
    auction_v2_egkn_context_max_features_per_layer: int = Field(default=25, ge=1, le=200)
    auction_v2_map_limit: int = Field(default=300, ge=10, le=1000)

    enable_live_osm: bool = True
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_fallback_urls: str = "https://overpass.private.coffee/api/interpreter"
    osm_query_timeout_seconds: int = Field(default=25, ge=5, le=60)
    osm_time_budget_seconds: int = Field(default=120, ge=20, le=300)
    osm_batch_size: int = Field(default=8, ge=1, le=20)
    max_lph_neighbor_distance_m: int = Field(default=15, ge=0, le=500)
    osm_road_clearance_m: int = Field(default=5, ge=0, le=100)
    osm_open_water_clearance_m: int = Field(default=30, ge=0, le=500)
    urban_plan_check_mode: str = "strict"
    urban_plan_auto_waive_unavailable: bool = True
    urban_plan_red_line_buffer_m: int = Field(default=5, ge=0, le=100)
    urban_plan_max_upload_mb: int = Field(default=20, ge=1, le=200)
    manual_genplan_files_root: str = ""
    urban_plan_source_domains: str = (
        "gov.kz,adilet.zan.kz,egov.kz,map.gov.kz,map.gov4c.kz,gov.ggk.kz,aisgzk.kz,geo-shym.kz,geopavlodar.kz"
    )
    egkn_wfs_url: str = "https://map.gov4c.kz/geoserver/egkn/ows"
    egkn_rest_url: str = "https://map.gov4c.kz/egkn/rest"
    egkn_timeout_seconds: int = Field(default=25, ge=5, le=180)
    egkn_request_attempts: int = Field(default=2, ge=1, le=3)
    egkn_verify_tls: bool = True
    search_mode: str = "live"
    live_search_radius_m: int = Field(default=180, ge=50, le=1000)
    live_search_time_budget_seconds: int = Field(default=180, ge=60, le=540)
    live_max_features: int = Field(default=20000, ge=1000, le=100000)
    demo_data_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
