from datetime import UTC, datetime
from types import SimpleNamespace

from app.auction_v2 import auction_v2_calendar_payload


class _ScalarResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self, _query):
        return _ScalarResult(self.rows)


def test_calendar_payload_exposes_local_event_time_for_template() -> None:
    event_at = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    lot = SimpleNamespace(
        id="lot-1",
        auction_starts_at=event_at,
        auction_number="42",
        source_lot_id="source-42",
        region="Астана",
        district="Алматы",
        guarantee_kzt=250_000,
    )
    pipeline = SimpleNamespace(
        account_id="account-1",
        lot=lot,
        stage="watching",
        max_bid_kzt=1_200_000,
        reminder_at=event_at,
        inspection_json=(
            '{"manual_checks":{"access":{"status":"done"},'
            '"electricity":{"status":"unknown"}}}'
        ),
        updated_at=event_at,
    )

    payload = auction_v2_calendar_payload(
        _Session([pipeline]),
        account_id="account-1",
        now=event_at,
    )

    assert payload["events"]
    assert payload["events"][0]["at"] == event_at
    assert payload["events"][0]["local_at"].hour == 17
    assert payload["events"][0]["guarantee_kzt"] == 250_000
    assert payload["events"][0]["personal_stop_kzt"] == 1_200_000
    assert payload["events"][0]["readiness_label"] == "1 из 2 проверок закрыто"
