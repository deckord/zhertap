import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.services as services
from app.config import settings
from app.db import Base
from app.models import (
    Candidate,
    FreePreviewStatus,
    PaymentStatus,
    ReviewStatus,
    SearchRequest,
    SearchStatus,
    UrbanPlanStatus,
    utcnow,
)


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def add_request(session: Session, *, user_id: str, candidate_count: int) -> SearchRequest:
    request = SearchRequest(
        region="Акмолинская область",
        district="Бурабайский район",
        locality="Бурабай",
        telegram_user_id=user_id,
        telegram_chat_id=user_id,
        status=SearchStatus.ready.value,
    )
    session.add(request)
    session.flush()
    for rank in range(1, candidate_count + 1):
        session.add(
            Candidate(
                request_id=request.id,
                rank=rank,
                region_chain="Акмолинская область → Бурабайский район",
                locality="Бурабай",
                latitude=52.9 + rank / 1000,
                longitude=70.2 + rank / 1000,
                nearby_cadastre=f"0117701516{rank:02d}",
                nearby_distance_m=rank,
                requested_area_ha=0.10,
                power_evidence="нет данных",
                water_evidence="нет данных",
                sewer_evidence="септик проверяется на месте",
                score=90 - rank,
                risk_notes="предварительный расчет",
                google_maps_url="https://maps.google.com/",
                review_status=ReviewStatus.approved.value,
                urban_plan_status=UrbanPlanStatus.passed.value,
            )
        )
    session.commit()
    return services.get_request_with_candidates(session, request.id)


def configure_free_preview(monkeypatch) -> list[tuple[str, dict]]:
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(settings, "free_preview_enabled", True)
    monkeypatch.setattr(settings, "free_preview_plot_limit", 3)
    monkeypatch.setattr(settings, "paid_search_enabled", False)
    monkeypatch.setattr(settings, "telegram_admin_chat_id", "9001")
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )
    return sent


def test_preview_packets_are_not_cumulative_across_requests(monkeypatch) -> None:
    sent = configure_free_preview(monkeypatch)
    with build_session() as session:
        first = add_request(session, user_id="1001", candidate_count=1)
        services.route_ready_report(session, first.id)
        assert first.free_preview_status == FreePreviewStatus.delivered.value
        assert services.free_preview_usage(session, "1001") == 1

        second = add_request(session, user_id="1001", candidate_count=5)
        services.route_ready_report(session, second.id)
        assert second.free_preview_count == 5
        assert second.free_preview_status == FreePreviewStatus.delivered.value
        assert services.free_preview_usage(session, "1001") == 6
        third = add_request(session, user_id="1001", candidate_count=4)
        assert services.reserve_free_preview(session, third.id) == 4
        services.route_ready_report(session, third.id)

    client_reports = [
        payload["text"]
        for _method, payload in sent
        if payload.get("chat_id") == "1001" and "ПРЕДВАРИТЕЛЬНЫЕ ВАРИАНТЫ" in payload["text"]
    ]
    assert len(client_reports) == 3
    assert "Пакет 1 · найдено 1" in client_reports[0]
    assert "Пакет 1 · найдено 5" in client_reports[1]
    assert "Пакет 1 · найдено 4" in client_reports[2]
    assert all("Открыть карту" not in report for report in client_reports)
    assert all("Координаты и карта доступны" in report for report in client_reports)
    assert all("0117701516" not in report for report in client_reports)
    assert not any("Лимит из 3 бесплатных участков" in payload["text"] for _, payload in sent)
    assert not any(
        button.get("callback_data", "").startswith("free:confirm:")
        for _, payload in sent
        for row in payload.get("reply_markup", {}).get("inline_keyboard", [])
        for button in row
    )


def test_web_request_without_telegram_is_marked_ready_without_preview(monkeypatch) -> None:
    configure_free_preview(monkeypatch)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    with build_session() as session:
        request = add_request(session, user_id="web-shadow", candidate_count=2)
        request.telegram_user_id = None
        request.telegram_chat_id = None
        request.web_account_id = "web-account-1"
        session.commit()

        services.route_ready_report(session, request.id)
        session.refresh(request)

        assert request.status == SearchStatus.ready.value
        assert request.free_preview_status == FreePreviewStatus.not_requested.value
        assert request.search_completed_notified_at is not None


def test_rejected_free_preview_does_not_consume_limit(monkeypatch) -> None:
    configure_free_preview(monkeypatch)
    with build_session() as session:
        request = add_request(session, user_id="2002", candidate_count=3)
        services.reserve_free_preview(session, request.id)
        assert services.free_preview_usage(session, "2002") == 3

        services.reject_free_preview(session, request.id)

        assert request.free_preview_status == FreePreviewStatus.rejected.value
        assert services.free_preview_usage(session, "2002") == 0


