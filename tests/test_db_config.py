import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

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
    assert "connect_args" not in kwargs


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
