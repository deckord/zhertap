from __future__ import annotations

import math
import uuid
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.access import find_pending_platform_invoice
from app.analytics import track_funnel_event
from app.auction_access import (
    can_view_auction_lot,
    get_auction_access,
    has_auction_paid_access,
    refresh_auction_payment,
    start_auction_payment,
)
from app.auction_exports import auction_lot_publication_history
from app.auction_service import (
    AuctionFilters,
    auction_lot_changes,
    auction_lot_geo_metrics,
    auction_lot_history,
    auction_lot_metrics,
    auction_market_snapshot,
    create_subscription,
    disable_subscription,
    format_auction_card,
    format_auction_metrics,
    get_auction_lot,
    is_favorite,
    list_auction_functional_purposes,
    list_auction_regions,
    list_favorites,
    list_subscriptions,
    toggle_favorite,
)
from app.auction_v2 import (
    AuctionV2Filters,
    format_auction_v2_telegram_card,
    get_auction_v2_payload,
    list_auction_v2_lots,
)
from app.config import settings
from app.db import SessionLocal
from app.i18n import normalize_language
from app.models import AuctionLot

auction_router = Router(name="auctions")
auction_router.message.filter(F.chat.type == "private")
auction_router.callback_query.filter(F.message.chat.type == "private")
LOTS_PER_PAGE = 5
REGIONS_PER_PAGE = 8
PURPOSES_PER_PAGE = 7

