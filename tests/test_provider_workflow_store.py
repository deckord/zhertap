from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import (
    AuctionCrawlRun,
    AuctionLot,
    AuctionSource,
    Base,
    ProviderRunDispatch,
    ProviderSyncRun,
    ProviderWorkflowState,
    ProviderWorkflowUnit,
)
from app.provider_workflow_store import (
    ProviderUnitSpec,
    attach_provider_run_parent,
    claim_provider_run_dispatch,
    claim_provider_unit,
    claim_ready_provider_run,
    complete_provider_run_dispatch,
    complete_provider_unit,
    create_provider_workflow,
    due_provider_workflow_keys,
    ensure_provider_crawl_run,
    ensure_provider_sync_run,
    eqazyna_history_checkpoint_key,
    eqazyna_history_resume_checkpoint,
    eqazyna_source_exhaustion_ledger,
    expire_stale_provider_parents,
    finalizable_provider_runs,
    finish_provider_run,
    finish_source_run_and_parents,
    provider_workflow_pending,
)
from app.provider_workflow_worker import (
    process_provider_workflow_step,
    seed_eqazyna_page_workflow,
)
from app.providers.egkn import DistrictInfo, EgknProviderError


def _sessions() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_300_units_survive_more_than_checkpoint_ttl_without_refetch_or_body_storage() -> None:
    sessions = _sessions()
    units = [
        ProviderUnitSpec(
            unit_key=f"page:{index:04d}",
            unit_kind="eqazyna_list_page",
            input_payload={"page": index, "search_status": "ApplicationsAccept"},
        )
        for index in range(1, 301)
    ]
    assert (
        create_provider_workflow(
            sessions,
            workflow_key="eq:max-window",
            provider="eqazyna",
            workflow_kind="auction_list_and_detail",
            units=units,
            now=datetime(2026, 8, 17, tzinfo=UTC),
        )
        == 300
    )

    fetched: dict[str, int] = {}
    now = datetime(2026, 8, 17, tzinfo=UTC)
    for _ in range(300):
        claimed = claim_provider_unit(sessions, workflow_key="eq:max-window", now=now)
        assert claimed is not None
        fetched[claimed.unit_key] = fetched.get(claimed.unit_key, 0) + 1
        assert complete_provider_unit(sessions, claimed, result_ref="persisted", now=now)
        now += timedelta(seconds=2)  # total elapsed is 600s (> rejected cache TTL)

    assert provider_workflow_pending(sessions, "eq:max-window") == 0
    assert len(fetched) == 300
    assert set(fetched.values()) == {1}
    with sessions() as session:
        state = session.get(ProviderWorkflowState, "eq:max-window")
        rows = session.query(ProviderWorkflowUnit).all()
        assert state is not None and state.completed_units == 300
        assert len(rows) == 300
        assert sum(len(row.input_json) for row in rows) < 30_000
        assert all(
            "response" not in row.input_json and "body" not in row.input_json
            for row in rows
        )


def test_completed_unit_is_never_claimed_again() -> None:
    sessions = _sessions()
    create_provider_workflow(
        sessions,
        workflow_key="osm:one",
        provider="osm_overpass",
        workflow_kind="site_context",
        units=[
            ProviderUnitSpec(
                unit_key="batch:1",
                unit_kind="osm_batch",
                input_payload={"rows": [["lot", 51.1, 71.4]]},
            )
        ],
    )
    claimed = claim_provider_unit(sessions, workflow_key="osm:one")
    assert claimed is not None
    assert complete_provider_unit(sessions, claimed)
    assert claim_provider_unit(sessions, workflow_key="osm:one") is None


def test_eqazyna_source_exhaustion_ledger_distinguishes_empty_page_from_page_cap() -> None:
    sessions = _sessions()
    run_key, _ = ensure_provider_sync_run(
        sessions,
        run_kind="history",
        detail_limit=100,
        config_payload={},
    )
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key=f"{run_key}:accepting",
        search_status="ApplicationsAccept",
        max_pages=3,
        publish_date_window=("01.01.2026", "31.12.2026"),
        run_key=run_key,
    )
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key=f"{run_key}:sold",
        search_status="SuccessProtocolSigned",
        max_pages=2,
        publish_date_window=("01.01.2025", "31.12.2025"),
        run_key=run_key,
    )

    accepting_page_1 = claim_provider_unit(sessions, workflow_key=f"{run_key}:accepting")
    assert accepting_page_1 is not None
    assert complete_provider_unit(sessions, accepting_page_1, result_ref="urls:2")
    accepting_page_2 = claim_provider_unit(sessions, workflow_key=f"{run_key}:accepting")
    assert accepting_page_2 is not None
    assert complete_provider_unit(sessions, accepting_page_2, result_ref="urls:0")

    for _ in range(2):
        sold_page = claim_provider_unit(sessions, workflow_key=f"{run_key}:sold")
        assert sold_page is not None
        assert complete_provider_unit(sessions, sold_page, result_ref="urls:20")

    ledger = eqazyna_source_exhaustion_ledger(sessions, run_key)

    assert [entry.search_status for entry in ledger] == [
        "ApplicationsAccept",
        "SuccessProtocolSigned",
    ]
    assert ledger[0].publish_date_window == ("01.01.2026", "31.12.2026")
    assert ledger[0].pages_requested == 2
    assert ledger[0].urls_seen == 2
    assert ledger[0].first_empty_page == 2
    assert ledger[0].exhausted is True
    assert ledger[0].partial_reason is None
    assert ledger[1].publish_date_window == ("01.01.2025", "31.12.2025")
    assert ledger[1].pages_requested == 2
    assert ledger[1].urls_seen == 40
    assert ledger[1].first_empty_page is None
    assert ledger[1].exhausted is False
    assert ledger[1].partial_reason == "max_pages_reached"


