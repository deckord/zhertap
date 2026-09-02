from types import SimpleNamespace

from app.auction_v2 import market_comparable_cards


def _comparable(**kwargs: object) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "title": "Аналог",
        "source_name": "Krisha",
        "source_url": "https://example.test/comparable",
        "area_ha": 1.0,
        "price_kzt": 1_000_000,
        "price_per_sotka": 10_000,
        "listing_status": "active",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_market_cards_mark_listing_and_price_outlier_with_reason() -> None:
    cards = market_comparable_cards(
        [
            _comparable(id=1, title="Нормальный"),
            _comparable(
                id=2,
                title="Продажа",
                source_name="E-Qazyna",
                listing_status="verified_sale",
                price_kzt=1_100_000,
                price_per_sotka=11_000,
            ),
            _comparable(
                id=3,
                title="Выброс",
                price_kzt=10_000_000,
                price_per_sotka=100_000,
            ),
            _comparable(
                id=4,
                title="Опора",
                price_kzt=900_000,
                price_per_sotka=9_000,
            ),
        ]
    )

    assert cards[0]["kind_label"] == "Объявление"
    assert cards[1]["kind_label"] == "Подтверждённая продажа"
    assert cards[2]["quality"] == "outlier"
    assert cards[2]["reason"] == "Исключён как ценовой выброс"
    assert cards[0]["reason"] == "Учитывается как ориентир; не подтверждает факт сделки"