TEXT = {
    "ru": {
        "menu": (
            "🏷 <b>Земельные аукционы</b>\n\n"
            "Раздел экономит время на просмотре E-Qazyna: система собирает активные "
            "публикации, фильтрует их по региону, назначению, цене и площади, помогает "
            "сравнивать варианты и следить за новыми лотами.\n\n"
            "Выберите параметры — бот покажет подходящие лоты, даст открыть официальный "
            "источник, сохранить варианты, сравнить их и получать уведомления о новых "
            "публикациях.\n\n"
            "🎁 Бесплатно можно смотреть список и краткую аналитику лотов.\n"
            "🔒 E-Qazyna, документы, избранное, сравнение и уведомления открываются после оплаты."
        ),
        "find": "🧭 Анализировать лоты",
        "favorites": "⭐ Избранное",
        "subscriptions": "🔔 Мои уведомления",
        "market_stats": "📊 Статистика рынка",
        "refresh": "🔄 Обновить каталог",
        "back_main": "🏠 Главное меню",
        "choose_region": "📍 Выберите область:",
        "all_regions": "🇰🇿 Весь Казахстан",
        "back": "⬅️ Назад",
        "choose_purpose": (
            "🎯 <b>Выберите функциональное назначение</b>\n\n"
            "Это официальные категории уровня 2 из карточек земельных лотов E-Qazyna. "
            "Конкретное целевое назначение будет указано внутри каждого лота."
        ),
        "purpose_all": "Все функциональные назначения",
        "choose_price": "💰 Выберите максимальную стартовую цену:",
        "price_all": "Любая цена",
        "choose_area": "📐 Выберите площадь:",
        "area_all": "Любая площадь",
        "area_small": "До 0,20 га",
        "area_medium": "0,20–1 га",
        "area_large": "Больше 1 га",
        "results": "🏷 Найдено лотов: {count}\n\nВыберите лот:",
        "none": (
            "По выбранным параметрам активные земельные лоты не найдены.\n\n"
            "Каталог обновляется автоматически. Можно изменить фильтры или включить уведомления."
        ),
        "syncing": (
            "🔄 Обновление каталога запущено.\n\n"
            "E-Qazyna может отвечать несколько минут. Нажмите «Обновить каталог» позже."
        ),
        "subscribe": "🔔 Сообщать о новых",
        "subscribed": "Уведомление создано. Бот сообщит о новых лотах по этому фильтру.",
        "favorite_add": "⭐ В избранное",
        "favorite_remove": "✖ Убрать из избранного",
        "favorite_added": "Лот добавлен в избранное",
        "favorite_removed": "Лот удален из избранного",
        "open_source": "Открыть E-Qazyna ↗",
        "documents": "📎 Документы",
        "all_documents": "📎 Все документы",
        "history": "📜 История торгов",
        "changes": "🧾 Что менялось",
        "no_favorites": "В избранном пока нет земельных лотов.",
        "compare": "⚖️ Сравнить лоты",
        "compare_title": "⚖️ <b>Сравнение сохраненных лотов</b>",
        "no_subscriptions": "Активных уведомлений пока нет.",
        "disable": "Отключить",
        "disabled": "Уведомление отключено",
        "catalog_empty": (
            "Каталог аукционов пока пуст. Запущено первое обновление данных E-Qazyna."
        ),
        "filter": "Фильтр",
        "lot_missing": "Лот не найден или уже удален.",
        "access_paid": "✅ Безлимитный доступ активен",
        "access_trial": "🎁 Бесплатно доступен краткий просмотр лотов",
        "unlock": "🔓 Разблокировать полный доступ — {price} ₸",
        "locked_favorites": "🔒 Избранное",
        "locked_subscriptions": "🔒 Уведомления",
        "trial_results": (
            "🏷 Найдено лотов: {count}\n\n"
            "🎁 Показаны краткие карточки лотов.\n"
            "Документы, E-Qazyna, избранное и уведомления открываются после оплаты."
        ),
        "trial_used": (
            "🏷 По выбранным параметрам найдено лотов: {count}\n\n"
            "В бесплатном режиме лоты показаны частично.\n"
            "Разблокируйте полный доступ, чтобы открыть документы и действия."
        ),
        "lot_locked": (
            "🔒 <b>Этот лот скрыт</b>\n\n"
            "В бесплатном режиме лот показан частично. После активации откроются "
            "документы, E-Qazyna, избранное, сравнение и уведомления.\n\n"
            "Единый доступ к анализу территории и аукционам: <b>{price} ₸</b>."
        ),
        "payment": (
            "🔓 <b>Единый доступ ко всему сервису</b>\n\n"
            "После оплаты вы получите:\n"
            "✅ неограниченный анализ территорий под участки;\n"
            "✅ все найденные лоты без ограничений;\n"
            "✅ избранное и сравнение;\n"
            "✅ уведомления о новых подходящих лотах;\n"
            "✅ доступ на 1 месяц для веба и Telegram.\n\n"
            "Стоимость: <b>{price} ₸</b>\n"
            "Оплата подтверждается автоматически через ApiPay."
        ),
        "pay": "💳 Разблокировать через Kaspi — {price} ₸",
        "refresh_payment": "🔄 Обновить ссылку оплаты",
        "payment_unavailable": "Не удалось создать ссылку оплаты. Повторите немного позже.",
        "market_stats_title": "📊 <b>Статистика земельных аукционов</b>",
    },
    "kz": {
        "menu": (
            "🏷 <b>Жер аукциондары</b>\n\n"
            "Бұл бөлім E-Qazyna қарауға кететін уақытты үнемдейді: жүйе белсенді "
            "жарияланымдарды жинайды, өңір, мақсат, баға және аудан бойынша сүзеді, "
            "нұсқаларды салыстыруға және жаңа лоттарды бақылауға көмектеседі.\n\n"
            "Параметрлерді таңдаңыз — бот сәйкес лоттарды көрсетеді, ресми дереккөзді "
            "ашуға, нұсқаларды сақтауға, салыстыруға және жаңа жарияланымдар туралы "
            "хабарлама алуға мүмкіндік береді.\n\n"
            "🎁 Тегін режимде лоттардың тізімі мен қысқа талдауы көрсетіледі.\n"
            "🔒 E-Qazyna, құжаттар, таңдаулылар, салыстыру және хабарламалар "
            "төлемнен кейін ашылады."
        ),
        "find": "🧭 Лоттарды талдау",
        "favorites": "⭐ Таңдаулылар",
        "subscriptions": "🔔 Менің хабарламаларым",
        "market_stats": "📊 Нарық статистикасы",
        "refresh": "🔄 Каталогты жаңарту",
        "back_main": "🏠 Негізгі мәзір",
        "choose_region": "📍 Облысты таңдаңыз:",
        "all_regions": "🇰🇿 Бүкіл Қазақстан",
        "back": "⬅️ Артқа",
        "choose_purpose": (
            "🎯 <b>Функционалдық мақсатын таңдаңыз</b>\n\n"
            "Бұл жер лоттарының E-Qazyna карточкаларында көрсетілген ресми "
            "2-деңгей санаттары. Нақты нысаналы мақсаты әр лоттың ішінде көрсетіледі."
        ),
        "purpose_all": "Барлық функционалдық мақсаттар",
        "choose_price": "💰 Ең жоғары бастапқы бағаны таңдаңыз:",
        "price_all": "Кез келген баға",
        "choose_area": "📐 Ауданды таңдаңыз:",
        "area_all": "Кез келген аудан",
        "area_small": "0,20 га дейін",
        "area_medium": "0,20–1 га",
        "area_large": "1 гектардан көп",
        "results": "🏷 Табылған лоттар: {count}\n\nЛотты таңдаңыз:",
        "none": (
            "Таңдалған параметрлер бойынша белсенді жер лоттары табылмады.\n\n"
            "Каталог автоматты түрде жаңартылады. Сүзгіні өзгертуге немесе хабарлама қосуға болады."
        ),
        "syncing": (
            "🔄 Каталогты жаңарту басталды.\n\n"
            "E-Qazyna бірнеше минут жауап беруі мүмкін. «Каталогты жаңарту» түймесін кейін басыңыз."
        ),
        "subscribe": "🔔 Жаңа лоттар туралы хабарлау",
        "subscribed": "Хабарлама құрылды. Бот осы сүзгі бойынша жаңа лоттарды хабарлайды.",
        "favorite_add": "⭐ Таңдаулыға қосу",
        "favorite_remove": "✖ Таңдаулыдан алып тастау",
        "favorite_added": "Лот таңдаулыға қосылды",
        "favorite_removed": "Лот таңдаулыдан алынды",
        "open_source": "E-Qazyna-ны ашу ↗",
        "documents": "📎 Құжаттар",
        "all_documents": "📎 Барлық құжаттар",
        "history": "📜 Сауда тарихы",
        "changes": "🧾 Өзгерістер",
        "no_favorites": "Таңдаулыларда жер лоттары жоқ.",
        "compare": "⚖️ Лоттарды салыстыру",
        "compare_title": "⚖️ <b>Сақталған лоттарды салыстыру</b>",
        "no_subscriptions": "Белсенді хабарламалар жоқ.",
        "disable": "Өшіру",
        "disabled": "Хабарлама өшірілді",
        "catalog_empty": (
            "Аукцион каталогы әзірге бос. E-Qazyna деректерінің алғашқы жаңартуы басталды."
        ),
        "filter": "Сүзгі",
        "lot_missing": "Лот табылмады немесе жойылған.",
        "access_paid": "✅ Шексіз қолжетімділік белсенді",
        "access_trial": "🎁 Лоттарды қысқа көру тегін қолжетімді",
        "unlock": "🔓 Толық қолжетімділікті ашу — {price} ₸",
        "locked_favorites": "🔒 Таңдаулылар",
        "locked_subscriptions": "🔒 Хабарламалар",
        "trial_results": (
            "🏷 Табылған лоттар: {count}\n\n"
            "🎁 Лоттардың қысқа карточкалары көрсетілді.\n"
            "Құжаттар, E-Qazyna, таңдаулылар және хабарламалар төлемнен кейін ашылады."
        ),
        "trial_used": (
            "🏷 Таңдалған параметрлер бойынша табылған лоттар: {count}\n\n"
            "Тегін режимде лоттар жартылай көрсетіледі.\n"
            "Құжаттар мен әрекеттерді ашу үшін толық қолжетімділікті қосыңыз."
        ),
        "lot_locked": (
            "🔒 <b>Бұл лот жасырылған</b>\n\n"
            "Тегін режимде лот жартылай көрсетіледі. Қолжетімділікті іске қосқаннан кейін "
            "құжаттар, E-Qazyna, таңдаулылар, салыстыру және хабарламалар ашылады.\n\n"
            "Аумақ талдауы мен аукциондарға бірыңғай қолжетімділік: <b>{price} ₸</b>."
        ),
        "payment": (
            "🔓 <b>Барлық сервиске бірыңғай қолжетімділік</b>\n\n"
            "Төлемнен кейін сізге:\n"
            "✅ учаске үшін аумақтарды шектеусіз талдау;\n"
            "✅ барлық лоттар шектеусіз;\n"
            "✅ таңдаулылар мен салыстыру;\n"
            "✅ жаңа сәйкес лоттар туралы хабарламалар;\n"
            "✅ веб пен Telegram үшін 1 айлық қолжетімділік беріледі.\n\n"
            "Құны: <b>{price} ₸</b>\n"
            "Төлем ApiPay арқылы автоматты түрде расталады."
        ),
        "pay": "💳 Kaspi арқылы ашу — {price} ₸",
        "refresh_payment": "🔄 Төлем сілтемесін жаңарту",
        "payment_unavailable": "Төлем сілтемесін жасау мүмкін болмады. Кейінірек қайталаңыз.",
        "market_stats_title": "📊 <b>Жер аукциондарының статистикасы</b>",
    },
}


def at(language: str | None, key: str, **values: object) -> str:
    selected = normalize_language(language)
    price = f"{settings.platform_access_price_kzt:,}".replace(",", " ")
    return TEXT[selected][key].format(price=price, **values)


def _language(data: dict) -> str:
    return normalize_language(data.get("language"))


def _track_auction_event(
    event_name: str,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
    language: str,
    funnel_session_id: str | None,
    metadata: dict | None = None,
) -> None:
    with SessionLocal() as session:
        track_funnel_event(
            session,
            event_name,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            funnel_session_id=funnel_session_id,
            language=language,
            metadata=metadata,
        )