def test_ready_request_with_only_rejected_candidates_notifies_without_limit(monkeypatch) -> None:
    sent = configure_free_preview(monkeypatch)
    with build_session() as session:
        request = add_request(session, user_id="2502", candidate_count=2)
        for candidate in request.candidates:
            candidate.review_status = ReviewStatus.rejected.value
        session.commit()

        services.route_ready_report(session, request.id)

        assert request.free_preview_status == FreePreviewStatus.not_requested.value
        assert services.free_preview_usage(session, "2502") == 0
        assert any(payload.get("chat_id") == "2502" for _, payload in sent)
        assert not any(
            button.get("callback_data", "").startswith("pay:start:")
            for _, payload in sent
            for row in payload.get("reply_markup", {}).get("inline_keyboard", [])
            for button in row
        )


def test_ready_no_candidate_notification_is_not_marked_until_sent(monkeypatch) -> None:
    configure_free_preview(monkeypatch)
    attempts = {"count": 0}

    def flaky_telegram(_method: str, _payload: dict) -> dict:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("telegram timeout")
        return {"ok": True}

    monkeypatch.setattr(services, "telegram_request", flaky_telegram)
    with build_session() as session:
        request = add_request(session, user_id="2602", candidate_count=0)
        request.status = SearchStatus.ready.value
        request.progress = 100
        request.search_outcome = "no_candidates"
        request.error_message = "Ничего не найдено"
        session.commit()

        with pytest.raises(RuntimeError, match="telegram timeout"):
            services.route_ready_report(session, request.id)

        assert request.search_completed_notified_at is None

        recovered = services.ensure_ready_delivery(session, request.id)

        assert recovered is True
        assert request.search_completed_notified_at is not None
        assert attempts["count"] == 2


def test_ready_request_with_completed_notice_but_no_report_is_recovered(monkeypatch) -> None:
    sent = configure_free_preview(monkeypatch)
    with build_session() as session:
        request = add_request(session, user_id="2702", candidate_count=2)
        request.search_completed_notified_at = utcnow()
        session.commit()

        recovered = services.ensure_ready_delivery(session, request.id)

        session.refresh(request)
        assert recovered is True
        assert request.free_preview_status == FreePreviewStatus.delivered.value
        assert request.status == SearchStatus.delivered.value
        assert all(item.delivered_at is not None for item in request.candidates)
        assert any(payload.get("chat_id") == "2702" for _, payload in sent)


def test_ready_request_with_delivered_preview_status_but_no_candidates_is_recovered(
    monkeypatch,
) -> None:
    sent = configure_free_preview(monkeypatch)
    with build_session() as session:
        request = add_request(session, user_id="2802", candidate_count=2)
        request.search_completed_notified_at = utcnow()
        request.free_preview_status = FreePreviewStatus.delivered.value
        request.free_preview_count = 2
        session.commit()

        recovered = services.ensure_ready_delivery(session, request.id)

        session.refresh(request)
        assert recovered is True
        assert request.free_preview_status == FreePreviewStatus.delivered.value
        assert request.status == SearchStatus.delivered.value
        assert all(item.delivered_at is not None for item in request.candidates)
        assert any(payload.get("chat_id") == "2802" for _, payload in sent)


def test_paid_request_is_blocked_when_paid_mode_is_disabled(monkeypatch) -> None:
    configure_free_preview(monkeypatch)
    with build_session() as session:
        request = add_request(session, user_id="3003", candidate_count=1)
        try:
            services.request_payment(session, request.id)
        except ValueError as exc:
            assert "временно отключен" in str(exc)
        else:
            raise AssertionError("Paid request must be disabled")


def test_fourth_search_offers_paid_report_and_starts_previous_flow(monkeypatch) -> None:
    sent = configure_free_preview(monkeypatch)
    monkeypatch.setattr(settings, "payment_card_number", "4400000000000000")
    with build_session() as session:
        free_request = add_request(session, user_id="4004", candidate_count=3)
        services.route_ready_report(session, free_request.id)
        assert services.free_preview_usage(session, "4004") == 3

        monkeypatch.setattr(settings, "paid_search_enabled", True)
        sent.clear()
        paid_request = add_request(session, user_id="4004", candidate_count=10)
        services.route_ready_report(session, paid_request.id)

        assert paid_request.payment_status == PaymentStatus.not_requested.value
        offer = sent[-1][1]
        button = offer["reply_markup"]["inline_keyboard"][0][0]
        assert button["text"] == "Разблокировать полный отчет — 4 990 ₸"
        assert button["callback_data"] == f"pay:start:{paid_request.id}"

        services.start_payment(
            session,
            paid_request.id,
            telegram_user_id="4004",
            telegram_chat_id="4004",
        )
        assert paid_request.payment_status == PaymentStatus.awaiting_transfer.value
        payment = sent[-1][1]
        assert payment["reply_markup"]["inline_keyboard"][0][0]["url"] == (
            "https://pay.kaspi.kz/pay/l31wvjsj"
        )
        assert payment["reply_markup"]["inline_keyboard"][2][0]["callback_data"] == (
            f"pay:claim:{paid_request.id}"
        )


