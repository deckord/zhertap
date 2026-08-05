from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _is_sqlite_url(database_url: str) -> bool:
    return database_url.strip().lower().startswith("sqlite")


def validate_database_url(database_url: str | None = None, app_env: str | None = None) -> None:
    url = database_url or settings.database_url
    environment = (app_env or settings.app_env).strip().lower()
    if environment in {"production", "prod"} and _is_sqlite_url(url):
        raise RuntimeError(
            "SQLite database is not allowed in production. "
            "Set DATABASE_URL to PostgreSQL/PostGIS before starting the service."
        )


def _engine_kwargs(database_url: str) -> dict:
    kwargs: dict = {"pool_pre_ping": True}
    if _is_sqlite_url(database_url):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(
            {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout": settings.db_pool_timeout_seconds,
                "pool_recycle": settings.db_pool_recycle_seconds,
            }
        )
    return kwargs


validate_database_url()
engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(bind: Engine | None = None) -> None:
    from app import models  # noqa: F401

    db_engine = bind or engine
    Base.metadata.create_all(bind=db_engine)
    _add_payment_columns(db_engine)
    _add_urban_plan_columns(db_engine)
    _add_planning_candidate_review_columns(db_engine)
    _add_urban_plan_coverage_indexes(db_engine)
    _add_urban_plan_source_indexes(db_engine)
    _add_analytics_columns(db_engine)
    _add_auction_columns(db_engine)
    _add_web_account_columns(db_engine)
    _add_account_payment_indexes(db_engine)
    _add_concurrency_indexes(db_engine)


def _add_concurrency_indexes(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    tables = set(inspector.get_table_names())
    with db_engine.begin() as connection:
        if "search_requests" in tables:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_search_requests_status_updated_at "
                    "ON search_requests (status, updated_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_search_requests_payment_status_user "
                    "ON search_requests (payment_status, telegram_user_id)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_search_requests_payment_status_account "
                    "ON search_requests (payment_status, web_account_id)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_search_requests_funnel_session_status "
                    "ON search_requests (funnel_session_id, status)"
                )
            )
        if "account_payments" in tables:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_account_payments_status_account_created "
                    "ON account_payments (payment_status, account_id, created_at)"
                )
            )
        if "web_sessions" in tables:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_web_sessions_account_active "
                    "ON web_sessions (account_id, revoked_at, expires_at)"
                )
            )
        if "auction_lots" in tables:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_lots_active_region_district_start "
                    "ON auction_lots (active, region, district, auction_starts_at)"
                )
            )
        if "auction_subscriptions" in tables:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_auction_subscriptions_active_region_district "
                    "ON auction_subscriptions (active, region, district, locality)"
                )
            )


def _add_account_payment_indexes(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "account_payments" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("account_payments")}
    with db_engine.begin() as connection:
        if "payment_provider_qr_image_url" not in columns:
            connection.execute(
                text("ALTER TABLE account_payments ADD COLUMN payment_provider_qr_image_url TEXT")
            )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_account_payments_account_id "
                "ON account_payments (account_id)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_account_payments_provider_invoice_unique "
                "ON account_payments (payment_provider, payment_provider_invoice_id) "
                "WHERE payment_provider IS NOT NULL "
                "AND payment_provider_invoice_id IS NOT NULL"
            )
        )


def _add_urban_plan_source_indexes(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "urban_plan_sources" not in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_urban_plan_sources_platform_external "
                "ON urban_plan_sources (platform, external_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_urban_plan_sources_scope "
                "ON urban_plan_sources (region, district, locality)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_urban_plan_sources_status "
                "ON urban_plan_sources (coverage_status, import_status)"
            )
        )


