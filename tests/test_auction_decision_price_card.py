import json
from types import SimpleNamespace

from app.auction_v2 import decision_price_card


def test_decision_price_card_exposes_blocking_reasons_and_ranges() -> None:
    snapshot = SimpleNamespace(
        fair_value_low_kzt=10_000_000,
        fair_value_high_kzt=14_000_000,
        bid_ceiling_kzt=None,
        data_readiness="partial",
        formula_version="price-ceiling/2026.1",
        stale_reasons_json=json.dumps(["module_incomplete:market_estimate"]),
        payload_json=json.dumps(
            {
                "price_analysis": {
                    "status": "insufficient",
                    "missing_reasons": ["Недостаточно подтверждённых продаж"],
                    "blocker_reasons": ["Не подтверждён юридический подъезд"],
                }
            }
        ),
    )

    card = decision_price_card(snapshot)

    assert card["status"] == "insufficient"
    assert card["fair_value_low_kzt"] == 10_000_000
    assert card["stop_kzt"] is None
    assert "Недостаточно подтверждённых продаж" in card["reasons"]
    assert "Не подтверждён юридический подъезд" in card["reasons"]
    assert "Рыночная оценка неполна" in card["reasons"]


def test_decision_price_card_without_snapshot_is_unknown() -> None:
    card = decision_price_card(None)

    assert card["status"] == "unknown"
    assert card["stop_kzt"] is None
    assert card["reasons"] == ["Итоговый расчёт ещё не выполнен"]