def test_eqazyna_source_exhaustion_ledger_waits_for_pending_details() -> None:
    sessions = _sessions()
    run_key, _ = ensure_provider_sync_run(
        sessions,
        run_kind="history",
        detail_limit=100,
        config_payload={},
    )
    workflow_key = f"{run_key}:sold"
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key=workflow_key,
        search_status="SuccessProtocolSigned",
        max_pages=2,
        publish_date_window=("01.01.2025", "31.12.2025"),
        run_key=run_key,
    )
    page = claim_provider_unit(sessions, workflow_key=workflow_key)
    assert page is not None
    assert complete_provider_unit(
        sessions,
        page,
        result_ref="urls:0",
        followup_units=[
            ProviderUnitSpec(
                unit_key="detail:pending",
                unit_kind="eqazyna_lot_detail",
                input_payload={
                    "source_url": "https://sauda.e-qazyna.kz/ru/list/1",
                    "search_status": "SuccessProtocolSigned",
                },
            )
        ],
    )

    entry = eqazyna_source_exhaustion_ledger(sessions, run_key)[0]

    assert entry.first_empty_page == 1
    assert entry.exhausted is False
    assert entry.partial_reason == "in_progress"


def test_history_barrier_persists_bounded_resume_and_replays_capped_page() -> None:
    sessions = _sessions()
    run_key, _ = ensure_provider_sync_run(
        sessions,
        run_kind="history",
        detail_limit=100,
        config_payload={"normalize_history": True},
    )
    exhausted_window = ("01.01.2024", "31.12.2024")
    capped_window = ("01.01.2023", "31.12.2023")
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key=f"{run_key}:exhausted",
        search_status="SuccessProtocolSigned",
        max_pages=2,
        publish_date_window=exhausted_window,
        run_key=run_key,
    )
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key=f"{run_key}:capped",
        search_status="SuccessProtocolSigned",
        max_pages=3,
        start_page=11,
        publish_date_window=capped_window,
        run_key=run_key,
    )
    for workflow_key, results in (
        (f"{run_key}:exhausted", ("urls:20", "urls:0")),
        (f"{run_key}:capped", ("urls:20", "urls:20")),
    ):
        for result_ref in results:
            unit = claim_provider_unit(sessions, workflow_key=workflow_key)
            assert unit is not None
            assert complete_provider_unit(sessions, unit, result_ref=result_ref)
    with sessions() as session:
        capped_tail = session.scalar(
            select(ProviderWorkflowUnit).where(
                ProviderWorkflowUnit.workflow_key == f"{run_key}:capped",
                ProviderWorkflowUnit.unit_key == "page:0013",
            )
        )
        assert capped_tail is not None
        capped_tail.status = "terminal"
        capped_tail.last_error = "detail_limit_reached"
        session.commit()

    barrier = claim_ready_provider_run(sessions, run_key)

    assert barrier is not None
    expected = {
        eqazyna_history_checkpoint_key("SuccessProtocolSigned", exhausted_window): 0,
        # Re-fetch the cap-triggering page so a crash between list handling and
        # durable detail insertion cannot leave a hole.
        eqazyna_history_checkpoint_key("SuccessProtocolSigned", capped_window): 12,
    }
    assert barrier.config_payload["eqazyna_history_pages"] == expected
    assert eqazyna_history_resume_checkpoint(sessions) == expected
    with sessions() as session:
        run = session.get(ProviderSyncRun, run_key)
        assert run is not None
        assert json.loads(run.config_json)["eqazyna_history_pages"] == expected
        assert len(run.config_json.encode("utf-8")) <= 16_000

    # A later capped run updates only its own window and retains the exhausted
    # sibling even though that sibling correctly has no new workflow.
    assert finish_provider_run(sessions, run_key, success=True)
    next_run_key, _ = ensure_provider_sync_run(
        sessions,
        run_kind="history",
        detail_limit=100,
        config_payload={
            "normalize_history": True,
            "eqazyna_history_pages": expected,
        },
    )
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key=f"{next_run_key}:capped",
        search_status="SuccessProtocolSigned",
        max_pages=2,
        start_page=expected[eqazyna_history_checkpoint_key("SuccessProtocolSigned", capped_window)],
        publish_date_window=capped_window,
        run_key=next_run_key,
    )
    for result_ref in ("urls:20", "urls:20"):
        unit = claim_provider_unit(sessions, workflow_key=f"{next_run_key}:capped")
        assert unit is not None
        assert complete_provider_unit(sessions, unit, result_ref=result_ref)
    next_barrier = claim_ready_provider_run(sessions, next_run_key)
    assert next_barrier is not None
    assert next_barrier.config_payload["eqazyna_history_pages"] == {
        eqazyna_history_checkpoint_key("SuccessProtocolSigned", exhausted_window): 0,
        eqazyna_history_checkpoint_key("SuccessProtocolSigned", capped_window): 13,
    }