def _main_menu_keyboard(language: str, *, paid: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=at(language, "find"),
                callback_data="auction:regions:0",
            )
        ],
        [
            InlineKeyboardButton(
                text=at(
                    language,
                    "favorites" if paid else "locked_favorites",
                ),
                callback_data="auction:favorites",
            )
        ],
        [
            InlineKeyboardButton(
                text=at(
                    language,
                    "subscriptions" if paid else "locked_subscriptions",
                ),
                callback_data="auction:subscriptions",
            )
        ],
        [
            InlineKeyboardButton(
                text=at(language, "market_stats"),
                callback_data="auction:stats",
            )
        ],
    ]
    if paid:
        rows.append(
            [InlineKeyboardButton(text=at(language, "refresh"), callback_data="auction:sync")]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text=at(language, "unlock"), callback_data="auction:pay")]
        )
    rows.append(
        [InlineKeyboardButton(text=at(language, "back_main"), callback_data="catalog:home")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _filters_from_data(data: dict) -> AuctionFilters:
    raw = data.get("auction_filters") or {}
    return AuctionFilters(
        region=raw.get("region"),
        district=raw.get("district"),
        locality=raw.get("locality"),
        purpose_query=raw.get("purpose_query"),
        min_price_kzt=raw.get("min_price_kzt"),
        max_price_kzt=raw.get("max_price_kzt"),
        min_area_ha=raw.get("min_area_ha"),
        max_area_ha=raw.get("max_area_ha"),
    )


def _filters_dict(filters: AuctionFilters) -> dict:
    return {
        "region": filters.region,
        "district": filters.district,
        "locality": filters.locality,
        "purpose_query": filters.purpose_query,
        "min_price_kzt": filters.min_price_kzt,
        "max_price_kzt": filters.max_price_kzt,
        "min_area_ha": filters.min_area_ha,
        "max_area_ha": filters.max_area_ha,
    }


def _lot_button(lot: AuctionLot) -> str:
    price = (
        f"{lot.start_price_kzt:,.0f}".replace(",", " ") + " ₸"
        if lot.start_price_kzt is not None
        else "—"
    )
    number = lot.auction_number or lot.source_lot_id
    return f"№{number} · {lot.region or '—'} · {price}"[:64]


def _lot_v2_button(payload) -> str:
    lot = payload.lot
    number = lot.auction_number or lot.source_lot_id
    price = (
        f"{lot.start_price_kzt:,.0f}".replace(",", " ") + " ₸"
        if lot.start_price_kzt is not None
        else "—"
    )
    return f"{payload.analysis.score}/100 · №{number} · {price}"[:64]


async def show_auction_menu(
    message: Message,
    state: FSMContext,
    *,
    telegram_user_id: str,
) -> None:
    data = await state.get_data()
    language = _language(data)
    with SessionLocal() as session:
        paid = has_auction_paid_access(session, telegram_user_id)
    access_label = at(language, "access_paid" if paid else "access_trial")
    await message.edit_text(
        f"{at(language, 'menu')}\n\n{access_label}",
        parse_mode="HTML",
        reply_markup=_main_menu_keyboard(language, paid=paid),
    )


async def _dispatch_sync() -> None:
    if not settings.auctions_enabled:
        return
    from app.tasks import sync_current_auctions_task

    sync_current_auctions_task.delay()


@auction_router.message(Command("auctions"))
async def auction_command(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    language = _language(data)
    funnel_session_id = data.get("funnel_session_id") or str(uuid.uuid4())
    await state.update_data(
        funnel_session_id=funnel_session_id,
        telegram_user_id=str(message.from_user.id),
        telegram_chat_id=str(message.chat.id),
    )
    _track_auction_event(
        "auction_opened",
        telegram_user_id=str(message.from_user.id),
        telegram_chat_id=str(message.chat.id),
        language=language,
        funnel_session_id=funnel_session_id,
        metadata={"entry": "command"},
    )
    with SessionLocal() as session:
        paid = has_auction_paid_access(session, str(message.from_user.id))
    await message.answer(
        f"{at(language, 'menu')}\n\n{at(language, 'access_paid' if paid else 'access_trial')}",
        parse_mode="HTML",
        reply_markup=_main_menu_keyboard(language, paid=paid),
    )


@auction_router.callback_query(F.data == "service:auctions")
@auction_router.callback_query(F.data == "auction:menu")
async def auction_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(
        telegram_user_id=str(callback.from_user.id),
        telegram_chat_id=str(
            callback.message.chat.id if callback.message else callback.from_user.id
        ),
    )
    _track_auction_event(
        "auction_opened",
        telegram_user_id=str(callback.from_user.id),
        telegram_chat_id=str(
            callback.message.chat.id if callback.message else callback.from_user.id
        ),
        language=_language(data),
        funnel_session_id=data.get("funnel_session_id"),
        metadata={"entry": "main_menu"},
    )
    await callback.answer()
    if callback.message:
        await show_auction_menu(
            callback.message,
            state,
            telegram_user_id=str(callback.from_user.id),
        )


@auction_router.callback_query(F.data == "auction:sync")
async def auction_sync(callback: CallbackQuery, state: FSMContext) -> None:
    language = _language(await state.get_data())
    with SessionLocal() as session:
        paid = has_auction_paid_access(session, str(callback.from_user.id))
    if not paid:
        await callback.answer()
        if callback.message:
            await _show_auction_paywall(callback.message, language)
        return
    await _dispatch_sync()
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            at(language, "syncing"),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=at(language, "refresh"),
                            callback_data="auction:regions:0",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=at(language, "back"),
                            callback_data="auction:menu",
                        )
                    ],
                ]
            ),
        )


@auction_router.callback_query(F.data == "auction:stats")
async def auction_stats_view(callback: CallbackQuery, state: FSMContext) -> None:
    language = _language(await state.get_data())
    with SessionLocal() as session:
        snapshot = auction_market_snapshot(session)
    catalog = snapshot["catalog"]
    statuses = snapshot["statuses"][:8]
    rankings = snapshot["district_price_rankings"]
    cheapest = rankings["cheapest"][:5]
    lines = [
        at(language, "market_stats_title"),
        "",
        (
            f"📦 Каталог: {catalog['total']} лотов"
            if language == "ru"
            else f"📦 Каталог: {catalog['total']} лот"
        ),
        (
            f"✅ Активных: {catalog['active']}"
            if language == "ru"
            else f"✅ Белсенді: {catalog['active']}"
        ),
        "",
        "📌 <b>Статусы</b>" if language == "ru" else "📌 <b>Мәртебелер</b>",
    ]
    for item in statuses:
        lines.append(f"• {item['status']}: {item['total']}")
    lines.extend(
        [
            "",
            (
                "💠 <b>Самые дешевые районы по средней цене за сотку</b>"
                if language == "ru"
                else "💠 <b>Сотықтың орташа бағасы бойынша ең арзан аудандар</b>"
            ),
        ]
    )
    for item in cheapest:
        price = f"{float(item['avg_price_per_sotka']):,.0f}".replace(",", " ")
        lines.append(f"• {item['district']} · {price} ₸/сотка")
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=at(language, "back"), callback_data="auction:menu")]
                ]
            ),
        )


