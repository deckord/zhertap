import asyncio
import logging
import math
import re
import uuid
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.analytics import track_funnel_event
from app.config import settings
from app.db import SessionLocal, init_db
from app.feedback import (
    feedback_start_text,
    feedback_thanks_text,
    has_pending_feedback_request,
    record_client_feedback,
)
from app.funnel import (
    client_t as t,
)
from app.funnel import (
    format_price,
    funnel_v2_enabled,
    group_message,
    progress_message,
    welcome_message,
)
from app.i18n import kazakh_region_label, normalize_language
from app.models import SearchRequest
from app.providers.egkn import EgknProvider, normalize_name
from app.purposes import (
    FIELD,
    GARDENING,
    GARDENING_ALLOWED_AREAS_HA,
    HOUSEHOLD,
    IRRIGATED,
    LPH,
    LPH_NEW,
    NON_IRRIGATED,
    allotment_label,
    irrigation_label,
    normalize_purpose,
    purpose_label,
    purpose_sotok,
)
from app.schemas import ALL_DISTRICTS, SearchCreate
from app.services import (
    accept_urban_plan_override,
    approve_free_preview,
    claim_payment,
    confirm_payment,
    create_next_batch,
    create_search,
    dispatch_search,
    has_paid_access,
    refresh_apipay_payment,
    reject_free_preview,
    reject_payment,
    retry_failed_search,
    start_payment,
)
from app.web import consume_telegram_link_token

group_router = Router(name="groups")
router = Router(name="private")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
logger = logging.getLogger(__name__)
catalog_provider = EgknProvider()
SETTLEMENTS_PER_PAGE = 8
TERMS_VERSION = "2026-07-27-v14"
PUBLIC_WEB_SITE_URL = "https://zhertap.kz"


class SearchForm(StatesGroup):
    choosing_language = State()
    choosing_terms = State()
    choosing_purpose = State()
    choosing_lph_mode = State()
    choosing_allotment = State()
    choosing_irrigation = State()
    choosing_region = State()
    choosing_district = State()
    choosing_settlement = State()
    choosing_area = State()
    waiting_confirmation = State()


class FeedbackForm(StatesGroup):
    waiting_message = State()


def track_bot_event(
    event_name: str,
    *,
    user_id: int | str | None = None,
    chat_id: int | str | None = None,
    request_id: str | None = None,
    funnel_session_id: str | None = None,
    language: str = "ru",
    metadata: dict | None = None,
) -> None:
    with SessionLocal() as session:
        track_funnel_event(
            session,
            event_name,
            telegram_user_id=str(user_id) if user_id is not None else None,
            telegram_chat_id=str(chat_id) if chat_id is not None else None,
            request_id=request_id,
            funnel_session_id=funnel_session_id,
            language=normalize_language(language),
            metadata=metadata,
        )


@group_router.message(CommandStart(), F.chat.type.in_({"group", "supergroup"}))
async def group_start(message: Message, bot: Bot) -> None:
    bot_user = await bot.get_me()
    text, button_text = group_message()
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button_text,
                        url=f"https://t.me/{bot_user.username}?start=group",
                    )
                ]
            ]
        ),
    )


def is_payment_admin(user_id: int, chat_id: int | None) -> bool:
    allowed = {item.strip() for item in settings.telegram_admin_user_ids.split(",") if item.strip()}
    if allowed:
        return str(user_id) in allowed
    return (
        chat_id is not None
        and str(chat_id) == settings.telegram_admin_chat_id
        and str(user_id) == settings.telegram_admin_chat_id
    )


def callback_request_id(data: str | None, prefix: str) -> str:
    request_id = (data or "").removeprefix(prefix)
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", request_id):
        raise ValueError("Некорректный номер заявки")
    return request_id


def indexed_choice(rows: list[dict], index_text: str) -> dict:
    try:
        return rows[int(index_text)]
    except (ValueError, IndexError, TypeError) as exc:
        raise ValueError("Меню устарело. Отправьте /start и выберите параметры заново.") from exc


def district_choices(raw_rows: list, language: str) -> list[dict]:
    return [
        {
            "id": row.id,
            "value": (
                f"{row.name} район" if row.display_name.lower().startswith("р-н") else row.name
            ),
            "label": (
                row.display_name_kz
                if language == "kz" and row.display_name_kz
                else row.display_name
            ),
        }
        for row in raw_rows
    ]


def one_column_keyboard(rows: list[dict], prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=row["label"], callback_data=f"{prefix}:{index}")]
            for index, row in enumerate(rows)
        ]
    )


def settlement_keyboard(rows: list[dict], page: int, language: str = "ru") -> InlineKeyboardMarkup:
    page_count = max(1, math.ceil(len(rows) / SETTLEMENTS_PER_PAGE))
    page = min(max(page, 0), page_count - 1)
    start = page * SETTLEMENTS_PER_PAGE
    buttons = [
        [InlineKeyboardButton(text=row["label"], callback_data=f"catalog:settlement:{index}")]
        for index, row in enumerate(rows[start : start + SETTLEMENTS_PER_PAGE], start=start)
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="←", callback_data=f"catalog:settlement-page:{page - 1}")
        )
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="catalog:noop")
    )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(text="→", callback_data=f"catalog:settlement-page:{page + 1}")
        )
    buttons.append(navigation)
    buttons.append(
        [
            InlineKeyboardButton(
                text=t(language, "back_districts"), callback_data="catalog:back:districts"
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=t(language, "main_regions"), callback_data="catalog:back:regions"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def web_site_url() -> str:
    base_url = settings.app_base_url.strip().rstrip("/")
    if not base_url or "localhost" in base_url or "127.0.0.1" in base_url:
        return PUBLIC_WEB_SITE_URL
    return base_url


def purpose_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    website_button = [
        InlineKeyboardButton(text=t(language, "web_site_button"), url=web_site_url())
    ]
    if funnel_v2_enabled():
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t(language, "lph"), callback_data="purpose:lph")],
                [
                    InlineKeyboardButton(
                        text=t(language, "gardening"), callback_data="purpose:gardening"
                    )
                ],
                website_button,
                [
                    InlineKeyboardButton(
                        text=t(language, "back"),
                        callback_data="catalog:back:terms",
                    )
                ],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "lph"), callback_data="purpose:lph")],
            [InlineKeyboardButton(text=t(language, "lph_new"), callback_data="purpose:lph-new")],
            [
                InlineKeyboardButton(
                    text=t(language, "gardening"), callback_data="purpose:gardening"
                )
            ],
            website_button,
            [InlineKeyboardButton(text=t(language, "back"), callback_data="catalog:back:terms")],
        ]
    )


def service_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(language, "service_land_search"),
                    callback_data="service:land-search",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "service_auctions"),
                    callback_data="service:auctions",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Кері байланыс" if language == "kz" else "Обратная связь",
                    callback_data="feedback:start",
                )
            ],
        ]
    )


def lph_mode_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if settings.enable_standard_lph_10:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(language, "lph_standard"),
                    callback_data="lph-mode:standard",
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=t(language, "lph_extended"),
                    callback_data="lph-mode:extended",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "back"),
                    callback_data="catalog:back:purpose",
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def allotment_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(language, "household_allotment"),
                    callback_data="allotment:household",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "field_allotment"), callback_data="allotment:field"
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        t(language, "back_lph_mode")
                        if funnel_v2_enabled() and settings.enable_standard_lph_10
                        else t(language, "back_purpose")
                    ),
                    callback_data=(
                        "catalog:back:lph-mode"
                        if funnel_v2_enabled() and settings.enable_standard_lph_10
                        else "catalog:back:purpose"
                    ),
                )
            ],
        ]
    )


def irrigation_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(language, "irrigated"), callback_data="irrigation:irrigated"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "non_irrigated"),
                    callback_data="irrigation:non-irrigated",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "back_allotment"), callback_data="catalog:back:allotment"
                )
            ],
        ]
    )


def lph_size_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(language, "lph_15_sotok"), callback_data="lph-size:15"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "lph_25_sotok"), callback_data="lph-size:25"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "back_purpose"), callback_data="catalog:back:purpose"
                )
            ],
        ]
    )


def gardening_size_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(language, "garden_12_sotok"), callback_data="garden-size:12"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "garden_6_sotok"), callback_data="garden-size:6"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "back_purpose"), callback_data="catalog:back:purpose"
                )
            ],
        ]
    )


def area_keyboard(
    language: str = "ru",
    purpose: str = LPH,
    irrigation_type: str | None = None,
    *,
    all_districts: bool = False,
) -> InlineKeyboardMarkup:
    if normalize_purpose(purpose) == GARDENING:
        area_rows = [
            [
                InlineKeyboardButton(
                    text=t(language, "garden_12_sotok"),
                    callback_data="catalog:area:12",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "garden_6_sotok"),
                    callback_data="catalog:area:6",
                )
            ],
        ]
    else:
        sotok = purpose_sotok(purpose, irrigation_type)
        area_text = f"{sotok} {'сотық' if language == 'kz' else 'соток'}"
        area_rows = [
            [
                InlineKeyboardButton(
                    text=area_text,
                    callback_data=f"catalog:area:{sotok}",
                )
            ]
        ]
    return InlineKeyboardMarkup(
        inline_keyboard=area_rows
        + [
            [
                InlineKeyboardButton(
                    text=(
                        t(language, "back_districts")
                        if all_districts
                        else t(language, "back_settlements")
                    ),
                    callback_data=(
                        "catalog:back:districts" if all_districts else "catalog:back:settlements"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "main_regions"),
                    callback_data="catalog:back:regions",
                )
            ],
        ]
    )


def provider_details(language: str = "ru") -> str:
    name = settings.service_provider_name.strip() or settings.payment_recipient.strip()
    values = [
        ("Қызмет көрсетуші" if language == "kz" else "Исполнитель", name),
        ("Мәртебесі" if language == "kz" else "Статус", settings.service_provider_status),
        ("ЖСН/БСН" if language == "kz" else "ИИН/БИН", settings.service_provider_id),
        (
            "Өтініштерге арналған мекенжай" if language == "kz" else "Адрес для обращений",
            settings.service_provider_address,
        ),
        ("Байланыс" if language == "kz" else "Контакт", settings.service_provider_contact),
    ]
    return "\n".join(f"{label}: {value.strip()}" for label, value in values if value.strip())