def test_history_checkpoint_does_not_exhaust_window_with_failed_detail() -> None:
    sessions = _sessions()
    window = ("01.01.2025", "31.12.2025")
    checkpoint_key = eqazyna_history_checkpoint_key("SuccessProtocolSigned", window)
    run_key, _ = ensure_provider_sync_run(
        sessions,
        run_kind="history",
        detail_limit=100,
        config_payload={
            "normalize_history": True,
            "eqazyna_history_pages": {checkpoint_key: 11},
        },
    )
    workflow_key = f"{run_key}:sold"
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key=workflow_key,
        search_status="SuccessProtocolSigned",
        max_pages=2,
        start_page=11,
        publish_date_window=window,
        run_key=run_key,
    )
    for result_ref in ("urls:1", "urls:0"):
        page = claim_provider_unit(sessions, workflow_key=workflow_key)
        assert page is not None
        assert complete_provider_unit(sessions, page, result_ref=result_ref)
    assert create_provider_workflow(
        sessions,
        workflow_key=workflow_key,
        provider="eqazyna",
        workflow_kind="auction_list_and_detail",
        units=[
            ProviderUnitSpec(
                unit_key="detail:failed",
                unit_kind="eqazyna_lot_detail",
                input_payload={"source_url": "https://sauda.e-qazyna.kz/ru/list/failed"},
            )
        ],
        run_key=run_key,
    ) == 1
    detail = claim_provider_unit(sessions, workflow_key=workflow_key)
    assert detail is not None
    with sessions() as session:
        row = session.get(ProviderWorkflowUnit, detail.id)
        assert row is not None
        row.status = "terminal"
        row.claim_token = None
        row.claim_expires_at = None
        row.last_error = "eqazyna:rate_limited"
        session.commit()

    barrier = claim_ready_provider_run(sessions, run_key)

    assert barrier is not None
    assert barrier.has_errors is True
    # The failed detail may have been discovered on any fetched page. Retain the
    # preceding durable checkpoint so the next bounded run replays the window.
    assert barrier.config_payload["eqazyna_history_pages"] == {checkpoint_key: 11}


def test_repeated_detail_key_does_not_consume_run_detail_limit() -> None:
    sessions = _sessions()
    run_key, _ = ensure_provider_sync_run(
        sessions,
        run_kind="history",
        detail_limit=2,
        config_payload={},
    )
    workflow_key = f"{run_key}:duplicate-detail"
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key=workflow_key,
        search_status="SuccessProtocolSigned",
        max_pages=3,
        publish_date_window=("01.01.2025", "31.12.2025"),
        run_key=run_key,
    )
    duplicate = ProviderUnitSpec(
        unit_key="detail:stable-offer",
        unit_kind="eqazyna_lot_detail",
        input_payload={"url": "https://example.test/stable-offer"},
    )

    first_page = claim_provider_unit(sessions, workflow_key=workflow_key)
    assert first_page is not None and first_page.unit_key == "page:0001"
    assert complete_provider_unit(sessions, first_page, followup_units=[duplicate])
    second_page = claim_provider_unit(sessions, workflow_key=workflow_key)
    assert second_page is not None and second_page.unit_key == "page:0002"
    assert complete_provider_unit(sessions, second_page, followup_units=[duplicate])

    with sessions() as session:
        run = session.get(ProviderSyncRun, run_key)
        page_three = session.scalar(
            select(ProviderWorkflowUnit).where(
                ProviderWorkflowUnit.workflow_key == workflow_key,
                ProviderWorkflowUnit.unit_key == "page:0003",
            )
        )
        detail_rows = list(
            session.scalars(
                select(ProviderWorkflowUnit).where(
                    ProviderWorkflowUnit.workflow_key == workflow_key,
                    ProviderWorkflowUnit.unit_kind == "eqazyna_lot_detail",
                )
            )
        )
    assert run is not None and run.details_enqueued == 1
    assert page_three is not None and page_three.status == "pending"
    assert len(detail_rows) == 1


def test_error_workflow_with_pending_units_can_continue() -> None:
    sessions = _sessions()
    create_provider_workflow(
        sessions,
        workflow_key="egkn:stale-error",
        provider="egkn",
        workflow_kind="cadastre",
        units=[
            ProviderUnitSpec(
                unit_key="resolve:1",
                unit_kind="egkn_resolve_district",
                input_payload={"lot_id": "lot-1", "cadastre": "01-001-001-001"},
            )
        ],
    )
    with sessions() as session:
        state = session.get(ProviderWorkflowState, "egkn:stale-error")
        assert state is not None
        state.status = "error"
        state.last_error = "old terminal unit failed"
        session.commit()

    claimed = claim_provider_unit(sessions, workflow_key="egkn:stale-error")

    assert claimed is not None
    assert claimed.unit_key == "resolve:1"


