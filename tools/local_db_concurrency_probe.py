from __future__ import annotations

import argparse
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.orm import Session

from app.db import init_db
from app.models import Account, SearchRequest, SearchStatus


def _build_engine(db_path: Path, pool_size: int, max_overflow: int):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=30,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        _ = connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def _create_request(engine, index: int) -> str:
    with Session(engine) as session:
        account = Account(phone=f"+7700000{index:04d}")
        session.add(account)
        session.flush()
        request = SearchRequest(
            web_account_id=account.id,
            region="local-probe-region",
            district="local-probe-district",
            locality=f"locality-{index % 25}",
            status=SearchStatus.queued.value,
            raw_query=f"local concurrency probe {index}",
        )
        session.add(request)
        session.commit()
        return request.id


def run_probe(total: int, workers: int, pool_size: int, max_overflow: int) -> dict[str, int]:
    with tempfile.TemporaryDirectory(prefix="land-scout-db-probe-") as tmp_dir:
        db_path = Path(tmp_dir) / "probe.db"
        engine = _build_engine(db_path, pool_size, max_overflow)
        try:
            init_db(bind=engine)

            created_ids: list[str] = []
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(_create_request, engine, index) for index in range(total)
                ]
                for future in as_completed(futures):
                    created_ids.append(future.result())

            with Session(engine) as session:
                request_count = session.scalar(select(func.count()).select_from(SearchRequest)) or 0
                account_count = session.scalar(select(func.count()).select_from(Account)) or 0
            search_indexes = {
                index["name"] for index in inspect(engine).get_indexes("search_requests")
            }
            required_indexes = {
                "ix_search_requests_status_updated_at",
                "ix_search_requests_payment_status_user",
                "ix_search_requests_payment_status_account",
            }
            missing_indexes = required_indexes - search_indexes
            if missing_indexes:
                raise RuntimeError(f"Missing expected indexes: {sorted(missing_indexes)}")
            if request_count != total or account_count != total or len(set(created_ids)) != total:
                raise RuntimeError(
                    "Concurrency probe mismatch: "
                    f"requests={request_count}, accounts={account_count}, "
                    f"ids={len(set(created_ids))}"
                )
            return {
                "total": total,
                "workers": workers,
                "pool_size": pool_size,
                "max_overflow": max_overflow,
                "requests": request_count,
                "accounts": account_count,
            }
        finally:
            engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local DB concurrency smoke probe.")
    parser.add_argument("--total", type=int, default=500)
    parser.add_argument("--workers", type=int, default=50)
    parser.add_argument("--pool-size", type=int, default=10)
    parser.add_argument("--max-overflow", type=int, default=20)
    args = parser.parse_args()

    result = run_probe(args.total, args.workers, args.pool_size, args.max_overflow)
    print(
        "OK "
        f"total={result['total']} workers={result['workers']} "
        f"pool_size={result['pool_size']} max_overflow={result['max_overflow']} "
        f"requests={result['requests']} accounts={result['accounts']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