def terms_text(language: str = "ru") -> str:
    price = f"{settings.platform_access_price_kzt:,}".replace(",", " ")
    provider = provider_details(language)
    if settings.free_preview_enabled:
        if settings.paid_search_enabled:
            access_ru = (
                "В бесплатном режиме бот показывает найденные варианты и объясняет, "
                "какую проверку выполнила система. Точные координаты, кадастровый "
                "ориентир, карта, ЕГКН и аукционные действия открываются после "
                f"оплаты {price} ₸/мес. "
                "Оплата активирует единый доступ к поиску участков и земельным "
                "аукционам на 1 месяц. Результаты выдаются пакетами до 10 новых вариантов; ранее "
                "показанные варианты исключаются."
            )
            access_kz = (
                "Тегін режимде бот табылған нұсқаларды көрсетіп, жүйе қандай тексеру "
                "жасағанын түсіндіреді. Нақты координаттар, кадастрлық бағдар, карта, "
                f"ЕГКН және аукцион әрекеттері {price} ₸/ай төлемнен кейін ашылады. "
                "Төлем жер іздеу мен жер аукциондарына 1 айлық бірыңғай қолжетімділік "
                "береді. Нәтижелер бұрын көрсетілген нұсқаларды қайталамай, 10 жаңа "
                "нұсқаға дейінгі топтамалармен беріледі."
            )
        else:
            access_ru = (
                "В бесплатном режиме бот показывает найденные варианты без точных "
                "координат, карты, ЕГКН и кадастрового ориентира. Прием платных отчетов "
                "временно отключен."
            )
            access_kz = (
                "Тегін режимде бот табылған нұсқаларды нақты координаттарсыз, картасыз, "
                "ЕГКН-сыз және кадастрлық бағдарсыз көрсетеді. Ақылы есептерді қабылдау "
                "уақытша өшірілген."
            )
    else:
        access_ru = (
            f"Оплата {price} ₸/мес активирует для Telegram user ID единый "
            "доступ к поиску участков и земельным аукционам на 1 месяц."
        )
        access_kz = (
            f"{price} ₸/ай төлем Telegram user ID үшін жер іздеу мен жер "
            "аукциондарына 1 айлық бірыңғай қолжетімділікті ашады."
        )
    if normalize_language(language) == "kz":
        return (
            "Land Scout Kazakhstan ботына қош келдіңіз.\n\n"
            "Бұл мемлекеттік органға, әкімдікке, «Азаматтарға арналған үкіметке» немесе "
            "ЖМБМК операторына қатысы жоқ тәуелсіз жеке ақпараттық сервис.\n\n"
            "Қызметтің мәні: ЖМБМК ашық қабатындағы тіркелген учаскелер арасындағы "
            "геометриялық аралықтарды бағдарламалық талдау, OSM ашық деректері бойынша "
            "жолдар мен нысандарды алып тастау және жүйеге жүктелген ресми геобайланыстырылған "
            "бас жоспар/ЕЖЖ қабатымен салыстыру.\n\n"
            "Іздеу режимдері: елді мекен шегіндегі ЖҚШ үшін 10 сотық алдын ала шаршы "
            "немесе бағбандық үшін заңдағы тегін нормаға сәйкес 12 сотық шаршы. «ЖҚШ "
            "(жаңа іздеу)» режимі үй іргесіндегі/далалық телімді және суармалы жерге "
            "15 сотық немесе суарылмайтын жерге 25 сотық есептік профильді таңдауға мүмкіндік "
            "береді. 15/25 сотық нормасы екі телімді қоса алғанда бүкіл ЖҚШ-ға қатысты; "
            "бот телім түрін және суарудың болуын растамайды.\n\n"
            + access_kz
            + " Есепке координаттары мен "
            "көршілес кадастрлық нөмірлері-бағдарлары бар 10 нұсқаға дейін кіреді. "
            "Табылған нақты саны төлемге дейін көрсетіледі; бірде-бір нұсқа табылмаса, "
            "төлем сұралмайды.\n\n"
            "Маңызды шарттар:\n"
            "1. Есеп мемлекеттік қызмет, өтініш, жерге орналастыру жобасы, кадастрлық, "
            "геодезиялық немесе заңдық қорытынды болып табылмайды.\n"
            "2. Есептік аралық жердің заңды түрде бос, мемлекет меншігінде екенін немесе "
            "таңдалған мақсат үшін берілетінін растамайды. Көрші кадастрлық нөмір бос "
            "учаскенің нөмірі "
            "емес және тек орналасу бағдары ретінде беріледі.\n"
            "3. Құқықтық мәртебені, шекараны, ауыртпалықтарды, санитарлық және қорғау "
            "аймақтарын, инженерлік желілерді, жердегі нысандарды және беру мүмкіндігін "
            "әкімдік пен уәкілетті органдар тексереді. Бот азаматтықты, бұрын тегін жер "
            "алған-алмағанын және өтініш берушінің жеке құқығын тексермейді. Жерді орнында "
            "қарау қажет.\n"
            "4. Жүйе алдымен ресми геобайланыстырылған бас жоспар/ЕЖЖ қабатын тексереді. "
            "Егер ондай қабат болмаса, координаттар тек пайдаланушы жеке батырмамен бас "
            "жоспарсыз алдын ала нәтижені сұрап, тәуекелді қабылдағаннан кейін берілуі мүмкін. "
            "Мұндай есеп ЖМБМК және OSM деректерімен ғана шектеледі. Сыртқы спутниктік карта "
            "автоматты түрде талданбайды.\n"
            "5. Ашық деректер толық болмауы немесе кешігіп жаңартылуы мүмкін. Төлем жер "
            "алу немесе оң шешім үшін емес, автоматтандырылған талдау мен есеп үшін алынады.\n"
            "6. Төлем расталғаннан кейін есеп техникалық себеппен жеткізілмесе, клиент "
            "қайта жіберуді немесе төлемді қайтаруды сұрай алады. Талап заңда көзделген "
            "құқықтарды шектемейді.\n\n"
            + (provider + "\n\n" if provider else "")
            + "Дербес деректерді өңдеу туралы толық ақпарат: /privacy. Қызмет пен талаптар "
            "туралы мәлімет: /offer.\n\n"
            "«Шарттарды қабылдап, жалғастыру» батырмасын басу арқылы сіз осы шарттарды және "
            "дербес деректер саясатын оқып, қабылдағаныңызды растайсыз."
        )
    return (
        "Добро пожаловать в Land Scout Kazakhstan.\n\n"
        "Это независимый частный информационный сервис, не связанный с государственными "
        "органами, акиматами, «Правительством для граждан» или оператором ЕГКН.\n\n"
        "Предмет услуги: программный анализ промежутков между зарегистрированными "
        "участками публичного слоя ЕГКН, исключение дорог и объектов по открытым данным "
        "OSM и сравнение с загруженным официальным геопривязанным слоем генплана/ПДП.\n\n"
        "Режимы поиска: предварительный квадрат 10 соток под ЛПХ в границах населенного "
        "пункта либо квадрат 12 соток под садоводство. Режим «ЛПХ(новый поиск)» позволяет "
        "выбрать приусадебный/полевой надел и расчетный профиль 15 соток для орошаемой или "
        "25 соток для неорошаемой земли. Норма 15/25 соток относится ко всему ЛПХ, включая "
        "оба надела; бот не подтверждает вид надела и фактическое наличие орошения.\n\n"
        + access_ru
        + " В отчет входит до 10 расчетных вариантов "
        "с координатами и соседними кадастровыми номерами-ориентирами. Фактическое "
        "количество показывается до оплаты; если не найдено ни одного варианта, оплата "
        "не запрашивается.\n\n"
        "Важные условия:\n"
        "1. Отчет не является государственной услугой, заявлением, землеустроительным "
        "проектом, кадастровым, геодезическим или юридическим заключением.\n"
        "2. Расчетный промежуток не подтверждает, что земля юридически свободна, находится "
        "в государственной собственности или может быть предоставлена для выбранного "
        "назначения. Соседний "
        "кадастровый номер не является номером свободного участка и служит только ориентиром.\n"
        "3. Правовой статус, границы, обременения, санитарные и охранные зоны, инженерные "
        "сети, фактические объекты и возможность предоставления проверяют акимат и "
        "уполномоченные органы. Бот не проверяет гражданство, прежнее бесплатное получение "
        "земли и личное право заявителя. Необходим осмотр на местности.\n"
        "4. Система сначала проверяет официальный геопривязанный слой генплана/ПДП. Если "
        "такого слоя нет, координаты могут быть выданы только после отдельного нажатия "
        "пользователем кнопки получения предварительного результата без генплана и принятия "
        "этого риска. Такой отчет ограничен данными ЕГКН и OSM. Внешняя спутниковая карта "
        "автоматически не анализируется.\n"
        "5. Публичные данные могут быть неполными или обновляться с задержкой. Оплачивается "
        "автоматизированный анализ и отчет, а не получение земли или положительного решения.\n"
        "6. Если после подтверждения оплаты отчет не доставлен по технической причине, "
        "клиент вправе запросить повторную отправку или возврат оплаты. Это условие не "
        "ограничивает права потребителя, установленные законом.\n\n"
        + (provider + "\n\n" if provider else "")
        + "Политика обработки данных: /privacy. Сведения об услуге и обращениях: /offer.\n\n"
        "Нажимая «Принять условия и продолжить», вы подтверждаете, что прочитали и "
        "принимаете эти условия и политику обработки персональных данных."
    )


def welcome_text(language: str = "ru") -> str:
    if funnel_v2_enabled():
        return welcome_message(language)
    if normalize_language(language) == "kz":
        return (
            "🗺 <b>Land Scout Kazakhstan</b>\n\n"
            "Ботта екі бөлім бар: ЖҚШ/бағбандық үшін ықтимал учаске орнын іздеу "
            "және E-Qazyna жер аукциондары.\n\n"
            "🎁 Тегін: табылған нұсқалар, аудан, қашықтық және қысқа талдау.\n"
            f"🔓 Толық қолжетімділік: {format_price()} ₸.\n"
            "Ол координаттарды, картаны, ЕГКН, құжаттарды, таңдаулыларды және "
            "хабарламаларды ашады.\n\n"
            "⚠️ Нәтиже ақпараттық бағдар болып табылады. Ол жердің берілуіне немесе "
            "аукциондағы жеңіске кепілдік бермейді.\n\n"
            "Құқықтық шарттар: /terms\n"
            "Дербес деректер: /privacy\n"
            "Қызмет шарттары: /offer"
        )
    return (
        "🗺 <b>Land Scout Kazakhstan</b>\n\n"
        "В боте есть два раздела: поиск возможного места под ЛПХ/садоводство "
        "и земельные аукционы E-Qazyna.\n\n"
        "🎁 Бесплатно: найденные варианты, район, расстояния и краткая аналитика.\n"
        f"🔓 Полный доступ: {format_price()} ₸.\n"
        "Он открывает координаты, карту, ЕГКН, документы, избранное и уведомления.\n\n"
        "⚠️ Результат является информационным ориентиром. Он не гарантирует "
        "выдачу земли или победу в аукционе.\n\n"
        "Юридические условия: /terms\n"
        "Обработка данных: /privacy\n"
        "Условия услуги: /offer"
    )


def terms_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "continue"), callback_data="terms:accept")]
        ]
    )


