from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.auction_history_normalization import (
    MAX_BATCH_SIZE,
    promote_history_generation,
    run_history_normalization_batch,
    start_history_generation,
)
from app.auction_history_store import SqlAlchemyHistoryNormalizationStore


def normalize_auction_history_step(
    session_factory: Callable[[], Session],
    *,
    generation: int | None = None,
    batch_size: int = 200,
    source_cutoff: datetime | None = None,
) -> dict[str, object]:
    """Run one bounded worker step; every database phase owns a short transaction."""
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or not 1 <= batch_size <= MAX_BATCH_SIZE
    ):
        raise ValueError("batch_size_out_of_bounds")
    if source_cutoff is not None and source_cutoff.utcoffset() is None:
        raise ValueError("source_cutoff must be timezone-aware")

    if generation is None:
        with session_factory() as session:
            store = SqlAlchemyHistoryNormalizationStore(session)
            building = store.get_building_generation()
            session.rollback()
            if building is None:
                building = start_history_generation(
                    store,
                    source_cutoff=source_cutoff or datetime.now(UTC),
                )
            if building is None:
                # Another worker won the partial-unique building-run race.
                return {
                    "status": "concurrent_start",
                    "generation": None,
                    "has_more": True,
                    "processed_count": 0,
                }
            generation = building.generation

    with session_factory() as session:
        store = SqlAlchemyHistoryNormalizationStore(session)
        run = store.get_generation(generation)
        session.rollback()
        if run is None:
            return {
                "status": "missing_generation",
                "generation": generation,
                "has_more": False,
                "processed_count": 0,
            }
        if run.status == "active":
            return {
                "status": "active",
                "generation": generation,
                "has_more": False,
                "processed_count": run.processed_count,
            }
        if run.status != "building":
            return {
                "status": run.status,
                "generation": generation,
                "has_more": False,
                "processed_count": run.processed_count,
            }

        result = run_history_normalization_batch(
            store,
            run=run,
            batch_size=batch_size,
        )
        promoted = None
        if result.status in {"ok", "complete"} and result.run.scan_complete:
            try:
                promoted = promote_history_generation(store, result.run)
            except ValueError as exc:
                failed = store.fail_generation(result.run, str(exc))
                return {
                    "status": failed.status,
                    "generation": failed.generation,
                    "has_more": False,
                    "processed_count": failed.processed_count,
                    "expected_count": failed.expected_count,
                    "upserted_count": result.upserted_count,
                    "fetched_count": result.fetched_count,
                    "detail": failed.detail,
                }
        final_run = promoted or result.run
        return {
            "status": final_run.status if promoted is not None else result.status,
            "generation": final_run.generation,
            "has_more": bool(result.has_more and final_run.status == "building"),
            "processed_count": final_run.processed_count,
            "expected_count": final_run.expected_count,
            "upserted_count": result.upserted_count,
            "fetched_count": result.fetched_count,
            "detail": result.detail,
        }