@auction_router.callback_query(F.data.startswith("auction:regions:"))
async def auction_regions(callback: CallbackQuery, state: FSMContext) -> None:
    language = _language(await state.get_data())
    try:
        page = max(0, int((callback.data or "").rsplit(":", 1)[-1]))
    except ValueError:
        page = 0
    with SessionLocal() as session:
        regions = list_auction_regions(session)
    if not regions:
        await _dispatch_sync()
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                at(language, "catalog_empty"),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=at(language, "refresh"),
                                callback_data="auction:regions:0",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=at(language, "back"),
                                callback_data="auction:menu",
                            )
                        ],
                    ]
                ),
            )
        return
    await state.update_data(auction_regions=[region for region, _ in regions])
    page_count = max(1, math.ceil(len(regions) / REGIONS_PER_PAGE))
    page = min(page, page_count - 1)
    start = page * REGIONS_PER_PAGE
    rows = [
        [
            InlineKeyboardButton(
                text=f"{region} · {count}",
                callback_data=f"auction:region:{index}",
            )
        ]
        for index, (region, count) in enumerate(
            regions[start : start + REGIONS_PER_PAGE],
            start=start,
        )
    ]
    rows.insert(
        0,
        [
            InlineKeyboardButton(
                text=at(language, "all_regions"),
                callback_data="auction:region:all",
            )
        ],
    )
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="←", callback_data=f"auction:regions:{page - 1}")
        )
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="auction:noop")
    )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(text="→", callback_data=f"auction:regions:{page + 1}")
        )
    rows.append(navigation)
    rows.append(
        [InlineKeyboardButton(text=at(language, "back"), callback_data="auction:menu")]
    )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            at(language, "choose_region"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@auction_router.callback_query(F.data.startswith("auction:region:"))
async def auction_choose_region(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = (callback.data or "").rsplit(":", 1)[-1]
    region = None
    if selected != "all":
        try:
            region = (data.get("auction_regions") or [])[int(selected)]
        except (ValueError, IndexError):
            await callback.answer("Меню устарело", show_alert=True)
            return
    filters = AuctionFilters(region=region)
    await state.update_data(auction_filters=_filters_dict(filters))
    await _show_functional_purpose_menu(callback, state, page=0)


async def _show_functional_purpose_menu(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    page: int,
) -> None:
    data = await state.get_data()
    language = _language(data)
    filters = _filters_from_data(data)
    with SessionLocal() as session:
        purposes = list_auction_functional_purposes(session, filters.region)
    await state.update_data(
        auction_functional_purposes=[purpose for purpose, _ in purposes]
    )
    page_count = max(1, math.ceil(len(purposes) / PURPOSES_PER_PAGE))
    page = min(max(0, page), page_count - 1)
    start = page * PURPOSES_PER_PAGE
    rows = [
        [
            InlineKeyboardButton(
                text=at(language, "purpose_all"),
                callback_data="auction:purpose:all",
            )
        ]
    ]
    rows.extend(
        [
            InlineKeyboardButton(
                text=f"{purpose[:45]} · {count}",
                callback_data=f"auction:purpose:{index}",
            )
        ]
        for index, (purpose, count) in enumerate(
            purposes[start : start + PURPOSES_PER_PAGE],
            start=start,
        )
    )
    if page_count > 1:
        navigation = []
        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="←",
                    callback_data=f"auction:purposes:{page - 1}",
                )
            )
        navigation.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{page_count}",
                callback_data="auction:noop",
            )
        )
        if page + 1 < page_count:
            navigation.append(
                InlineKeyboardButton(
                    text="→",
                    callback_data=f"auction:purposes:{page + 1}",
                )
            )
        rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton(
                text=at(language, "back"),
                callback_data="auction:regions:0",
            )
        ]
    )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            at(language, "choose_purpose"),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@auction_router.callback_query(F.data.startswith("auction:purposes:"))
async def auction_purpose_page(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        page = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        page = 0
    await _show_functional_purpose_menu(callback, state, page=page)


@auction_router.callback_query(F.data.startswith("auction:purpose:"))
async def auction_choose_purpose(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    language = _language(data)
    filters = _filters_from_data(data)
    selected = (callback.data or "").rsplit(":", 1)[-1]
    if selected == "all":
        filters.purpose_query = None
    else:
        try:
            filters.purpose_query = (data.get("auction_functional_purposes") or [])[
                int(selected)
            ]
        except (ValueError, IndexError):
            await callback.answer("Меню устарело", show_alert=True)
            return
    await state.update_data(auction_filters=_filters_dict(filters))
    rows = [
        [
            InlineKeyboardButton(
                text=at(language, "price_all"),
                callback_data="auction:price:all",
            )
        ],
        [InlineKeyboardButton(text="до 1 000 000 ₸", callback_data="auction:price:1000000")],
        [InlineKeyboardButton(text="до 5 000 000 ₸", callback_data="auction:price:5000000")],
        [InlineKeyboardButton(text="до 20 000 000 ₸", callback_data="auction:price:20000000")],
        [
            InlineKeyboardButton(
                text=at(language, "back"),
                callback_data="auction:purposes:0",
            )
        ],
    ]
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            at(language, "choose_price"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@auction_router.callback_query(F.data.startswith("auction:price:"))
async def auction_choose_price(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    language = _language(data)
    filters = _filters_from_data(data)
    selected = (callback.data or "").rsplit(":", 1)[-1]
    filters.max_price_kzt = None if selected == "all" else float(selected)
    await state.update_data(auction_filters=_filters_dict(filters))
    rows = [
        [
            InlineKeyboardButton(
                text=at(language, "area_all"),
                callback_data="auction:area:all",
            )
        ],
        [
            InlineKeyboardButton(
                text=at(language, "area_small"),
                callback_data="auction:area:small",
            )
        ],
        [
            InlineKeyboardButton(
                text=at(language, "area_medium"),
                callback_data="auction:area:medium",
            )
        ],
        [
            InlineKeyboardButton(
                text=at(language, "area_large"),
                callback_data="auction:area:large",
            )
        ],
        [
            InlineKeyboardButton(
                text=at(language, "back"),
                callback_data="auction:regions:0",
            )
        ],
    ]
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            at(language, "choose_area"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@auction_router.callback_query(F.data.startswith("auction:area:"))
async def auction_choose_area(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    filters = _filters_from_data(data)
    selected = (callback.data or "").rsplit(":", 1)[-1]
    filters.min_area_ha = 1.000001 if selected == "large" else 0.2 if selected == "medium" else None
    filters.max_area_ha = 0.2 if selected == "small" else 1.0 if selected == "medium" else None
    await state.update_data(auction_filters=_filters_dict(filters))
    _track_auction_event(
        "auction_filter_completed",
        telegram_user_id=str(callback.from_user.id),
        telegram_chat_id=str(
            callback.message.chat.id if callback.message else callback.from_user.id
        ),
        language=_language(data),
        funnel_session_id=data.get("funnel_session_id"),
        metadata=_filters_dict(filters),
    )
    await callback.answer()
    if callback.message:
        await _show_lot_list(callback.message, state, page=0)


async def _show_lot_list(message: Message, state: FSMContext, *, page: int) -> None:
    data = await state.get_data()
    language = _language(data)
    filters = _filters_from_data(data)
    telegram_user_id = str(data.get("telegram_user_id") or message.chat.id)
    telegram_chat_id = str(data.get("telegram_chat_id") or message.chat.id)
    with SessionLocal() as session:
        paid = has_auction_paid_access(session, telegram_user_id)
        v2_filters = AuctionV2Filters(
            base=filters,
            lot_scope="active",
            sort_by="best",
        )
        payloads, total = list_auction_v2_lots(
            session,
            v2_filters,
            offset=page * LOTS_PER_PAGE,
            limit=LOTS_PER_PAGE,
        )
    rows = [
        [
            InlineKeyboardButton(
                text=_lot_v2_button(payload),
                callback_data=f"auction:lot:{payload.lot.id}",
            )
        ]
        for payload in payloads
    ]
    page_count = max(1, math.ceil(total / LOTS_PER_PAGE))
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="←", callback_data=f"auction:list:{page - 1}")
        )
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="auction:noop")
    )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(text="→", callback_data=f"auction:list:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    if paid:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text=at(language, "subscribe"),
                        callback_data="auction:subscribe",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=at(language, "back"),
                        callback_data="auction:regions:0",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=at(language, "back_main"),
                        callback_data="catalog:home",
                    )
                ],
            ]
        )
    elif total:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text=at(language, "unlock"),
                        callback_data="auction:pay",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=at(language, "back"),
                        callback_data="auction:regions:0",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=at(language, "back_main"),
                        callback_data="catalog:home",
                    )
                ],
            ]
        )
    else:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text=at(language, "back"),
                        callback_data="auction:regions:0",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=at(language, "back_main"),
                        callback_data="catalog:home",
                    )
                ],
            ]
        )
    if not total:
        text = at(language, "none")
    elif paid:
        text = at(language, "results", count=total)
    else:
        text = at(language, "trial_results", count=total)
    if not paid and total:
        _track_auction_event(
            "auction_paywall_viewed",
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            language=language,
            funnel_session_id=data.get("funnel_session_id"),
            metadata={"available_lots": total, "locked_mode": True},
        )
    await message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@auction_router.callback_query(F.data.startswith("auction:list:"))
