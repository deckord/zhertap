from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.services as services
from app.config import settings
from app.db import Base
from app.models import (
    PaymentStatus,
    ReviewStatus,
    SearchRequest,
    SearchStatus,
    UrbanPlanCoverage,
)
from app.schemas import SearchCreate
from app.search_types import CandidateResult


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


class FakeSearchEngine:
    def search(self, query: SearchCreate) -> list[CandidateResult]:
        return [
            CandidateResult(
                region_chain="Акмолинская область (01) → р-н. Бурабайский (01-171)",
                locality=query.locality or "Бурабай",
                latitude=52.9,
                longitude=70.2,
                nearby_cadastre="011770151680",
                nearby_distance_m=8,
                cemetery_distance_m=None,
                road_distance_m=40,
                score=92,
                risk_notes="Предварительный кандидат",
                power_evidence="ЛЭП рядом",
                water_evidence="Застройка рядом",
                sewer_evidence="Индивидуальный септик",
            ),
            CandidateResult(
                region_chain="Акмолинская область (01) → р-н. Бурабайский (01-171)",
                locality=query.locality or "Бурабай",
                latitude=52.901,
                longitude=70.201,
                nearby_cadastre="011770151681",
                nearby_distance_m=12,
                cemetery_distance_m=None,
                road_distance_m=50,
                score=88,
                risk_notes="Предварительный кандидат",
                power_evidence="ЛЭП рядом",
                water_evidence="Застройка рядом",
                sewer_evidence="Индивидуальный септик",
            ),
        ]


class EmptySearchEngine:
    def search(self, query: SearchCreate) -> list[CandidateResult]:
        return []


class CountingSearchEngine(FakeSearchEngine):
    def __init__(self) -> None:
        self.calls = 0

    def search(self, query: SearchCreate) -> list[CandidateResult]:
        self.calls += 1
        return super().search(query)


def test_search_verifies_egkn_geometry_and_offers_payment(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "off")
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "free_preview_enabled", False)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "payment_recipient", "Получатель")
    monkeypatch.setattr(settings, "payment_bank_name", "Test Bank")
    monkeypatch.setattr(settings, "payment_card_number", "4111111111111111")
    sent: list[dict] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append(payload) or {"ok": True},
    )

    with build_session() as session:
        request = SearchRequest(
            region="Акмолинская область",
            district="Бурабайский район",
            locality="Бурабай",
            telegram_user_id="1001",
            telegram_chat_id="1001",
        )
        session.add(request)
        session.commit()

        completed = services.process_search(
            session,
            request.id,
            search_engine=FakeSearchEngine(),
        )

        assert completed.status == SearchStatus.ready.value
        assert completed.progress == 100
        assert completed.payment_status == PaymentStatus.not_requested.value
        assert completed.candidates[0].review_status == ReviewStatus.approved.value
        assert completed.candidates[1].review_status == ReviewStatus.approved.value
        assert all(not item.google_checked for item in completed.candidates)
        assert all("квадрат" in item.review_notes.lower() for item in completed.candidates)
        assert len(sent) == 1
        assert "011770151680" not in sent[0]["text"]
        offer_keyboard = sent[0]["reply_markup"]["inline_keyboard"]
        assert offer_keyboard[0][0]["callback_data"] == f"pay:start:{completed.id}"

        services.start_payment(
            session,
            completed.id,
            telegram_user_id="1001",
            telegram_chat_id="1001",
        )
        assert completed.payment_status == PaymentStatus.awaiting_transfer.value
        assert len(sent) == 2
        keyboard = sent[1]["reply_markup"]["inline_keyboard"]
        assert keyboard[0][0]["url"] == "https://pay.kaspi.kz/pay/l31wvjsj"
        assert keyboard[1][0]["copy_text"]["text"] == "4111111111111111"
        assert keyboard[2][0]["text"] == "✅ Я оплатил"
        assert "Телефон:" not in sent[1]["text"]


def test_process_search_does_not_reprocess_non_queued_request(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "off")
    engine = CountingSearchEngine()

    with build_session() as session:
        request = SearchRequest(
            region="РђРєРјРѕР»РёРЅСЃРєР°СЏ РѕР±Р»СЊ",
            district="Р‘СѓСЂР°Р±Р°Р№СЃРєРёР№ СЂР°Р№РѕРЅ",
            locality="Р‘СѓСЂР°Р±Р°Р№",
            status=SearchStatus.processing.value,
        )
        session.add(request)
        session.commit()

        result = services.process_search(session, request.id, search_engine=engine)

        assert result.status == SearchStatus.processing.value
        assert engine.calls == 0