def _add_web_account_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "accounts" not in inspector.get_table_names():
        return
    account_columns = {column["name"] for column in inspector.get_columns("accounts")}
    account_additions = {
        "paid_access": "BOOLEAN NOT NULL DEFAULT FALSE",
        "access_granted_at": "TIMESTAMP",
        "access_expires_at": "TIMESTAMP",
        "trial_started_at": "TIMESTAMP",
        "trial_expires_at": "TIMESTAMP",
        "password_hash": "VARCHAR(220)",
        "password_set_at": "TIMESTAMP",
        "failed_login_attempts": "INTEGER NOT NULL DEFAULT 0",
        "locked_until": "TIMESTAMP",
        "telegram_chat_id": "VARCHAR(32)",
        "offer_version": "VARCHAR(32)",
        "offer_accepted_at": "TIMESTAMP",
        "offer_accepted_ip": "VARCHAR(64)",
        "offer_accepted_user_agent": "TEXT",
        "onboarding_tour_available_at": "TIMESTAMP",
        "onboarding_tour_dismissed_at": "TIMESTAMP",
    }
    with db_engine.begin() as connection:
        for name, definition in account_additions.items():
            if name not in account_columns:
                connection.execute(text(f"ALTER TABLE accounts ADD COLUMN {name} {definition}"))
        connection.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_phone ON accounts (phone)")
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_telegram_user_id "
                "ON accounts (telegram_user_id) WHERE telegram_user_id IS NOT NULL"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_accounts_trial_expires_at "
                "ON accounts (trial_expires_at)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_accounts_access_expires_at "
                "ON accounts (access_expires_at)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_web_login_codes_phone "
                "ON web_login_codes (phone)"
            )
        )
        login_code_columns = {
            column["name"] for column in inspect(db_engine).get_columns("web_login_codes")
        }
        login_code_additions = {
            "purpose": "VARCHAR(24) NOT NULL DEFAULT 'login'",
            "password_hash": "VARCHAR(220)",
        }
        for name, definition in login_code_additions.items():
            if name not in login_code_columns:
                connection.execute(
                    text(f"ALTER TABLE web_login_codes ADD COLUMN {name} {definition}")
                )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_web_sessions_token_hash "
                "ON web_sessions (token_hash)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_telegram_link_tokens_token_hash "
                "ON telegram_link_tokens (token_hash)"
            )
        )