def privacy_text(language: str = "ru") -> str:
    provider = provider_details(language)
    months = settings.data_retention_months
    storage = settings.data_storage_location.strip() or "Республика Казахстан"
    if normalize_language(language) == "kz":
        return (
            "ДЕРБЕС ДЕРЕКТЕРДІ ӨҢДЕУ САЯСАТЫ\n\n"
            + (provider + "\n\n" if provider else "")
            + "Өңделетін деректер: Telegram user ID және chat ID, профильде көрінетін "
            "аты, таңдалған тіл мен іздеу аумағы, өтінім нөмірі, іздеу нәтижелері, "
            "алдын ала көрсетілген нұсқалар саны, хабарламалар мәртебесі, төлем мәртебесі, "
            "келісім нұсқасы мен уақыты. Бот "
            "ЖСН, жеке куәлік көшірмесін немесе банкке кіру деректерін сұрамайды.\n\n"
            "Мақсаты: өтінімді қалыптастыру және орындау, есепті жеткізу, төлемнің "
            "түскенін қолмен растау, өтініштер мен дауларды қарау және сервистің "
            "қауіпсіздігін қамтамасыз ету.\n\n"
            f"Негізгі дерекқордың көрсетілген сақтау орны: {storage}. Telegram хабар "
            "алмасу арнасы ретінде деректерді Қазақстаннан тыс жерде өңдеуі мүмкін. "
            "Деректер сатылмайды және жарнама үшін берілмейді.\n\n"
            f"Жоспарлы сақтау мерзімі: өтінім аяқталғаннан кейін {months} айға дейін, "
            "егер шарттық міндеттемелер, дау немесе заң талабы ұзағырақ сақтауды қажет "
            "етпесе.\n\n"
            "Сіз өз деректеріңіздің көшірмесін, түзетілуін немесе жойылуын сұрай аласыз "
            "және келісімді кері қайтара аласыз. Ол үшін өтінім нөмірімен жоғарыда "
            "көрсетілген байланысқа жазыңыз. Келісімді кері қайтару бұрын заңды түрде "
            "орындалған өңдеуді және орындалмаған міндеттемелерді жоймайды.\n\n"
            "Қолданыстағы шарттар: /terms. Қызмет туралы мәлімет: /offer"
        )
    return (
        "ПОЛИТИКА ОБРАБОТКИ ПЕРСОНАЛЬНЫХ ДАННЫХ\n\n"
        + (provider + "\n\n" if provider else "")
        + "Обрабатываемые данные: Telegram user ID и chat ID, видимое имя профиля, "
        "выбранные язык и территория поиска, номер заявки, результаты, количество "
        "предварительно показанных вариантов, статусы сообщений и оплаты, версия и время "
        "согласия. Бот не запрашивает ИИН, копии "
        "удостоверения личности или данные доступа к банковскому приложению.\n\n"
        "Цели обработки: создание и выполнение заявки, доставка отчета, ручное "
        "подтверждение поступления оплаты, рассмотрение обращений и споров, обеспечение "
        "безопасности сервиса.\n\n"
        f"Заявленное место основной базы данных: {storage}. Telegram используется как "
        "канал сообщений и может обрабатывать данные за пределами Казахстана. Данные "
        "не продаются и не передаются для сторонней рекламы.\n\n"
        f"Плановый срок хранения: до {months} месяцев после завершения заявки, если "
        "договорные обязательства, спор или закон не требуют более длительного хранения.\n\n"
        "Вы можете запросить копию, исправление или удаление своих данных и отозвать "
        "согласие, обратившись по указанному выше контакту с номером заявки. Отзыв не "
        "отменяет уже законно выполненную обработку и неисполненные обязательства.\n\n"
        "Действующие условия: /terms. Сведения об услуге: /offer"
    )


def offer_text(language: str = "ru") -> str:
    provider = provider_details(language)
    price = f"{settings.platform_access_price_kzt:,}".replace(",", " ")
    if normalize_language(language) == "kz":
        access = (
            "Тегін режим: қысқа алдын ала карточкалар. Нақты координаттар, карта, "
            "ЕГКН, құжаттар және аукцион әрекеттері төлемнен кейін ашылады.\n"
            if settings.free_preview_enabled
            else ""
        )
        return (
            "АҚПАРАТТЫҚ ҚЫЗМЕТТІҢ НЕГІЗГІ ШАРТТАРЫ\n\n"
            + (provider + "\n\n" if provider else "Қызмет көрсетушінің деректері бапталмаған.\n\n")
            + "Қызмет: ЖМБМК геометриясын талдау, OSM бойынша жолдар мен нысандарды "
            "алып тастау және бар болса ресми бас жоспар/ЕЖЖ қабатымен салыстыру. Қабат "
            "болмаса, алдын ала нәтиже пайдаланушының жеке келісімімен ғана беріледі.\n"
            + access
            + (
                f"Жер іздеу мен аукциондарға бірыңғай қолжетімділік: "
                f"{price} ₸/ай.\n"
                if settings.paid_search_enabled
                else "Ақылы есептерді қабылдау уақытша өшірілген.\n"
            )
            + "Нәтиже: бір топтамада 10 жаңа нұсқаға дейін; кейінгі топтамалар "
            "қосымша төлемсіз.\n\n"
            "Төлем кемінде бір есептік нұсқа табылғаннан кейін ғана сұралады. Толық есеп "
            "төлемнің түскені қолмен расталғаннан кейін Telegram арқылы жіберіледі.\n\n"
            "Егер төлем расталып, есеп техникалық себеппен жеткізілмесе, клиент қайта "
            "жіберуді немесе төлемді қайтаруды сұрай алады. Өтініште өтінім нөмірін, "
            "төлем күнін және мәселенің сипаттамасын көрсету керек.\n\n"
            "Есеп жердің заңды түрде бос екеніне, мемлекет меншігіне немесе берілуіне "
            "кепілдік бермейді. Толық шарттар: /terms. Дербес деректер: /privacy"
        )
    access = (
        "Бесплатный режим: краткие предварительные карточки. Точные координаты, карта, "
        "ЕГКН, документы и аукционные действия открываются после оплаты.\n"
        if settings.free_preview_enabled
        else ""
    )
    return (
        "ОСНОВНЫЕ УСЛОВИЯ ИНФОРМАЦИОННОЙ УСЛУГИ\n\n"
        + (provider + "\n\n" if provider else "Сведения об исполнителе не настроены.\n\n")
        + "Услуга: анализ геометрии ЕГКН, исключение дорог и объектов по OSM и сравнение "
        "с официальным слоем генплана/ПДП, если он доступен. При отсутствии слоя "
        "предварительный результат выдается только после отдельного согласия пользователя.\n"
        + access
        + (
            f"Единый доступ к поиску участков и земельным аукционам: "
            f"{price} ₸/мес.\n"
            if settings.paid_search_enabled
            else "Прием платных отчетов временно отключен.\n"
        )
        + "Результат: до 10 новых вариантов в одном пакете; следующие пакеты без доплаты.\n\n"
        "Оплата запрашивается только после нахождения хотя бы одного расчетного варианта. "
        "Полный отчет направляется в Telegram после ручного подтверждения поступления.\n\n"
        "Если оплата подтверждена, но отчет не доставлен по технической причине, клиент "
        "может потребовать повторную отправку или возврат оплаты. В обращении необходимо "
        "указать номер заявки, дату оплаты и описание проблемы.\n\n"
        "Отчет не гарантирует юридическую свободу, государственную собственность или "
        "предоставление земли. Полные условия: /terms. Персональные данные: /privacy"
    )


async def catalog_call(function, *args):
    return await asyncio.wait_for(
        asyncio.to_thread(function, *args),
        timeout=settings.egkn_timeout_seconds + 5,
    )


async def reopen_search_catalog(
    callback: CallbackQuery,
    state: FSMContext,
    request_id: str,
    *,
    target: str,
) -> None:
    chat_id = str(callback.message.chat.id) if callback.message else ""
    with SessionLocal() as session:
        request = session.get(SearchRequest, request_id)
        if request is None:
            raise LookupError("Заявка не найдена")
        if (
            request.telegram_user_id != str(callback.from_user.id)
            or request.telegram_chat_id != chat_id
        ):
            raise PermissionError("Эта кнопка относится к другой заявке")
        language = normalize_language(request.language)
        context = {
            "language": language,
            "terms_version": request.terms_version or TERMS_VERSION,
            "terms_text_snapshot": request.terms_text_snapshot or terms_text(language),
            "terms_accepted_at": (
                request.terms_accepted_at.isoformat()
                if request.terms_accepted_at
                else datetime.now(UTC).isoformat()
            ),
            "purpose": request.purpose,
            "allotment_type": request.allotment_type,
            "irrigation_type": request.irrigation_type,
            "telegram_user_id": request.telegram_user_id,
            "telegram_chat_id": request.telegram_chat_id,
            "region": {
                "value": request.region,
                "label": request.region_label or request.region,
            },
        }
        district_value = request.district

    await state.clear()
    await state.update_data(**context)
    await callback.answer()
    if not callback.message:
        return
    if target == "regions":
        await show_regions(callback.message, state, edit=True)
        return
    if target == "districts" or district_value == ALL_DISTRICTS:
        await show_districts(callback.message, state)
        return

    raw_rows = await catalog_call(
        catalog_provider.districts,
        context["region"]["value"],
    )
    rows = district_choices(raw_rows, language)
    district_key = normalize_name(district_value)
    district = next(
        (
            row
            for row in rows
            if district_key == normalize_name(row["value"])
            or district_key in normalize_name(row["value"])
            or normalize_name(row["value"]) in district_key
        ),
        None,
    )
    if district is None:
        await show_districts(callback.message, state)
        return
    await state.update_data(
        districts=rows,
        district=district,
        settlements=None,
        locality=None,
    )
    await show_settlements(callback.message, state)


async def show_catalog_error(
    callback: CallbackQuery,
    error: Exception,
    *,
    retry_data: str,
    back_data: str,
    language: str = "ru",
) -> None:
    logger.error("EGKN catalog request failed: %s", error)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "retry"), callback_data=retry_data)],
            [InlineKeyboardButton(text=t(language, "back"), callback_data=back_data)],
            [
                InlineKeyboardButton(
                    text=t(language, "main_regions"),
                    callback_data="catalog:home",
                )
            ],
        ]
    )
    text = t(
        language,
        "catalog_error",
        error=str(error)[:300] or type(error).__name__,
    )
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except Exception:
            await callback.message.answer(text, reply_markup=keyboard)


async def show_language(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SearchForm.choosing_language)
    await message.answer(
        t("ru", "choose_language"),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Қазақша", callback_data="language:kz")],
                [InlineKeyboardButton(text="Русский", callback_data="language:ru")],
            ]
        ),
    )


async def show_terms(message: Message, state: FSMContext, language: str) -> None:
    await state.set_state(SearchForm.choosing_terms)
    await message.edit_text(
        welcome_text(language),
        reply_markup=terms_keyboard(language),
        parse_mode="HTML",
    )


