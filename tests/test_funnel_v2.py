from sqlalchemy import select

import app.services as services
from app.apipay import ApiPayQrInvoice
from app.bot import (
    lph_mode_keyboard,
    lph_size_keyboard,
    purpose_keyboard,
    terms_keyboard,
    welcome_text,
)
from app.config import settings
from app.models import FunnelEvent, PaymentStatus, SearchRequest, SearchStatus
from tests.test_automation import EmptySearchEngine, FakeSearchEngine
from tests.test_free_preview import add_request, build_session
from tests.test_payments import add_paid_search_candidate


def enable_v2(monkeypatch) -> None:
    monkeypatch.setattr(settings, "client_funnel_version", "v2")
    monkeypatch.setattr(settings, "enable_standard_lph_10", False)
    monkeypatch.setattr(settings, "service_provider_status", "")
    monkeypatch.setattr(settings, "service_provider_name", "")


def test_v2_welcome_and_lph_choice_use_client_language(monkeypatch) -> None:
    enable_v2(monkeypatch)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(settings, "free_preview_plot_limit", 3)

    welcome_ru = welcome_text("ru")
    welcome_kz = welcome_text("kz")
    purpose = purpose_keyboard("ru")
    lph_mode = lph_mode_keyboard("ru")
    lph_size = lph_size_keyboard("ru")

    assert "Сервис предварительной проверки земли" in welcome_ru
    assert "перспективные места под ЛПХ" in welcome_ru
    assert "зарегистрированные участки в ЕГКН" in welcome_ru
    assert "OSM-карте" in welcome_ru
    assert "генплан/ПДП" in welcome_ru
    assert "Полный доступ" in welcome_ru
    assert "В тестовый период все функции открыты" in welcome_ru
    assert "₸ / месяц" in welcome_ru
    assert "4 990" in welcome_ru
    assert "не гарантирует выдачу земли" in welcome_ru
    assert "Чтобы продолжить, примите условия" in welcome_ru
    assert "Жерді алдын ала тексеруге арналған сервис" in welcome_kz
    assert terms_keyboard("ru").inline_keyboard[0][0].text == (
        "✅ Принять условия и выбрать анализ"
    )
    assert [row[0].callback_data for row in purpose.inline_keyboard[:2]] == [
        "purpose:lph",
        "purpose:gardening",
    ]
    assert purpose.inline_keyboard[2][0].text == "🌐 Открыть веб-сайт"
    assert purpose.inline_keyboard[2][0].url == "https://zhertap.kz"
    assert purpose.inline_keyboard[2][0].callback_data is None
    assert purpose.inline_keyboard[3][0].callback_data == "catalog:back:terms"
    assert [row[0].callback_data for row in lph_mode.inline_keyboard] == [
        "lph-mode:extended",
        "catalog:back:purpose",
    ]
    assert [row[0].callback_data for row in lph_size.inline_keyboard] == [
        "lph-size:15",
        "lph-size:25",
        "catalog:back:purpose",
    ]

    monkeypatch.setattr(settings, "enable_standard_lph_10", True)
    restored_lph_mode = lph_mode_keyboard("ru")
    assert [row[0].callback_data for row in restored_lph_mode.inline_keyboard[:2]] == [
        "lph-mode:standard",
        "lph-mode:extended",
    ]


def test_v2_search_progress_edits_one_message_with_real_stages(monkeypatch) -> None:
    enable_v2(monkeypatch)
    monkeypatch.setattr(settings, "urban_plan_check_mode", "off")
    monkeypatch.setattr(settings, "free_preview_enabled", False)
    monkeypatch.setattr(settings, "paid_search_enabled", False)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        request = SearchRequest(
            region="Акмолинская область",
            district="Бурабайский район",
            locality="Бурабай",
            telegram_user_id="1001",
            telegram_chat_id="1001",
            progress_message_id=77,
        )
        session.add(request)
        session.commit()

        completed = services.process_search(
            session,
            request.id,
            search_engine=FakeSearchEngine(),
        )

        assert completed.progress == 100
        edits = [payload for method, payload in sent if method == "editMessageText"]
        assert edits
        assert {payload["message_id"] for payload in edits} == {77}
        combined = "\n".join(payload["text"] for payload in edits)
        assert "Шаг 2 из 5" in combined
        assert "градостроительные ограничения" in combined
        assert "готовлю отчет" in combined
        assert "Анализ завершен" in edits[-1]["text"]


def test_v2_free_results_then_paywall_are_separate_messages(monkeypatch) -> None:
    enable_v2(monkeypatch)
    monkeypatch.setattr(settings, "free_preview_enabled", True)
    monkeypatch.setattr(settings, "free_preview_plot_limit", 3)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        request = add_request(session, user_id="2002", candidate_count=7)
        services.route_ready_report(session, request.id)

        texts = [payload["text"] for method, payload in sent if method == "sendMessage"]
        assert "Найдено возможных вариантов: <b>7</b>" in texts[0]
        assert "По этой заявке найдено: <b>7</b>" in texts[1]
        assert "Показано в этом пакете: <b>7</b>" in texts[1]
        assert "Точные координаты, карта, ЕГКН" in texts[1]
        assert any("🗺 Результаты поиска" in text for text in texts)
        offer = next(
            payload
            for method, payload in sent
            if method == "sendMessage"
            and payload.get("reply_markup", {})
            .get("inline_keyboard", [[{}]])[0][0]
            .get("callback_data", "")
            .startswith("pay:start:")
        )
        assert offer["reply_markup"]["inline_keyboard"][0][0]["text"] == (
            "Разблокировать полный отчет — 4 990 ₸"
        )