def test_run_reconciliation_reopens_terminal_child_with_pending_unit() -> None:
    sessions = _sessions()
    run_key, created = ensure_provider_sync_run(
        sessions,
        run_kind="history",
        detail_limit=100,
        config_payload={},
    )
    assert created
    create_provider_workflow(
        sessions,
        workflow_key="eq:stale-terminal-child",
        provider="eqazyna",
        workflow_kind="auction_list_and_detail",
        units=[
            ProviderUnitSpec(
                unit_key="detail:late",
                unit_kind="eqazyna_lot_detail",
                input_payload={"url": "https://example.test/late"},
            )
        ],
        run_key=run_key,
    )
    with sessions() as session:
        run = session.get(ProviderSyncRun, run_key)
        state = session.get(ProviderWorkflowState, "eq:stale-terminal-child")
        assert run is not None and state is not None
        run.completed_children = run.child_count
        state.status = "complete"
        session.commit()

    assert claim_ready_provider_run(sessions, run_key) is None

    with sessions() as session:
        run = session.get(ProviderSyncRun, run_key)
        state = session.get(ProviderWorkflowState, "eq:stale-terminal-child")
        assert run is not None and state is not None
        assert run.completed_children == 0
        assert state.status == "pending"
        assert state.next_attempt_at is not None
    assert due_provider_workflow_keys(sessions) == ["eq:stale-terminal-child"]


def test_bad_egkn_cadastre_unit_does_not_poison_parallel_workflow() -> None:
    sessions = _sessions()
    create_provider_workflow(
        sessions,
        workflow_key="egkn:mixed",
        provider="egkn",
        workflow_kind="cadastre",
        units=[
            ProviderUnitSpec(
                unit_key="resolve:bad",
                unit_kind="egkn_resolve_district",
                input_payload={"lot_id": "lot-bad", "cadastre": "08-114-"},
            ),
            ProviderUnitSpec(
                unit_key="resolve:good",
                unit_kind="egkn_resolve_district",
                input_payload={"lot_id": "lot-good", "cadastre": "01-001-001-001"},
            ),
        ],
    )

    class _Egkn:
        def resolve_district_for_cadastre(self, cadastre: str) -> DistrictInfo:
            if cadastre == "08-114-":
                raise EgknProviderError("Некорректный кадастровый номер")
            return DistrictInfo(
                id=252,
                region_name="г. Астана (21)",
                code="21-318",
                name="Алматы",
                display_name="р-н. Алматы (21-318)",
                srs=32642,
                ate_code="107193",
            )

    first = process_provider_workflow_step(
        sessions,
        workflow_key="egkn:mixed",
        egkn=_Egkn(),  # type: ignore[arg-type]
    )
    second = process_provider_workflow_step(
        sessions,
        workflow_key="egkn:mixed",
        egkn=_Egkn(),  # type: ignore[arg-type]
    )

    assert first.status == "terminal"
    assert first.pending == 1
    assert second.status == "progress"
    with sessions() as session:
        state = session.get(ProviderWorkflowState, "egkn:mixed")
        assert state is not None
        assert state.status == "pending"
        units = {
            row.unit_key: row.status
            for row in session.query(ProviderWorkflowUnit)
            .filter(ProviderWorkflowUnit.workflow_key == "egkn:mixed")
            .all()
        }
    assert units["resolve:bad"] == "terminal"
    assert units["resolve:good"] == "done"
    assert any(key.startswith("parcel:") and status == "pending" for key, status in units.items())


def test_exhausted_unit_is_quarantined_before_claiming_next_unit() -> None:
    sessions = _sessions()
    create_provider_workflow(
        sessions,
        workflow_key="docs:attempt-limit",
        provider="eqazyna",
        workflow_kind="auction_documents",
        units=[
            ProviderUnitSpec(
                unit_key="document:bad",
                unit_kind="auction_document",
                input_payload={"document_id": 25827},
            ),
            ProviderUnitSpec(
                unit_key="document:next",
                unit_kind="auction_document",
                input_payload={"document_id": 25828},
            ),
        ],
    )
    with sessions() as session:
        exhausted = (
            session.query(ProviderWorkflowUnit)
            .filter_by(workflow_key="docs:attempt-limit", unit_key="document:bad")
            .one()
        )
        exhausted.status = "error"
        exhausted.attempts = 100
        exhausted.last_error = "auction_documents:network_error"
        session.commit()

    claimed = claim_provider_unit(sessions, workflow_key="docs:attempt-limit")

    assert claimed is not None
    assert claimed.unit_key == "document:next"
    with sessions() as session:
        exhausted = (
            session.query(ProviderWorkflowUnit)
            .filter_by(workflow_key="docs:attempt-limit", unit_key="document:bad")
            .one()
        )
        state = session.get(ProviderWorkflowState, "docs:attempt-limit")
        assert exhausted.status == "terminal"
        assert exhausted.attempts == 100
        assert exhausted.last_error == "auction_documents:network_error"
        assert state is not None
        assert state.failed_units == 1
        assert state.status == "processing"


