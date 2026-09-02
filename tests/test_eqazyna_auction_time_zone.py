from datetime import UTC, datetime

from app.providers.eqazyna import _parse_datetime


def test_eqazyna_local_auction_clock_uses_current_kazakhstan_offset() -> None:
    assert _parse_datetime("01.09.2026 10:00") == datetime(
        2026, 9, 1, 5, 0, tzinfo=UTC
    )


def test_eqazyna_local_auction_clock_uses_historical_kazakhstan_offset() -> None:
    assert _parse_datetime("01.09.2023 10:00") == datetime(
        2023, 9, 1, 4, 0, tzinfo=UTC
    )