def test_v2_apipay_message_has_only_automatic_kaspi_flow(monkeypatch) -> None:
    enable_v2(monkeypatch)
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(settings, "payment_card_number", "4400430373806295")
    monkeypatch.setattr(settings, "payment_recipient", "Даурен К")
    monkeypatch.setattr(
        services,
        "create_qr_invoice",
        lambda **_: ApiPayQrInvoice(
            invoice_id="901",
            status="pending",
            payment_url="https://qr.kaspi.kz/v2",
        ),
    )
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        request = add_paid_search_candidate(session)
        services.request_payment(session, request.id)

        payment = sent[-1][1]
        assert "Полный отчет и единый доступ готовы к активации" in payment["text"]
        assert "К оплате: <b>4 990 ₸</b>" in payment["text"]
        assert "4400" not in payment["text"]
        assert "Даурен" not in payment["text"]
        keyboard = payment["reply_markup"]["inline_keyboard"]
        assert keyboard[0][0]["text"] == "Разблокировать отчет через Kaspi — 4 990 ₸"
        assert keyboard[1][0]["text"] == "🔄 Обновить ссылку"


def test_v2_paid_activation_precedes_report_and_is_tracked(monkeypatch) -> None:
    enable_v2(monkeypatch)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        request = add_paid_search_candidate(session)
        request.payment_status = PaymentStatus.paid.value
        request.payment_amount_kzt = 1490
        request.status = SearchStatus.ready.value
        session.commit()

        services.deliver_request(session, request.id)

        client_texts = [
            payload["text"]
            for method, payload in sent
            if method == "sendMessage" and payload.get("chat_id") == "1001"
        ]
        assert "Оплата подтверждена" in client_texts[0]
        assert "🗺 Результаты поиска" in client_texts[1]
        events = session.scalars(
            select(FunnelEvent.event_name).where(FunnelEvent.request_id == request.id)
        ).all()
        assert "payment_paid" in events
        assert "report_delivered" in events


def test_v2_empty_result_replaces_progress_and_keeps_navigation(monkeypatch) -> None:
    enable_v2(monkeypatch)
    monkeypatch.setattr(settings, "urban_plan_check_mode", "off")
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        request = SearchRequest(
            region="Туркестанская область",
            district="г. Сарыагаш",
            locality="г Сарыагаш",
            telegram_user_id="3003",
            telegram_chat_id="3003",
            progress_message_id=88,
        )
        session.add(request)
        session.commit()

        services.process_search(session, request.id, search_engine=EmptySearchEngine())

        terminal = sent[-1]
        assert terminal[0] == "sendMessage"
        assert "message_id" not in terminal[1]
        assert "Подходящее место не найдено" in terminal[1]["text"]
        assert "Проверка генплана здесь не запускалась" in terminal[1]["text"]
        callbacks = {
            button["callback_data"]
            for row in terminal[1]["reply_markup"]["inline_keyboard"]
            for button in row
        }
        assert f"search:retry:{request.id}" in callbacks
        assert f"search:districts:{request.id}" in callbacks
        assert f"search:regions:{request.id}" in callbacks


def test_v2_missing_genplan_auto_delivers_without_manual_fallback(
    monkeypatch,
) -> None:
    enable_v2(monkeypatch)
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    monkeypatch.setattr(settings, "urban_plan_auto_waive_unavailable", False)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        request = SearchRequest(
            region="Акмолинская область",
            district="Бурабайский район",
            locality="Бурабай",
            telegram_user_id="4004",
            telegram_chat_id="4004",
            progress_message_id=99,
        )
        session.add(request)
        session.commit()

        services.process_search(session, request.id, search_engine=FakeSearchEngine())

        terminal = sent[-1]
        assert terminal[0] == "sendMessage"
        assert "message_id" not in terminal[1]
        assert "Разблокируйте полный отчет" in terminal[1]["text"]
        combined_text = "\n".join(payload.get("text", "") for _, payload in sent)
        assert "генплан" in combined_text.lower()
        assert "0</b>" not in terminal[1]["text"]
        callbacks = {
            button["callback_data"]
            for row in terminal[1]["reply_markup"]["inline_keyboard"]
            for button in row
            if "callback_data" in button
        }
        assert f"urban:waive:{request.id}" not in callbacks


def test_v2_kazakh_apipay_copy_does_not_leak_russian_or_card(monkeypatch) -> None:
    enable_v2(monkeypatch)
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(settings, "payment_card_number", "4400430373806295")
    monkeypatch.setattr(
        services,
        "create_qr_invoice",
        lambda **_: ApiPayQrInvoice(
            invoice_id="902",
            status="pending",
            payment_url="https://qr.kaspi.kz/kz-v2",
        ),
    )
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        request = add_paid_search_candidate(session, language="kz")
        services.request_payment(session, request.id)

        payment = sent[-1][1]
        assert "Толық есеп пен бірыңғай қолжетімділікті" in payment["text"]
        assert "Төлем сомасы: <b>4 990 ₸</b>" in payment["text"]
        assert "Полный доступ" not in payment["text"]
        assert "4400" not in payment["text"]
        assert payment["reply_markup"]["inline_keyboard"][0][0]["text"] == (
            "Толық есепті Kaspi арқылы ашу — 4 990 ₸"
        )