def _add_payment_columns(db_engine: Engine) -> None:
    columns = {
        column["name"] for column in inspect(db_engine).get_columns("search_requests")
    }
    additions = {
        "payment_status": "VARCHAR(32) NOT NULL DEFAULT 'not_requested'",
        "web_account_id": "VARCHAR(36)",
        "payment_amount_kzt": "INTEGER",
        "payment_requested_at": "TIMESTAMP",
        "payment_claimed_at": "TIMESTAMP",
        "payment_confirmed_at": "TIMESTAMP",
        "payment_confirmed_by": "VARCHAR(64)",
        "access_expires_at": "TIMESTAMP",
        "payment_confirmation_notified_at": "TIMESTAMP",
        "payment_provider": "VARCHAR(32)",
        "payment_provider_invoice_id": "VARCHAR(64)",
        "payment_provider_status": "VARCHAR(32)",
        "payment_provider_url": "TEXT",
        "payment_provider_updated_at": "TIMESTAMP",
        "free_preview_status": "VARCHAR(32) NOT NULL DEFAULT 'not_requested'",
        "free_preview_count": "INTEGER NOT NULL DEFAULT 0",
        "free_preview_delivered_at": "TIMESTAMP",
        "free_preview_approved_by": "VARCHAR(64)",
        "retry_of_request_id": "VARCHAR(36)",
        "continuation_of_request_id": "VARCHAR(36)",
        "batch_number": "INTEGER NOT NULL DEFAULT 1",
        "terms_version": "VARCHAR(32)",
        "terms_text_snapshot": "TEXT",
        "terms_accepted_at": "TIMESTAMP",
        "language": "VARCHAR(2) NOT NULL DEFAULT 'ru'",
        "region_label": "VARCHAR(160)",
        "district_label": "VARCHAR(160)",
        "locality_label": "VARCHAR(160)",
        "allotment_type": "VARCHAR(32)",
        "irrigation_type": "VARCHAR(32)",
        "urban_plan_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
        "urban_plan_message": "TEXT",
        "urban_plan_checked_at": "TIMESTAMP",
        "urban_plan_override_accepted_at": "TIMESTAMP",
        "urban_plan_override_user_id": "VARCHAR(32)",
        "urban_plan_override_text": "TEXT",
        "urban_plan_waiver_kind": "VARCHAR(32)",
        "urban_plan_auto_waive_reason": "TEXT",
        "urban_plan_coverage_status": "VARCHAR(32)",
        "urban_plan_coverage_id": "INTEGER",
        "progress_message_id": "INTEGER",
        "search_completed_notified_at": "TIMESTAMP",
        "funnel_session_id": "VARCHAR(36)",
        "search_started_at": "TIMESTAMP",
        "search_finished_at": "TIMESTAMP",
        "search_outcome": "VARCHAR(48)",
    }
    with db_engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE search_requests ADD COLUMN {name} {definition}")
                )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_search_requests_retry_of_request_id_unique "
                "ON search_requests (retry_of_request_id) "
                "WHERE retry_of_request_id IS NOT NULL"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_search_requests_continuation_unique "
                "ON search_requests (continuation_of_request_id) "
                "WHERE continuation_of_request_id IS NOT NULL"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_search_requests_payment_provider_invoice_unique "
                "ON search_requests (payment_provider, payment_provider_invoice_id) "
                "WHERE payment_provider IS NOT NULL "
                "AND payment_provider_invoice_id IS NOT NULL"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_search_requests_web_account_id "
                "ON search_requests (web_account_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_search_requests_access_expires_at "
                "ON search_requests (access_expires_at)"
            )
        )

    candidate_columns = {
        column["name"] for column in inspect(db_engine).get_columns("candidates")
    }
    candidate_additions = {
        "nearby_category_id": "VARCHAR(16)",
        "urban_plan_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
        "urban_plan_zone": "VARCHAR(240)",
        "urban_plan_document": "VARCHAR(320)",
        "urban_plan_source_url": "TEXT",
    }
    with db_engine.begin() as connection:
        for name, definition in candidate_additions.items():
            if name not in candidate_columns:
                connection.execute(
                    text(f"ALTER TABLE candidates ADD COLUMN {name} {definition}")
                )
    if "delivered_at" not in candidate_columns:
        with db_engine.begin() as connection:
            connection.execute(text("ALTER TABLE candidates ADD COLUMN delivered_at TIMESTAMP"))
            connection.execute(
                text(
                    "UPDATE candidates SET delivered_at = CURRENT_TIMESTAMP "
                    "WHERE request_id IN ("
                    "SELECT id FROM search_requests WHERE status = 'delivered'"
                    ")"
                )
            )
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_candidates_delivered_at "
                "ON candidates (delivered_at)"
            )
        )


def _add_analytics_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "funnel_events" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("funnel_events")}
    if "funnel_session_id" not in columns:
        with db_engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE funnel_events ADD COLUMN funnel_session_id VARCHAR(36)")
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_funnel_events_funnel_session_id "
                    "ON funnel_events (funnel_session_id)"
                )
            )


def _add_urban_plan_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "urban_plan_layers" not in inspector.get_table_names():
        return
    columns = {
        column["name"] for column in inspector.get_columns("urban_plan_layers")
    }
    additions = {
        "purpose": "VARCHAR(32) NOT NULL DEFAULT 'all'",
        "source_file_name": "VARCHAR(260)",
        "source_sha256": "VARCHAR(64)",
        "source_version": "VARCHAR(120)",
        "provenance_status": "VARCHAR(64) NOT NULL DEFAULT 'unknown'",
        "identity_status": "VARCHAR(64) NOT NULL DEFAULT 'unverified'",
        "qa_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
        "independent_review": "BOOLEAN NOT NULL DEFAULT FALSE",
        "qa_review_json": "TEXT",
        "approved_for_search": "BOOLEAN NOT NULL DEFAULT FALSE",
        "uploaded_by": "VARCHAR(120)",
    }
    with db_engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE urban_plan_layers ADD COLUMN {name} {definition}")
                )


