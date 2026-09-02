from types import SimpleNamespace

from app.auction_llm import _fact_is_grounded


def test_money_fact_requires_exact_number_in_evidence() -> None:
    fact = SimpleNamespace(field="annual_payment_kzt", value=196_600)

    assert _fact_is_grounded(fact, "БИН 050540004455, ИИК KZ946017111000000330") is False
    assert _fact_is_grounded(fact, "Ежегодный платеж составляет 196 600 тенге") is True


def test_deadline_fact_requires_exact_number_in_evidence() -> None:
    fact = SimpleNamespace(field="development_deadline", value="30 календарных дней")

    assert _fact_is_grounded(fact, "БСК HSBKKZKX, ТТК 730") is False
    assert _fact_is_grounded(fact, "в течение 30 календарных дней") is True
