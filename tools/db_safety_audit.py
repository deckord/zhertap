from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, inspect, text

from app.config import settings
from app.db import _engine_kwargs, validate_database_url


@dataclass(frozen=True)
class Check:
    name: str
    sql: str
    risk: str


CHECKS = [
    Check(
        name="duplicate_account_payments_awaiting_transfer",
        sql="""
            SELECT account_id, COUNT(*) AS count
            FROM account_payments
            WHERE payment_status = 'awaiting_transfer'
            GROUP BY account_id
            HAVING COUNT(*) > 1
            ORDER BY count DESC
            LIMIT 20
        """,
        risk="More than one pending account payment can confuse payment callbacks.",
    ),
    Check(
        name="duplicate_search_payment_invoices",
        sql="""
            SELECT payment_provider, payment_provider_invoice_id, COUNT(*) AS count
            FROM search_requests
            WHERE payment_provider IS NOT NULL
              AND payment_provider_invoice_id IS NOT NULL
            GROUP BY payment_provider, payment_provider_invoice_id
            HAVING COUNT(*) > 1
            ORDER BY count DESC
            LIMIT 20
        """,
        risk="A provider invoice should map to one search request.",
    ),
    Check(
        name="stale_processing_searches",
        sql="""
            SELECT id, telegram_user_id, web_account_id, updated_at
            FROM search_requests
            WHERE status = 'processing'
              AND updated_at < :stale_before
            ORDER BY updated_at ASC
            LIMIT 20
        """,
        risk="Processing rows older than the threshold may be stuck worker jobs.",
    ),
    Check(
        name="ready_not_notified_searches",
        sql="""
            SELECT id, telegram_user_id, web_account_id, updated_at
            FROM search_requests
            WHERE status = 'ready'
              AND telegram_chat_id IS NOT NULL
              AND search_completed_notified_at IS NULL
            ORDER BY updated_at ASC
            LIMIT 20
        """,
        risk="Telegram searches ready without completion notification can create support load.",
    ),
    Check(
        name="duplicate_active_web_sessions_by_token",
        sql="""
            SELECT token_hash, COUNT(*) AS count
            FROM web_sessions
            WHERE revoked_at IS NULL
            GROUP BY token_hash
            HAVING COUNT(*) > 1
            ORDER BY count DESC
            LIMIT 20
        """,
        risk="Token hash duplicates would be a security and session consistency issue.",
    ),
]


def _safe_database_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    if not parsed.password:
        return database_url
    username = parsed.username or ""
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{username}:***@{hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _table_names(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _run_check(connection, check: Check, params: dict[str, Any]) -> dict[str, Any]:
    rows = [dict(row._mapping) for row in connection.execute(text(check.sql), params)]
    return {
        "name": check.name,
        "risk": check.risk,
        "row_count": len(rows),
        "sample": rows,
        "status": "ok" if not rows else "attention",
    }


def audit_database(database_url: str, stale_minutes: int) -> dict[str, Any]:
    validate_database_url(database_url, settings.app_env)
    engine = create_engine(database_url, **_engine_kwargs(database_url))
    stale_before = datetime.now(UTC) - timedelta(minutes=stale_minutes)
    tables = _table_names(engine)
    applicable_checks = [
        check
        for check in CHECKS
        if _required_tables(check.sql).issubset(tables)
    ]

    with engine.connect() as connection:
        checks = [
            _run_check(connection, check, {"stale_before": stale_before})
            for check in applicable_checks
        ]

    return {
        "database_url": _safe_database_url(database_url),
        "dialect": engine.dialect.name,
        "checked_at": datetime.now(UTC).isoformat(),
        "stale_minutes": stale_minutes,
        "skipped_checks": sorted(
            check.name for check in CHECKS if check not in applicable_checks
        ),
        "checks": checks,
        "attention_count": sum(1 for check in checks if check["status"] == "attention"),
    }


def _required_tables(sql: str) -> set[str]:
    known_tables = {
        "account_payments",
        "search_requests",
        "web_sessions",
    }
    return {table for table in known_tables if table in sql}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only DB safety audit.")
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--stale-minutes", type=int, default=30)
    args = parser.parse_args(argv)

    report = audit_database(args.database_url, args.stale_minutes)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default))
    return 1 if report["attention_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
