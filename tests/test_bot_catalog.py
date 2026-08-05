import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import (
    SETTLEMENTS_PER_PAGE,
    TERMS_VERSION,
    area_keyboard,
    gardening_size_keyboard,
    group_start,
    lph_size_keyboard,
    offer_text,
    privacy_text,
    purpose_keyboard,
    settlement_keyboard,
    terms_keyboard,
    terms_text,
)
from app.config import settings
from app.i18n import kazakh_region_label, t
from app.schemas import SearchCreate


def test_group_start_redirects_to_private_chat() -> None:
    message = SimpleNamespace(answer=AsyncMock())
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="zem_poisk"))
    )

    asyncio.run(group_start(message, bot))

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert "только в личном чате" in text
    assert "жеке чатта" in text
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/zem_poisk?start=group"


def test_area_catalog_starts_with_ten_sotok() -> None:
    keyboard = area_keyboard()

    assert keyboard.inline_keyboard[0][0].callback_data == "catalog:area:10"
    area_callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("catalog:area:")
    ]
    assert area_callbacks == ["catalog:area:10"]


def test_kazakh_catalog_controls_are_translated() -> None:
    keyboard = area_keyboard("kz")

    assert keyboard.inline_keyboard[0][0].text == "10 сотық"
    assert keyboard.inline_keyboard[1][0].text == "Елді мекендерге қайту"


def test_all_district_area_returns_to_districts_and_keeps_region_home() -> None:
    keyboard = area_keyboard("ru", all_districts=True)

    assert keyboard.inline_keyboard[1][0].callback_data == "catalog:back:districts"
    assert keyboard.inline_keyboard[2][0].callback_data == "catalog:back:regions"


def test_purpose_catalog_has_separate_lph_and_gardening_profiles() -> None:
    keyboard = purpose_keyboard("ru")
    gardening_area = area_keyboard("ru", "Садоводство")
    gardening_size = gardening_size_keyboard("ru")
    irrigated_area = area_keyboard("ru", "ЛПХ (новый поиск)", "irrigated")
    non_irrigated_area = area_keyboard("ru", "ЛПХ (новый поиск)", "non_irrigated")

    assert keyboard.inline_keyboard[0][0].callback_data == "purpose:lph"
    assert keyboard.inline_keyboard[1][0].callback_data == "purpose:lph-new"
    assert keyboard.inline_keyboard[2][0].callback_data == "purpose:gardening"
    assert "6/12" in keyboard.inline_keyboard[2][0].text
    assert keyboard.inline_keyboard[3][0].text == "🌐 Открыть веб-сайт"
    assert keyboard.inline_keyboard[3][0].url == "https://zhertap.kz"
    assert keyboard.inline_keyboard[3][0].callback_data is None
    assert gardening_area.inline_keyboard[0][0].text == "12 соток"
    assert gardening_area.inline_keyboard[0][0].callback_data == "catalog:area:12"
    assert gardening_area.inline_keyboard[1][0].text == "6 соток"
    assert gardening_area.inline_keyboard[1][0].callback_data == "catalog:area:6"
    assert [row[0].callback_data for row in gardening_size.inline_keyboard] == [
        "garden-size:12",
        "garden-size:6",
        "catalog:back:purpose",
    ]
    assert irrigated_area.inline_keyboard[0][0].callback_data == "catalog:area:15"
    assert non_irrigated_area.inline_keyboard[0][0].callback_data == "catalog:area:25"


def test_purpose_prompt_and_size_choice_do_not_claim_land_type() -> None:
    assert "15 или 25 соток" in t("ru", "choose_purpose")
    assert "15 немесе 25 сотық" in t("kz", "choose_purpose")
    assert "/terms" in t("ru", "choose_purpose")
    assert [row[0].callback_data for row in lph_size_keyboard("ru").inline_keyboard] == [
        "lph-size:15",
        "lph-size:25",
        "catalog:back:purpose",
    ]


