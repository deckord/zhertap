from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.services as services
from app.config import settings
from app.db import Base
from app.models import (
    Candidate,
    PaymentStatus,
    ReviewStatus,
    SearchRequest,
    SearchStatus,
    UrbanPlanStatus,
)


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def add_paid_search_candidate(session: Session, language: str = "ru") -> SearchRequest:
    request = SearchRequest(
        language=language,
        region="Акмолинская область",
        region_label="Ақмола облысы (01)" if language == "kz" else "Акмолинская область (01)",
        district="Бурабайский район",
        district_label="Бурабай (01-171)" if language == "kz" else "Бурабайский (01-171)",
        locality="Бурабай",
        locality_label="Бурабай",
        telegram_user_id="1001",
        telegram_chat_id="1001",
        status=SearchStatus.review.value,
    )
    session.add(request)
    session.flush()
    session.add(
        Candidate(
            request_id=request.id,
            rank=1,
            region_chain="Акмолинская область (01) → р-н. Бурабайский (01-171)",
            locality="Бурабай",
            latitude=52.9,
            longitude=70.2,
            nearby_cadastre="011770151680",
            nearby_distance_m=5,
            nearby_land_use="ЛПХ",
            requested_area_ha=0.10,
            road_distance_m=50,
            power_evidence="проверено",
            water_evidence="проверено",
            sewer_evidence="септик",
            cemetery_distance_m=None,
            score=90,
            google_maps_url="https://maps.google.com/",
            review_status=ReviewStatus.approved.value,
            urban_plan_status=UrbanPlanStatus.passed.value,
            google_checked=False,
        )
    )
    session.commit()
    session.refresh(request)
    return request


def configure_payment(monkeypatch) -> None:
    monkeypatch.setattr(settings, "apipay_enabled", False)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_admin_chat_id", "9001")
    monkeypatch.setattr(settings, "platform_access_price_kzt", 5000)
    monkeypatch.setattr(settings, "payment_recipient", "Получатель")
    monkeypatch.setattr(settings, "payment_bank_name", "Test Bank")
    monkeypatch.setattr(settings, "payment_card_number", "4111111111111111")
    monkeypatch.setattr(settings, "payment_url", "https://pay.kaspi.kz/pay/l31wvjsj")


def test_new_lph_report_uses_neutral_area_without_unverified_profile() -> None:
    with build_session() as session:
        request = add_paid_search_candidate(session)
        request.candidates[0].urban_plan_zone = "Ж-1: усадебная застройка (1-3 этажа)"
        request.purpose = "ЛПХ (новый поиск)"
        request.allotment_type = "field"
        request.irrigation_type = "non_irrigated"
        request.area_ha = 0.25
        session.commit()

        report = services.format_telegram_result(request)

        assert "Вид надела: полевой надел" not in report
        assert "Расчетный профиль: неорошаемая земля" not in report
        assert "0.25 га" in report
        assert (
            "Зона генплана показывает разрешенное использование территории, "
            "а не наличие здания на месте."
        ) in report
        assert (
            "Разрешенная зона генплана/ПДП: "
            "Ж-1: усадебная застройка (1-3 этажа)"
        ) in report