async def show_purposes(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    await state.set_state(SearchForm.choosing_purpose)
    await message.edit_text(t(language, "choose_purpose"), reply_markup=purpose_keyboard(language))


async def show_services(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    await state.set_state(SearchForm.choosing_purpose)
    await message.edit_text(
        t(language, "choose_service"),
        reply_markup=service_keyboard(language),
        parse_mode="HTML",
    )


async def show_lph_modes(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    await state.set_state(SearchForm.choosing_lph_mode)
    await message.edit_text(
        t(language, "choose_lph_mode"),
        reply_markup=lph_mode_keyboard(language),
    )


async def show_allotments(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    await state.set_state(SearchForm.choosing_allotment)
    await message.edit_text(
        t(language, "choose_allotment"), reply_markup=allotment_keyboard(language)
    )


async def show_irrigation(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    await state.set_state(SearchForm.choosing_irrigation)
    await message.edit_text(
        t(language, "choose_irrigation"), reply_markup=irrigation_keyboard(language)
    )


async def show_lph_size(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    await state.set_state(SearchForm.choosing_irrigation)
    await message.edit_text(
        t(language, "choose_lph_size"), reply_markup=lph_size_keyboard(language)
    )


async def show_gardening_size(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    await state.set_state(SearchForm.choosing_area)
    await message.edit_text(
        t(language, "choose_gardening_size"),
        reply_markup=gardening_size_keyboard(language),
    )


async def show_regions(message: Message, state: FSMContext, *, edit: bool = False) -> None:
    data = await state.get_data()
    language = normalize_language(data.get("language"))
    if edit:
        await message.edit_text(t(language, "loading_regions"))
        loading = message
    else:
        loading = await message.answer(t(language, "loading_regions"))
    raw_rows = await catalog_call(catalog_provider.regions)
    rows = [
        {
            "value": row.get("name") or row.get("nameRu") or "",
            "label": (kazakh_region_label(row) if language == "kz" else row.get("nameRu"))
            or row.get("name")
            or "",
        }
        for row in raw_rows
        if row.get("name") or row.get("nameRu")
    ]
    await state.update_data(regions=rows)
    await state.set_state(SearchForm.choosing_region)
    keyboard_rows = one_column_keyboard(rows, "catalog:region").inline_keyboard
    purpose = normalize_purpose(data.get("purpose"))
    back_callback = (
        "catalog:back:lph-mode"
        if (
            funnel_v2_enabled()
            and settings.enable_standard_lph_10
            and purpose in {LPH, LPH_NEW}
        )
        else "catalog:back:lph-size"
        if funnel_v2_enabled() and purpose == LPH_NEW
        else "catalog:back:garden-size"
        if funnel_v2_enabled() and purpose == GARDENING
        else "catalog:back:purpose"
    )
    keyboard_rows.append(
        [
            InlineKeyboardButton(
                text=t(language, "back_purpose"), callback_data=back_callback
            )
        ]
    )
    await loading.edit_text(
        t(language, "choose_region"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


async def show_districts(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    language = normalize_language(data.get("language"))
    region = data.get("region")
    if not region:
        raise ValueError("Сначала выберите область через /start.")
    await message.edit_text(t(language, "loading_districts"))
    raw_rows = await catalog_call(catalog_provider.districts, region["value"])
    rows = district_choices(raw_rows, language)
    await state.update_data(districts=rows)
    await state.set_state(SearchForm.choosing_district)
    keyboard_rows = one_column_keyboard(rows, "catalog:district").inline_keyboard
    keyboard_rows.insert(
        0,
        [
            InlineKeyboardButton(
                text=t(language, "all_districts"),
                callback_data="catalog:district:all",
            )
        ],
    )
    keyboard_rows.append(
        [
            InlineKeyboardButton(
                text=t(language, "back_regions"), callback_data="catalog:back:regions"
            )
        ]
    )
    await message.edit_text(
        t(language, "choose_district", region=region["label"]),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


async def show_settlements(message: Message, state: FSMContext, *, page: int = 0) -> None:
    data = await state.get_data()
    language = normalize_language(data.get("language"))
    district = data.get("district")
    if not district:
        raise ValueError("Сначала выберите район через /start.")
    rows = data.get("settlements")
    if not rows:
        await message.edit_text(t(language, "loading_settlements"))
        raw_rows = await catalog_call(catalog_provider.settlement_options, district["id"], "ru")
        labels = {row.gid: row.name for row in raw_rows}
        if language == "kz":
            kz_rows = await catalog_call(catalog_provider.settlement_options, district["id"], "kz")
            labels.update({row.gid: row.name for row in kz_rows if row.name})
        raw_rows.sort(key=lambda row: normalize_name(labels.get(row.gid, row.name)))
        rows = [
            {
                "value": row.name,
                "label": f"{labels.get(row.gid, row.name)} · КАТО {row.kato}",
            }
            for row in raw_rows
        ]
        await state.update_data(settlements=rows)
    if not rows:
        locality = {
            "value": None,
            "label": t(language, "district_search_area", district=district["label"]),
        }
        await state.update_data(locality=locality, district_area_only=True)
        await show_area_choice(message, state, locality)
        return
    await state.update_data(district_area_only=False)
    await state.set_state(SearchForm.choosing_settlement)
    await message.edit_text(
        t(language, "choose_settlement", district=district["label"]),
        reply_markup=settlement_keyboard(rows, page, language),
    )


async def show_area_choice(message: Message, state: FSMContext, locality: dict) -> None:
    data = await state.get_data()
    language = normalize_language(data.get("language"))
    purpose = normalize_purpose(data.get("purpose"))
    irrigation_type = data.get("irrigation_type")
    if purpose == GARDENING and data.get("area_ha"):
        sotok = round(float(data["area_ha"]) * 100)
    else:
        sotok = purpose_sotok(purpose, irrigation_type)
    if funnel_v2_enabled():
        region = data["region"]
        district = data["district"]
        terms_version = data.get("terms_version")
        terms_text_snapshot = data.get("terms_text_snapshot")
        terms_accepted_at = data.get("terms_accepted_at")
        if not terms_version or not terms_text_snapshot or not terms_accepted_at:
            raise ValueError(
                "Алдымен шарттарды қабылдаңыз."
                if language == "kz"
                else "Сначала примите условия."
            )
        chat_id = str(data.get("telegram_chat_id") or message.chat.id)
        user_id = str(data.get("telegram_user_id") or message.chat.id)
        parsed = SearchCreate(
            language=language,
            region=region["value"],
            region_label=region["label"],
            district=district["value"],
            district_label=district["label"],
            locality=locality["value"],
            locality_label=locality["label"].split(" · КАТО", 1)[0],
            purpose=purpose,
            allotment_type=data.get("allotment_type"),
            irrigation_type=irrigation_type,
            area_ha=sotok / 100,
            result_limit=10,
            cemetery_buffer_m=0,
            telegram_user_id=user_id,
            telegram_chat_id=chat_id,
            funnel_session_id=data.get("funnel_session_id"),
            terms_version=terms_version,
            terms_text_snapshot=terms_text_snapshot,
            terms_accepted_at=terms_accepted_at,
        )
        await state.update_data(search=parsed.model_dump())
        await state.set_state(SearchForm.waiting_confirmation)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t(language, "send_queue"),
                        callback_data="search:confirm",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=t(language, "choose_again"),
                        callback_data=(
                            "catalog:back:districts"
                            if district["value"] == ALL_DISTRICTS
                            or data.get("district_area_only")
                            else "catalog:back:settlements"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=t(language, "main_regions"),
                        callback_data="search:edit",
                    )
                ],
            ]
        )
        area_notice = (
            "\n\nℹ️ " + t(language, "no_settlements")
            if data.get("district_area_only")
            else ""
        )
        if language == "kz":
            purpose_display = (
                "Бағбандық" if purpose == GARDENING else "ЖҚШ"
            )
            profile = ""
            if False and purpose == LPH_NEW:
                usage = (
                    "🏠 Үй және жеке шаруашылық"
                    if data.get("allotment_type") == HOUSEHOLD
                    else "🌾 Тек ауыл шаруашылығы өндірісі"
                )
                irrigation = (
                    "💧 Суармалы"
                    if irrigation_type == IRRIGATED
                    else "🌾 Суарылмайтын"
                )
                profile = f"\n{usage}\n{irrigation}"
            text = (
                "✅ <b>Талдауға бәрі дайын</b>\n\n"
                f"📍 {region['label']} → {district['label']}\n"
                f"🏘 {locality['label']}\n"
                f"🏡 {purpose_display}\n"
                f"📐 {sotok} сотық"
                f"{profile}\n\n"
                "Жүйе 10 ықтимал нұсқаға дейін талдап, есеп дайындауға тырысады."
                f"{area_notice}"
            )
        else:
            purpose_display = (
                "Садоводство" if purpose == GARDENING else "ЛПХ"
            )
            profile = ""
            if False and purpose == LPH_NEW:
                usage = (
                    "🏠 Дом и личное хозяйство"
                    if data.get("allotment_type") == HOUSEHOLD
                    else "🌾 Только сельхозпроизводство"
                )
                irrigation = (
                    "💧 Орошаемая"
                    if irrigation_type == IRRIGATED
                    else "🌾 Неорошаемая"
                )
                profile = f"\n{usage}\n{irrigation}"
            text = (
                "✅ <b>Все готово к анализу</b>\n\n"
                f"📍 {region['label']} → {district['label']}\n"
                f"🏘 {locality['label']}\n"
                f"🏡 {purpose_display}\n"
                f"📐 {sotok} соток"
                f"{profile}\n\n"
                "Система попробует проанализировать и подготовить до 10 перспективных вариантов."
                f"{area_notice}"
            )
        await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        return
    profile_lines = ""
    if purpose == LPH_NEW:
        profile_lines = (
            f"\n{allotment_label(data.get('allotment_type'), language).capitalize()}"
            f" · {irrigation_label(irrigation_type, language)}"
        )
    await state.set_state(SearchForm.choosing_area)
    await message.edit_text(
        t(
            language,
            "area_prompt",
            locality=locality["label"],
            purpose=purpose_label(purpose, language),
            sotok=sotok,
        )
        + profile_lines,
        reply_markup=area_keyboard(
            language,
            purpose,
            irrigation_type,
            all_districts=(
                data.get("district", {}).get("value") == ALL_DISTRICTS
                or bool(data.get("district_area_only"))
            ),
        ),
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    payload = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""
    if payload.startswith("link_"):
        raw_token = payload.removeprefix("link_")
        with SessionLocal() as session:
            account = consume_telegram_link_token(
                session,
                raw_token,
                telegram_user_id=str(message.from_user.id),
                telegram_chat_id=str(message.chat.id),
            )
        if account:
            await message.answer("Telegram привязан к вашему аккаунту Жертап.")
        else:
            await message.answer("Ссылка привязки устарела. Создайте новую в личном кабинете.")

    funnel_session_id = str(uuid.uuid4())
    await show_language(message, state)
    await state.update_data(
        telegram_user_id=str(message.from_user.id),
        telegram_chat_id=str(message.chat.id),
        funnel_session_id=funnel_session_id,
    )
    track_bot_event(
        "start_opened",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        funnel_session_id=funnel_session_id,
    )


@router.callback_query(F.data.startswith("language:"))
async def choose_language(callback: CallbackQuery, state: FSMContext) -> None:
    language = normalize_language((callback.data or "").rsplit(":", 1)[-1])
    data = await state.get_data()
    await state.update_data(language=language)
    track_bot_event(
        "language_selected",
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id if callback.message else None,
        language=language,
        funnel_session_id=data.get("funnel_session_id"),
    )
    await callback.answer()
    if callback.message:
        await show_terms(callback.message, state, language)


@router.message(Command("terms"))
async def terms(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    language = data.get("language")
    if not language:
        await show_language(message, state)
        return
    loading = await message.answer("...")
    await state.set_state(SearchForm.choosing_terms)
    await loading.edit_text(
        terms_text(normalize_language(language)),
        reply_markup=terms_keyboard(normalize_language(language)),
    )


@router.message(Command("feedback"))
async def feedback_command(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    language = normalize_language(data.get("language"))
    if not data.get("language"):
        with SessionLocal() as session:
            language = normalize_language(
                session.scalar(
                    select(SearchRequest.language)
                    .where(SearchRequest.telegram_user_id == str(message.from_user.id))
                    .order_by(SearchRequest.created_at.desc())
                    .limit(1)
                )
            )
    await state.update_data(language=language)
    await state.set_state(FeedbackForm.waiting_message)
    await message.answer(feedback_start_text(language))


@router.callback_query(F.data == "feedback:start")
async def feedback_start(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    language = normalize_language(data.get("language"))
    await state.update_data(language=language)
    await state.set_state(FeedbackForm.waiting_message)
    await callback.answer()
    if callback.message:
        await callback.message.answer(feedback_start_text(language))


@router.message(FeedbackForm.waiting_message, F.text)
async def feedback_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    language = normalize_language(data.get("language"))
    try:
        with SessionLocal() as session:
            record_client_feedback(
                session,
                text=message.text or "",
                channel="telegram",
                telegram_user_id=str(message.from_user.id),
                telegram_chat_id=str(message.chat.id),
                language=language,
            )
    except ValueError:
        await message.answer(
            "Хабарлама мәтінін жазыңыз." if language == "kz" else "Напишите текст сообщения."
        )
        return
    await state.clear()
    await state.update_data(language=language)
    await message.answer(feedback_thanks_text(language), reply_markup=service_keyboard(language))


@router.callback_query(F.data == "terms:accept")
async def accept_terms(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        language = normalize_language(data.get("language"))
        await state.update_data(
            terms_version=TERMS_VERSION,
            terms_text_snapshot=terms_text(language),
            terms_accepted_at=datetime.now(UTC).isoformat(),
        )
        track_bot_event(
            "terms_accepted",
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id if callback.message else None,
            language=language,
            metadata={"terms_version": TERMS_VERSION},
            funnel_session_id=data.get("funnel_session_id"),
        )
        await callback.answer(t(language, "terms_accepted"))
        if callback.message:
            await show_services(callback.message, state)
    except Exception as exc:
        await show_catalog_error(
            callback,
            exc,
            retry_data="terms:accept",
            back_data="terms:accept",
            language=normalize_language((await state.get_data()).get("language")),
        )


@router.callback_query(F.data.startswith("purpose:"))
async def choose_purpose(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    language = normalize_language(data.get("language"))
    if not data.get("terms_accepted_at"):
        await callback.answer(
            "Сначала примите условия." if language == "ru" else "Алдымен шарттарды қабылдаңыз.",
            show_alert=True,
        )
        if callback.message:
            await show_terms(callback.message, state, language)
        return
    selected = (callback.data or "").rsplit(":", 1)[-1]
    purpose = {
        "gardening": GARDENING,
        "lph-new": LPH_NEW,
    }.get(selected, LPH)
    if (
        funnel_v2_enabled()
        and selected == "lph"
        and not settings.enable_standard_lph_10
    ):
        purpose = LPH_NEW
    await state.update_data(
        purpose=purpose,
        allotment_type=None,
        irrigation_type=None,
        area_ha=None,
    )
    track_bot_event(
        "purpose_selected",
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id if callback.message else None,
        language=language,
        metadata={"purpose": purpose},
        funnel_session_id=data.get("funnel_session_id"),
    )
    await callback.answer()
    if callback.message:
        if (
            funnel_v2_enabled()
            and selected == "lph"
            and settings.enable_standard_lph_10
        ):
            await show_lph_modes(callback.message, state)
        elif purpose == LPH_NEW:
            await show_lph_size(callback.message, state)
        elif purpose == GARDENING:
            await show_gardening_size(callback.message, state)
        else:
            await show_regions(callback.message, state, edit=True)


@router.callback_query(F.data == "service:land-search")
async def choose_land_search_service(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await show_purposes(callback.message, state)


@router.callback_query(F.data.startswith("lph-mode:"))
async def choose_lph_mode(callback: CallbackQuery, state: FSMContext) -> None:
    selected = (callback.data or "").rsplit(":", 1)[-1]
    purpose = LPH_NEW if selected == "extended" else LPH
    await state.update_data(
        purpose=purpose,
        allotment_type=None,
        irrigation_type=None,
        area_ha=None,
    )
    language = normalize_language((await state.get_data()).get("language"))
    track_bot_event(
        "lph_mode_selected",
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id if callback.message else None,
        language=language,
        metadata={"mode": selected, "purpose": purpose},
    )
    await callback.answer()
    if callback.message:
        if purpose == LPH_NEW:
            await show_lph_size(callback.message, state)
        else:
            await show_regions(callback.message, state, edit=True)


@router.callback_query(F.data.startswith("allotment:"))
async def choose_allotment(callback: CallbackQuery, state: FSMContext) -> None:
    selected = (callback.data or "").rsplit(":", 1)[-1]
    allotment_type = FIELD if selected == "field" else HOUSEHOLD
    await state.update_data(allotment_type=allotment_type, irrigation_type=None)
    await callback.answer()
    if callback.message:
        await show_irrigation(callback.message, state)


@router.callback_query(F.data.startswith("irrigation:"))
async def choose_irrigation(callback: CallbackQuery, state: FSMContext) -> None:
    selected = (callback.data or "").rsplit(":", 1)[-1]
    irrigation_type = IRRIGATED if selected == "irrigated" else NON_IRRIGATED
    await state.update_data(irrigation_type=irrigation_type, area_ha=None)
    await callback.answer()
    if callback.message:
        await show_regions(callback.message, state, edit=True)


@router.callback_query(F.data.startswith("lph-size:"))
async def choose_lph_size(callback: CallbackQuery, state: FSMContext) -> None:
    selected = (callback.data or "").rsplit(":", 1)[-1]
    irrigation_type = IRRIGATED if selected == "15" else NON_IRRIGATED
    await state.update_data(
        allotment_type=HOUSEHOLD,
        irrigation_type=irrigation_type,
        area_ha=None,
    )
    await callback.answer()
    if callback.message:
        await show_regions(callback.message, state, edit=True)


@router.callback_query(F.data.startswith("garden-size:"))
async def choose_gardening_size(callback: CallbackQuery, state: FSMContext) -> None:
    selected = (callback.data or "").rsplit(":", 1)[-1]
    area_ha = 0.06 if selected == "6" else 0.12
    await state.update_data(
        allotment_type=None,
        irrigation_type=None,
        area_ha=area_ha,
    )
    await callback.answer()
    if callback.message:
        await show_regions(callback.message, state, edit=True)


@router.callback_query(F.data == "catalog:back:purpose")
async def back_to_purpose(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await show_purposes(callback.message, state)


@router.callback_query(F.data == "catalog:back:lph-mode")
async def back_to_lph_mode(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        if settings.enable_standard_lph_10:
            await show_lph_modes(callback.message, state)
        else:
            await show_purposes(callback.message, state)


@router.callback_query(F.data == "catalog:back:terms")
async def back_to_terms(callback: CallbackQuery, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    await callback.answer()
    if callback.message:
        await show_terms(callback.message, state, language)


@router.callback_query(F.data == "catalog:home")
async def catalog_home(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    language = normalize_language(data.get("language"))
    await callback.answer()
    if not callback.message:
        return
    if not data.get("terms_accepted_at"):
        await show_terms(callback.message, state, language)
    else:
        await show_services(callback.message, state)


@router.callback_query(F.data == "catalog:back:allotment")
async def back_to_allotment(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await show_allotments(callback.message, state)


@router.callback_query(F.data == "catalog:back:irrigation")
async def back_to_irrigation(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await show_irrigation(callback.message, state)


@router.callback_query(F.data == "catalog:back:lph-size")
async def back_to_lph_size(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await show_lph_size(callback.message, state)


@router.callback_query(F.data == "catalog:back:garden-size")
async def back_to_gardening_size(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await show_gardening_size(callback.message, state)


@router.message(Command("privacy"))
async def privacy(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    await message.answer(privacy_text(language))


@router.message(Command("offer"))
async def offer(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    await message.answer(offer_text(language))


@router.message(Command("whoami"))
async def whoami(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    user_id = message.from_user.id if message.from_user else "-"
    if language == "kz":
        text = f"Telegram user ID: {user_id}\nChat ID: {message.chat.id}"
    else:
        text = (
            f"Ваш Telegram user ID: {user_id}\nChat ID: {message.chat.id}\n"
            "Для личного админ-чата обычно оба значения совпадают."
        )
    await message.answer(text)


@router.message(Command("status"))
async def status(message: Message, state: FSMContext) -> None:
    match = re.search(r"[0-9a-fA-F-]{36}", message.text or "")
    if not match:
        language = normalize_language((await state.get_data()).get("language"))
        prefix = "Нөмірді көрсетіңіз" if language == "kz" else "Укажите номер"
        await message.answer(f"{prefix}: /status 00000000-0000-0000-0000-000000000000")
        return
    with SessionLocal() as session:
        request = session.get(SearchRequest, match.group(0))
        if request is None:
            language = normalize_language((await state.get_data()).get("language"))
            await message.answer("Өтінім табылмады." if language == "kz" else "Заявка не найдена.")
            return
        language = normalize_language(request.language)
        paid_access = has_paid_access(session, request.telegram_user_id)
        if funnel_v2_enabled():
            reply_markup = None
            if request.status == "failed":
                reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=t(language, "retry_search"),
                                callback_data=f"search:retry:{request.id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=t(language, "back_districts"),
                                callback_data=f"search:districts:{request.id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=t(language, "main_regions"),
                                callback_data=f"search:regions:{request.id}",
                            )
                        ],
                    ]
                )
            if language == "kz":
                status_label = {
                    "queued": "кезекте",
                    "processing": "аумақ талданып жатыр",
                    "ready": "анализ аяқталды",
                    "delivered": "есеп жіберілді",
                    "failed": "қате",
                }.get(request.status, request.status)
                payment_label = {
                    "not_requested": "қажет емес",
                    "awaiting_transfer": "Kaspi арқылы төлем күтілуде",
                    "pending_confirmation": "төлем тексерілуде",
                    "paid": "расталды",
                    "rejected": "расталмады",
                }.get(request.payment_status, request.payment_status)
                next_action = {
                    "queued": "Өтінім кезегін күтуде. Бот нәтижені өзі жібереді.",
                    "processing": "Аумақ талдауы жалғасуда. Чатты жабуға болады.",
                    "ready": "Есеп дайындалып немесе төлемді күтуде.",
                    "delivered": "Есеп чатқа жіберілді.",
                    "failed": "Төмендегі батырмамен іздеуді қайталаңыз.",
                }.get(request.status, "")
                text = (
                    f"Өтінім: {request.id}\n"
                    f"Мәртебе: {status_label}\n"
                    f"Орындалуы: {request.progress}%\n"
                    f"Төлем: {payment_label}\n"
                    f"Қолжетімділік: "
                    f"{'ақылы белсенді' if paid_access else 'сынақ'}\n\n"
                    f"{next_action}"
                )
            else:
                status_label = {
                    "queued": "в очереди",
                    "processing": "идет анализ территории",
                    "ready": "анализ завершен",
                    "delivered": "отчет отправлен",
                    "failed": "ошибка",
                }.get(request.status, request.status)
                payment_label = {
                    "not_requested": "не требуется",
                    "awaiting_transfer": "ожидается оплата через Kaspi",
                    "pending_confirmation": "платеж проверяется",
                    "paid": "подтверждена",
                    "rejected": "не подтверждена",
                }.get(request.payment_status, request.payment_status)
                next_action = {
                    "queued": "Заявка ожидает обработки. Бот сам пришлет результат.",
                    "processing": "Анализ продолжается. Чат можно закрыть.",
                    "ready": "Отчет готовится к отправке или ожидает оплаты.",
                    "delivered": "Отчет уже отправлен в этот чат.",
                    "failed": "Повторите поиск кнопкой ниже.",
                }.get(request.status, "")
                text = (
                    f"Заявка: {request.id}\n"
                    f"Статус: {status_label}\n"
                    f"Прогресс: {request.progress}%\n"
                    f"Оплата: {payment_label}\n"
                    f"Доступ: {'оплачен' if paid_access else 'тестовый'}\n\n"
                    f"{next_action}"
                )
            await message.answer(text, reply_markup=reply_markup)
            return
        if language == "kz":
            status_labels = {
                "queued": "кезекте немесе қайта өңдеуді күтуде",
                "processing": "аумақ талдауы орындалуда",
                "ready": "анализ аяқталды",
                "delivered": "толық есеп жіберілді",
                "failed": "іздеу техникалық қатемен аяқталды",
            }
            payment_labels = {
                "not_requested": "сұралмады",
                "awaiting_transfer": (
                    "Kaspi арқылы төлем күтілуде"
                    if settings.apipay_enabled
                    else "аударым күтілуде"
                ),
                "pending_confirmation": "қаражаттың түсуі тексерілуде",
                "paid": "расталды",
                "rejected": "қаражаттың түсуі расталмады",
            }
            detail = ""
            if request.status == "failed":
                detail = "\nЕсеп дайындалмады. Төлем сұралмайды. Кейінірек жаңа өтінім беріңіз."
            elif request.status == "ready" and not request.candidates:
                detail = "\nСәйкес бос орындар табылмады. Төлем сұралмайды."
            elif request.urban_plan_status == "unavailable":
                detail = (
                    "\nРесми бас жоспар/ЕЖЖ қабаты жоқ. Координаттар берілмейді, төлем сұралмайды."
                )
            elif request.urban_plan_status == "blocked":
                detail = (
                    "\nКандидаттар таңдалған мақсатқа арналған рұқсат етілген аймақпен "
                    "расталмады немесе шектеуге түсті. Төлем сұралмайды."
                )
            elif request.urban_plan_status == "waived":
                detail = "\nАлдын ала нәтиже пайдаланушының келісімімен бас жоспарсыз берілді."
            elif request.status in {"queued", "processing"}:
                detail = "\nӨтінім әлі талданып жатыр. Ірі елді мекендерге көбірек уақыт қажет."
            free_detail = ""
            if request.free_preview_status == "pending":
                free_detail = (
                    f"\nТегін учаскелер: {request.free_preview_count}, автоматты жіберу қайталанады"
                )
            elif request.free_preview_status == "delivered":
                free_detail = f"\nТегін жіберілді: {request.free_preview_count} учаске"
            elif request.free_preview_status == "rejected":
                free_detail = "\nТегін жіберу қабылданбады, лимит азайған жоқ"
            reply_markup = None
            if request.status == "failed":
                reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=t(language, "retry_search"),
                                callback_data=f"search:retry:{request.id}",
                            )
                        ]
                    ]
                )
            await message.answer(
                f"Өтінім: {request.id}\n"
                f"Топтама: {request.batch_number}\n"
                f"Мәртебе: {status_labels.get(request.status, request.status)}\n"
                f"Орындалуы: {request.progress}%\n"
                f"Төлем: {payment_labels.get(request.payment_status, request.payment_status)}"
                f"\nАқылы қолжетімділік: {'белсенді' if paid_access else 'белсенді емес'}"
                f"{detail}{free_detail}",
                reply_markup=reply_markup,
            )
            return
        status_labels = {
            "queued": "в очереди или ожидает повторной попытки",
            "processing": "выполняется анализ территории",
            "ready": "анализ завершен",
            "delivered": "полный отчет отправлен",
            "failed": "поиск завершился технической ошибкой",
        }
        payment_labels = {
            "not_requested": "не запрашивалась",
            "awaiting_transfer": (
                "ожидается оплата через Kaspi"
                if settings.apipay_enabled
                else "ожидается перевод"
            ),
            "pending_confirmation": "проверяется поступление",
            "paid": "подтверждена",
            "rejected": "поступление не подтверждено",
        }
        detail = ""
        if request.status == "failed":
            detail = "\nРезультат не сформирован. Оплата не запрашивается. Создайте заявку позже."
        elif request.status == "ready" and not request.candidates:
            detail = "\nПодходящие промежутки не найдены. Оплата не запрашивается."
        elif request.urban_plan_status == "unavailable":
            detail = (
                "\nНет официального слоя генплана/ПДП. Координаты не выдаются, "
                "оплата не запрашивается."
            )
        elif request.urban_plan_status == "blocked":
            detail = (
                "\nКандидаты не подтвердились разрешенной зоной генплана/ПДП для "
                "выбранной цели или попали под ограничение. Оплата не запрашивается."
            )
        elif request.urban_plan_status == "waived":
            detail = "\nПредварительный результат выдан без генплана по согласию пользователя."
        elif request.status in {"queued", "processing"}:
            detail = (
                "\nЗаявка еще анализируется. Для крупных населенных пунктов нужно больше времени."
            )
        free_detail = ""
        if request.free_preview_status == "pending":
            free_detail = (
                f"\nБесплатные участки: {request.free_preview_count}, автоматическая отправка "
                "будет повторена"
            )
        elif request.free_preview_status == "delivered":
            free_detail = f"\nОтправлено бесплатно: {request.free_preview_count} участка"
        elif request.free_preview_status == "rejected":
            free_detail = "\nБесплатная отправка отклонена, лимит не уменьшен"
        reply_markup = None
        if request.status == "failed":
            reply_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t(language, "retry_search"),
                            callback_data=f"search:retry:{request.id}",
                        )
                    ]
                ]
            )
        await message.answer(
            f"Заявка: {request.id}\n"
            f"Пакет: {request.batch_number}\n"
            f"Статус: {status_labels.get(request.status, request.status)}\n"
            f"Прогресс: {request.progress}%\n"
            f"Оплата: {payment_labels.get(request.payment_status, request.payment_status)}"
            f"\nОплаченный доступ: {'активен' if paid_access else 'не активен'}"
            f"{detail}{free_detail}",
            reply_markup=reply_markup,
        )


@router.callback_query(F.data.startswith("search:retry:"))
async def retry_search(callback: CallbackQuery, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    try:
        source_request_id = callback_request_id(callback.data, "search:retry:")
        chat_id = str(callback.message.chat.id) if callback.message else ""
        with SessionLocal() as session:
            source = session.get(SearchRequest, source_request_id)
            if source is not None:
                language = normalize_language(source.language)
            request, position, created = retry_failed_search(
                session,
                source_request_id,
                telegram_user_id=str(callback.from_user.id),
                telegram_chat_id=chat_id,
            )
            request_id = request.id
            language = normalize_language(request.language)
        await state.update_data(language=language)
        await callback.answer(t(language, "retry_created" if created else "retry_already_created"))
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            if language == "kz":
                sotok = round(request.area_ha * 100)
                text = (
                    f"Анализ сол параметрлермен қайта жіберілді. Жаңа өтінім нөмірі: "
                    f"{request_id}\n"
                    + (f"Кезектегі орны: {position}\n" if created else "")
                    + f"Облыс, аудан, елді мекен, {purpose_label(request.purpose, 'kz')} "
                    f"мақсаты және {sotok} сотық аумақ өзгерген жоқ. "
                    "Бот талдау аяқталған кезде есеп немесе қате туралы хабарлайды.\n"
                    f"Мәртебе: /status {request_id}"
                )
            else:
                sotok = round(request.area_ha * 100)
                text = (
                    f"Анализ повторно запущен с теми же параметрами. Номер новой заявки: "
                    f"{request_id}\n"
                    + (f"Позиция в очереди: {position}\n" if created else "")
                    + f"Область, район, населенный пункт, назначение "
                    f"«{purpose_label(request.purpose, 'ru')}» и площадь {sotok} соток сохранены. "
                    "Бот сообщит об отчете или ошибке после завершения анализа.\n"
                    f"Статус: /status {request_id}"
                )
            await callback.message.answer(text)
            if created and funnel_v2_enabled():
                progress_object = await callback.message.answer(
                    progress_message(language, "boundaries"),
                    parse_mode="HTML",
                )
                with SessionLocal() as session:
                    stored = session.get(SearchRequest, request_id)
                    if stored is not None:
                        stored.progress_message_id = getattr(progress_object, "message_id", None)
                        session.commit()
        if created:
            dispatch_search(request_id)
    except Exception as exc:
        logger.warning("Could not retry search: %s", exc)
        await callback.answer(t(language, "retry_error"), show_alert=True)


@router.callback_query(F.data.startswith("search:regions:"))
async def reopen_regions(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        request_id = callback_request_id(callback.data, "search:regions:")
        await reopen_search_catalog(
            callback,
            state,
            request_id,
            target="regions",
        )
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("search:districts:"))
async def reopen_districts(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        request_id = callback_request_id(callback.data, "search:districts:")
        await reopen_search_catalog(
            callback,
            state,
            request_id,
            target="districts",
        )
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("search:localities:"))
async def reopen_localities(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        request_id = callback_request_id(callback.data, "search:localities:")
        await reopen_search_catalog(
            callback,
            state,
            request_id,
            target="localities",
        )
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data == "catalog:noop")
async def catalog_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "catalog:back:regions")
async def back_to_regions(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        await callback.answer()
        if callback.message:
            await show_regions(callback.message, state, edit=True)
    except Exception as exc:
        await show_catalog_error(
            callback,
            exc,
            retry_data="catalog:back:regions",
            back_data="terms:accept",
            language=normalize_language((await state.get_data()).get("language")),
        )


@router.callback_query(F.data.startswith("catalog:region:"))
async def choose_region(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        language = normalize_language(data.get("language"))
        region = indexed_choice(data.get("regions", []), (callback.data or "").rsplit(":", 1)[-1])
        await state.update_data(
            region=region,
            district=None,
            settlements=None,
            locality=None,
            district_area_only=False,
        )
        track_bot_event(
            "region_selected",
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id if callback.message else None,
            language=language,
            metadata={"region": region["value"], "region_label": region["label"]},
            funnel_session_id=data.get("funnel_session_id"),
        )
        await callback.answer(t(language, "region_selected"))
        if callback.message:
            await show_districts(callback.message, state)
    except Exception as exc:
        await show_catalog_error(
            callback,
            exc,
            retry_data=callback.data or "catalog:back:regions",
            back_data="catalog:back:regions",
            language=normalize_language((await state.get_data()).get("language")),
        )


@router.callback_query(F.data == "catalog:back:districts")
async def back_to_districts(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        await state.update_data(
            district=None,
            settlements=None,
            locality=None,
            district_area_only=False,
        )
        await callback.answer()
        if callback.message:
            await show_districts(callback.message, state)
    except Exception as exc:
        await show_catalog_error(
            callback,
            exc,
            retry_data="catalog:back:districts",
            back_data="catalog:back:regions",
            language=normalize_language((await state.get_data()).get("language")),
        )


@router.callback_query(F.data == "catalog:district:all")
async def choose_all_districts(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    language = normalize_language(data.get("language"))
    district = {
        "id": None,
        "value": ALL_DISTRICTS,
        "label": t(language, "all_districts"),
    }
    locality = {
        "value": None,
        "label": t(language, "all_districts"),
    }
    await state.update_data(
        district=district,
        settlements=None,
        locality=locality,
        district_area_only=True,
    )
    track_bot_event(
        "district_selected",
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id if callback.message else None,
        language=language,
        metadata={"district": ALL_DISTRICTS},
        funnel_session_id=data.get("funnel_session_id"),
    )
    await callback.answer(t(language, "district_selected"))
    if callback.message:
        await show_area_choice(callback.message, state, locality)


@router.callback_query(F.data.startswith("catalog:district:"))
async def choose_district(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        language = normalize_language(data.get("language"))
        district = indexed_choice(
            data.get("districts", []), (callback.data or "").rsplit(":", 1)[-1]
        )
        await state.update_data(
            district=district,
            settlements=None,
            locality=None,
            district_area_only=False,
        )
        track_bot_event(
            "district_selected",
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id if callback.message else None,
            language=language,
            metadata={"district": district["value"], "district_label": district["label"]},
            funnel_session_id=data.get("funnel_session_id"),
        )
        await callback.answer(t(language, "district_selected"))
        if callback.message:
            await show_settlements(callback.message, state)
    except Exception as exc:
        await show_catalog_error(
            callback,
            exc,
            retry_data=callback.data or "catalog:back:districts",
            back_data="catalog:back:districts",
            language=normalize_language((await state.get_data()).get("language")),
        )


@router.callback_query(F.data.startswith("catalog:settlement-page:"))
async def settlement_page(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        page = int((callback.data or "").rsplit(":", 1)[-1])
        await callback.answer()
        if callback.message:
            await show_settlements(callback.message, state, page=page)
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data == "catalog:back:settlements")
async def back_to_settlements(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await show_settlements(callback.message, state)


@router.callback_query(F.data.startswith("catalog:settlement:"))
async def choose_settlement(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        language = normalize_language(data.get("language"))
        locality = indexed_choice(
            data.get("settlements", []), (callback.data or "").rsplit(":", 1)[-1]
        )
        await state.update_data(locality=locality)
        track_bot_event(
            "locality_selected",
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id if callback.message else None,
            language=language,
            metadata={"locality": locality["value"], "locality_label": locality["label"]},
            funnel_session_id=data.get("funnel_session_id"),
        )
        await callback.answer(t(language, "settlement_selected"))
        if callback.message:
            await show_area_choice(callback.message, state, locality)
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("catalog:area:"))
async def choose_area(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        language = normalize_language(data.get("language"))
        purpose = normalize_purpose(data.get("purpose"))
        allotment_type = data.get("allotment_type")
        irrigation_type = data.get("irrigation_type")
        sotok = int((callback.data or "").rsplit(":", 1)[-1])
        if purpose == GARDENING:
            allowed_sotok = {round(area * 100) for area in GARDENING_ALLOWED_AREAS_HA}
            if sotok not in allowed_sotok:
                message = (
                    "Бұл мақсат үшін 6 немесе 12 сотық талдау қолжетімді."
                    if language == "kz"
                    else "Для садоводства доступен анализ 6 или 12 соток."
                )
                raise ValueError(message)
        else:
            expected_sotok = purpose_sotok(purpose, irrigation_type)
            if sotok != expected_sotok:
                message = (
                    f"Бұл мақсат үшін тек {expected_sotok} сотық талдау қолжетімді."
                    if language == "kz"
                    else f"Для этого назначения доступен анализ только {expected_sotok} соток."
                )
                raise ValueError(message)
        region = data["region"]
        district = data["district"]
        locality = data["locality"]
        terms_version = data.get("terms_version")
        terms_text_snapshot = data.get("terms_text_snapshot")
        terms_accepted_at = data.get("terms_accepted_at")
        if not terms_version or not terms_text_snapshot or not terms_accepted_at:
            message = (
                "Алдымен /start арқылы шарттарды қабылдаңыз."
                if language == "kz"
                else "Сначала примите условия через /start."
            )
            raise ValueError(message)
        parsed = SearchCreate(
            language=language,
            region=region["value"],
            region_label=region["label"],
            district=district["value"],
            district_label=district["label"],
            locality=locality["value"],
            locality_label=locality["label"].split(" · КАТО", 1)[0],
            purpose=purpose,
            allotment_type=allotment_type,
            irrigation_type=irrigation_type,
            area_ha=sotok / 100,
            result_limit=10,
            cemetery_buffer_m=0,
            telegram_user_id=str(callback.from_user.id),
            telegram_chat_id=str(callback.message.chat.id) if callback.message else None,
            funnel_session_id=data.get("funnel_session_id"),
            terms_version=terms_version,
            terms_text_snapshot=terms_text_snapshot,
            terms_accepted_at=terms_accepted_at,
        )
        await state.update_data(search=parsed.model_dump())
        await state.set_state(SearchForm.waiting_confirmation)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t(language, "send_queue"), callback_data="search:confirm"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=(
                            t(language, "back_districts")
                            if district["value"] == ALL_DISTRICTS or data.get("district_area_only")
                            else t(language, "back_settlements")
                        ),
                        callback_data=(
                            "catalog:back:districts"
                            if district["value"] == ALL_DISTRICTS or data.get("district_area_only")
                            else "catalog:back:settlements"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=t(language, "main_regions"),
                        callback_data="search:edit",
                    )
                ],
            ]
        )
        await callback.answer(t(language, "area_selected"))
        if callback.message:
            if language == "kz":
                lph_profile = (
                    f"Телім түрі: {allotment_label(parsed.allotment_type, 'kz')}\n"
                    f"Профиль: {irrigation_label(parsed.irrigation_type, 'kz')}\n"
                    if False
                    else ""
                )
                text = (
                    "Параметрлерді тексеріңіз:\n"
                    f"Облыс: {region['label']}\n"
                    f"Аудан: {district['label']}\n"
                    f"Елді мекен: {locality['label']}\n"
                    f"Мақсаты: {purpose_label(purpose, 'kz')}\n"
                    + lph_profile
                    + f"Ауданы: {sotok} сотық ({parsed.area_ha:.2f} га)\n"
                    "Нәтижелер: 10-ға дейін"
                )
            else:
                lph_profile = (
                    f"Вид надела: {allotment_label(parsed.allotment_type)}\n"
                    f"Профиль: {irrigation_label(parsed.irrigation_type)}\n"
                    if False
                    else ""
                )
                text = (
                    "Проверьте параметры:\n"
                    f"Область: {region['label']}\n"
                    f"Район: {district['label']}\n"
                    f"Населенный пункт: {locality['label']}\n"
                    f"Назначение: {purpose_label(purpose, 'ru')}\n"
                    + lph_profile
                    + f"Площадь: {sotok} соток ({parsed.area_ha:.2f} га)\n"
                    "Результатов: до 10"
                )
            await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data == "search:edit")
async def edit_query(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await show_regions(callback.message, state, edit=True)


@router.callback_query(F.data == "search:confirm")
async def confirm_query(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        payload = SearchCreate.model_validate(data["search"])
        with SessionLocal() as session:
            request, position = create_search(session, payload)
            request_id = request.id
            paid_access = has_paid_access(session, request.telegram_user_id)
        track_bot_event(
            "search_confirmed",
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id if callback.message else None,
            request_id=request_id,
            language=payload.language,
            funnel_session_id=payload.funnel_session_id,
            metadata={
                "purpose": payload.purpose,
                "area_ha": payload.area_ha,
                "region": payload.region,
                "district": payload.district,
                "locality": payload.locality,
            },
        )
        await callback.answer("Өтінім құрылды" if payload.language == "kz" else "Заявка создана")
        progress_message_id: int | None = None
        if callback.message:
            sotok = round(payload.area_ha * 100)
            profile_ru = (
                f"\n🧭 Профиль: {allotment_label(payload.allotment_type)}, "
                f"{irrigation_label(payload.irrigation_type)}"
                if False
                else ""
            )
            profile_kz = (
                f"\n🧭 Профиль: {allotment_label(payload.allotment_type, 'kz')}, "
                f"{irrigation_label(payload.irrigation_type, 'kz')}"
                if False
                else ""
            )
            await callback.message.edit_reply_markup(reply_markup=None)
            if funnel_v2_enabled() and payload.language == "kz":
                purpose_name = (
                    "Бағбандық"
                    if normalize_purpose(payload.purpose) == GARDENING
                    else "ЖҚШ"
                )
                access = (
                    "\n\n✅ Толық қолжетімділік белсенді.\n"
                    "Барлық табылған нұсқалар қосымша төлемсіз жіберіледі."
                    if paid_access
                    else (
                        "\n\n🎁 Нұсқалар табылса, қысқа алдын ала карточкалар көрсетіледі. "
                        "Нақты координаттар мен карта толық есепте ашылады."
                    )
                )
                text = (
                    "🧭 <b>Аумақ талдауы басталды</b>\n\n"
                    f"📍 {payload.region_label} → {payload.district_label}\n"
                    f"🏘 {payload.locality_label}\n"
                    f"🏡 {purpose_name}\n"
                    f"📐 {sotok} сотық"
                    f"{access}\n\n"
                    "Жүйе кадастрлық аралықтарды, жолдарды, объектілерді және қолжетімді "
                    "шектеулерді тексереді. Әдетте бұл бірнеше минут алады.\n"
                    "Чатты жабуға болады — бот есеп дайын болғанда хабарлайды.\n\n"
                    f"Өтінім: <code>{request_id}</code>\n"
                    f"Күйін тексеру: /status {request_id}"
                )
            elif funnel_v2_enabled():
                purpose_name = (
                    "Садоводство"
                    if normalize_purpose(payload.purpose) == GARDENING
                    else "ЛПХ"
                )
                access = (
                    "\n\n✅ Полный доступ активен.\n"
                    "Все найденные варианты придут без дополнительной оплаты."
                    if paid_access
                    else (
                        "\n\n🎁 Если варианты найдутся, бот покажет краткие "
                        "предварительные карточки. "
                        "Точные координаты и карта откроются в полном отчете."
                    )
                )
                text = (
                    "🧭 <b>Анализ территории запущен</b>\n\n"
                    f"📍 {payload.region_label} → {payload.district_label}\n"
                    f"🏘 {payload.locality_label}\n"
                    f"🏡 {purpose_name}\n"
                    f"📐 {sotok} соток"
                    f"{access}\n\n"
                    "Система проверит кадастровые промежутки, дороги, объекты и доступные "
                    "ограничения. Обычно это занимает несколько минут.\n"
                    "Можно закрыть чат — бот сам сообщит, когда отчет будет готов.\n\n"
                    f"Заявка: <code>{request_id}</code>\n"
                    f"Проверить состояние: /status {request_id}"
                )
            elif payload.language == "kz":
                access = (
                    "✅ Қолжетімділік белсенді. Нәтиже қосымша төлемсіз жіберіледі."
                    if paid_access
                    else (
                        "🎁 Тегін режим: қысқа алдын ала карточкалар, нақты нүкте толық есепте."
                        if settings.free_preview_enabled
                        else "💳 Нәтиже табылса, бот төлем ақпаратын жібереді."
                    )
                )
                text = (
                    "🔎 <b>Өтінім қабылданды</b>\n\n"
                    f"📍 {payload.region_label} → {payload.district_label}\n"
                    f"🏘 {payload.locality_label}\n"
                    f"🎯 {purpose_label(payload.purpose, 'kz')}\n"
                    f"📐 {sotok} сотық" + profile_kz + "\n\n"
                    f"{access}\n"
                    "⏳ 10 жаңа нұсқаға дейін талдаймын. Есеп немесе табылмағаны туралы "
                    "хабарлама автоматты түрде келеді.\n\n"
                    f"Өтінім: <code>{request_id}</code>\n"
                    f"Кезек: {position}\n"
                    f"Мәртебе: /status {request_id}"
                )
            else:
                access = (
                    "✅ Доступ активен. Результат придет без дополнительной оплаты."
                    if paid_access
                    else (
                        "🎁 Бесплатный режим: краткие предварительные карточки, "
                        "точная точка в полном отчете."
                        if settings.free_preview_enabled
                        else "💳 Если варианты найдутся, бот пришлет информацию об оплате."
                    )
                )
                text = (
                    "🔎 <b>Заявка принята</b>\n\n"
                    f"📍 {payload.region_label} → {payload.district_label}\n"
                    f"🏘 {payload.locality_label}\n"
                    f"🎯 {purpose_label(payload.purpose, 'ru')}\n"
                    f"📐 {sotok} соток" + profile_ru + "\n\n"
                    f"{access}\n"
                    "⏳ Анализирую до 10 новых вариантов. Бот автоматически сообщит, "
                    "когда отчет будет готов или подходящих мест не найдется.\n\n"
                    f"Заявка: <code>{request_id}</code>\n"
                    f"Очередь: {position}\n"
                    f"Статус: /status {request_id}"
                )
            await callback.message.answer(text, parse_mode="HTML")
            if funnel_v2_enabled():
                progress_message_object = await callback.message.answer(
                    progress_message(payload.language, "boundaries"),
                    parse_mode="HTML",
                )
                progress_message_id = getattr(progress_message_object, "message_id", None)
        if progress_message_id is not None:
            with SessionLocal() as session:
                stored = session.get(SearchRequest, request_id)
                if stored is not None:
                    stored.progress_message_id = progress_message_id
                    session.commit()
        await state.clear()
        await state.update_data(
            language=payload.language,
            telegram_user_id=str(callback.from_user.id),
            telegram_chat_id=(
                str(callback.message.chat.id) if callback.message else payload.telegram_chat_id
            ),
        )
        dispatch_search(request_id)
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("urban:waive:"))
async def urban_plan_waived(callback: CallbackQuery) -> None:
    try:
        request_id = callback_request_id(callback.data, "urban:waive:")
        chat_id = str(callback.message.chat.id) if callback.message else ""
        with SessionLocal() as session:
            request, accepted = accept_urban_plan_override(
                session,
                request_id,
                telegram_user_id=str(callback.from_user.id),
                telegram_chat_id=chat_id,
            )
        language = normalize_language(request.language)
        if accepted:
            answer = "Таңдау сақталды" if language == "kz" else "Выбор сохранен"
        else:
            answer = "Бұрын расталған" if language == "kz" else "Уже подтверждено ранее"
        await callback.answer(answer)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                "Алдын ала нәтиже бас жоспарды тексермей өңделді."
                if language == "kz"
                else "Предварительный результат обработан без проверки генплана."
            )
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("pay:start:"))
async def payment_started(callback: CallbackQuery) -> None:
    try:
        request_id = callback_request_id(callback.data, "pay:start:")
        chat_id = str(callback.message.chat.id) if callback.message else ""
        with SessionLocal() as session:
            source_request = session.get(SearchRequest, request_id)
            if source_request is None:
                raise LookupError("Заявка не найдена")
            language = normalize_language(source_request.language)
            request = start_payment(
                session,
                request_id,
                telegram_user_id=str(callback.from_user.id),
                telegram_chat_id=chat_id,
            )
        track_bot_event(
            "payment_button_clicked",
            user_id=callback.from_user.id,
            chat_id=chat_id,
            request_id=request_id,
            language=language,
        )
        access_active = (
            request.status == "delivered" and request.payment_status != "awaiting_transfer"
        )
        if access_active:
            await callback.answer(
                "Қолжетімділік белсенді, есеп жіберілді"
                if language == "kz"
                else "Доступ уже активен, отчет отправлен"
            )
        else:
            await callback.answer(
                (
                    "Төлем сілтемесі жіберілді"
                    if settings.apipay_enabled
                    else "Төлем деректері жіберілді"
                )
                if language == "kz"
                else (
                    "Ссылка на оплату отправлена"
                    if settings.apipay_enabled
                    else "Реквизиты отправлены"
                )
            )
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("pay:refresh:"))
async def payment_refreshed(callback: CallbackQuery) -> None:
    try:
        payload = (callback.data or "").removeprefix("pay:refresh:")
        request_id, invoice_id = payload.rsplit(":", 1)
        request_id = callback_request_id(
            f"pay:refresh:{request_id}",
            "pay:refresh:",
        )
        if not invoice_id.isdigit():
            raise ValueError("Некорректный номер счета")
        chat_id = str(callback.message.chat.id) if callback.message else ""
        with SessionLocal() as session:
            request, link_sent = refresh_apipay_payment(
                session,
                request_id,
                expected_invoice_id=invoice_id,
                telegram_user_id=str(callback.from_user.id),
                telegram_chat_id=chat_id,
            )
        language = normalize_language(request.language)
        if link_sent:
            await callback.answer(
                "Жаңа сілтеме жіберілді"
                if language == "kz"
                else "Новая ссылка отправлена"
            )
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=None)
        else:
            await callback.answer(
                "Алдыңғы төлемнің күйі тексерілуде. Бір минуттан кейін қайталаңыз."
                if language == "kz"
                else "Проверяем предыдущий платеж. Повторите через минуту.",
                show_alert=True,
            )
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("search:next:"))
async def next_search_batch(callback: CallbackQuery, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    try:
        source_id = callback_request_id(callback.data, "search:next:")
        chat_id = str(callback.message.chat.id) if callback.message else ""
        with SessionLocal() as session:
            request, position, created = create_next_batch(
                session,
                source_id,
                telegram_user_id=str(callback.from_user.id),
                telegram_chat_id=chat_id,
                require_paid_access=False,
            )
            language = normalize_language(request.language)
        track_bot_event(
            "next_batch_clicked",
            user_id=callback.from_user.id,
            chat_id=chat_id,
            request_id=request.id,
            language=language,
            metadata={"created": created, "batch_number": request.batch_number},
        )
        await callback.answer(
            "Келесі талдау кезекке қойылды"
            if language == "kz"
            else "Следующий анализ поставлен в очередь"
        )
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            if language == "kz":
                text = (
                    f"Келесі 10 нұсқаны талдау басталды. Топтама: {request.batch_number}.\n"
                    f"Өтінім: {request.id}\nМәртебе: /status {request.id}"
                )
            else:
                text = (
                    f"Начат анализ следующих 10 вариантов. Пакет: {request.batch_number}.\n"
                    f"Заявка: {request.id}\nСтатус: /status {request.id}"
                )
            if not created:
                text += (
                    "\nБұл пакет бұрын жасалған."
                    if language == "kz"
                    else "\nЭтот пакет уже был создан ранее."
                )
            await callback.message.answer(text)
            if created and funnel_v2_enabled():
                progress_object = await callback.message.answer(
                    progress_message(language, "boundaries"),
                    parse_mode="HTML",
                )
                with SessionLocal() as session:
                    stored = session.get(SearchRequest, request.id)
                    if stored is not None:
                        stored.progress_message_id = getattr(progress_object, "message_id", None)
                        session.commit()
        if created:
            dispatch_search(request.id)
    except Exception as exc:
        logger.exception("Next search batch could not be created: %s", exc)
        await callback.answer(
            "Келесі талдауды бастау мүмкін болмады. Кейінірек қайталап көріңіз."
            if language == "kz"
            else "Не удалось запустить следующий анализ. Попробуйте позже.",
            show_alert=True,
        )


@router.callback_query(F.data.startswith("pay:claim:"))
async def payment_claimed(callback: CallbackQuery) -> None:
    try:
        request_id = callback_request_id(callback.data, "pay:claim:")
        chat_id = str(callback.message.chat.id) if callback.message else ""
        with SessionLocal() as session:
            request = claim_payment(
                session,
                request_id,
                telegram_user_id=str(callback.from_user.id),
                telegram_chat_id=chat_id,
                client_label=callback.from_user.full_name,
            )
        language = normalize_language(request.language)
        access_active = request.status == "delivered" and request.payment_status == "not_requested"
        await callback.answer(
            (
                "Қолжетімділік белсенді, қайта төлеу қажет емес"
                if language == "kz"
                else "Доступ уже активен, повторно платить не нужно"
            )
            if access_active
            else t(language, "payment_sent")
        )
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                ("Есеп жіберілді." if language == "kz" else "Отчет отправлен.")
                if access_active
                else t(language, "payment_wait")
            )
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("free:confirm:"))
async def free_preview_confirmed(callback: CallbackQuery) -> None:
    chat_id = callback.message.chat.id if callback.message else None
    if not is_payment_admin(callback.from_user.id, chat_id):
        await callback.answer("Нет прав для бесплатной отправки", show_alert=True)
        return
    try:
        request_id = callback_request_id(callback.data, "free:confirm:")
        with SessionLocal() as session:
            request, message = approve_free_preview(
                session,
                request_id,
                approved_by=str(callback.from_user.id),
            )
        await callback.answer("Бесплатные участки отправлены")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                f"Клиенту отправлено бесплатно: {request.free_preview_count}. "
                + ("Повторная отправка не требовалась." if message is None else "")
            )
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("free:reject:"))
async def free_preview_rejected(callback: CallbackQuery) -> None:
    chat_id = callback.message.chat.id if callback.message else None
    if not is_payment_admin(callback.from_user.id, chat_id):
        await callback.answer("Нет прав для отклонения", show_alert=True)
        return
    try:
        request_id = callback_request_id(callback.data, "free:reject:")
        with SessionLocal() as session:
            reject_free_preview(session, request_id)
        await callback.answer("Бесплатная отправка отклонена")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("pay:confirm:"))
async def payment_confirmed(callback: CallbackQuery) -> None:
    chat_id = callback.message.chat.id if callback.message else None
    if not is_payment_admin(callback.from_user.id, chat_id):
        await callback.answer("Нет прав для подтверждения оплаты", show_alert=True)
        return
    try:
        request_id = callback_request_id(callback.data, "pay:confirm:")
        with SessionLocal() as session:
            request, _message = confirm_payment(
                session, request_id, confirmed_by=str(callback.from_user.id)
            )
        newly_confirmed = request.payment_status == "paid"
        await callback.answer(
            "Оплата подтверждена" if newly_confirmed else "Доступ уже был активирован ранее"
        )
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                f"Оплата подтверждена. Доступ Telegram ID активирован, отчет по "
                f"заявке {request.id} отправлен клиенту."
                if newly_confirmed
                else f"Повторная оплата не требуется. Отчет по заявке {request.id} "
                "отправлен по ранее активированному доступу."
            )
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("pay:reject:"))
async def payment_rejected(callback: CallbackQuery) -> None:
    chat_id = callback.message.chat.id if callback.message else None
    if not is_payment_admin(callback.from_user.id, chat_id):
        await callback.answer("Нет прав для проверки оплаты", show_alert=True)
        return
    try:
        request_id = callback_request_id(callback.data, "pay:reject:")
        with SessionLocal() as session:
            reject_payment(session, request_id)
        await callback.answer("Клиенту отправлено уведомление")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)


@router.message(F.text)
async def use_catalog(message: Message, state: FSMContext) -> None:
    language = normalize_language((await state.get_data()).get("language"))
    with SessionLocal() as session:
        if has_pending_feedback_request(session, str(message.from_user.id)):
            record_client_feedback(
                session,
                text=message.text or "",
                channel="telegram",
                telegram_user_id=str(message.from_user.id),
                telegram_chat_id=str(message.chat.id),
                language=language,
            )
            await message.answer(
                feedback_thanks_text(language),
                reply_markup=service_keyboard(language),
            )
            return
    await message.answer(t(language, "use_catalog"))


async def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty. Fill it in .env before starting the bot.")
    init_db()
    bot = Bot(settings.telegram_bot_token)
    dispatcher = Dispatcher()
    from app.auction_bot import auction_router

    dispatcher.include_router(group_router)
    dispatcher.include_router(auction_router)
    dispatcher.include_router(router)
    await dispatcher.start_polling(
        bot,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )


if __name__ == "__main__":
    asyncio.run(main())