def test_search_profile_forces_its_fixed_area() -> None:
    lph = SearchCreate(district="test", purpose="ЛПХ", area_ha=0.12)
    gardening = SearchCreate(district="test", purpose="Садоводство", area_ha=0.10)
    gardening_small = SearchCreate(district="test", purpose="Садоводство", area_ha=0.06)
    irrigated = SearchCreate(
        district="test",
        purpose="ЛПХ (новый поиск)",
        allotment_type="household",
        irrigation_type="irrigated",
    )
    non_irrigated = SearchCreate(
        district="test",
        purpose="ЛПХ (новый поиск)",
        allotment_type="field",
        irrigation_type="non_irrigated",
    )

    assert lph.area_ha == 0.10
    assert gardening.area_ha == 0.12
    assert gardening_small.area_ha == 0.06
    assert irrigated.area_ha == 0.15
    assert non_irrigated.area_ha == 0.25
    assert non_irrigated.allotment_type == "field"


def test_kazakh_region_name_uses_local_translation() -> None:
    assert kazakh_region_label({"code": "01", "nameKz": "Акмолинская область"}) == (
        "Ақмола облысы (01)"
    )


def test_settlement_catalog_is_paginated() -> None:
    rows = [{"value": f"v{index}", "label": f"Поселок {index}"} for index in range(12)]

    first_page = settlement_keyboard(rows, 0)
    second_page = settlement_keyboard(rows, 1)

    first_callbacks = [button[0].callback_data for button in first_page.inline_keyboard[:8]]
    second_callbacks = [
        button[0].callback_data
        for button in second_page.inline_keyboard[: 12 - SETTLEMENTS_PER_PAGE]
    ]
    assert first_callbacks[0] == "catalog:settlement:0"
    assert first_callbacks[-1] == "catalog:settlement:7"
    assert second_callbacks[0] == "catalog:settlement:8"


def test_terms_show_price_limitations_and_explicit_acceptance(monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_access_price_kzt", 5000)
    monkeypatch.setattr(settings, "free_preview_enabled", True)
    monkeypatch.setattr(settings, "paid_search_enabled", True)

    text = terms_text()
    keyboard = terms_keyboard()

    assert "5 000 ₸" in text
    assert "доступ к поиску участков и земельным аукционам на 1 месяц" in text.lower()
    assert "ранее показанные варианты исключаются" in text
    assert "до 10 расчетных вариантов" in text
    assert "не подтверждает, что земля юридически свободна" in text
    assert "не связанный с государственными органами" in text
    assert "возврат оплаты" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "terms:accept"
    assert keyboard.inline_keyboard[0][0].text == "✅ Согласен и продолжить"
    assert TERMS_VERSION


def test_kazakh_terms_are_fully_localized(monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_access_price_kzt", 5000)
    monkeypatch.setattr(settings, "free_preview_enabled", True)
    monkeypatch.setattr(settings, "paid_search_enabled", True)

    text = terms_text("kz")
    keyboard = terms_keyboard("kz")

    assert "5 000 ₸/ай" in text
    assert "1 айлық бірыңғай қолжетімділік береді" in text
    assert "бұрын көрсетілген нұсқаларды қайталамай" in text
    assert "тәуелсіз жеке ақпараттық сервис" in text
    assert "төлемді қайтаруды" in text
    assert keyboard.inline_keyboard[0][0].text == "✅ Келісемін және жалғастырамын"


def test_privacy_and_offer_cover_required_client_information(monkeypatch) -> None:
    monkeypatch.setattr(settings, "service_provider_name", "Test Provider")
    monkeypatch.setattr(settings, "service_provider_contact", "@test")
    monkeypatch.setattr(settings, "data_storage_location", "Республика Казахстан")

    privacy = privacy_text("ru")
    offer = offer_text("ru")

    assert "Telegram user ID и chat ID" in privacy
    assert "за пределами Казахстана" in privacy
    assert "удаление своих данных" in privacy
    assert "Test Provider" in offer
    assert "повторную отправку или возврат оплаты" in offer