async def auction_list_page(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        page = max(0, int((callback.data or "").rsplit(":", 1)[-1]))
    except ValueError:
        page = 0
    await callback.answer()
    if callback.message:
        await _show_lot_list(callback.message, state, page=page)


async def _show_auction_paywall(message: Message, language: str) -> None:
    await message.edit_text(
        at(language, "lot_locked"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=at(language, "unlock"),
                        callback_data="auction:pay",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=at(language, "back"),
                        callback_data="auction:list:0",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=at(language, "back_main"),
                        callback_data="catalog:home",
                    )
                ],
            ]
        ),
    )


@auction_router.callback_query(F.data.startswith("auction:lot:"))
async def auction_lot_detail(callback: CallbackQuery, state: FSMContext) -> None:
    lot_id = (callback.data or "").rsplit(":", 1)[-1]
    data = await state.get_data()
    _track_auction_event(
        "auction_lot_viewed",
        telegram_user_id=str(callback.from_user.id),
        telegram_chat_id=str(
            callback.message.chat.id if callback.message else callback.from_user.id
        ),
        language=_language(data),
        funnel_session_id=data.get("funnel_session_id"),
        metadata={"lot_id": lot_id},
    )
    await callback.answer()
    if callback.message:
        await _show_lot_detail(
            callback.message,
            state,
            telegram_user_id=str(callback.from_user.id),
            lot_id=lot_id,
        )


async def _show_lot_detail(
    message: Message,
    state: FSMContext,
    *,
    telegram_user_id: str,
    lot_id: str,
) -> None:
    data = await state.get_data()
    language = _language(data)
    with SessionLocal() as session:
        paid = has_auction_paid_access(session, telegram_user_id)
        lot = get_auction_lot(session, lot_id)
        favorite = is_favorite(session, telegram_user_id, lot_id) if lot else False
        metrics = auction_lot_metrics(session, lot) if lot else None
        geo_metrics = auction_lot_geo_metrics(lot) if lot else None
        v2_payload = (
            get_auction_v2_payload(session, lot_id, force=True) if lot else None
        )
    if lot is None:
        await message.edit_text(
            at(language, "lot_missing"),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=at(language, "back"),
                            callback_data="auction:list:0",
                        )
                    ]
                ]
            ),
        )
        return
    rows = [
        [
            InlineKeyboardButton(
                text=at(language, "open_source"),
                url=lot.source_url,
            )
        ],
        [
            InlineKeyboardButton(
                text=(
                    at(language, "favorite_remove")
                    if favorite
                    else at(language, "favorite_add")
                ),
                callback_data=f"auction:favorite:{lot.id}",
            )
        ],
        [
            InlineKeyboardButton(
                text=at(language, "history"),
                callback_data=f"auction:history:{lot.id}",
            )
        ],
        [
            InlineKeyboardButton(
                text=at(language, "changes"),
                callback_data=f"auction:changes:{lot.id}",
            )
        ],
    ]
    for document in lot.documents[:3]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📎 {document.title}"[:64],
                    url=document.source_url,
                )
            ]
        )
    if len(lot.documents) > 3:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{at(language, 'all_documents')} ({len(lot.documents)})",
                    callback_data=f"auction:documents:{lot.id}:0",
                )
            ]
        )
    if not paid:
        rows = [
            [
                InlineKeyboardButton(
                    text=at(language, "unlock"),
                    callback_data="auction:pay",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔒 E-Qazyna после оплаты",
                    callback_data="auction:pay",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🔒 Документы после оплаты ({len(lot.documents)})",
                    callback_data="auction:pay",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Избранное после оплаты",
                    callback_data="auction:pay",
                )
            ],
            [
                InlineKeyboardButton(
                    text=at(language, "history"),
                    callback_data=f"auction:history:{lot.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=at(language, "changes"),
                    callback_data=f"auction:changes:{lot.id}",
                )
            ],
        ]
    rows.extend(
        [
            [InlineKeyboardButton(text=at(language, "back"), callback_data="auction:list:0")],
            [
                InlineKeyboardButton(
                    text=at(language, "back_main"),
                    callback_data="catalog:home",
                )
            ],
        ]
    )
    card_text = (
        format_auction_v2_telegram_card(v2_payload)
        if v2_payload is not None
        else format_auction_card(lot, language)
        + (format_auction_metrics(metrics, language) if metrics else "")
        + (_format_auction_geo_metrics(geo_metrics, language) if geo_metrics else "")
    )
    await message.edit_text(
        card_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@auction_router.callback_query(F.data.startswith("auction:documents:"))
async def auction_documents(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return
    lot_id = parts[2]
    try:
        page = max(0, int(parts[3]))
    except ValueError:
        page = 0
    data = await state.get_data()
    language = _language(data)
    telegram_user_id = str(callback.from_user.id)
    with SessionLocal() as session:
        if not has_auction_paid_access(session, telegram_user_id):
            await callback.answer()
            if callback.message:
                await _show_auction_paywall(callback.message, language)
            return
        lot = get_auction_lot(session, lot_id)
    if lot is None:
        await callback.answer(at(language, "lot_missing"), show_alert=True)
        return
    per_page = 10
    page_count = max(1, math.ceil(len(lot.documents) / per_page))
    page = min(page, page_count - 1)
    documents = lot.documents[page * per_page : (page + 1) * per_page]
    rows = [
        [
            InlineKeyboardButton(
                text=f"📎 {document.title}"[:64],
                url=document.source_url,
            )
        ]
        for document in documents
    ]
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="←",
                callback_data=f"auction:documents:{lot.id}:{page - 1}",
            )
        )
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="auction:noop")
    )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(
                text="→",
                callback_data=f"auction:documents:{lot.id}:{page + 1}",
            )
        )
    rows.append(navigation)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=at(language, "back"),
                    callback_data=f"auction:lot:{lot.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=at(language, "back_main"),
                    callback_data="catalog:home",
                )
            ],
        ]
    )
    title = "📎 <b>Құжаттар</b>" if language == "kz" else "📎 <b>Документы</b>"
    notice = (
        "Құжаттар E-Qazyna ресми порталындағы ашық сілтемелер арқылы ашылады."
        if language == "kz"
        else "Документы открываются по публичным ссылкам официального портала E-Qazyna."
    )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"{title}\n\n{notice}\n\n{len(lot.documents)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