def test_eqazyna_worker_completes_300_parsed_page_units_without_refetch() -> None:
    sessions = _sessions()
    units = [
        ProviderUnitSpec(
            unit_key=f"page:{index:04d}",
            unit_kind="eqazyna_list_page",
            input_payload={
                "page": index,
                "search_status": "ApplicationsAccept",
                "publish_date_window": None,
            },
        )
        for index in range(1, 301)
    ]
    create_provider_workflow(
        sessions,
        workflow_key="eq:worker-max",
        provider="eqazyna",
        workflow_kind="auction_list_and_detail",
        units=units,
    )

    class _Eqazyna:
        def __init__(self) -> None:
            self.pages: dict[int, int] = {}

        def lot_url_page(self, *, page: int, **_kwargs):
            self.pages[page] = self.pages.get(page, 0) + 1
            return ["https://sauda.e-qazyna.kz/ru/auction/452662"]

    provider = _Eqazyna()
    for _ in range(300):
        result = process_provider_workflow_step(
            sessions,
            workflow_key="eq:worker-max",
            eqazyna=provider,  # type: ignore[arg-type]
        )
        assert result.status in {"progress", "complete"}

    # One deduplicated detail unit remains after every configured page advanced.
    assert provider_workflow_pending(sessions, "eq:worker-max") == 1
    assert len(provider.pages) == 300
    assert set(provider.pages.values()) == {1}


def test_history_page_skips_existing_lots_before_spending_detail_limit() -> None:
    sessions = _sessions()
    with sessions() as session:
        session.add(
            AuctionLot(
                source="e-qazyna",
                source_lot_id="100",
                source_url="https://sauda.e-qazyna.kz/ru/list/100",
                title="Existing archive lot",
                published_at=datetime(2020, 1, 1, tzinfo=UTC).date(),
            )
        )
        session.commit()
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key="eq:history-incremental",
        search_status="SuccessProtocolSigned",
        max_pages=1,
        skip_existing_details=True,
    )

    class _Eqazyna:
        def lot_url_page(self, **_kwargs):
            return [
                "https://sauda.e-qazyna.kz/ru/list/100",
                "https://sauda.e-qazyna.kz/ru/list/101",
            ]

    result = process_provider_workflow_step(
        sessions,
        workflow_key="eq:history-incremental",
        eqazyna=_Eqazyna(),  # type: ignore[arg-type]
    )

    assert result.status == "progress"
    with sessions() as session:
        details = list(
            session.scalars(
                select(ProviderWorkflowUnit).where(
                    ProviderWorkflowUnit.workflow_key == "eq:history-incremental",
                    ProviderWorkflowUnit.unit_kind == "eqazyna_lot_detail",
                )
            )
        )
    assert len(details) == 1
    assert '"source_url":"https://sauda.e-qazyna.kz/ru/list/101"' in details[0].input_json


def test_history_page_does_not_repeat_recent_missing_publication_date() -> None:
    sessions = _sessions()
    with sessions() as session:
        session.add(
            AuctionLot(
                source="e-qazyna",
                source_lot_id="100",
                source_url="https://sauda.e-qazyna.kz/ru/list/100",
                title="Archive lot missing publication date",
                published_at=None,
            )
        )
        session.commit()
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key="eq:history-repair-publication",
        search_status="SuccessProtocolSigned",
        max_pages=1,
        skip_existing_details=True,
    )

    class _Eqazyna:
        def lot_url_page(self, **_kwargs):
            return ["https://sauda.e-qazyna.kz/ru/list/100"]

    process_provider_workflow_step(
        sessions,
        workflow_key="eq:history-repair-publication",
        eqazyna=_Eqazyna(),  # type: ignore[arg-type]
    )

    with sessions() as session:
        details = list(
            session.scalars(
                select(ProviderWorkflowUnit).where(
                    ProviderWorkflowUnit.workflow_key == "eq:history-repair-publication",
                    ProviderWorkflowUnit.unit_kind == "eqazyna_lot_detail",
                )
            )
        )
    assert details == []