def test_empty_search_always_notifies_client_and_does_not_request_payment(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append(payload) or {"ok": True},
    )

    with build_session() as session:
        request = SearchRequest(
            region="Туркестанская область",
            district="г. Сарыагаш",
            locality="г Сарыагаш",
            telegram_user_id="1001",
            telegram_chat_id="1001",
        )
        session.add(request)
        session.commit()

        completed = services.process_search(
            session,
            request.id,
            search_engine=EmptySearchEngine(),
        )

        assert completed.status == SearchStatus.ready.value
        assert completed.progress == 100
        assert completed.payment_status == PaymentStatus.not_requested.value
        assert len(sent) == 1
        assert "Поиск завершен" in sent[0]["text"]
        assert "не найдено места для участка 10 соток" in sent[0]["text"]
        assert "Оплата не требуется" in sent[0]["text"]


def test_strict_search_auto_delivers_preliminary_candidates_without_urban_plan(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    monkeypatch.setattr(settings, "urban_plan_auto_waive_unavailable", False)
    sent: list[dict] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append(payload) or {"ok": True},
    )

    with build_session() as session:
        request = SearchRequest(
            region="Акмолинская область",
            district="Бурабайский район",
            locality="Бурабай",
            telegram_user_id="1001",
            telegram_chat_id="1001",
        )
        session.add(request)
        session.commit()

        completed = services.process_search(
            session,
            request.id,
            search_engine=FakeSearchEngine(),
        )

        assert completed.urban_plan_status == "waived"
        assert completed.urban_plan_waiver_kind == "auto_no_approved_layer"
        assert services.approved_candidates(completed)
        assert all(item.review_status == "approved" for item in completed.candidates)
        combined_text = "\n".join(payload.get("text", "") for payload in sent)
        assert f"urban:waive:{completed.id}" not in combined_text


def test_strict_search_auto_waives_when_no_approved_plan_layer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    monkeypatch.setattr(settings, "urban_plan_auto_waive_unavailable", True)
    monkeypatch.setattr(settings, "free_preview_enabled", False)
    monkeypatch.setattr(settings, "paid_search_enabled", False)
    sent: list[dict] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append(payload) or {"ok": True},
    )

    with build_session() as session:
        request = SearchRequest(
            region="Акмолинская область",
            district="Бурабайский район",
            locality="Бурабай",
            telegram_user_id="1001",
            telegram_chat_id="1001",
        )
        session.add(request)
        session.commit()

        completed = services.process_search(
            session,
            request.id,
            search_engine=FakeSearchEngine(),
        )

        coverage = session.query(UrbanPlanCoverage).one()
        assert coverage.coverage_status == "unavailable"
        assert completed.urban_plan_status == "waived"
        assert completed.urban_plan_waiver_kind == "auto_no_approved_layer"
        assert completed.urban_plan_override_user_id == "system:auto-no-layer"
        assert services.approved_candidates(completed)
        assert all(item.review_status == "approved" for item in completed.candidates)
        combined_text = "\n".join(payload.get("text", "") for payload in sent)
        assert "Продолжить без проверки генплана" not in combined_text
        report_text = services.format_telegram_result(
            completed,
            services.approved_candidates(completed),
            free_preview=True,
        )
        assert "нет пригодного цифрового слоя" in report_text


def test_user_can_explicitly_accept_preliminary_result_without_plan(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    monkeypatch.setattr(settings, "urban_plan_auto_waive_unavailable", False)
    monkeypatch.setattr(settings, "free_preview_enabled", True)
    monkeypatch.setattr(settings, "free_preview_plot_limit", 3)
    sent: list[dict] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append(payload) or {"ok": True},
    )

    with build_session() as session:
        request = SearchRequest(
            region="Акмолинская область",
            district="Бурабайский район",
            locality="Бурабай",
            telegram_user_id="1001",
            telegram_chat_id="1001",
        )
        session.add(request)
        session.commit()
        completed = services.process_search(
            session,
            request.id,
            search_engine=FakeSearchEngine(),
        )

        waived, accepted = services.accept_urban_plan_override(
            session,
            completed.id,
            telegram_user_id="1001",
            telegram_chat_id="1001",
        )

        assert accepted is False
        assert waived.urban_plan_status == "waived"
        assert waived.urban_plan_override_accepted_at is not None
        assert waived.urban_plan_override_user_id == "system:auto-no-layer"
        assert all(item.urban_plan_status == "waived" for item in waived.candidates)
        assert waived.status == SearchStatus.delivered.value
        report_text = "\n".join(payload["text"] for payload in sent if "text" in payload)
        assert "БЕЗ ПРОВЕРКИ ГЕНПЛАНА/ПДП" in report_text
        assert "011770151680" not in report_text
        assert any(
            button.get("callback_data") == f"pay:start:{waived.id}"
            for payload in sent
            for row in payload.get("reply_markup", {}).get("inline_keyboard", [])
            for button in row
        )