def _short_datetime(value: object) -> str:
    if value is None:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y %H:%M")
    return str(value)


def _short_money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}".replace(",", " ") + " ₸"


def _short_distance(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1000:
        return f"{value / 1000:.1f} км"
    return f"{round(value):.0f} м"


def _format_auction_geo_metrics(metrics: object, language: str) -> str:
    if getattr(metrics, "status", "") == "no_coordinates":
        text = (
            "📍 Координаты лота в открытых данных не найдены, расстояния не рассчитаны."
            if language == "ru"
            else "📍 Ашық деректерде лот координаттары табылмады, қашықтық есептелмеді."
        )
        return f"\n\n{text}"

    latitude = getattr(metrics, "latitude", None)
    longitude = getattr(metrics, "longitude", None)
    if latitude is None or longitude is None:
        return ""

    labels = (
        [
            ("road_m", "дорога"),
            ("school_m", "школа"),
            ("hospital_m", "больница"),
            ("fuel_m", "АЗС"),
            ("railway_m", "ж/д"),
            ("power_line_m", "ЛЭП"),
        ]
        if language == "ru"
        else [
            ("road_m", "жол"),
            ("school_m", "мектеп"),
            ("hospital_m", "аурухана"),
            ("fuel_m", "ЖҚС"),
            ("railway_m", "т/ж"),
            ("power_line_m", "ЭЖЖ"),
        ]
    )
    lines = [
        "",
        (
            "📍 <b>Расстояния по открытым данным</b>"
            if language == "ru"
            else "📍 <b>Ашық деректер бойынша қашықтықтар</b>"
        ),
        (
            f"Координаты: {latitude:.6f}, {longitude:.6f}"
            if language == "ru"
            else f"Координаттар: {latitude:.6f}, {longitude:.6f}"
        ),
    ]
    for field_name, label in labels:
        value = getattr(metrics, field_name, None)
        if value is not None:
            lines.append(f"• {label}: {_short_distance(value)}")
    if len(lines) == 3:
        lines.append(
            "Ориентиры рядом не найдены в сохраненных открытых данных."
            if language == "ru"
            else "Сақталған ашық деректерде жақын бағдарлар табылмады."
        )
    return "\n".join(lines)


def _safe_text(value: object) -> str:
    return escape(str(value or "-"))


def _subscription_label(subscription: object, language: str) -> str:
    parts = []
    region = getattr(subscription, "region", None)
    district = getattr(subscription, "district", None)
    locality = getattr(subscription, "locality", None)
    purpose_query = getattr(subscription, "purpose_query", None)
    min_price = getattr(subscription, "min_price_kzt", None)
    max_price = getattr(subscription, "max_price_kzt", None)
    min_area = getattr(subscription, "min_area_ha", None)
    max_area = getattr(subscription, "max_area_ha", None)
    parts.append(region or at(language, "all_regions"))
    if district:
        parts.append(str(district))
    if locality:
        parts.append(str(locality))
    if purpose_query:
        parts.append(str(purpose_query))
    if min_price is not None or max_price is not None:
        if min_price is not None and max_price is not None:
            parts.append(f"{_short_money(min_price)}–{_short_money(max_price)}")
        elif min_price is not None:
            parts.append(f"от {_short_money(min_price)}")
        else:
            parts.append(f"до {_short_money(max_price)}")
    if min_area is not None or max_area is not None:
        if min_area is not None and max_area is not None:
            parts.append(f"{min_area:g}–{max_area:g} га")
        elif min_area is not None:
            parts.append(f"от {min_area:g} га")
        else:
            parts.append(f"до {max_area:g} га")
    return " · ".join(parts)


@auction_router.callback_query(F.data.startswith("auction:history:"))
async def auction_history(callback: CallbackQuery, state: FSMContext) -> None:
    lot_id = (callback.data or "").rsplit(":", 1)[-1]
    data = await state.get_data()
    language = _language(data)
    telegram_user_id = str(callback.from_user.id)
    with SessionLocal() as session:
        if not can_view_auction_lot(session, telegram_user_id, lot_id):
            await callback.answer()
            if callback.message:
                await _show_auction_paywall(callback.message, language)
            return
        lot = get_auction_lot(session, lot_id)
        history = auction_lot_history(session, lot_id)[:10]
        summary = (
            auction_lot_publication_history(
                session,
                cadastre_number=lot.cadastre_number,
            )
            if lot and lot.cadastre_number
            else None
        )
    if lot is None:
        await callback.answer(at(language, "lot_missing"), show_alert=True)
        return
    title = "📜 <b>Сауда тарихы</b>" if language == "kz" else "📜 <b>История торгов</b>"
    if not history:
        body = (
            "Бұл лот бойынша өзгеріс тарихы әлі жиналмаған."
            if language == "kz"
            else "По этому лоту история пока не накоплена."
        )
    else:
        summary_lines = []
        if summary is not None:
            summary_lines = [
                f"Связанных лотов: {summary.lot_count}",
                f"Публикаций: {summary.publication_count}",
                f"Несостоявшихся: {summary.failed_count}",
                f"Первая стартовая цена: {_short_money(summary.first_start_price_kzt)}",
                f"Последняя стартовая цена: {_short_money(summary.last_start_price_kzt)}",
                f"Изменение цены: {_short_money(summary.start_price_change_kzt)}",
                "",
            ]
        rows = []
        for index, item in enumerate(history, start=1):
            rows.append(
                "\n".join(
                    [
                        f"<b>{index}.</b> {_short_datetime(item.observed_at)}",
                        f"Статус: {_safe_text(item.status)}",
                        f"Старт: {_short_money(item.start_price_kzt)}",
                        f"Итог: {_short_money(item.sale_price_kzt)}",
                        f"Торги: {_short_datetime(item.auction_starts_at)}",
                    ]
                )
            )
        body = "\n\n".join(rows)
        if summary_lines:
            body = "\n".join(summary_lines) + body
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"{title}\n\n{body}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=at(language, "back"),
                            callback_data=f"auction:lot:{lot.id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=at(language, "back_main"),
                            callback_data="catalog:home",
                        )
                    ],
                ]
            ),
        )