def test_history_page_retries_stale_lot_missing_publication_date() -> None:
    sessions = _sessions()
    with sessions() as session:
        session.add(
            AuctionLot(
                source="e-qazyna",
                source_lot_id="100",
                source_url="https://sauda.e-qazyna.kz/ru/list/100",
                title="Stale archive lot missing publication date",
                published_at=None,
                updated_at=datetime.now(UTC) - timedelta(days=31),
            )
        )
        session.commit()
    seed_eqazyna_page_workflow(
        sessions,
        workflow_key="eq:history-repair-stale-publication",
        search_status="SuccessProtocolSigned",
        max_pages=1,
        skip_existing_details=True,
    )

    class _Eqazyna:
        def lot_url_page(self, **_kwargs):
            return ["https://sauda.e-qazyna.kz/ru/list/100"]

    process_provider_workflow_step(
        sessions,
        workflow_key="eq:history-repair-stale-publication",
        eqazyna=_Eqazyna(),  # type: ignore[arg-type]
    )

    with sessions() as session:
        details = list(
            session.scalars(
                select(ProviderWorkflowUnit).where(
                    ProviderWorkflowUnit.workflow_key == "eq:history-repair-stale-publication",
                    ProviderWorkflowUnit.unit_kind == "eqazyna_lot_detail",
                )
            )
        )
    assert len(details) == 1


def test_provider_workflow_migration_roundtrip(tmp_path, monkeypatch) -> None:
    database = tmp_path / "provider-workflow.sqlite3"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{database.as_posix()}")
    monkeypatch.setattr(settings, "app_env", "test")
    config = Config("alembic.ini")
    command.upgrade(config, "f1a7c3e9b5d2")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    db_inspector = inspect(engine)
    assert "provider_workflow_states" in db_inspector.get_table_names()
    assert "provider_workflow_units" in db_inspector.get_table_names()
    command.downgrade(config, "ec8a2f4d6b91")
    db_inspector.clear_cache()
    assert "provider_workflow_states" not in db_inspector.get_table_names()
    command.upgrade(config, "f1a7c3e9b5d2")
    db_inspector.clear_cache()
    assert "provider_workflow_units" in db_inspector.get_table_names()


def test_active_run_is_reused_and_barrier_is_claimed_exactly_once() -> None:
    sessions = _sessions()
    run_key, created = ensure_provider_sync_run(
        sessions,
        run_kind="sources",
        detail_limit=0,
        config_payload={"decision_input": True},
    )
    same_key, created_again = ensure_provider_sync_run(
        sessions,
        run_kind="sources",
        detail_limit=0,
        config_payload={"decision_input": True},
    )
    assert created is True
    assert created_again is False
    assert same_key == run_key
    create_provider_workflow(
        sessions,
        workflow_key=f"{run_key}:noop",
        provider="eqazyna",
        workflow_kind="barrier_noop",
        units=[ProviderUnitSpec("noop", "provider_barrier_noop", {})],
        run_key=run_key,
    )
    claimed = claim_provider_unit(sessions, workflow_key=f"{run_key}:noop")
    assert claimed is not None
    assert claim_provider_unit(sessions, workflow_key=f"{run_key}:noop") is None
    assert complete_provider_unit(sessions, claimed, result_ref="noop")
    barrier = claim_ready_provider_run(sessions, run_key)
    assert barrier is not None and barrier.run_kind == "sources"
    assert claim_ready_provider_run(sessions, run_key) is None
    assert finish_provider_run(sessions, run_key, success=True)


def test_source_barrier_atomically_finishes_parent_and_legacy_crawl() -> None:
    sessions = _sessions()
    with sessions() as session:
        session.add(
            AuctionSource(
                code="eqazyna_current_lots",
                source_type="auction",
                name="E-Qazyna",
                base_url="https://example.test",
            )
        )
        session.commit()
    parent_key, _ = ensure_provider_sync_run(
        sessions,
        run_kind="full",
        detail_limit=100,
        config_payload={"deactivate_missing": True},
    )
    crawl_id = ensure_provider_crawl_run(
        sessions, run_key=parent_key, source_code="eqazyna_current_lots"
    )
    create_provider_workflow(
        sessions,
        workflow_key=f"{parent_key}:noop",
        provider="eqazyna",
        workflow_kind="barrier_noop",
        units=[ProviderUnitSpec("noop", "provider_barrier_noop", {})],
        run_key=parent_key,
    )
    claimed = claim_provider_unit(sessions, workflow_key=f"{parent_key}:noop")
    assert claimed is not None and complete_provider_unit(sessions, claimed)
    assert claim_ready_provider_run(sessions, parent_key) is not None

    child_key, _ = ensure_provider_sync_run(
        sessions,
        run_kind="sources",
        detail_limit=0,
        config_payload={"decision_input": True},
    )
    assert attach_provider_run_parent(
        sessions,
        child_run_key=child_key,
        parent_run_key=parent_key,
        parent_success=True,
    )
    create_provider_workflow(
        sessions,
        workflow_key=f"{child_key}:noop",
        provider="eqazyna",
        workflow_kind="barrier_noop",
        units=[ProviderUnitSpec("noop", "provider_barrier_noop", {})],
        run_key=child_key,
    )
    claimed = claim_provider_unit(sessions, workflow_key=f"{child_key}:noop")
    assert claimed is not None and complete_provider_unit(sessions, claimed)
    assert claim_ready_provider_run(sessions, child_key) is not None
    assert finish_source_run_and_parents(sessions, child_key, success=True) == 1

    with sessions() as session:
        parent = session.get(ProviderSyncRun, parent_key)
        child = session.get(ProviderSyncRun, child_key)
        crawl = session.get(AuctionCrawlRun, crawl_id)
        assert parent is not None and parent.status == "complete"
        assert child is not None and child.status == "complete"
        assert crawl is not None and crawl.status == "success"
        assert crawl.finished_at is not None