def test_paid_access_is_personal_and_future_searches_are_delivered(monkeypatch) -> None:
    sent = configure_free_preview(monkeypatch)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    with build_session() as session:
        activation = add_request(session, user_id="5005", candidate_count=1)
        activation.payment_status = PaymentStatus.paid.value
        activation.status = SearchStatus.delivered.value
        session.commit()

        future = add_request(session, user_id="5005", candidate_count=2)
        services.route_ready_report(session, future.id)

        assert services.has_paid_access(session, "5005") is True
        assert future.status == SearchStatus.delivered.value
        assert future.payment_status == PaymentStatus.not_requested.value
        assert all(item.delivered_at is not None for item in future.candidates)
        assert any(
            payload.get("chat_id") == "5005" and "ИНФОРМАЦИОННЫЙ ОТЧЕТ" in payload["text"]
            for _, payload in sent
        )
        callbacks = {
            button.get("callback_data")
            for _, payload in sent
            if payload.get("chat_id") == "5005"
            for row in payload.get("reply_markup", {}).get("inline_keyboard", [])
            for button in row
        }
        assert f"search:localities:{future.id}" in callbacks
        assert f"search:districts:{future.id}" in callbacks
        assert f"search:regions:{future.id}" in callbacks

        other_user = add_request(session, user_id="6006", candidate_count=2)
        services.route_ready_report(session, other_user.id)

        assert services.has_paid_access(session, "6006") is False
        assert other_user.free_preview_status == FreePreviewStatus.delivered.value
        assert other_user.status == SearchStatus.delivered.value


def test_next_batch_is_idempotent_and_tracks_previous_delivery(monkeypatch) -> None:
    configure_free_preview(monkeypatch)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    with build_session() as session:
        source = add_request(session, user_id="7007", candidate_count=10)
        source.purpose = "ЛПХ (новый поиск)"
        source.allotment_type = "field"
        source.irrigation_type = "non_irrigated"
        source.area_ha = 0.25
        source.payment_status = PaymentStatus.paid.value
        session.commit()
        services.deliver_request(session, source.id)

        next_request, position, created = services.create_next_batch(
            session,
            source.id,
            telegram_user_id="7007",
            telegram_chat_id="7007",
        )
        duplicate, duplicate_position, duplicate_created = services.create_next_batch(
            session,
            source.id,
            telegram_user_id="7007",
            telegram_chat_id="7007",
        )

        assert created is True
        assert position >= 1
        assert duplicate_created is False
        assert duplicate_position == 0
        assert duplicate.id == next_request.id
        assert next_request.batch_number == 2
        assert next_request.continuation_of_request_id == source.id
        assert next_request.purpose == "ЛПХ (новый поиск)"
        assert next_request.allotment_type == "field"
        assert next_request.irrigation_type == "non_irrigated"
        assert next_request.area_ha == 0.25
        assert len(services.delivered_coordinates(session, next_request)) == 10


def test_web_next_batch_marks_visible_candidates_as_delivered(monkeypatch) -> None:
    configure_free_preview(monkeypatch)
    with build_session() as session:
        source = add_request(session, user_id="web-user", candidate_count=10)
        source.web_account_id = "account-1"
        source.telegram_user_id = None
        source.telegram_chat_id = None
        session.commit()

        next_request, position, created = services.create_next_batch(
            session,
            source.id,
            telegram_user_id=None,
            telegram_chat_id=None,
            web_account_id="account-1",
            require_paid_access=False,
        )

        assert created is True
        assert position >= 1
        assert next_request.web_account_id == "account-1"
        assert next_request.telegram_user_id is None
        assert next_request.batch_number == 2
        assert all(item.delivered_at is not None for item in source.candidates)
        assert len(services.delivered_coordinates(session, next_request)) == 10


def test_user_cannot_open_two_payments_and_old_offer_becomes_free(monkeypatch) -> None:
    configure_free_preview(monkeypatch)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "payment_card_number", "4400000000000000")
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    with build_session() as session:
        first = add_request(session, user_id="8008", candidate_count=1)
        second = add_request(session, user_id="8008", candidate_count=1)
        services.start_payment(
            session,
            first.id,
            telegram_user_id="8008",
            telegram_chat_id="8008",
        )

        with pytest.raises(ValueError, match="ожидающая оплата"):
            services.start_payment(
                session,
                second.id,
                telegram_user_id="8008",
                telegram_chat_id="8008",
            )

        services.claim_payment(
            session,
            first.id,
            telegram_user_id="8008",
            telegram_chat_id="8008",
            client_label="Test User",
        )
        services.confirm_payment(session, first.id, confirmed_by="9001")
        services.start_payment(
            session,
            second.id,
            telegram_user_id="8008",
            telegram_chat_id="8008",
        )

        assert first.payment_status == PaymentStatus.paid.value
        assert second.payment_status == PaymentStatus.not_requested.value
        assert second.status == SearchStatus.delivered.value