@auction_router.callback_query(F.data.startswith("auction:changes:"))
async def auction_changes(callback: CallbackQuery, state: FSMContext) -> None:
    lot_id = (callback.data or "").rsplit(":", 1)[-1]
    data = await state.get_data()
    language = _language(data)
    telegram_user_id = str(callback.from_user.id)
    with SessionLocal() as session:
        if not can_view_auction_lot(session, telegram_user_id, lot_id):
            await callback.answer()
            if callback.message:
                await _show_auction_paywall(callback.message, language)
            return
        lot = get_auction_lot(session, lot_id)
        changes = auction_lot_changes(session, lot_id)[:12]
    if lot is None:
        await callback.answer(at(language, "lot_missing"), show_alert=True)
        return
    title = "🧾 <b>Өзгерістер</b>" if language == "kz" else "🧾 <b>Что менялось</b>"
    if not changes:
        body = (
            "Бұл лот бойынша өзгерістер әлі тіркелмеген."
            if language == "kz"
            else "По этому лоту изменения пока не зафиксированы."
        )
    else:
        body = "\n\n".join(
            [
                "\n".join(
                    [
                        f"<b>{index}.</b> {_short_datetime(item.changed_at)}",
                        f"{_safe_text(item.field_name)}:",
                        f"было: {_safe_text(item.old_value)}",
                        f"стало: {_safe_text(item.new_value)}",
                    ]
                )
                for index, item in enumerate(changes, start=1)
            ]
        )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"{title}\n\n{body}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=at(language, "back"),
                            callback_data=f"auction:lot:{lot.id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=at(language, "back_main"),
                            callback_data="catalog:home",
                        )
                    ],
                ]
            ),
        )


@auction_router.callback_query(F.data.startswith("auction:favorite:"))
async def auction_toggle_favorite(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    language = _language(data)
    lot_id = (callback.data or "").rsplit(":", 1)[-1]
    with SessionLocal() as session:
        if not has_auction_paid_access(session, str(callback.from_user.id)):
            await callback.answer()
            if callback.message:
                await _show_auction_paywall(callback.message, language)
            return
        added = toggle_favorite(session, str(callback.from_user.id), lot_id)
    _track_auction_event(
        "auction_favorite_toggled",
        telegram_user_id=str(callback.from_user.id),
        telegram_chat_id=str(
            callback.message.chat.id if callback.message else callback.from_user.id
        ),
        language=language,
        funnel_session_id=data.get("funnel_session_id"),
        metadata={"lot_id": lot_id, "added": added},
    )
    await callback.answer(
        at(language, "favorite_added" if added else "favorite_removed"),
        show_alert=False,
    )
    if callback.message:
        await _show_lot_detail(
            callback.message,
            state,
            telegram_user_id=str(callback.from_user.id),
            lot_id=lot_id,
        )


@auction_router.callback_query(F.data == "auction:favorites")
async def auction_favorites(callback: CallbackQuery, state: FSMContext) -> None:
    language = _language(await state.get_data())
    with SessionLocal() as session:
        paid = has_auction_paid_access(session, str(callback.from_user.id))
        access = get_auction_access(session, str(callback.from_user.id))
        lots = list_favorites(session, str(callback.from_user.id))
        if not paid:
            lots = [
                lot
                for lot in lots
                if access is not None and lot.id == access.free_lot_id
            ]
    if not paid and not lots:
        await callback.answer()
        if callback.message:
            await _show_auction_paywall(callback.message, language)
        return
    rows = [
        [
            InlineKeyboardButton(
                text=_lot_button(lot),
                callback_data=f"auction:lot:{lot.id}",
            )
        ]
        for lot in lots[:20]
    ]
    if paid and len(lots) >= 2:
        rows.append(
            [
                InlineKeyboardButton(
                    text=at(language, "compare"),
                    callback_data="auction:compare",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=at(language, "back"), callback_data="auction:menu")])
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            (
                f"⭐ <b>{at(language, 'favorites')}</b>\n\n"
                f"{len(lots)}"
                if lots
                else at(language, "no_favorites")
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@auction_router.callback_query(F.data == "auction:compare")
async def auction_compare(callback: CallbackQuery, state: FSMContext) -> None:
    language = _language(await state.get_data())
    with SessionLocal() as session:
        if not has_auction_paid_access(session, str(callback.from_user.id)):
            await callback.answer()
            if callback.message:
                await _show_auction_paywall(callback.message, language)
            return
        lots = list_favorites(session, str(callback.from_user.id))[:10]
        metrics_by_lot = {lot.id: auction_lot_metrics(session, lot) for lot in lots}
        geo_by_lot = {lot.id: auction_lot_geo_metrics(lot) for lot in lots}
    lines = [at(language, "compare_title")]
    for index, lot in enumerate(lots, start=1):
        metrics = metrics_by_lot[lot.id]
        geo = geo_by_lot[lot.id]
        road_distance = _short_distance(getattr(geo, "road_m", None))
        if language == "kz":
            lines.extend(
                [
                    "",
                    f"<b>{index}. Лот №{_safe_text(lot.auction_number or lot.source_lot_id)}</b>",
                    f"📍 {_safe_text(lot.district or lot.region or '-')}",
                    f"📐 {_safe_text(lot.area_ha or '-')} га",
                    f"💰 {_safe_text(_short_money(lot.start_price_kzt))}",
                    f"💠 Сотық: {_safe_text(_short_money(metrics.price_per_sotka))}",
                    f"🛣 Жол: {_safe_text(road_distance)}",
                    f"⭐ {metrics.rating}/100",
                    f"📄 {metrics.document_count}",
                    f"📌 {_safe_text(lot.status or '-')}",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    f"<b>{index}. Лот №{_safe_text(lot.auction_number or lot.source_lot_id)}</b>",
                    f"📍 {_safe_text(lot.district or lot.region or '-')}",
                    f"📐 {_safe_text(lot.area_ha or '-')} га",
                    f"💰 {_safe_text(_short_money(lot.start_price_kzt))}",
                    f"💠 Сотка: {_safe_text(_short_money(metrics.price_per_sotka))}",
                    f"🛣 Дорога: {_safe_text(road_distance)}",
                    f"⭐ {metrics.rating}/100",
                    f"📄 Документов: {metrics.document_count}",
                    f"📌 {_safe_text(lot.status or '-')}",
                ]
            )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=at(language, "back"),
                            callback_data="auction:favorites",
                        )
                    ]
                ]
            ),
        )


