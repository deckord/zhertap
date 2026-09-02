import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import _engine_kwargs, init_db, validate_database_url


def test_sqlite_is_rejected_in_production() -> None:
    with pytest.raises(RuntimeError, match="SQLite database is not allowed"):
        validate_database_url("sqlite:///./land_scout.db", "production")


def test_sqlite_is_allowed_outside_production() -> None:
    validate_database_url("sqlite:///./land_scout.db", "development")


def test_postgres_engine_uses_pool_settings() -> None:
    kwargs = _engine_kwargs("postgresql+psycopg://user:pass@localhost:5432/db")

    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_size"] >= 1
    assert kwargs["max_overflow"] >= 0
    assert kwargs["pool_timeout"] >= 1
    assert kwargs["pool_recycle"] >= 60
    assert "statement_timeout=" in kwargs["connect_args"]["options"]
    assert "lock_timeout=" in kwargs["connect_args"]["options"]


def test_production_settings_require_hardening() -> None:
    with pytest.raises(RuntimeError, match="Insecure production config"):
        Settings(
            app_env="production",
            app_base_url="http://localhost:8000",
            admin_password="admin",
            internal_api_key="",
            session_secret="",
            apipay_enabled=False,
            database_url="sqlite:///./land_scout.db",
        )


def test_production_settings_allow_strong_values() -> None:
    configured = Settings(
        app_env="production",
        app_base_url="https://example.com",
        admin_password="very-strong-admin-password",
        internal_api_key="internal-api-key-secret-0123456789",
        session_secret="0123456789abcdef" * 4,
        apipay_enabled=True,
        apipay_webhook_secret="apipay-secret",
        database_url="postgresql+psycopg://user:pass@localhost:5432/land_scout",
        run_tasks_inline=False,
        demo_data_enabled=False,
        eqazyna_verify_tls=True,
        gov_kz_verify_tls=True,
        egkn_verify_tls=True,
        auction_cache_enabled=True,
    )
    assert configured.app_env == "production"


def test_production_admin_password_minimum_is_separate_from_web_password() -> None:
    configured = Settings(
        app_env="production",
        app_base_url="https://example.com",
        admin_password="Vtqgzz9g!@#",
        internal_api_key="internal-api-key-secret-0123456789",
        session_secret="0123456789abcdef" * 4,
        apipay_enabled=False,
        database_url="postgresql+psycopg://user:pass@localhost:5432/land_scout",
        run_tasks_inline=False,
        demo_data_enabled=False,
        eqazyna_verify_tls=True,
        gov_kz_verify_tls=True,
        egkn_verify_tls=True,
        auction_cache_enabled=True,
    )

    assert configured.admin_password == "Vtqgzz9g!@#"


def test_init_db_adds_concurrency_indexes() -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    init_db(bind=test_engine)

    inspector = inspect(test_engine)
    search_indexes = {index["name"] for index in inspector.get_indexes("search_requests")}
    account_payment_indexes = {
        index["name"] for index in inspector.get_indexes("account_payments")
    }
    web_session_indexes = {index["name"] for index in inspector.get_indexes("web_sessions")}
    auction_lot_indexes = {index["name"] for index in inspector.get_indexes("auction_lots")}

    assert "ix_search_requests_status_updated_at" in search_indexes
    assert "ix_search_requests_payment_status_user" in search_indexes
    assert "ix_search_requests_payment_status_account" in search_indexes
    assert "ix_account_payments_status_account_created" in account_payment_indexes
    assert "ix_web_sessions_account_active" in web_session_indexes
    assert "ix_auction_lots_active_region_district_start" in auction_lot_indexes