def test_source_success_does_not_hide_parent_terminal_error() -> None:
    sessions = _sessions()
    parent_key, _ = ensure_provider_sync_run(
        sessions, run_kind="full", detail_limit=0, config_payload={}
    )
    with sessions() as session:
        parent = session.get(ProviderSyncRun, parent_key)
        assert parent is not None
        parent.status = "finalizing"
        parent.downstream_dispatched = True
        session.commit()
    child_key, _ = ensure_provider_sync_run(
        sessions, run_kind="sources", detail_limit=0, config_payload={}
    )
    assert attach_provider_run_parent(
        sessions,
        child_run_key=child_key,
        parent_run_key=parent_key,
        parent_success=False,
    )
    with sessions() as session:
        child = session.get(ProviderSyncRun, child_key)
        assert child is not None
        child.status = "finalizing"
        session.commit()
    assert finish_source_run_and_parents(sessions, child_key, success=True) == 1
    with sessions() as session:
        parent = session.get(ProviderSyncRun, parent_key)
        assert parent is not None and parent.status == "error"


def test_unacknowledged_finalize_lease_recovers_after_broker_crash() -> None:
    sessions = _sessions()
    started = datetime(2026, 8, 17, tzinfo=UTC)
    run_key, _ = ensure_provider_sync_run(
        sessions,
        run_kind="history",
        detail_limit=0,
        config_payload={"normalize_history": True},
        now=started,
    )
    create_provider_workflow(
        sessions,
        workflow_key=f"{run_key}:noop",
        provider="eqazyna",
        workflow_kind="barrier_noop",
        units=[ProviderUnitSpec("noop", "provider_barrier_noop", {})],
        run_key=run_key,
        now=started,
    )
    claimed = claim_provider_unit(
        sessions, workflow_key=f"{run_key}:noop", now=started
    )
    assert claimed is not None and complete_provider_unit(sessions, claimed, now=started)
    assert claim_ready_provider_run(sessions, run_key, now=started) is not None
    # No workflow continuation is needed: the durable outbox is independently due.
    dispatch = claim_provider_run_dispatch(sessions, now=started + timedelta(seconds=1))
    assert dispatch is not None
    assert dispatch.run_key == run_key
    assert dispatch.action == "normalize_history"
    assert (
        claim_ready_provider_run(
            sessions, run_key, now=started + timedelta(seconds=299)
        )
        is None
    )
    assert (
        claim_ready_provider_run(
            sessions, run_key, now=started + timedelta(seconds=301)
        )
        is not None
    )


def test_dispatched_sources_start_is_republished_until_child_attaches() -> None:
    sessions = _sessions()
    started = datetime(2026, 8, 17, tzinfo=UTC)
    run_key, _ = ensure_provider_sync_run(
        sessions, run_kind="full", detail_limit=0, config_payload={}, now=started
    )
    create_provider_workflow(
        sessions,
        workflow_key=f"{run_key}:noop",
        provider="eqazyna",
        workflow_kind="barrier_noop",
        units=[ProviderUnitSpec("noop", "provider_barrier_noop", {})],
        run_key=run_key,
        now=started,
    )
    unit = claim_provider_unit(sessions, workflow_key=f"{run_key}:noop", now=started)
    assert unit is not None and complete_provider_unit(sessions, unit, now=started)
    assert claim_ready_provider_run(sessions, run_key, now=started) is not None
    first = claim_provider_run_dispatch(sessions, now=started)
    assert first is not None and first.action == "start_sources"
    assert complete_provider_run_dispatch(sessions, first, now=started)
    assert (
        claim_provider_run_dispatch(
            sessions, now=started + timedelta(seconds=299)
        )
        is None
    )
    repeated = claim_provider_run_dispatch(
        sessions, now=started + timedelta(seconds=301)
    )
    assert repeated is not None and repeated.id == first.id
    with sessions() as session:
        row = session.get(ProviderRunDispatch, first.id)
        assert row is not None and row.attempts == 2


def test_stale_current_parent_fails_closed_without_stopping_shared_sources() -> None:
    sessions = _sessions()
    started = datetime(2026, 8, 17, tzinfo=UTC)
    parent_key, _ = ensure_provider_sync_run(
        sessions, run_kind="current", detail_limit=0, config_payload={}, now=started
    )
    source_key, _ = ensure_provider_sync_run(
        sessions, run_kind="sources", detail_limit=0, config_payload={}, now=started
    )
    with sessions() as session:
        parent = session.get(ProviderSyncRun, parent_key)
        assert parent is not None
        parent.status = "finalizing"
        # Child reconciliation may refresh the parent row while the original
        # downstream-dispatch wait remains stale.
        parent.updated_at = started + timedelta(hours=1)
        session.add(
            ProviderRunDispatch(
                run_key=parent_key,
                action="start_sources",
                status="dispatched",
                payload_json='{"parent_success":true}',
                attempts=1,
                created_at=started,
                updated_at=started,
            )
        )
        session.commit()

    assert expire_stale_provider_parents(
        sessions,
        now=started + timedelta(hours=1, seconds=1),
        timeout_seconds=3600,
    ) == [parent_key]
    with sessions() as session:
        parent = session.get(ProviderSyncRun, parent_key)
        source = session.get(ProviderSyncRun, source_key)
        assert parent is not None and parent.status == "error"
        assert parent.completed_at is not None
        assert parent.completed_at.replace(tzinfo=UTC) == started + timedelta(hours=1, seconds=1)
        assert source is not None and source.status == "active"