@auction_router.callback_query(F.data == "auction:subscribe")
async def auction_subscribe(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    language = _language(data)
    filters = _filters_from_data(data)
    with SessionLocal() as session:
        if not has_auction_paid_access(session, str(callback.from_user.id)):
            await callback.answer()
            if callback.message:
                await _show_auction_paywall(callback.message, language)
            return
        create_subscription(
            session,
            telegram_user_id=str(callback.from_user.id),
            telegram_chat_id=str(
                callback.message.chat.id if callback.message else callback.from_user.id
            ),
            language=language,
            filters=filters,
        )
    _track_auction_event(
        "auction_subscription_created",
        telegram_user_id=str(callback.from_user.id),
        telegram_chat_id=str(
            callback.message.chat.id if callback.message else callback.from_user.id
        ),
        language=language,
        funnel_session_id=data.get("funnel_session_id"),
        metadata=_filters_dict(filters),
    )
    await callback.answer(at(language, "subscribed"), show_alert=True)


@auction_router.callback_query(F.data == "auction:subscriptions")
async def auction_subscriptions(callback: CallbackQuery, state: FSMContext) -> None:
    language = _language(await state.get_data())
    with SessionLocal() as session:
        paid = has_auction_paid_access(session, str(callback.from_user.id))
    if not paid:
        await callback.answer()
        if callback.message:
            await _show_auction_paywall(callback.message, language)
        return
    await callback.answer()
    if callback.message:
        await _show_subscriptions(
            callback.message,
            state,
            telegram_user_id=str(callback.from_user.id),
        )


async def _show_subscriptions(
    message: Message,
    state: FSMContext,
    *,
    telegram_user_id: str,
) -> None:
    language = _language(await state.get_data())
    with SessionLocal() as session:
        subscriptions = [
            item
            for item in list_subscriptions(session, telegram_user_id)
            if item.active
        ]
    lines = [f"🔔 <b>{at(language, 'subscriptions')}</b>"]
    rows = []
    for subscription in subscriptions:
        label = _subscription_label(subscription, language)
        lines.append(f"\n• {label}")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{at(language, 'disable')}: {label}"[:64],
                    callback_data=f"auction:unsubscribe:{subscription.id}",
                )
            ]
        )
    if not subscriptions:
        lines = [at(language, "no_subscriptions")]
    rows.append([InlineKeyboardButton(text=at(language, "back"), callback_data="auction:menu")])
    await message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@auction_router.callback_query(F.data.startswith("auction:unsubscribe:"))
async def auction_unsubscribe(callback: CallbackQuery, state: FSMContext) -> None:
    language = _language(await state.get_data())
    try:
        subscription_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    with SessionLocal() as session:
        disable_subscription(session, str(callback.from_user.id), subscription_id)
    await callback.answer(at(language, "disabled"))
    if callback.message:
        await _show_subscriptions(
            callback.message,
            state,
            telegram_user_id=str(callback.from_user.id),
        )


async def _show_auction_payment(
    message: Message,
    state: FSMContext,
    *,
    refresh: bool,
) -> None:
    data = await state.get_data()
    language = _language(data)
    telegram_user_id = str(data.get("telegram_user_id") or message.chat.id)
    telegram_chat_id = str(data.get("telegram_chat_id") or message.chat.id)
    try:
        with SessionLocal() as session:
            access = (
                refresh_auction_payment(
                    session,
                    telegram_user_id=telegram_user_id,
                    telegram_chat_id=telegram_chat_id,
                    language=language,
                )
                if refresh
                else start_auction_payment(
                    session,
                    telegram_user_id=telegram_user_id,
                    telegram_chat_id=telegram_chat_id,
                    language=language,
                )
            )
            paid_access = has_auction_paid_access(session, telegram_user_id)
            pending_invoice = None
            if (
                not paid_access
                and access.payment_provider == "apipay"
                and access.payment_provider_url
                and not access.payment_provider_invoice_id
            ):
                pending_invoice = find_pending_platform_invoice(
                    session,
                    telegram_user_id,
                    exclude_auction_access_id=access.id,
                )
    except Exception:
        await message.edit_text(
            at(language, "payment_unavailable"),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=at(language, "back"),
                            callback_data="auction:menu",
                        )
                    ]
                ]
            ),
        )
        return
    if paid_access:
        await show_auction_menu(
            message,
            state,
            telegram_user_id=telegram_user_id,
        )
        return
    _track_auction_event(
        "auction_invoice_created",
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        language=language,
        funnel_session_id=data.get("funnel_session_id"),
        metadata={
            "amount_kzt": access.payment_amount_kzt,
            "invoice_id": access.payment_provider_invoice_id,
        },
    )
    rows = []
    refresh_callback = "auction:pay:refresh"
    if access.payment_provider_url:
        if (
            pending_invoice is not None
            and pending_invoice.source == "search"
            and pending_invoice.payment_provider_invoice_id
        ):
            refresh_callback = (
                f"pay:refresh:{pending_invoice.object_id}:"
                f"{pending_invoice.payment_provider_invoice_id}"
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text=at(language, "pay"),
                    url=access.payment_provider_url,
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=at(language, "refresh_payment"),
                    callback_data=refresh_callback,
                )
            ],
            [
                InlineKeyboardButton(
                    text=at(language, "back"),
                    callback_data="auction:menu",
                )
            ],
        ]
    )
    await message.edit_text(
        at(language, "payment"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@auction_router.callback_query(F.data == "auction:pay")
async def auction_pay(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(
        telegram_user_id=str(callback.from_user.id),
        telegram_chat_id=str(
            callback.message.chat.id if callback.message else callback.from_user.id
        ),
    )
    _track_auction_event(
        "auction_payment_clicked",
        telegram_user_id=str(callback.from_user.id),
        telegram_chat_id=str(
            callback.message.chat.id if callback.message else callback.from_user.id
        ),
        language=_language(data),
        funnel_session_id=data.get("funnel_session_id"),
        metadata={"amount_kzt": settings.platform_access_price_kzt},
    )
    await callback.answer()
    if callback.message:
        await _show_auction_payment(callback.message, state, refresh=False)


@auction_router.callback_query(F.data == "auction:pay:refresh")
async def auction_pay_refresh(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(
        telegram_user_id=str(callback.from_user.id),
        telegram_chat_id=str(
            callback.message.chat.id if callback.message else callback.from_user.id
        ),
    )
    await callback.answer()
    if callback.message:
        await _show_auction_payment(callback.message, state, refresh=True)


@auction_router.callback_query(F.data == "auction:noop")
async def auction_noop(callback: CallbackQuery) -> None:
    await callback.answer()