def test_manual_payment_gate_hides_cadastre_and_avoids_duplicate_claims(monkeypatch) -> None:
    configure_payment(monkeypatch)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        request = add_paid_search_candidate(session)
        services.request_payment(session, request.id)
        services.request_payment(session, request.id)

        assert request.payment_status == PaymentStatus.awaiting_transfer.value
        assert len(sent) == 1
        assert "011770151680" not in sent[0][1]["text"]
        assert "<b>ОПЛАТА ЧЕРЕЗ TEST BANK</b>" in sent[0][1]["text"]
        assert "<code>4111 1111 1111 1111</code>" in sent[0][1]["text"]
        keyboard = sent[0][1]["reply_markup"]["inline_keyboard"]
        assert keyboard[0][0]["url"] == "https://pay.kaspi.kz/pay/l31wvjsj"
        assert keyboard[0][0]["text"] == "💳 Оплатить через Kaspi"
        assert keyboard[1][0]["copy_text"]["text"] == "4111111111111111"
        assert keyboard[1][0]["text"] == "📋 Скопировать 4111 1111 1111 1111"
        assert keyboard[2][0]["text"] == "✅ Я оплатил"
        assert "Телефон:" not in sent[0][1]["text"]

        services.claim_payment(
            session,
            request.id,
            telegram_user_id="1001",
            telegram_chat_id="1001",
            client_label="Клиент",
        )
        services.claim_payment(
            session,
            request.id,
            telegram_user_id="1001",
            telegram_chat_id="1001",
            client_label="Клиент",
        )

        assert request.payment_status == PaymentStatus.pending_confirmation.value
        assert len(sent) == 2
        assert sent[1][1]["chat_id"] == "9001"


def test_admin_confirmation_marks_paid_and_delivers_once(monkeypatch) -> None:
    configure_payment(monkeypatch)
    monkeypatch.setattr(services, "telegram_request", lambda method, payload: {"ok": True})
    deliveries: list[str] = []

    def fake_deliver(session: Session, request_id: str) -> str:
        deliveries.append(request_id)
        request = session.get(SearchRequest, request_id)
        request.status = SearchStatus.delivered.value
        session.commit()
        return "full report"

    monkeypatch.setattr(services, "deliver_request", fake_deliver)

    with build_session() as session:
        request = add_paid_search_candidate(session)
        request.payment_status = PaymentStatus.pending_confirmation.value
        request.payment_amount_kzt = 5000
        session.commit()

        services.confirm_payment(session, request.id, confirmed_by="9001")
        services.confirm_payment(session, request.id, confirmed_by="9001")

        assert request.payment_status == PaymentStatus.paid.value
        assert request.status == SearchStatus.delivered.value
        assert request.payment_confirmed_by == "9001"
        assert deliveries == [request.id]


def test_operator_can_cancel_waiting_payment_and_unblock_new_request(monkeypatch) -> None:
    configure_payment(monkeypatch)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        blocked = add_paid_search_candidate(session)
        services.request_payment(session, blocked.id)

        services.reject_payment(session, blocked.id)

        assert blocked.payment_status == PaymentStatus.rejected.value
        cancel_message = sent[-1][1]
        assert "отменил ожидание оплаты" in cancel_message["text"]
        assert cancel_message["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ] == f"pay:start:{blocked.id}"

        next_request = add_paid_search_candidate(session)
        services.request_payment(session, next_request.id)

        assert next_request.payment_status == PaymentStatus.awaiting_transfer.value


def test_kazakh_client_receives_kazakh_payment_and_report(monkeypatch) -> None:
    configure_payment(monkeypatch)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        request = add_paid_search_candidate(session, language="kz")
        services.request_payment(session, request.id)
        report = services.format_telegram_result(request)

        payment = sent[0][1]
        assert "<b>Есеп дайын</b>" in payment["text"]
        assert "<b>TEST BANK АРҚЫЛЫ ТӨЛЕУ</b>" in payment["text"]
        assert "<code>4111 1111 1111 1111</code>" in payment["text"]
        assert payment["reply_markup"]["inline_keyboard"][0][0]["text"] == (
            "💳 Kaspi арқылы төлеу"
        )
        assert payment["reply_markup"]["inline_keyboard"][0][0]["url"] == (
            "https://pay.kaspi.kz/pay/l31wvjsj"
        )
        assert payment["reply_markup"]["inline_keyboard"][1][0]["text"] == (
            "📋 4111 1111 1111 1111 көшіру"
        )
        assert payment["reply_markup"]["inline_keyboard"][2][0]["text"] == "✅ Төледім"
        assert "АҚПАРАТТЫҚ ЕСЕП" in report
        assert "ЖМБМК жария кадастрлық картасы" in report
        assert "Ақмола облысы (01)" in report
        assert "Жақын кадастрлық нөмір" in report