def test_recent_or_undispatched_provider_parent_is_not_expired() -> None:
    sessions = _sessions()
    started = datetime(2026, 8, 17, tzinfo=UTC)
    recent_key, _ = ensure_provider_sync_run(
        sessions, run_kind="full", detail_limit=0, config_payload={}, now=started
    )
    with sessions() as session:
        recent = session.get(ProviderSyncRun, recent_key)
        assert recent is not None
        recent.status = "finalizing"
        recent.updated_at = started
        session.commit()

    assert expire_stale_provider_parents(
        sessions,
        now=started + timedelta(hours=2),
        timeout_seconds=3600,
    ) == []


def test_sources_crash_after_multiple_broker_acks_is_recovery_finalizable() -> None:
    sessions = _sessions()
    run_key, _ = ensure_provider_sync_run(
        sessions, run_kind="sources", detail_limit=0, config_payload={}
    )
    create_provider_workflow(
        sessions,
        workflow_key=f"{run_key}:noop",
        provider="eqazyna",
        workflow_kind="barrier_noop",
        units=[ProviderUnitSpec("noop", "provider_barrier_noop", {})],
        run_key=run_key,
    )
    unit = claim_provider_unit(sessions, workflow_key=f"{run_key}:noop")
    assert unit is not None and complete_provider_unit(sessions, unit)
    assert claim_ready_provider_run(sessions, run_key) is not None
    actions: set[str] = set()
    for _ in range(2):
        dispatch = claim_provider_run_dispatch(sessions)
        assert dispatch is not None
        actions.add(dispatch.action)
        assert complete_provider_run_dispatch(sessions, dispatch)
    assert actions == {"normalize_history", "decision_input"}
    # Simulate SIGKILL before finish_source_run_and_parents(). The periodic
    # recovery query finds it without a workflow continuation.
    assert finalizable_provider_runs(sessions) == [(run_key, "sources", True)]


def test_child_reconciliation_query_count_is_constant() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    run_key, _ = ensure_provider_sync_run(
        sessions, run_kind="history", detail_limit=0, config_payload={}
    )
    for index in range(40):
        create_provider_workflow(
            sessions,
            workflow_key=f"{run_key}:child:{index}",
            provider="eqazyna",
            workflow_kind="barrier_noop",
            units=[ProviderUnitSpec("noop", "provider_barrier_noop", {})],
            run_key=run_key,
        )
    claimed = claim_provider_unit(sessions, workflow_key=f"{run_key}:child:0")
    assert claimed is not None
    statements = 0

    def count_statement(*_args: object) -> None:
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        assert complete_provider_unit(sessions, claimed)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    # Includes claim guard, state/run locks, unit update/inserts and two grouped
    # reconciliation queries; it must not grow by 2 SELECTs per child.
    assert statements <= 14


def test_due_provider_workflow_keys_recovers_due_and_expired_states_only() -> None:
    sessions = _sessions()
    now = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
    for key in (
        "pending",
        "deferred",
        "future",
        "complete",
        "expired-processing",
        "state-only",
    ):
        create_provider_workflow(
            sessions,
            workflow_key=key,
            provider="eqazyna",
            workflow_kind="auction_list_and_detail",
            units=[ProviderUnitSpec("page:0001", "eqazyna_list_page", {"page": 1})],
            now=now - timedelta(hours=1),
        )
    with sessions() as session:
        session.get(ProviderWorkflowState, "deferred").status = "deferred"
        session.get(ProviderWorkflowState, "deferred").next_attempt_at = now - timedelta(seconds=1)
        session.get(ProviderWorkflowState, "future").status = "deferred"
        session.get(ProviderWorkflowState, "future").next_attempt_at = now + timedelta(minutes=5)
        session.get(ProviderWorkflowState, "complete").status = "complete"
        processing = session.get(ProviderWorkflowState, "expired-processing")
        processing.status = "processing"
        processing.claim_token = "lost-worker"
        processing.claim_expires_at = now - timedelta(seconds=1)
        state_only = session.get(ProviderWorkflowState, "state-only")
        state_only.status = "pending"
        state_only_unit = session.query(ProviderWorkflowUnit).filter_by(
            workflow_key="state-only"
        ).one()
        state_only_unit.status = "done"
        session.commit()

    assert due_provider_workflow_keys(sessions, now=now, limit=10) == [
        "deferred",
        "expired-processing",
        "pending",
    ]