def _add_planning_candidate_review_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "planning_candidate_reviews" not in inspector.get_table_names():
        return
    columns = {
        column["name"] for column in inspector.get_columns("planning_candidate_reviews")
    }
    additions = {
        "nearby_cadastre": "VARCHAR(64)",
        "nearby_distance_m": "FLOAT",
        "nearby_land_use": "VARCHAR(240)",
        "candidate_area_ha": "FLOAT",
        "selection_reason": "TEXT",
    }
    with db_engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE planning_candidate_reviews "
                        f"ADD COLUMN {name} {definition}"
                    )
                )


def _add_urban_plan_coverage_indexes(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "urban_plan_coverage" not in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_urban_plan_coverage_scope_unique "
                "ON urban_plan_coverage (region, district, locality, purpose)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_urban_plan_coverage_status "
                "ON urban_plan_coverage (coverage_status)"
            )
        )


def _add_auction_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    tables = set(inspector.get_table_names())
    if "auction_lots" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("auction_lots")}
    additions = {
        "source_search_status": "VARCHAR(64)",
        "functional_purpose_level2": "VARCHAR(240)",
        "functional_purpose_level3": "VARCHAR(320)",
        "functional_purpose_level4": "VARCHAR(320)",
        "use_goal": "VARCHAR(160)",
    }
    with db_engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE auction_lots ADD COLUMN {name} {definition}")
                )
        if db_engine.dialect.name == "postgresql":
            status_types = dict(
                connection.execute(
                    text(
                        """
                        SELECT table_name, data_type
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND column_name = 'status'
                          AND table_name IN ('auction_lots', 'auction_lot_history')
                        """
                    )
                ).all()
            )
            if status_types.get("auction_lots") != "text":
                connection.execute(
                    text("ALTER TABLE auction_lots ALTER COLUMN status TYPE TEXT")
                )
            if (
                "auction_lot_history" in tables
                and status_types.get("auction_lot_history") != "text"
            ):
                connection.execute(
                    text("ALTER TABLE auction_lot_history ALTER COLUMN status TYPE TEXT")
                )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_auction_lots_functional_purpose_level2 "
                "ON auction_lots (functional_purpose_level2)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_auction_lots_source_search_status "
                "ON auction_lots (source_search_status)"
            )
        )
        if "auction_subscriptions" in inspector.get_table_names():
            subscription_columns = {
                column["name"]
                for column in inspector.get_columns("auction_subscriptions")
            }
            subscription_additions = {
                "account_id": "VARCHAR(36)",
                "district": "VARCHAR(160)",
                "locality": "VARCHAR(160)",
                "min_price_kzt": "FLOAT",
            }
            for name, definition in subscription_additions.items():
                if name not in subscription_columns:
                    connection.execute(
                        text(
                            f"ALTER TABLE auction_subscriptions "
                            f"ADD COLUMN {name} {definition}"
                        )
                    )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_subscriptions_district "
                    "ON auction_subscriptions (district)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_subscriptions_locality "
                    "ON auction_subscriptions (locality)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_subscriptions_account_id "
                    "ON auction_subscriptions (account_id)"
                )
            )
            if db_engine.dialect.name == "postgresql":
                connection.execute(
                    text(
                        "ALTER TABLE auction_subscriptions "
                        "DROP CONSTRAINT IF EXISTS uq_auction_subscription_filter"
                    )
                )
                connection.execute(
                    text("DROP INDEX IF EXISTS uq_auction_subscription_filter")
                )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_auction_subscription_filter_v2 "
                    "ON auction_subscriptions ("
                    "telegram_user_id, "
                    "COALESCE(region, ''), "
                    "COALESCE(district, ''), "
                    "COALESCE(locality, ''), "
                    "COALESCE(purpose_query, ''), "
                    "COALESCE(min_price_kzt, -1), "
                    "COALESCE(max_price_kzt, -1), "
                    "COALESCE(min_area_ha, -1), "
                    "COALESCE(max_area_ha, -1)"
                    ")"
                )
            )
            connection.execute(
                text(
                    "UPDATE auction_subscriptions SET purpose_query = NULL "
                    "WHERE purpose_query IN ('жил', 'магазин', 'сельск', 'производ')"
                )
            )
        if "auction_watchlists" in inspector.get_table_names():
            watchlist_columns = {
                column["name"]
                for column in inspector.get_columns("auction_watchlists")
            }
            watchlist_additions = {
                "lot_scope": "VARCHAR(24)",
                "min_price_kzt": "FLOAT",
                "risk_level": "VARCHAR(16)",
                "confidence_level": "VARCHAR(16)",
                "stage": "VARCHAR(40)",
                "deadline_status": "VARCHAR(24)",
                "geo_status": "VARCHAR(32)",
            }
            for name, definition in watchlist_additions.items():
                if name not in watchlist_columns:
                    connection.execute(
                        text(
                            f"ALTER TABLE auction_watchlists "
                            f"ADD COLUMN {name} {definition}"
                        )
                    )
            connection.execute(
                text(
                    "UPDATE auction_watchlists SET lot_scope = 'active' "
                    "WHERE lot_scope IS NULL OR lot_scope = ''"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_watchlists_lot_scope "
                    "ON auction_watchlists (lot_scope)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_watchlists_risk_level "
                    "ON auction_watchlists (risk_level)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_watchlists_confidence_level "
                    "ON auction_watchlists (confidence_level)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_watchlists_stage "
                    "ON auction_watchlists (stage)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_watchlists_deadline_status "
                    "ON auction_watchlists (deadline_status)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_watchlists_geo_status "
                    "ON auction_watchlists (geo_status)"
                )
            )
        if "auction_favorites" in inspector.get_table_names():
            favorite_columns = {
                column["name"] for column in inspector.get_columns("auction_favorites")
            }
            if "account_id" not in favorite_columns:
                connection.execute(
                    text("ALTER TABLE auction_favorites ADD COLUMN account_id VARCHAR(36)")
                )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_favorites_account_id "
                    "ON auction_favorites (account_id)"
                )
            )
            if db_engine.dialect.name == "postgresql":
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "uq_auction_favorite_account_lot "
                        "ON auction_favorites (account_id, lot_id) "
                        "WHERE account_id IS NOT NULL"
                    )
                )
        if "auction_access" in inspector.get_table_names():
            access_columns = {
                column["name"] for column in inspector.get_columns("auction_access")
            }
            if "access_expires_at" not in access_columns:
                connection.execute(
                    text("ALTER TABLE auction_access ADD COLUMN access_expires_at TIMESTAMP")
                )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_access_access_expires_at "
                    "ON auction_access (access_expires_at)"
                )
            )
        if "auction_documents" in inspector.get_table_names():
            document_columns = {
                column["name"] for column in inspector.get_columns("auction_documents")
            }
            document_additions = {
                "storage_status": "VARCHAR(32) NOT NULL DEFAULT 'linked'",
                "local_path": "TEXT",
                "content_sha256": "VARCHAR(64)",
                "downloaded_at": "TIMESTAMP",
                "download_error": "TEXT",
            }
            for name, definition in document_additions.items():
                if name not in document_columns:
                    connection.execute(
                        text(
                            f"ALTER TABLE auction_documents "
                            f"ADD COLUMN {name} {definition}"
                        )
                    )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_documents_storage_status "
                    "ON auction_documents (storage_status)"
                )
            )


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