def test_user_cannot_waive_actual_urban_plan_block(monkeypatch) -> None:
    with build_session() as session:
        request = SearchRequest(
            region="Акмолинская область",
            district="Бурабайский район",
            locality="Бурабай",
            telegram_user_id="1001",
            telegram_chat_id="1001",
            urban_plan_status="blocked",
        )
        session.add(request)
        session.commit()

        try:
            services.accept_urban_plan_override(
                session,
                request.id,
                telegram_user_id="1001",
                telegram_chat_id="1001",
            )
        except ValueError as exc:
            assert "только когда официальный слой отсутствует" in str(exc)
        else:
            raise AssertionError("Urban-plan block must not be waivable")


def test_terminal_failure_notification_contains_status_command(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append(payload) or {"ok": True},
    )

    request = SearchRequest(
        id="11111111-1111-1111-1111-111111111111",
        region="Акмолинская область",
        district="Бурабайский район",
        locality="Бурабай",
        telegram_chat_id="1001",
    )
    services.notify_terminal_search_failure(request)

    assert len(sent) == 1
    assert "оплата не запрашивается" in sent[0]["text"].lower()
    assert f"/status {request.id}" in sent[0]["text"]
    button = sent[0]["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Повторить поиск"
    assert button["callback_data"] == f"search:retry:{request.id}"
    callbacks = [row[0]["callback_data"] for row in sent[0]["reply_markup"]["inline_keyboard"]]
    assert f"search:localities:{request.id}" in callbacks
    assert f"search:districts:{request.id}" in callbacks
    assert f"search:regions:{request.id}" in callbacks


def test_retry_failed_search_copies_parameters_and_is_idempotent() -> None:
    with build_session() as session:
        source = SearchRequest(
            region="Акмолинская область",
            region_label="Акмолинская область (01)",
            district="Бурабайский район",
            district_label="р-н. Бурабайский (01-171)",
            locality="Бурабай",
            locality_label="Бурабай",
            language="kz",
            purpose="ЛПХ (новый поиск)",
            allotment_type="household",
            irrigation_type="irrigated",
            area_ha=0.15,
            result_limit=10,
            cemetery_buffer_m=0,
            telegram_user_id="1001",
            telegram_chat_id="1001",
            status=SearchStatus.failed.value,
            terms_version="test-v1",
            terms_text_snapshot="accepted terms text",
        )
        session.add(source)
        session.commit()

        retry, position, created = services.retry_failed_search(
            session,
            source.id,
            telegram_user_id="1001",
            telegram_chat_id="1001",
        )
        same_retry, second_position, second_created = services.retry_failed_search(
            session,
            source.id,
            telegram_user_id="1001",
            telegram_chat_id="1001",
        )

        assert created is True
        assert position == 1
        assert retry.id == same_retry.id
        assert second_created is False
        assert second_position == 0
        assert retry.retry_of_request_id == source.id
        assert retry.language == "kz"
        assert retry.region_label == source.region_label
        assert retry.district_label == source.district_label
        assert retry.locality_label == source.locality_label
        assert retry.purpose == "ЛПХ (новый поиск)"
        assert retry.allotment_type == "household"
        assert retry.irrigation_type == "irrigated"
        assert retry.area_ha == 0.15
        assert retry.terms_version == "test-v1"
        assert retry.terms_text_snapshot == "accepted terms text"


def test_retry_failed_admin_search_without_telegram() -> None:
    with build_session() as session:
        source = SearchRequest(
            region="Алматинская область",
            district="Талгарский район",
            locality="г. Талгар",
            purpose="ЛПХ (новый поиск)",
            allotment_type="household",
            irrigation_type="irrigated",
            area_ha=0.15,
            result_limit=10,
            cemetery_buffer_m=0,
            status=SearchStatus.failed.value,
            error_message="Public OSM timeout",
        )
        session.add(source)
        session.commit()

        retry, position, created = services.retry_failed_search(
            session,
            source.id,
            telegram_user_id=None,
            telegram_chat_id=None,
        )

        assert created is True
        assert position == 1
        assert retry.retry_of_request_id == source.id
        assert retry.telegram_user_id is None
        assert retry.telegram_chat_id is None
        assert retry.status == SearchStatus.queued.value
