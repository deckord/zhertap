import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html import escape
from threading import Thread

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.access import (
    find_pending_platform_invoice,
    has_platform_access,
    next_platform_access_expiry,
)
from app.analytics import track_funnel_event
from app.apipay import cancel_invoice, create_qr_invoice, get_invoice
from app.config import settings
from app.funnel import (
    client_t as t,
)
from app.funnel import (
    completed_message,
    existing_invoice_message,
    funnel_v2_enabled,
    paid_offer_message,
    payment_confirmed_message,
    payment_link_message,
    progress_message,
)
from app.genplan_references import genplan_reference_payload
from app.legal_rules import legal_restriction_reason
from app.models import (
    Candidate,
    FreePreviewStatus,
    FunnelEvent,
    PaymentStatus,
    ReviewStatus,
    SearchRequest,
    SearchStatus,
    UrbanPlanStatus,
)
from app.provider_guard import ProviderCallDeferred
from app.providers.urban_plan import allowed_search_area_geojsons, evaluate_urban_plan
from app.purposes import (
    LPH_NEW,
    allotment_label,
    irrigation_label,
    normalize_purpose,
    purpose_activity_phrase,
    purpose_label,
)
from app.schemas import ALL_DISTRICTS, ReviewUpdate, SearchCreate
from app.search_engine import SearchEngine
from app.search_explanations import explain_search_result
from app.urban_plan_labels import telegram_urban_plan_line

logger = logging.getLogger(__name__)

EXCLUDED_SEARCH_QUEUE_TELEGRAM_USER_IDS = {"70557953"}
AUTO_URBAN_PLAN_WAIVER_KIND = "auto_no_approved_layer"
MANUAL_URBAN_PLAN_WAIVER_KIND = "manual"
AUTO_URBAN_PLAN_WAIVER_USER_ID = "system:auto-no-layer"

AUTO_URBAN_PLAN_WAIVER_TEXT = {
    "ru": (
        "Система автоматически продолжила предварительный анализ без проверки "
        "генплана/ПДП, потому что для выбранной территории в базе нет активного "
        "официального геопривязанного слоя, прошедшего QA и разрешенного для поиска. "
        "Результат требует дополнительной проверки в акимате."
    ),
    "kz": (
        "Жүйе бас жоспар/ЕЖЖ тексерісінсіз алдын ала талдауды автоматты түрде "
        "жалғастырды, себебі таңдалған аумақ бойынша базада QA-дан өткен және "
        "іздеуге рұқсат етілген ресми геобайланыстырылған қабат жоқ. Нәтижені "
        "әкімдікте қосымша тексеру қажет."
    ),
}


def search_queue_visible_condition():
    return or_(
        SearchRequest.telegram_user_id.is_(None),
        SearchRequest.telegram_user_id.not_in(EXCLUDED_SEARCH_QUEUE_TELEGRAM_USER_IDS),
    )


def active_search_queue_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(SearchRequest)
            .where(
                SearchRequest.status.in_(
                    [SearchStatus.queued.value, SearchStatus.processing.value]
                ),
                search_queue_visible_condition(),
            )
        )
        or 0
    )

URBAN_PLAN_OVERRIDE_TEXT = {
    "ru": (
        "Официальный геопривязанный слой генплана/ПДП для выбранного населенного пункта "
        "отсутствует. Я прошу продолжить предварительный поиск только по публичному слою "
        "ЕГКН и открытым данным OSM и понимаю, что результат не проверен по генплану, ПДП, "
        "красным линиям и может не соответствовать градостроительным ограничениям."
    ),
    "kz": (
        "Таңдалған елді мекен бойынша ресми геобайланыстырылған бас жоспар/ЕЖЖ қабаты жоқ. "
        "Мен іздеуді тек ЖМБМК жария қабаты және OSM ашық деректері бойынша жалғастыруды "
        "сұраймын және нәтиженің бас жоспар, ЕЖЖ және қызыл сызықтар бойынша тексерілмегенін, "
        "қала құрылысы шектеулеріне сәйкес келмеуі мүмкін екенін түсінемін."
    ),
}


def _urban_plan_waiver_is_auto(request: SearchRequest) -> bool:
    return request.urban_plan_waiver_kind == AUTO_URBAN_PLAN_WAIVER_KIND


def _urban_plan_waiver_text(request: SearchRequest, language: str) -> str:
    selected = "kz" if language == "kz" else "ru"
    if _urban_plan_waiver_is_auto(request):
        return (
            "⚠ генплан/ЕЖЖ жүйеде осы аумаққа жарамды цифрлық қабат болмағандықтан "
            "тексерілмеді."
            if selected == "kz"
            else "⚠ генплан/ПДП не проверен: в системе нет пригодного цифрового слоя "
            "для этой территории."
        )
    return (
        "⚠ бас жоспар сіздің жеке таңдауыңыз бойынша тексерілмеді."
        if selected == "kz"
        else "⚠ генплан не проверен по вашему отдельному выбору."
    )


def elapsed_seconds(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    """Return a safe duration while legacy database columns may be timezone-naive."""
    if started_at is None or finished_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)
    return max(0, int((finished_at - started_at).total_seconds()))


def telegram_request(method: str, payload: dict) -> dict:
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN не заполнен")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    for attempt in range(3):
        try:
            response = httpx.post(url, json=payload, timeout=httpx.Timeout(30, connect=10))
            try:
                result = response.json()
            except ValueError:
                result = {}
            description = str(result.get("description") or "неизвестная ошибка")

            if response.is_success and result.get("ok"):
                return result
            if (
                method == "editMessageText"
                and response.status_code == 400
                and "message is not modified" in description.lower()
            ):
                return {"ok": True, "result": None}
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = 2**attempt
                try:
                    retry_after = int(result.get("parameters", {}).get("retry_after", retry_after))
                except (TypeError, ValueError):
                    pass
                if attempt < 2:
                    time.sleep(min(retry_after, 10))
                    continue
            raise RuntimeError(
                f"Telegram API вернул ошибку {response.status_code}: {description}"
            )
        except httpx.RequestError:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise RuntimeError("Telegram API не ответил после повторных попыток") from None
    raise RuntimeError("Telegram API не ответил после повторных попыток")


def telegram_genplan_reply_markup(request: SearchRequest) -> dict[str, list[list[dict[str, str]]]]:
    reference = genplan_reference_payload(
        request,
        language=request.language,
        base_url=settings.app_base_url,
        manual_files_root=settings.manual_genplan_files_root,
    )
    return {"inline_keyboard": [[{"text": reference["action_text"], "url": reference["url"]}]]}


PROGRESS_PERCENT = {
    "boundaries": 20,
    "objects": 45,
    "area": 65,
    "planning": 85,
    "ranking": 95,
}


def update_search_progress(
    session: Session,
    request: SearchRequest,
    stage: str,
) -> None:
    target_progress = PROGRESS_PERCENT[stage]
    if request.progress >= target_progress:
        return
    request.progress = target_progress
    session.commit()
    if (
        not funnel_v2_enabled()
        or not request.telegram_chat_id
        or not request.progress_message_id
    ):
        return
    try:
        telegram_request(
            "editMessageText",
            {
                "chat_id": request.telegram_chat_id,
                "message_id": request.progress_message_id,
                "text": progress_message(request.language, stage),
                "parse_mode": "HTML",
            },
        )
    except Exception:
        logger.warning("Could not update progress message for %s", request.id, exc_info=True)


def announce_search_completed(
    session: Session,
    request: SearchRequest,
    count: int,
    *,
    notify_client: bool = True,
) -> None:
    if notify_client and request.search_completed_notified_at is not None:
        return
    was_finished = request.search_finished_at is not None
    notified_at: datetime | None = None
    if notify_client and request.telegram_chat_id and funnel_v2_enabled():
        payload = {
            "chat_id": request.telegram_chat_id,
            "text": completed_message(
                request.language,
                count,
                plan_waived=request.urban_plan_status == UrbanPlanStatus.waived.value,
            ),
            "parse_mode": "HTML",
        }
        try:
            if request.progress_message_id:
                payload["message_id"] = request.progress_message_id
                telegram_request("editMessageText", payload)
            else:
                telegram_request("sendMessage", payload)
            notified_at = datetime.now(UTC)
        except Exception:
            logger.warning("Could not announce completed search %s", request.id, exc_info=True)
    elif notify_client and request.telegram_chat_id:
        notified_at = datetime.now(UTC)

    if notified_at is not None:
        request.search_completed_notified_at = notified_at
    request.search_finished_at = request.search_finished_at or datetime.now(UTC)
    session.commit()
    if was_finished:
        return
    duration_seconds = elapsed_seconds(request.search_started_at, request.search_finished_at)
    track_funnel_event(
        session,
        "search_completed",
        telegram_user_id=request.telegram_user_id,
        telegram_chat_id=request.telegram_chat_id,
        request_id=request.id,
        funnel_session_id=request.funnel_session_id,
        language=request.language,
        metadata={
            "candidate_count": count,
            "urban_plan_status": request.urban_plan_status,
            "outcome": request.search_outcome or ("candidates_found" if count else "no_candidates"),
            "duration_seconds": duration_seconds,
        },
    )


def approved_candidates(request: SearchRequest) -> list[Candidate]:
    return [
        item
        for item in request.candidates
        if item.review_status
        in {ReviewStatus.approved.value, ReviewStatus.approved_with_note.value}
        and item.urban_plan_status in {UrbanPlanStatus.passed.value, UrbanPlanStatus.waived.value}
    ]


def create_search(session: Session, payload: SearchCreate) -> tuple[SearchRequest, int]:
    request = SearchRequest(**payload.model_dump())
    session.add(request)
    session.commit()
    session.refresh(request)
    queued = active_search_queue_count(session)
    return request, queued or 1


def has_paid_access(
    session: Session,
    telegram_user_id: str | None,
    *,
    exclude_request_id: str | None = None,
) -> bool:
    return has_platform_access(
        session,
        telegram_user_id,
        exclude_search_request_id=exclude_request_id,
    )


def delivered_coordinates(session: Session, request: SearchRequest) -> list[tuple[float, float]]:
    if not request.continuation_of_request_id:
        return []
    identity_conditions = []
    if request.telegram_user_id:
        identity_conditions.append(SearchRequest.telegram_user_id == request.telegram_user_id)
    if request.web_account_id:
        identity_conditions.append(SearchRequest.web_account_id == request.web_account_id)
    if not identity_conditions:
        return []
    rows = session.execute(
        select(Candidate.latitude, Candidate.longitude)
        .join(SearchRequest, Candidate.request_id == SearchRequest.id)
        .where(
            or_(*identity_conditions),
            SearchRequest.region == request.region,
            SearchRequest.district == request.district,
            SearchRequest.locality == request.locality,
            Candidate.delivered_at.is_not(None),
        )
    ).all()
    return [(float(latitude), float(longitude)) for latitude, longitude in rows]


def create_next_batch(
    session: Session,
    source_request_id: str,
    *,
    telegram_user_id: str | None,
    telegram_chat_id: str | None,
    web_account_id: str | None = None,
    require_paid_access: bool = True,
) -> tuple[SearchRequest, int, bool]:
    source = get_request_with_candidates(session, source_request_id)
    if source is None:
        raise LookupError("Заявка не найдена")
    same_telegram = bool(
        telegram_user_id
        and source.telegram_user_id == telegram_user_id
        and (not source.telegram_chat_id or source.telegram_chat_id == telegram_chat_id)
    )
    same_web = bool(web_account_id and source.web_account_id == web_account_id)
    if not (same_telegram or same_web):
        raise PermissionError("Эта кнопка относится к другой заявке")
    if require_paid_access and not has_paid_access(session, telegram_user_id):
        raise PermissionError("Сначала необходимо активировать оплаченный доступ")
    if same_web:
        status_is_eligible = source.status in {
            SearchStatus.ready.value,
            SearchStatus.delivered.value,
        }
    else:
        status_is_eligible = source.status == SearchStatus.delivered.value
    if not status_is_eligible:
        raise ValueError("Текущий пакет еще не отправлен")
    delivered_count = sum(item.delivered_at is not None for item in source.candidates)
    if same_web and delivered_count < len(source.candidates):
        delivered_at = datetime.now(UTC)
        for candidate in source.candidates:
            candidate.delivered_at = candidate.delivered_at or delivered_at
        delivered_count = len(source.candidates)
    if delivered_count < source.result_limit:
        raise ValueError("В этом населенном пункте новых вариантов больше не найдено")

    existing = session.scalar(
        select(SearchRequest).where(SearchRequest.continuation_of_request_id == source.id)
    )
    if existing is not None:
        return existing, 0, False

    request = SearchRequest(
        web_account_id=web_account_id or source.web_account_id,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        language=source.language,
        region=source.region,
        region_label=source.region_label,
        district=source.district,
        district_label=source.district_label,
        locality=source.locality,
        locality_label=source.locality_label,
        purpose=source.purpose,
        allotment_type=source.allotment_type,
        irrigation_type=source.irrigation_type,
        area_ha=source.area_ha,
        result_limit=source.result_limit,
        cemetery_buffer_m=source.cemetery_buffer_m,
        max_road_distance_m=source.max_road_distance_m,
        max_power_distance_m=source.max_power_distance_m,
        raw_query=source.raw_query,
        continuation_of_request_id=source.id,
        batch_number=source.batch_number + 1,
        terms_version=source.terms_version,
        terms_text_snapshot=source.terms_text_snapshot,
        terms_accepted_at=source.terms_accepted_at,
        urban_plan_override_accepted_at=source.urban_plan_override_accepted_at,
        urban_plan_override_user_id=source.urban_plan_override_user_id,
        urban_plan_override_text=source.urban_plan_override_text,
    )
    session.add(request)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(SearchRequest).where(SearchRequest.continuation_of_request_id == source.id)
        )
        if existing is None:
            raise
        return existing, 0, False
    session.refresh(request)
    queued = active_search_queue_count(session)
    return request, queued or 1, True


def retry_failed_search(
    session: Session,
    source_request_id: str,
    *,
    telegram_user_id: str | None,
    telegram_chat_id: str | None,
) -> tuple[SearchRequest, int, bool]:
    source = session.get(SearchRequest, source_request_id)
    if source is None:
        raise LookupError("Заявка не найдена")
    if source.telegram_user_id and source.telegram_user_id != telegram_user_id:
        raise PermissionError("Эта заявка принадлежит другому пользователю")
    if source.telegram_chat_id and source.telegram_chat_id != telegram_chat_id:
        raise PermissionError("Эта заявка создана в другом чате")
    if source.status != SearchStatus.failed.value:
        raise ValueError("Повторить можно только поиск, завершившийся технической ошибкой")

    existing = session.scalar(
        select(SearchRequest).where(SearchRequest.retry_of_request_id == source.id)
    )
    if existing is not None:
        return existing, 0, False

    request = SearchRequest(
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        language=source.language,
        region=source.region,
        region_label=source.region_label,
        district=source.district,
        district_label=source.district_label,
        locality=source.locality,
        locality_label=source.locality_label,
        purpose=source.purpose,
        allotment_type=source.allotment_type,
        irrigation_type=source.irrigation_type,
        area_ha=source.area_ha,
        result_limit=source.result_limit,
        cemetery_buffer_m=source.cemetery_buffer_m,
        max_road_distance_m=source.max_road_distance_m,
        max_power_distance_m=source.max_power_distance_m,
        raw_query=source.raw_query,
        retry_of_request_id=source.id,
        batch_number=source.batch_number,
        terms_version=source.terms_version,
        terms_text_snapshot=source.terms_text_snapshot,
        terms_accepted_at=source.terms_accepted_at,
        urban_plan_override_accepted_at=source.urban_plan_override_accepted_at,
        urban_plan_override_user_id=source.urban_plan_override_user_id,
        urban_plan_override_text=source.urban_plan_override_text,
    )
    session.add(request)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(SearchRequest).where(SearchRequest.retry_of_request_id == source.id)
        )
        if existing is None:
            raise
        return existing, 0, False
    session.refresh(request)
    queued = active_search_queue_count(session)
    return request, queued or 1, True


def process_search(
    session: Session,
    request_id: str,
    *,
    search_engine: SearchEngine | None = None,
) -> SearchRequest:
    request = session.scalar(
        select(SearchRequest).where(SearchRequest.id == request_id).with_for_update()
    )
    if request is None:
        raise LookupError(f"Search request {request_id} was not found")
    if request.status != SearchStatus.queued.value:
        logger.info(
            "Skipping search request %s because status is %s",
            request_id,
            request.status,
        )
        return request

    request.status = SearchStatus.processing.value
    request.progress = 20
    request.error_message = None
    request.search_started_at = datetime.now(UTC)
    request.search_finished_at = None
    request.search_outcome = None
    request.urban_plan_status = UrbanPlanStatus.pending.value
    request.urban_plan_message = None
    request.urban_plan_checked_at = None
    session.commit()
    track_funnel_event(
        session,
        "search_started",
        telegram_user_id=request.telegram_user_id,
        telegram_chat_id=request.telegram_chat_id,
        request_id=request.id,
        funnel_session_id=request.funnel_session_id,
        language=request.language,
    )
    update_search_progress(session, request, "boundaries")

    try:
        restriction = legal_restriction_reason(
            region=request.region,
            district=request.district,
            locality=request.locality,
            purpose=request.purpose,
            language=request.language,
        )
        if restriction:
            request.error_message = restriction
            request.status = SearchStatus.ready.value
            request.progress = 100
            request.search_outcome = "legal_restriction"
            session.commit()
            route_ready_report(session, request.id)
            session.refresh(request)
            return request
        payload = SearchCreate(
            language=request.language,
            region=request.region,
            region_label=request.region_label,
            district=request.district,
            district_label=request.district_label,
            locality=request.locality,
            locality_label=request.locality_label,
            purpose=request.purpose,
            allotment_type=request.allotment_type,
            irrigation_type=request.irrigation_type,
            area_ha=request.area_ha,
            result_limit=request.result_limit,
            cemetery_buffer_m=request.cemetery_buffer_m,
            max_road_distance_m=request.max_road_distance_m,
            max_power_distance_m=request.max_power_distance_m,
            raw_query=request.raw_query,
            telegram_user_id=request.telegram_user_id,
            telegram_chat_id=request.telegram_chat_id,
            terms_version=request.terms_version,
            terms_text_snapshot=request.terms_text_snapshot,
            terms_accepted_at=request.terms_accepted_at,
            excluded_coordinates=delivered_coordinates(session, request),
            urban_plan_allowed_geojsons=allowed_search_area_geojsons(session, request),
        )
        engine = search_engine or SearchEngine(
            progress_callback=lambda stage: update_search_progress(
                session,
                request,
                stage,
            )
        )
        results = engine.search(payload)
        if search_engine is not None:
            update_search_progress(session, request, "objects")
            update_search_progress(session, request, "area")
        session.query(Candidate).filter(Candidate.request_id == request_id).delete()

        for rank, result in enumerate(results, start=1):
            session.add(
                Candidate(
                    request_id=request.id,
                    rank=rank,
                    region_chain=result.region_chain,
                    locality=result.locality,
                    latitude=result.latitude,
                    longitude=result.longitude,
                    nearby_cadastre=result.nearby_cadastre,
                    nearby_distance_m=result.nearby_distance_m,
                    nearby_land_use=result.nearby_land_use or request.purpose,
                    nearby_category_id=result.nearby_category_id,
                    requested_area_ha=request.area_ha,
                    road_distance_m=result.road_distance_m,
                    power_evidence=result.power_evidence,
                    water_evidence=result.water_evidence,
                    sewer_evidence=result.sewer_evidence,
                    cemetery_distance_m=result.cemetery_distance_m,
                    score=result.score,
                    risk_notes=result.risk_notes,
                    google_maps_url=result.google_maps_url,
                )
            )

        if not results:
            purpose_name = purpose_label(request.purpose, "ru")
            request.error_message = (
                "ЕГКН не показал геометрических промежутков, куда помещается участок "
                f"площадью {request.area_ha:.2f} га рядом с зарегистрированными "
                f"участками назначения «{purpose_name}»."
            )
            request.status = SearchStatus.ready.value
            request.progress = 100
            request.search_outcome = "no_candidates"
            session.commit()
            route_ready_report(session, request.id)
            session.refresh(request)
            return request

        session.commit()
        request = get_request_with_candidates(session, request_id)
        if request is None:
            raise LookupError(f"Search request {request_id} was not found after candidate insert")
        update_search_progress(session, request, "planning")
        apply_candidate_decisions(session, request)
        update_search_progress(session, request, "ranking")
        request.status = SearchStatus.ready.value
        request.progress = 100
        approved = approved_candidates(request)
        if not approved:
            request.error_message = request.urban_plan_message or (
                "Совместная проверка ЕГКН, дорог/объектов и генплана/ПДП не оставила "
                "подходящих вариантов. Оплата не запрашивалась."
            )
        session.commit()

        if not approved:
            request.search_outcome = (
                "urban_plan_unavailable"
                if request.urban_plan_status == UrbanPlanStatus.unavailable.value
                else "filtered_out"
            )
            session.commit()
            route_ready_report(session, request.id)
        elif request.telegram_chat_id:
            request.search_outcome = "candidates_found"
            session.commit()
            route_ready_report(session, request.id)
        session.refresh(request)
        return request
    except ProviderCallDeferred as exc:
        # A deferred provider call is not an active computation. Persist the
        # queued state before Celery schedules the next attempt.
        request.status = SearchStatus.queued.value
        request.progress = 10
        request.error_message = (
            f"Публичный сервис {exc.provider} временно ограничил запросы; "
            "заявка будет повторена автоматически."
        )
        request.search_outcome = None
        request.search_finished_at = None
        session.commit()
        raise
    except Exception as exc:
        request.status = SearchStatus.failed.value
        request.error_message = str(exc)
        request.search_finished_at = datetime.now(UTC)
        request.search_outcome = "technical_error"
        session.commit()
        raise


def apply_candidate_decisions(session: Session, request: SearchRequest) -> None:
    for candidate in request.candidates:
        candidate.google_checked = False
        candidate.google_checked_at = None
        candidate.reviewer = "egkn geometry verifier"
        candidate.review_notes = (
            "По публичному слою ЕГКН квадрат "
            f"{request.area_ha:.2f} га целиком помещается в геометрический промежуток "
            "без пересечения зарегистрированных участков. Соседний кадастровый номер "
            "служит только ориентиром. Юридическую свободу земли и фактическое состояние "
            "местности должен подтвердить акимат."
        )
        candidate.review_status = ReviewStatus.pending.value

    evaluation = evaluate_urban_plan(session, request, list(request.candidates))
    request.urban_plan_checked_at = datetime.now(UTC)
    request.urban_plan_message = evaluation.message
    request.urban_plan_coverage_id = evaluation.coverage_id
    request.urban_plan_coverage_status = evaluation.coverage_status
    override_active = bool(request.urban_plan_override_accepted_at)
    auto_waive_active = (
        not evaluation.coverage_available
        and not override_active
        and all(
            decision.status == UrbanPlanStatus.unavailable.value
            for decision in evaluation.decisions
        )
        and evaluation.coverage_status in {None, "unavailable", "broken"}
    )
    if auto_waive_active:
        language = "kz" if request.language == "kz" else "ru"
        request.urban_plan_override_accepted_at = datetime.now(UTC)
        request.urban_plan_override_user_id = AUTO_URBAN_PLAN_WAIVER_USER_ID
        request.urban_plan_override_text = AUTO_URBAN_PLAN_WAIVER_TEXT[language]
        request.urban_plan_waiver_kind = AUTO_URBAN_PLAN_WAIVER_KIND
        request.urban_plan_auto_waive_reason = evaluation.message
        override_active = True
    if not evaluation.coverage_available and override_active:
        request.urban_plan_status = UrbanPlanStatus.waived.value
        request.urban_plan_message = (
            "Предварительный результат будет выдан без проверки генплана/ПДП, "
            "потому что для выбранной территории нет пригодного цифрового слоя."
            if auto_waive_active
            else "Пользователь явно согласился получить предварительный результат без "
            "проверки генплана/ПДП."
        )
    elif not evaluation.coverage_available:
        request.urban_plan_status = UrbanPlanStatus.unavailable.value
    elif any(item.status == UrbanPlanStatus.passed.value for item in evaluation.decisions):
        request.urban_plan_status = UrbanPlanStatus.passed.value
    else:
        request.urban_plan_status = UrbanPlanStatus.blocked.value

    for candidate, decision in zip(request.candidates, evaluation.decisions, strict=True):
        candidate.urban_plan_status = (
            UrbanPlanStatus.waived.value
            if decision.status == UrbanPlanStatus.unavailable.value and override_active
            else decision.status
        )
        candidate.urban_plan_zone = decision.zone
        candidate.urban_plan_document = decision.document
        candidate.urban_plan_source_url = decision.source_url
        if candidate.urban_plan_status == UrbanPlanStatus.waived.value:
            plan_note = (
                "Генплан/ПДП не проверен: в системе нет пригодного цифрового слоя "
                "для выбранной территории."
                if _urban_plan_waiver_is_auto(request)
                else "Генплан/ПДП не проверен по явному выбору пользователя."
            )
        else:
            plan_note = decision.message
        candidate.review_notes = f"{candidate.review_notes} {plan_note}".strip()
        candidate.review_status = (
            ReviewStatus.approved.value
            if candidate.urban_plan_status
            in {UrbanPlanStatus.passed.value, UrbanPlanStatus.waived.value}
            else ReviewStatus.rejected.value
        )


def apply_egkn_geometry_decisions(request: SearchRequest) -> None:
    """Compatibility helper for callers that only need the legacy EGKN annotation."""
    for candidate in request.candidates:
        candidate.review_status = ReviewStatus.approved.value


def notify_search_without_payment(request: SearchRequest, reason: str | None) -> None:
    if not request.telegram_chat_id:
        return
    if request.continuation_of_request_id:
        text = (
            (
                "Келесі топтамада жаңа нұсқалар табылмады.\n\n"
                "Бұрын жіберілген координаттар қайталанған жоқ.\n"
                "Сіздің ақылы қолжетімділігіңіз белсенді болып қалады."
                if request.language == "kz"
                else "В следующем пакете новые варианты не найдены.\n\n"
                "Ранее отправленные координаты не повторялись.\n"
                "Ваш оплаченный доступ остается активным."
            )
            if funnel_v2_enabled()
            else (
            "Келесі пакет бойынша жаңа учаскелер табылмады. Бұрын жіберілген "
            "нұсқалар қайталанбады. Ақылы қолжетімділік сақталады."
            if request.language == "kz"
            else "В следующем пакете новые участки не найдены. Уже отправленные "
            "варианты не повторялись. Оплаченный доступ остается активным."
            )
        )
        buttons = [
            [
                {
                    "text": t(request.language, "back_districts"),
                    "callback_data": f"search:districts:{request.id}",
                }
            ],
            [
                {
                    "text": t(request.language, "main_regions"),
                    "callback_data": f"search:regions:{request.id}",
                }
            ],
        ]
        if request.district != ALL_DISTRICTS:
            buttons.insert(
                0,
                [
                    {
                        "text": t(request.language, "choose_other_locality"),
                        "callback_data": f"search:localities:{request.id}",
                    }
                ],
            )
        payload = {
            "chat_id": request.telegram_chat_id,
            "text": text,
            "reply_markup": {"inline_keyboard": buttons},
        }
        telegram_request("sendMessage", payload)
        return
    if funnel_v2_enabled():
        explanation = explain_search_result(request)
        text = explanation.text
        if request.urban_plan_status == UrbanPlanStatus.unavailable.value:
            text += "\n\n" + URBAN_PLAN_OVERRIDE_TEXT[request.language]
    elif reason and (
        "предоставление земельных участков" in reason or "жер учаскелерін беру тоқтатылды" in reason
    ):
        text = reason + (
            "\n\nТөлем сұралмайды. Басқа елді мекенді таңдаңыз."
            if request.language == "kz"
            else "\n\nОплата не запрашивается. Выберите другой населенный пункт."
        )
    elif not funnel_v2_enabled() and request.urban_plan_status == UrbanPlanStatus.unavailable.value:
        text = (
            (
                "Ықтимал нұсқалар табылды\n\n"
                "Бірақ таңдалған аумақ бойынша ботта қала құрылысы шектеулерінің "
                "жарамды цифрлық картасы — бас жоспар немесе ЕЖЖ жоқ.\n\n"
                "Іздеуді кадастрлық шекаралар, жолдар және белгіленген нысандар бойынша "
                "жалғастыруға болады. Есепте бас жоспардың тексерілмегені анық "
                "көрсетіледі.\n\n"
                "Маңызды: мұндай нәтижені әкімдікте қосымша тексеру қажет.\n\n"
                + URBAN_PLAN_OVERRIDE_TEXT["kz"]
                if request.language == "kz"
                else "Найдены возможные варианты\n\n"
                "Но для выбранной территории у бота нет пригодной цифровой карты "
                "градостроительных ограничений — генплана или ПДП.\n\n"
                "Можно продолжить поиск по кадастровым границам, дорогам и отмеченным "
                "объектам. В отчете будет явно указано, что генплан не проверен.\n\n"
                "Важно: такой результат требует дополнительной проверки в акимате.\n\n"
                + URBAN_PLAN_OVERRIDE_TEXT["ru"]
            )
            if funnel_v2_enabled()
            else (
            "Таңдалған елді мекен бойынша жүйеде ресми геобайланыстырылған бас жоспар/ЕЖЖ "
            "қабаты жоқ немесе ол жарамсыз. Қаласаңыз, төмендегі батырма арқылы бас "
            "жоспарды тексермей, тек ЖМБМК және OSM бойынша алдын ала нәтижені сұрай аласыз.\n\n"
            + URBAN_PLAN_OVERRIDE_TEXT["kz"]
            if request.language == "kz"
            else "Для выбранного населенного пункта нет пригодного официального "
            "геопривязанного слоя генплана/ПДП. При желании можно запросить предварительный "
            "результат только по ЕГКН и OSM.\n\n" + URBAN_PLAN_OVERRIDE_TEXT["ru"]
            )
        )
    elif not funnel_v2_enabled() and request.urban_plan_status == UrbanPlanStatus.blocked.value:
        text = (
            (
                "Іздеу аяқталды\n\n"
                "Ықтимал орындар табылды, бірақ қолжетімді цифрлық картада қала "
                "құрылысы шектеулері көрсетілген.\n\n"
                "Күмәнді координаттарды жібермеу үшін бот бұл нұсқаларды алып тастады.\n\n"
                "Төлем қажет емес."
                if request.language == "kz"
                else "Поиск завершен\n\n"
                "Возможные места были найдены, но доступная цифровая карта показывает "
                "градостроительные ограничения.\n\n"
                "Чтобы не отправлять сомнительные координаты, бот исключил эти варианты.\n\n"
                "Оплата не требуется."
            )
            if funnel_v2_enabled()
            else (
            "ЖМБМК бойынша кандидаттар табылды, бірақ олардың ешқайсысы жүктелген ресми "
            "бас жоспар/ЕЖЖ тексеруінен өтпеді. Координаттар берілмейді, төлем сұралмайды."
            if request.language == "kz"
            else "ЕГКН показал расчетные промежутки, но ни один из них не прошел "
            "проверку по загруженному официальному генплану/ПДП. Координаты не "
            "выдаются, оплата не запрашивается."
            )
        )
    elif not funnel_v2_enabled() and request.search_outcome == "no_candidates":
        sotok = round(request.area_ha * 100)
        text = (
            "Іздеу аяқталды\n\n"
                f"Таңдалған аумақта {sotok} сотық жер толық орналасатын орын табылмады.\n\n"
                "Бас жоспар тексерісі басталған жоқ: алдымен кадастрлық картадан "
                "орын табылуы керек. Сондықтан бұл жағдайда бас жоспарсыз іздеуді "
                "жалғастыру нәтижені өзгертпейді және батырма көрсетілмейді.\n\n"
                "Басқа елді мекенді, ауданды немесе облысты таңдаңыз.\n\n"
                "Төлем қажет емес."
                if request.language == "kz"
                else "Поиск завершен\n\n"
                f"В выбранной территории не найдено места для участка {sotok} соток.\n\n"
                "Проверка генплана не запускалась: сначала нужно найти место по "
                "кадастровой карте. Поэтому поиск без проверки генплана здесь "
                "не изменит результат и кнопка не показывается.\n\n"
                "Выберите другой населённый пункт, район или область.\n\n"
                "Оплата не требуется."
        )
    elif funnel_v2_enabled() and request.language == "kz":
        sotok = round(request.area_ha * 100)
        text = (
            "Іздеу аяқталды\n\n"
            f"Таңдалған аумақта {sotok} сотық жер қолжетімді деректер бойынша толық "
            "орналасатын және міндетті тексерулерден өтетін орын табылмады.\n\n"
            "Басқа елді мекенді, ауданды немесе облысты таңдаңыз.\n\n"
            "Төлем қажет емес."
        )
    elif funnel_v2_enabled():
        sotok = round(request.area_ha * 100)
        text = (
            "Поиск завершен\n\n"
            f"В выбранной территории не найдено места, где участок площадью {sotok} "
            "соток помещается по доступным данным и проходит обязательные проверки.\n\n"
            "Попробуйте другой населенный пункт, район или область.\n\n"
            "Оплата не требуется."
        )
    elif request.language == "kz":
        purpose_kz = purpose_label(request.purpose, "kz")
        sotok = round(request.area_ha * 100)
        text = (
            f"Іздеу аяқталды. Табылғаны: {request.result_limit} нұсқаның 0-і.\n"
            f"ЖМБМК тіркелген «{purpose_kz}» учаскелерінің жанынан {sotok} сотық жер "
            "толық орналасатын "
            "геометриялық бос аралықты көрсетпеді.\n\n"
            "Жүйе 10 нұсқаға дейін іздейді және міндетті түрде 10 нұсқа табуды талап "
            "етпейді. Бұл елді мекенде бірде-бір сәйкес аралық табылмады.\n\n"
            "Төлем сұралмайды."
        )
    else:
        text = (
            f"Поиск завершен. Найдено: 0 из {request.result_limit}.\n"
            f"{reason or 'Попробуйте другой населенный пункт или параметры поиска.'}\n\n"
            "Система ищет до 10 вариантов и не требует найти ровно 10. В этом "
            "населенном пункте не найдено ни одного подходящего промежутка.\n\n"
            "Оплата не запрашивается."
        )
    buttons: list[list[dict[str, str]]] = []
    if request.urban_plan_status == UrbanPlanStatus.unavailable.value:
        buttons.append(
            [
                {
                    "text": (
                        "Бас жоспарды тексермей жалғастыру"
                        if request.language == "kz"
                        else "Продолжить без проверки генплана"
                    ),
                    "callback_data": f"urban:waive:{request.id}",
                }
            ]
        )
    buttons.append(
        [
            {
                "text": t(request.language, "retry_search"),
                "callback_data": f"search:retry:{request.id}",
            }
        ]
    )
    if request.district != ALL_DISTRICTS:
        buttons.append(
            [
                {
                    "text": t(request.language, "choose_other_locality"),
                    "callback_data": f"search:localities:{request.id}",
                }
            ]
        )
    buttons.extend(
        [
            [
                {
                    "text": t(request.language, "back_districts"),
                    "callback_data": f"search:districts:{request.id}",
                }
            ],
            [
                {
                    "text": t(request.language, "main_regions"),
                    "callback_data": f"search:regions:{request.id}",
                }
            ],
        ]
    )
    payload: dict = {
        "chat_id": request.telegram_chat_id,
        "text": text,
        "reply_markup": {"inline_keyboard": buttons},
    }
    telegram_request("sendMessage", payload)


def accept_urban_plan_override(
    session: Session,
    request_id: str,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
) -> tuple[SearchRequest, bool]:
    request = get_request_with_candidates(session, request_id)
    if request is None:
        raise LookupError("Заявка не найдена")
    if request.telegram_user_id != telegram_user_id or request.telegram_chat_id != telegram_chat_id:
        raise PermissionError("Эта кнопка относится к другой заявке")
    if request.urban_plan_override_accepted_at:
        return request, False
    if request.urban_plan_status != UrbanPlanStatus.unavailable.value:
        raise ValueError("Продолжить без генплана можно только когда официальный слой отсутствует")
    if not request.candidates:
        raise ValueError("Нет предварительных кандидатов для выдачи")

    language = "kz" if request.language == "kz" else "ru"
    accepted_at = datetime.now(UTC)
    request.urban_plan_override_accepted_at = accepted_at
    request.urban_plan_override_user_id = telegram_user_id
    request.urban_plan_override_text = URBAN_PLAN_OVERRIDE_TEXT[language]
    request.urban_plan_waiver_kind = MANUAL_URBAN_PLAN_WAIVER_KIND
    request.urban_plan_status = UrbanPlanStatus.waived.value
    request.urban_plan_message = (
        "Пользователь явно согласился получить предварительный результат без проверки генплана/ПДП."
    )
    request.error_message = None
    for candidate in request.candidates:
        if candidate.urban_plan_status != UrbanPlanStatus.unavailable.value:
            continue
        candidate.urban_plan_status = UrbanPlanStatus.waived.value
        candidate.review_status = ReviewStatus.approved.value
        candidate.reviewer = "user urban-plan waiver"
        candidate.review_notes = (
            "По публичному слою ЕГКН квадрат не пересекает зарегистрированные участки; "
            "дороги, объекты и водные объекты проверены по доступным данным OSM. "
            "Генплан/ПДП и красные "
            "линии не проверены по явному выбору пользователя."
        )
    session.commit()
    route_ready_report(session, request.id)
    session.refresh(request)
    return request, True


def notify_terminal_search_failure(request: SearchRequest) -> None:
    if not request.telegram_chat_id:
        return
    if funnel_v2_enabled():
        text = explain_search_result(request).text + (
            "\n\nАқша алынған жоқ. Тест/демо қолжетімділік сақталады."
            if request.language == "kz"
            else "\n\nДеньги не списывались. Тестовый/демо-доступ сохраняется."
        )
    elif request.language == "kz":
        text = (
            "Қайталама әрекеттерден кейін іздеуді аяқтау мүмкін болмады. ЖМБМК ашық "
            "сервисі немесе OSM жолдар мен нысандар сервисі уақытша жауап бермеген, "
            "не елді мекенді өңдеу тым ұзаққа созылған "
            "болуы мүмкін.\n\nНәтиже дайындалмады, төлем сұралмайды. Кейінірек қайта "
            f"іздеу үшін төмендегі батырманы басыңыз.\nӨтінім нөмірі: {request.id}\n"
            f"Мәртебе: /status {request.id}"
        )
    else:
        text = (
            "Поиск не удалось завершить после повторных попыток. Публичный сервис "
            "ЕГКН или сервис проверки дорог и объектов OSM мог временно не ответить, "
            "либо обработка населенного пункта заняла "
            "слишком много времени.\n\n"
            "Результат не сформирован, оплата не запрашивается. Запустите повторный "
            "поиск кнопкой ниже.\n"
            f"Номер заявки: {request.id}\n"
            f"Статус: /status {request.id}"
        )
    payload = {
        "chat_id": request.telegram_chat_id,
        "text": text,
        "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": t(request.language, "retry_search"),
                            "callback_data": f"search:retry:{request.id}",
                        }
                    ],
                    *(
                        [
                            [
                                {
                                    "text": t(
                                        request.language,
                                        "choose_other_locality",
                                    ),
                                    "callback_data": (f"search:localities:{request.id}"),
                                }
                            ]
                        ]
                        if request.district != ALL_DISTRICTS
                        else []
                    ),
                    [
                        {
                            "text": t(request.language, "back_districts"),
                            "callback_data": f"search:districts:{request.id}",
                        }
                    ],
                    [
                        {
                            "text": t(request.language, "main_regions"),
                            "callback_data": f"search:regions:{request.id}",
                        }
                    ],
                ]
            },
        }
    method = "sendMessage"
    if funnel_v2_enabled() and request.progress_message_id:
        payload["message_id"] = request.progress_message_id
        method = "editMessageText"
    telegram_request(method, payload)


def dispatch_search(request_id: str) -> None:
    if settings.run_tasks_inline:
        Thread(
            target=_process_search_in_background,
            args=(request_id,),
            name=f"land-search-{request_id[:8]}",
            daemon=True,
        ).start()
    else:
        from app.tasks import process_search_task

        process_search_task.delay(request_id)


def _process_search_in_background(request_id: str) -> None:
    from app.db import SessionLocal

    try:
        with SessionLocal() as session:
            process_search(session, request_id)
    except Exception:
        logger.exception("Background search %s failed", request_id)
        try:
            with SessionLocal() as session:
                request = session.get(SearchRequest, request_id)
                if request is not None:
                    notify_terminal_search_failure(request)
        except Exception:
            logger.exception("Could not notify about failed background search %s", request_id)


def update_candidate_review(
    session: Session, candidate_id: int, payload: ReviewUpdate
) -> Candidate:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise LookupError("Кандидат не найден")
    allowed = {item.value for item in ReviewStatus}
    if payload.status not in allowed:
        raise ValueError("Недопустимый статус проверки")
    if payload.status in {
        ReviewStatus.approved.value,
        ReviewStatus.approved_with_note.value,
    } and candidate.urban_plan_status not in {
        UrbanPlanStatus.passed.value,
        UrbanPlanStatus.waived.value,
    }:
        raise ValueError(
            "Нельзя одобрить кандидата, который не прошел строгую проверку генплана/ПДП"
        )
    candidate.review_status = payload.status
    candidate.google_checked = payload.google_checked
    candidate.review_notes = payload.notes.strip()
    candidate.reviewer = payload.reviewer.strip() or "operator"
    candidate.google_checked_at = datetime.now(UTC) if payload.google_checked else None
    session.commit()
    session.refresh(candidate)
    return candidate


def get_request_with_candidates(session: Session, request_id: str) -> SearchRequest | None:
    return session.scalar(
        select(SearchRequest)
        .options(selectinload(SearchRequest.candidates))
        .where(SearchRequest.id == request_id)
    )


def free_preview_usage(session: Session, telegram_user_id: str) -> int:
    value = session.scalar(
        select(func.coalesce(func.sum(SearchRequest.free_preview_count), 0)).where(
            SearchRequest.telegram_user_id == telegram_user_id,
            SearchRequest.free_preview_status.in_(
                [FreePreviewStatus.pending.value, FreePreviewStatus.delivered.value]
            ),
        )
    )
    return int(value or 0)


def reserve_free_preview(session: Session, request_id: str) -> int:
    request = get_request_with_candidates(session, request_id)
    if request is None:
        raise LookupError("Заявка не найдена")
    if not request.telegram_user_id or not request.telegram_chat_id:
        return 0
    if request.free_preview_status in {
        FreePreviewStatus.pending.value,
        FreePreviewStatus.delivered.value,
    }:
        return request.free_preview_count
    if request.free_preview_status == FreePreviewStatus.rejected.value:
        return 0

    count = min(request.result_limit, len(approved_candidates(request)))
    if count <= 0:
        return 0

    request.free_preview_status = FreePreviewStatus.pending.value
    request.free_preview_count = count
    session.commit()
    session.refresh(request)
    return count


def notify_paid_search_unavailable(request: SearchRequest) -> None:
    if not request.telegram_chat_id:
        return
    text = (
        "Алдын ала нұсқалар көрсетілді, бірақ толық есептерді ашу әзірге "
        "уақытша өшірілген. Төлем сұралмайды."
        if request.language == "kz"
        else "Предварительные варианты показаны, но разблокировка полных отчетов "
        "сейчас временно отключена. Оплата не запрашивается."
    )
    telegram_request("sendMessage", {"chat_id": request.telegram_chat_id, "text": text})


def offer_paid_report(request: SearchRequest, session: Session | None = None) -> None:
    if not request.telegram_chat_id:
        return
    approved = approved_candidates(request)
    if not approved:
        return
    price = f"{settings.platform_access_price_kzt:,}".replace(",", " ")
    remaining_count = len(approved)
    if funnel_v2_enabled():
        text, button_text = paid_offer_message(request.language, remaining_count)
    elif request.language == "kz":
        text = (
            "Алдын ала нұсқалар көрсетілді.\n"
            f"Осы өтінім бойынша табылған нұсқалар: {len(approved)}. "
            f"Толық есепті және бірыңғай 1 айлық қолжетімділікті ашу бағасы: {price} ₸. "
            "Расталғаннан кейін осы Telegram user ID аумақ талдауы мен жер аукциондарын "
            "осы кезеңде пайдаланады.\n"
            "Kaspi арқылы төлем бетіне өту үшін төмендегі батырманы басыңыз."
        )
        button_text = f"Толық есепті ашу — {price} ₸"
    else:
        text = (
            "Предварительные варианты уже показаны.\n"
            f"По этой заявке найдено вариантов: {len(approved)}. "
            "Разблокировка полного отчета и единого доступа на 1 месяц: "
            f"{price} ₸. После подтверждения "
            "этот Telegram user ID сможет в течение оплаченного периода запускать "
            "анализ территории "
            "и пользоваться земельными аукционами.\n"
            "Чтобы перейти на страницу оплаты через Kaspi, нажмите кнопку ниже."
        )
        button_text = f"Разблокировать полный отчет — {price} ₸"
    payload = {
        "chat_id": request.telegram_chat_id,
        "text": text,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": button_text,
                        "callback_data": f"pay:start:{request.id}",
                    }
                ]
            ]
        },
    }
    if funnel_v2_enabled():
        payload["parse_mode"] = "HTML"
    telegram_request("sendMessage", payload)
    if session is not None:
        track_funnel_event(
            session,
            "paywall_viewed",
            telegram_user_id=request.telegram_user_id,
            telegram_chat_id=request.telegram_chat_id,
            request_id=request.id,
            language=request.language,
            metadata={
                "candidate_count": len(approved),
                "remaining_count": remaining_count,
                "price_kzt": settings.platform_access_price_kzt,
            },
        )


def route_ready_report(session: Session, request_id: str) -> SearchRequest:
    request = get_request_with_candidates(session, request_id)
    if request is None:
        raise LookupError("Заявка не найдена")
    approved = approved_candidates(request)
    if not approved:
        if not request.error_message:
            request.error_message = (
                "По выбранной территории найдены предварительные места, но ни одно не прошло "
                "обязательные проверки. Оплата не запрашивается."
            )
        request.search_outcome = request.search_outcome or "filtered_out"
        session.commit()
        was_finished = request.search_finished_at is not None
        notify_search_without_payment(request, request.error_message)
        delivered_at = datetime.now(UTC)
        request.search_completed_notified_at = delivered_at
        request.search_finished_at = request.search_finished_at or delivered_at
        session.commit()
        if not was_finished:
            track_funnel_event(
                session,
                "search_completed",
                telegram_user_id=request.telegram_user_id,
                telegram_chat_id=request.telegram_chat_id,
                request_id=request.id,
                funnel_session_id=request.funnel_session_id,
                language=request.language,
                metadata={
                    "candidate_count": 0,
                    "urban_plan_status": request.urban_plan_status,
                    "outcome": request.search_outcome or "filtered_out",
                    "duration_seconds": elapsed_seconds(
                        request.search_started_at, request.search_finished_at
                    ),
                },
            )
        return request
    announce_search_completed(session, request, len(approved))
    if has_paid_access(session, request.telegram_user_id):
        deliver_request(session, request.id)
        return request
    if not request.telegram_chat_id:
        request.search_completed_notified_at = request.search_completed_notified_at or datetime.now(
            UTC
        )
        request.search_finished_at = request.search_finished_at or datetime.now(UTC)
        session.commit()
        return request
    if settings.free_preview_enabled and reserve_free_preview(session, request_id) > 0:
        approve_free_preview(session, request_id, approved_by="automatic")
        return request
    if settings.paid_search_enabled:
        offer_paid_report(request, session)
        request.search_completed_notified_at = request.search_completed_notified_at or datetime.now(
            UTC
        )
        request.search_finished_at = request.search_finished_at or datetime.now(UTC)
        session.commit()
        return request
    notify_paid_search_unavailable(request)
    request.search_completed_notified_at = request.search_completed_notified_at or datetime.now(UTC)
    request.search_finished_at = request.search_finished_at or datetime.now(UTC)
    session.commit()
    return request


def request_has_event(session: Session, request_id: str, event_name: str) -> bool:
    return (
        session.scalar(
            select(FunnelEvent.id)
            .where(
                FunnelEvent.request_id == request_id,
                FunnelEvent.event_name == event_name,
            )
            .limit(1)
        )
        is not None
    )


def ensure_ready_delivery(session: Session, request_id: str) -> bool:
    request = get_request_with_candidates(session, request_id)
    if request is None:
        return False
    if request.status not in {SearchStatus.ready.value, SearchStatus.delivered.value}:
        return False
    approved = approved_candidates(request)
    delivered_count = sum(1 for candidate in approved if candidate.delivered_at is not None)
    if request.status == SearchStatus.delivered.value:
        if (
            approved
            and request.free_preview_status == FreePreviewStatus.delivered.value
            and settings.paid_search_enabled
            and not has_paid_access(session, request.telegram_user_id)
            and not request_has_event(session, request.id, "paywall_viewed")
        ):
            offer_paid_report(request, session)
            return True
        if request.search_completed_notified_at is None:
            request.search_completed_notified_at = datetime.now(UTC)
            session.commit()
            return True
        return False
    if approved and delivered_count == 0 and request.telegram_chat_id:
        if (
            request.free_preview_status == FreePreviewStatus.delivered.value
            and request.free_preview_count > 0
        ):
            request.free_preview_status = FreePreviewStatus.pending.value
            session.commit()
        route_ready_report(session, request.id)
        return True
    if (
        approved
        and request.free_preview_status == FreePreviewStatus.delivered.value
        and settings.paid_search_enabled
        and not has_paid_access(session, request.telegram_user_id)
        and not request_has_event(session, request.id, "paywall_viewed")
    ):
        offer_paid_report(request, session)
        request.search_completed_notified_at = request.search_completed_notified_at or datetime.now(
            UTC
        )
        request.search_finished_at = request.search_finished_at or datetime.now(UTC)
        session.commit()
        return True
    if request.search_completed_notified_at is None:
        route_ready_report(session, request.id)
        return True
    return False


def format_telegram_result(
    request: SearchRequest,
    candidates: list[Candidate] | None = None,
    *,
    free_preview: bool = False,
) -> str:
    approved = candidates if candidates is not None else approved_candidates(request)
    if not approved:
        raise ValueError("Нет кандидатов, прошедших все обязательные проверки")
    if request.language == "kz":
        return format_telegram_result_kz(request, approved, free_preview=free_preview)

    urban_plan_waived = request.urban_plan_status == UrbanPlanStatus.waived.value
    purpose_ru = purpose_label(request.purpose, "ru")
    is_new_lph = normalize_purpose(request.purpose) == LPH_NEW
    if funnel_v2_enabled() and is_new_lph:
        purpose_ru = "ЛПХ"
    checked_at = max(item.source_checked_at for item in approved).strftime("%d.%m.%Y %H:%M UTC")
    region = request.region_label or request.region
    district = request.district_label or request.district
    genplan_reference = genplan_reference_payload(
        request,
        language=request.language,
        base_url=settings.app_base_url,
        manual_files_root=settings.manual_genplan_files_root,
    )
    locality = request.locality_label or request.locality or "территория района"
    if funnel_v2_enabled():
        lines = [
            "🗺 Результаты поиска",
            "",
            f"📍 {region} → {district}",
            f"🏘 {locality}",
            f"🏡 {purpose_ru}",
            f"📐 {round(request.area_ha * 100)} соток ({request.area_ha:.2f} га)",
            f"📦 Пакет {request.batch_number} · вариантов {len(approved)}",
        ]
    else:
        lines = [
            "🔎 ПРЕДВАРИТЕЛЬНЫЕ ВАРИАНТЫ"
            if free_preview
            else "🗺 ИНФОРМАЦИОННЫЙ ОТЧЕТ",
            "",
            f"📍 {region} → {district}",
            f"🏘 {locality}",
            f"🎯 {purpose_ru}",
            f"📐 {request.area_ha:.2f} га",
            f"📦 Пакет {request.batch_number} · найдено {len(approved)}",
        ]
    if False and is_new_lph:
        lines.extend(
            [
                f"Вид надела: {allotment_label(request.allotment_type)}",
                f"Расчетный профиль: {irrigation_label(request.irrigation_type)}",
            ]
        )
    if funnel_v2_enabled():
        lines.extend(
            [
                "",
                "Что проверено:",
                "✔ выбранная площадь помещается между зарегистрированными участками;",
                "✔ исключены отмеченные дороги, объекты и водные объекты;",
                telegram_urban_plan_line(
                    request.urban_plan_status,
                    language="ru",
                    reference_source_kind=genplan_reference.get("source_kind"),
                ),
                *(
                    []
                    if urban_plan_waived
                    else [
                        "ℹ️ Зона генплана показывает допустимое использование "
                        "территории, а не наличие здания на месте."
                    ]
                ),
                f"Данные проверены: {checked_at}",
            ]
        )
    else:
        lines.extend(
            [
                "",
                (
                (
                    "⚠️ БЕЗ ПРОВЕРКИ ГЕНПЛАНА/ПДП: в системе нет пригодного "
                    "цифрового слоя для этой территории."
                    if _urban_plan_waiver_is_auto(request)
                    else "⚠️ БЕЗ ПРОВЕРКИ ГЕНПЛАНА/ПДП по вашему отдельному выбору."
                )
                if urban_plan_waived
                else "✅ Проверены геометрия ЕГКН, фильтр дорог/объектов OSM и "
                "доступный официальный слой генплана/ПДП."
            ),
            *(
                []
                if urban_plan_waived
                else [
                    "ℹ️ Зона генплана показывает разрешенное использование территории, "
                    "а не наличие здания на месте."
                ]
            ),
            f"🕒 Данные проверены: {checked_at}",
            ]
        )
    for display_rank, item in enumerate(approved[: request.result_limit], start=1):
        cemetery = (
            f"около {item.cemetery_distance_m:.0f} м"
            if item.cemetery_distance_m is not None
            else "рядом не найдено в открытых данных"
        )
        distance = (
            f"{item.nearby_distance_m:.0f} м"
            if item.nearby_distance_m is not None
            else "нет данных"
        )
        plan = "не проверен" if urban_plan_waived else item.urban_plan_zone or "допустимый слой"
        if funnel_v2_enabled():
            if _urban_plan_waiver_is_auto(request):
                note = (
                    "Площадь помещается между кадастровыми границами; отмеченные дороги, "
                    "объекты и водные объекты исключены. Генплан не проверен: "
                    "нет пригодного цифрового слоя."
                )
            elif urban_plan_waived:
                note = (
                    "Площадь помещается между кадастровыми границами; отмеченные дороги, "
                    "объекты и водные объекты исключены. Генплан не проверен."
                )
            else:
                note = (
                    "Площадь помещается между кадастровыми границами; обязательные "
                    "автоматические проверки пройдены."
                )
        else:
            note = (item.review_notes or item.risk_notes or "").strip()
        if len(note) > 220:
            note = note[:217].rstrip() + "..."
        lines.extend(
            [
                "",
                "────────────",
                f"📌 {'Вариант' if funnel_v2_enabled() else 'УЧАСТОК'} {display_rank}",
                "",
                (
                    "🔒 Координаты и карта доступны в полном отчете после оплаты"
                    if free_preview
                    else f"📍 Координаты: {item.latitude:.6f}, {item.longitude:.6f}"
                ),
                f"🗺 Район: {item.region_chain}",
                (
                    "🔒 Кадастровый ориентир откроется после оплаты"
                    if free_preview
                    else (
                        f"🔢 Соседний кадастровый номер: {item.nearby_cadastre}"
                        if funnel_v2_enabled()
                        else f"🔢 Рядом с кадастровым номером: {item.nearby_cadastre}"
                    )
                ),
                f"↔️ Расстояние до ориентира: {distance}",
                f"🏷 Назначение соседнего участка: {item.nearby_land_use or 'нет данных'}",
                (
                    f"🏙 Что допускает генплан: {plan}"
                    if funnel_v2_enabled()
                    else f"🏙 Разрешенная зона генплана/ПДП: {plan}"
                ),
                f"🪦 Кладбище: {cemetery}",
            ]
        )
        if note:
            lines.append(
                f"ℹ️ {'Комментарий проверки' if funnel_v2_enabled() else 'Проверка'}: {note}"
            )
        if not free_preview:
            lines.append(f"🔗 Открыть карту: {item.google_maps_url}")
    if free_preview:
        lines.extend(
            [
                "",
                "────────────",
                "🔒 В полном отчете откроются:",
                "координаты, карта, ЕГКН, кадастровый ориентир и текст для ручной проверки.",
                "",
                "Важно: это расчетные места, а не официально свободные участки. "
                "Финальное решение принимает акимат.",
            ]
        )
        return "\n".join(lines)
    lines.extend(
        [
            "",
            "────────────",
            "📝 ФОРМУЛИРОВКА ДЛЯ ОБРАЩЕНИЯ",
            "",
            "Прошу предоставить земельный участок площадью "
            f"{request.area_ha:.2f} га для "
            f"{purpose_activity_phrase(request.purpose, allotment_type=request.allotment_type)}, "
            + ""
            + "ориентировочно рядом с земельным участком с кадастровым номером ________.",
            "",
            "⚠️ ВАЖНО",
            "Это расчетные места, а не официально свободные участки. Указанный "
            "кадастровый номер принадлежит соседнему участку и служит ориентиром. "
            "Правовой статус, возможность предоставления, сети и фактическое состояние "
            "подтверждают акимат и осмотр на местности."
            + (" Отчет не подтверждает вид надела или орошаемость земли." if is_new_lph else ""),
            "",
            "⚖️ Полные юридические условия: /terms",
            "📚 Источники: публичная кадастровая карта ЕГКН, OpenStreetMap"
            + ("" if urban_plan_waived else ", загруженный официальный слой генплана/ПДП"),
        ]
    )
    return "\n".join(lines)


def format_telegram_result_kz(
    request: SearchRequest,
    approved: list[Candidate],
    *,
    free_preview: bool = False,
) -> str:
    urban_plan_waived = request.urban_plan_status == UrbanPlanStatus.waived.value
    purpose_kz = purpose_label(request.purpose, "kz")
    is_new_lph = normalize_purpose(request.purpose) == LPH_NEW
    if funnel_v2_enabled() and is_new_lph:
        purpose_kz = "ЖҚШ"
    checked_at = max(item.source_checked_at for item in approved).strftime("%d.%m.%Y %H:%M UTC")
    region = request.region_label or request.region
    district = request.district_label or request.district
    genplan_reference = genplan_reference_payload(
        request,
        language=request.language,
        base_url=settings.app_base_url,
        manual_files_root=settings.manual_genplan_files_root,
    )
    locality = request.locality_label or request.locality or "аудан аумағы"
    if funnel_v2_enabled():
        lines = [
            "🗺 Іздеу нәтижелері",
            "",
            f"📍 {region} → {district}",
            f"🏘 {locality}",
            f"🏡 {purpose_kz}",
            f"📐 {round(request.area_ha * 100)} сотық ({request.area_ha:.2f} га)",
            f"📦 Топтама {request.batch_number} · нұсқалар {len(approved)}",
        ]
    else:
        lines = [
            "🔎 АЛДЫН АЛА НҰСҚАЛАР" if free_preview else "🗺 АҚПАРАТТЫҚ ЕСЕП",
            "",
            f"📍 {region} → {district}",
            f"🏘 {locality}",
            f"🎯 {purpose_kz}",
            f"📐 {request.area_ha:.2f} га",
            f"📦 Топтама {request.batch_number} · табылғаны {len(approved)}",
        ]
    if False and is_new_lph:
        lines.extend(
            [
                f"Телім түрі: {allotment_label(request.allotment_type, 'kz')}",
                f"Есептік профиль: {irrigation_label(request.irrigation_type, 'kz')}",
            ]
        )
    if funnel_v2_enabled():
        lines.extend(
            [
                "",
                "Тексерілгені:",
                "✔ таңдалған аудан тіркелген учаскелердің арасына орналасады;",
                "✔ белгіленген жолдар мен нысандар алынып тасталды;",
                telegram_urban_plan_line(
                    request.urban_plan_status,
                    language="kz",
                    reference_source_kind=genplan_reference.get("source_kind"),
                ),
                *(
                    []
                    if urban_plan_waived
                    else [
                        "ℹ️ Бас жоспар аймағы аумақты рұқсат етілген пайдалануды "
                        "көрсетеді, бұл жерде ғимарат бар дегенді білдірмейді."
                    ]
                ),
                f"Деректер тексерілген уақыт: {checked_at}",
            ]
        )
    else:
        lines.extend(
            [
                "",
                (
                (
                    "⚠️ Бас жоспар/ЕЖЖ тексерілмеді: жүйеде осы аумаққа жарамды "
                    "цифрлық қабат жоқ."
                    if _urban_plan_waiver_is_auto(request)
                    else "⚠️ Бас жоспар/ЕЖЖ сіздің жеке таңдауыңыз бойынша тексерілмеді."
                )
                if urban_plan_waived
                else "✅ ЖМБМК геометриясы, OSM жолдар/нысандар сүзгісі және қолжетімді "
                "ресми бас жоспар/ЕЖЖ қабаты тексерілді."
            ),
            *(
                []
                if urban_plan_waived
                else [
                    "ℹ️ Бас жоспар аймағы аумақты рұқсат етілген пайдалануды көрсетеді, "
                    "бұл жерде ғимарат бар дегенді білдірмейді."
                ]
            ),
            f"🕒 Деректер тексерілген уақыт: {checked_at}",
            ]
        )
    for display_rank, item in enumerate(approved[: request.result_limit], start=1):
        cemetery = (
            f"шамамен {item.cemetery_distance_m:.0f} м"
            if item.cemetery_distance_m is not None
            else "ашық деректерден жақын маңда табылмады"
        )
        distance = (
            f"{item.nearby_distance_m:.0f} м" if item.nearby_distance_m is not None else "дерек жоқ"
        )
        plan = (
            "тексерілмеді" if urban_plan_waived else item.urban_plan_zone or "рұқсат етілген қабат"
        )
        if funnel_v2_enabled():
            if _urban_plan_waiver_is_auto(request):
                note = (
                    "Аудан кадастрлық шекаралардың арасына орналасады; белгіленген жолдар "
                    "мен нысандар алынып тасталды. Бас жоспар тексерілмеді: "
                    "жарамды цифрлық қабат жоқ."
                )
            elif urban_plan_waived:
                note = (
                    "Аудан кадастрлық шекаралардың арасына орналасады; белгіленген жолдар "
                    "мен нысандар алынып тасталды. Бас жоспар тексерілмеді."
                )
            else:
                note = (
                    "Аудан кадастрлық шекаралардың арасына орналасады; міндетті "
                    "автоматты тексерулерден өтті."
                )
        else:
            note = (item.review_notes or item.risk_notes or "").strip()
        if len(note) > 220:
            note = note[:217].rstrip() + "..."
        lines.extend(
            [
                "",
                "────────────",
                f"📌 {'Нұсқа' if funnel_v2_enabled() else 'УЧАСКЕ'} {display_rank}",
                "",
                (
                    "🔒 Координаттар мен карта төлемнен кейін толық есепте ашылады"
                    if free_preview
                    else f"📍 Координаттар: {item.latitude:.6f}, {item.longitude:.6f}"
                ),
                f"🗺 Аудан: {item.region_chain}",
                (
                    "🔒 Кадастрлық бағдар төлемнен кейін ашылады"
                    if free_preview
                    else (
                        f"🔢 Көршілес кадастрлық нөмір: {item.nearby_cadastre}"
                        if funnel_v2_enabled()
                        else f"🔢 Жақын кадастрлық нөмір: {item.nearby_cadastre}"
                    )
                ),
                f"↔️ Бағдарға дейінгі қашықтық: {distance}",
                f"🏷 Көршілес учаскенің мақсаты: {item.nearby_land_use or 'дерек жоқ'}",
                (
                    f"🏙 Бас жоспар бойынша рұқсат етілгені: {plan}"
                    if funnel_v2_enabled()
                    else f"🏙 Бас жоспар/ЕЖЖ бойынша рұқсат етілген аймақ: {plan}"
                ),
                f"🪦 Зират: {cemetery}",
            ]
        )
        if note:
            lines.append(
                f"ℹ️ {'Тексеру түсіндірмесі' if funnel_v2_enabled() else 'Тексеру'}: {note}"
            )
        if not free_preview:
            lines.append(f"🔗 Картаны ашу: {item.google_maps_url}")
    if free_preview:
        lines.extend(
            [
                "",
                "────────────",
                "🔒 Толық есепте ашылады:",
                "координаттар, карта, ЕГКН, кадастрлық бағдар және қолмен тексеруге "
                "арналған мәтін.",
                "",
                "Маңызды: бұл ресми бос учаскелер емес, есептік іздеу нәтижелері. "
                "Соңғы шешімді әкімдік қабылдайды.",
            ]
        )
        return "\n".join(lines)
    lines.extend(
        [
            "",
            "────────────",
            "📝 ӨТІНІШКЕ АРНАЛҒАН ТҰЖЫРЫМ",
            "",
            purpose_activity_phrase(request.purpose, "kz", request.allotment_type).capitalize()
            + f" үшін ауданы {request.area_ha:.2f} га жер учаскесін "
            "________ кадастрлық нөмірі бар жер учаскесінің маңынан беруді сұраймын."
            + "",
            "",
            "⚠️ МАҢЫЗДЫ",
            "Бұл есептік орындар, ресми бос учаскелер емес. Көрсетілген кадастрлық "
            "нөмір көршілес учаскеге тиесілі және тек бағдар ретінде беріледі. "
            "Құқықтық мәртебені, беру мүмкіндігін, желілерді және нақты жағдайды "
            "әкімдік пен жерді орнында қарау арқылы растау қажет."
            + (" Есеп телім түрін немесе жердің суармалы екенін растамайды." if is_new_lph else ""),
            "",
            "⚖️ Толық құқықтық шарттар: /terms",
            "📚 Дереккөздер: ЖМБМК жария кадастрлық картасы, OpenStreetMap"
            + ("" if urban_plan_waived else ", жүктелген ресми бас жоспар/ЕЖЖ қабаты"),
        ]
    )
    return "\n".join(lines)


def free_preview_delivery_intro(
    request: SearchRequest,
    *,
    shown_count: int,
    total_found: int,
    used_before: int,
) -> str:
    _ = used_before
    hidden_count = max(total_found - shown_count, 0)

    if request.language == "kz":
        lines = [
            "<b>Алдын ала нұсқалар дайын</b>",
            "",
            f"Осы өтінім бойынша табылғаны: <b>{total_found}</b>.",
            f"Осы топтамада көрсетілгені: <b>{shown_count}</b>.",
            "",
            "Тегін режимде аудан, арақашықтық және тексеру түсіндірмесі көрінеді.",
            "Нақты координаттар, карта, ЕГКН және кадастрлық бағдар төлемнен кейін ашылады.",
        ]
        if hidden_count > 0:
            lines.extend(["", "Келесі нұсқаларды төмендегі батырма арқылы сұрауға болады."])
        else:
            lines.extend(["", "Осы аймақ және сүзгілер бойынша қосымша нұсқа табылған жоқ."])
        lines.extend(
            [
                "",
                (
                    "Маңызды: бұл ресми бос учаскелер емес, есептік іздеу нәтижелері. "
                    "Соңғы шешімді әкімдік қабылдайды."
                ),
            ]
        )
        return "\n".join(lines)

    lines = [
        "<b>Предварительные варианты готовы</b>",
        "",
        f"По этой заявке найдено: <b>{total_found}</b>.",
        f"Показано в этом пакете: <b>{shown_count}</b>.",
        "",
        "В бесплатном режиме видны район, расстояния и пояснение проверки.",
        "Точные координаты, карта, ЕГКН и кадастровый ориентир откроются после оплаты.",
    ]
    if hidden_count > 0:
        lines.extend(["", "Следующие варианты можно запросить кнопкой ниже."])
    else:
        lines.extend(
            ["", "По этому региону и выбранным фильтрам дополнительных вариантов не найдено."]
        )
    lines.extend(
        [
            "",
            (
                "Важно: это результаты расчетного поиска, а не официально свободные участки. "
                "Финальное решение принимает акимат."
            ),
        ]
    )
    return "\n".join(lines)


def approve_free_preview(
    session: Session, request_id: str, *, approved_by: str
) -> tuple[SearchRequest, str | None]:
    request = session.scalar(
        select(SearchRequest)
        .options(selectinload(SearchRequest.candidates))
        .where(SearchRequest.id == request_id)
        .with_for_update()
    )
    if request is None:
        raise LookupError("Заявка не найдена")
    if request.free_preview_status == FreePreviewStatus.delivered.value:
        return request, None
    if request.free_preview_status != FreePreviewStatus.pending.value:
        raise ValueError("Бесплатные участки по этой заявке не ожидают подтверждения")
    approved = approved_candidates(request)
    preview = approved[: request.free_preview_count]
    if not preview:
        raise ValueError("Нет участков для бесплатной отправки")
    if not request.telegram_chat_id:
        raise ValueError("У заявки нет Telegram chat ID")

    message = format_telegram_result(request, preview, free_preview=True)
    if funnel_v2_enabled():
        telegram_request(
            "sendMessage",
            {
                "chat_id": request.telegram_chat_id,
                "text": free_preview_delivery_intro(
                    request,
                    shown_count=len(preview),
                    total_found=len(approved),
                    used_before=0,
                ),
                "parse_mode": "HTML",
            },
        )
    message_parts = split_telegram_message(message)
    genplan_reply_markup = telegram_genplan_reply_markup(request)
    for index, part in enumerate(message_parts):
        payload = {
            "chat_id": request.telegram_chat_id,
            "text": part,
            "disable_web_page_preview": True,
        }
        if index == len(message_parts) - 1:
            payload["reply_markup"] = genplan_reply_markup
        telegram_request(
            "sendMessage",
            payload,
        )
    delivered_at = datetime.now(UTC)
    for candidate in preview:
        candidate.delivered_at = delivered_at
    request.search_completed_notified_at = request.search_completed_notified_at or delivered_at
    request.search_finished_at = request.search_finished_at or delivered_at
    request.free_preview_status = FreePreviewStatus.delivered.value
    request.free_preview_delivered_at = delivered_at
    request.free_preview_approved_by = approved_by
    if len(approved) <= len(preview):
        request.status = SearchStatus.delivered.value
    session.commit()
    session.refresh(request)
    track_funnel_event(
        session,
        "free_results_delivered",
        telegram_user_id=request.telegram_user_id,
        telegram_chat_id=request.telegram_chat_id,
        request_id=request.id,
        funnel_session_id=request.funnel_session_id,
        language=request.language,
        metadata={"count": len(preview)},
    )

    include_next = len(approved) >= request.result_limit
    if include_next:
        send_report_navigation(request, include_next=True)
    if settings.paid_search_enabled:
        try:
            offer_paid_report(request, session)
        except Exception:
            logger.warning(
                "Could not send paywall after preview for %s; recovery will retry",
                request.id,
                exc_info=True,
            )
    else:
        try:
            notify_paid_search_unavailable(request)
        except Exception:
            logger.warning(
                "Could not notify paid search unavailable for %s; recovery will retry",
                request.id,
                exc_info=True,
            )
    return request, message


def reject_free_preview(session: Session, request_id: str) -> SearchRequest:
    request = session.scalar(
        select(SearchRequest)
        .options(selectinload(SearchRequest.candidates))
        .where(SearchRequest.id == request_id)
        .with_for_update()
    )
    if request is None:
        raise LookupError("Заявка не найдена")
    if request.free_preview_status == FreePreviewStatus.delivered.value:
        raise ValueError("Бесплатные участки уже отправлены")
    if request.free_preview_status == FreePreviewStatus.rejected.value:
        return request
    if request.free_preview_status != FreePreviewStatus.pending.value:
        raise ValueError("Бесплатная отправка по этой заявке не ожидается")
    request.free_preview_status = FreePreviewStatus.rejected.value
    session.commit()
    if request.telegram_chat_id:
        text = (
            "Оператор бұл өтінім бойынша алдын ала нұсқаларды жібермеді. "
            "Төлем сұралмайды."
            if request.language == "kz"
            else "Оператор не отправил предварительные варианты по этой заявке. "
            "Оплата не запрашивается."
        )
        telegram_request("sendMessage", {"chat_id": request.telegram_chat_id, "text": text})
    session.refresh(request)
    return request


def deliver_request(session: Session, request_id: str) -> str:
    request = get_request_with_candidates(session, request_id)
    if request is None:
        raise LookupError("Заявка не найдена")
    if (
        request.telegram_chat_id
        and request.payment_status != PaymentStatus.paid.value
        and not has_paid_access(session, request.telegram_user_id)
    ):
        raise ValueError("Полный отчет нельзя отправить до подтверждения оплаты")
    if (
        funnel_v2_enabled()
        and request.telegram_chat_id
        and request.payment_status == PaymentStatus.paid.value
        and request.payment_confirmation_notified_at is None
    ):
        telegram_request(
            "sendMessage",
            {
                "chat_id": request.telegram_chat_id,
                "text": payment_confirmed_message(request.language),
                "parse_mode": "HTML",
            },
        )
        request.payment_confirmation_notified_at = datetime.now(UTC)
        session.commit()
        track_funnel_event(
            session,
            "payment_paid",
            telegram_user_id=request.telegram_user_id,
            telegram_chat_id=request.telegram_chat_id,
            request_id=request.id,
            language=request.language,
            metadata={"amount_kzt": request.payment_amount_kzt},
        )
    message = format_telegram_result(request)
    if request.telegram_chat_id and settings.telegram_bot_token:
        message_parts = split_telegram_message(message)
        genplan_reply_markup = telegram_genplan_reply_markup(request)
        for index, part in enumerate(message_parts):
            payload = {
                "chat_id": request.telegram_chat_id,
                "text": part,
                "disable_web_page_preview": True,
            }
            if index == len(message_parts) - 1:
                payload["reply_markup"] = genplan_reply_markup
            telegram_request(
                "sendMessage",
                payload,
            )
        send_report_navigation(
            request,
            include_next=len(approved_candidates(request)) >= request.result_limit,
        )
    delivered_at = datetime.now(UTC)
    for candidate in approved_candidates(request):
        candidate.delivered_at = delivered_at
    request.search_completed_notified_at = request.search_completed_notified_at or delivered_at
    request.search_finished_at = request.search_finished_at or delivered_at
    request.status = SearchStatus.delivered.value
    session.commit()
    track_funnel_event(
        session,
        "report_delivered",
        telegram_user_id=request.telegram_user_id,
        telegram_chat_id=request.telegram_chat_id,
        request_id=request.id,
        language=request.language,
        metadata={"candidate_count": len(approved_candidates(request))},
    )
    return message


def report_navigation_buttons(
    request: SearchRequest,
    *,
    include_next: bool = False,
) -> list[list[dict[str, str]]]:
    buttons: list[list[dict[str, str]]] = []
    if include_next:
        buttons.append(
            [
                {
                    "text": (
                        "Келесі 10 учаске"
                        if request.language == "kz"
                        else "Следующие 10 участков"
                    ),
                    "callback_data": f"search:next:{request.id}",
                }
            ]
        )
    if request.district != ALL_DISTRICTS:
        buttons.append(
            [
                {
                    "text": t(request.language, "choose_other_locality"),
                    "callback_data": f"search:localities:{request.id}",
                }
            ]
        )
    buttons.extend(
        [
            [
                {
                    "text": t(request.language, "back_districts"),
                    "callback_data": f"search:districts:{request.id}",
                }
            ],
            [
                {
                    "text": t(request.language, "main_regions"),
                    "callback_data": f"search:regions:{request.id}",
                }
            ],
        ]
    )
    return buttons


def send_report_navigation(
    request: SearchRequest,
    *,
    include_next: bool = False,
) -> None:
    if not request.telegram_chat_id:
        return
    if request.language == "kz":
        text = (
            (
                "Жалғастырғыңыз келе ме?\n\n"
                "Келесі 10 жаңа нұсқаны табуға болады.\n"
                "Бұрын жіберілген координаттар қайталанбайды."
            )
            if include_next
            else (
                "Бұл аумақта жаңа нұсқалар табылған жоқ.\n\n"
                "Мерзімсіз қолжетімділігіңіз белсенді болып қалады. "
                "Жаңа іздеу үшін басқа аумақты таңдаңыз."
            )
        )
    else:
        text = (
            (
                "Хотите продолжить?\n\n"
                "Можно найти следующие 10 новых вариантов.\n"
                "Уже отправленные координаты повторяться не будут."
            )
            if include_next
            else (
                "По этой территории новых вариантов больше не найдено.\n\n"
                "Ваш оплаченный доступ остается активным. "
                "Выберите другую территорию для нового поиска."
            )
        )
    telegram_request(
        "sendMessage",
        {
            "chat_id": request.telegram_chat_id,
            "text": text,
            "reply_markup": {
                "inline_keyboard": report_navigation_buttons(
                    request,
                    include_next=include_next,
                )
            },
        },
    )


def send_next_batch_button(request: SearchRequest) -> None:
    """Backward-compatible wrapper for callers that still use the old helper."""
    send_report_navigation(request, include_next=True)


@dataclass(frozen=True, slots=True)
class ApiPayWebhookResult:
    event: str
    request_id: str | None = None
    auction_access_id: str | None = None
    account_payment_id: str | None = None
    status: str | None = None
    deliver_report: bool = False
    notify_payment_retry: bool = False
    activate_auction_access: bool = False
    notify_auction_payment_retry: bool = False
    activate_account_access: bool = False
    notify_account_payment_retry: bool = False
    payment_received: bool = False
    ignored: bool = False


def notify_admin_payment_received(
    *,
    external_order_id: str,
    invoice_id: str,
    amount_kzt: object,
    telegram_user_id: str | None = None,
) -> None:
    """Notify the configured admin chat once a payment becomes paid."""
    if not settings.telegram_admin_chat_id:
        logger.warning("Payment received but TELEGRAM_ADMIN_CHAT_ID is not configured")
        return
    text = (
        "✅ <b>Получена оплата</b>\n\n"
        f"Сумма: <b>{amount_kzt} ₸</b>\n"
        f"Заказ: <code>{escape(external_order_id)}</code>\n"
        f"Счёт ApiPay: <code>{escape(invoice_id)}</code>"
    )
    if telegram_user_id:
        text += f"\nTelegram ID клиента: <code>{escape(telegram_user_id)}</code>"
    try:
        telegram_request(
            "sendMessage",
            {
                "chat_id": settings.telegram_admin_chat_id,
                "text": text,
                "parse_mode": "HTML",
            },
        )
    except Exception:
        logger.exception("Could not notify admin about payment %s", invoice_id)


def _apipay_invoice_idempotency_key(request: SearchRequest) -> str:
    status = request.payment_provider_status or ""
    if status.startswith("refresh:"):
        return f"land-scout:{request.id}:{status}"
    return f"land-scout:{request.id}"


def _prepare_apipay_invoice_refresh(request: SearchRequest) -> None:
    request.payment_status = PaymentStatus.rejected.value
    request.payment_provider = "apipay"
    request.payment_provider_invoice_id = None
    request.payment_provider_url = None
    request.payment_provider_status = f"refresh:{uuid.uuid4().hex[:12]}"
    request.payment_provider_updated_at = datetime.now(UTC)


def apply_apipay_webhook(
    session: Session,
    payload: dict,
) -> ApiPayWebhookResult:
    event = str(payload.get("event") or "")
    if event == "webhook.test":
        return ApiPayWebhookResult(event=event)
    if event != "invoice.status_changed":
        return ApiPayWebhookResult(event=event or "unknown", ignored=True)

    invoice = payload.get("invoice")
    if not isinstance(invoice, dict):
        raise ValueError("ApiPay webhook не содержит объект invoice")
    request_id = str(invoice.get("external_order_id") or "")
    invoice_id = str(invoice.get("id") or "")
    provider_status = str(invoice.get("status") or "")
    if not request_id or not invoice_id or not provider_status:
        raise ValueError("ApiPay webhook не содержит обязательные поля счета")

    from app.account_payments import ACCOUNT_ORDER_PREFIX, apply_account_apipay_invoice
    from app.auction_access import AUCTION_ORDER_PREFIX, apply_auction_apipay_invoice

    if request_id.startswith(ACCOUNT_ORDER_PREFIX):
        try:
            account_result = apply_account_apipay_invoice(session, invoice)
        except LookupError:
            logger.warning(
                "Ignoring ApiPay account invoice %s for unknown external order %s",
                invoice_id,
                request_id,
            )
            return ApiPayWebhookResult(
                event=event,
                account_payment_id=request_id.removeprefix(ACCOUNT_ORDER_PREFIX),
                status=provider_status,
                ignored=True,
            )
        if account_result.activated:
            from app.models import Account, AccountPayment

            payment = session.get(AccountPayment, account_result.payment_id)
            account = session.get(Account, payment.account_id) if payment else None
            notify_admin_payment_received(
                external_order_id=request_id,
                invoice_id=invoice_id,
                amount_kzt=invoice.get("amount") or "не указана",
                telegram_user_id=account.telegram_user_id if account else None,
            )
        return ApiPayWebhookResult(
            event=event,
            account_payment_id=account_result.payment_id,
            status=account_result.status,
            activate_account_access=account_result.activated,
            notify_account_payment_retry=account_result.notify_retry,
            payment_received=account_result.activated,
        )

    if request_id.startswith(AUCTION_ORDER_PREFIX):
        try:
            auction_result = apply_auction_apipay_invoice(session, invoice)
        except LookupError:
            logger.warning(
                "Ignoring ApiPay auction invoice %s for unknown external order %s",
                invoice_id,
                request_id,
            )
            return ApiPayWebhookResult(
                event=event,
                auction_access_id=request_id.removeprefix(AUCTION_ORDER_PREFIX),
                status=provider_status,
                ignored=True,
            )
        if auction_result.activated:
            from app.models import AuctionAccess

            access = session.get(AuctionAccess, auction_result.access_id)
            if access is not None:
                deliver_pending_platform_reports(session, access.telegram_user_id)
                notify_admin_payment_received(
                    external_order_id=request_id,
                    invoice_id=invoice_id,
                    amount_kzt=invoice.get("amount") or "не указана",
                    telegram_user_id=access.telegram_user_id,
                )
        return ApiPayWebhookResult(
            event=event,
            auction_access_id=auction_result.access_id,
            status=auction_result.status,
            activate_auction_access=auction_result.activated,
            notify_auction_payment_retry=auction_result.notify_retry,
            payment_received=auction_result.activated,
        )

    request = get_request_with_candidates(session, request_id)
    if request is None:
        logger.warning(
            "Ignoring ApiPay invoice %s for unknown external order %s",
            invoice_id,
            request_id,
        )
        return ApiPayWebhookResult(
            event=event,
            request_id=request_id,
            status=provider_status,
            ignored=True,
        )
    if request.payment_provider != "apipay":
        raise ValueError("ID счета ApiPay не совпадает с заявкой")
    if request.payment_provider_invoice_id != invoice_id:
        if provider_status == "paid":
            logger.info(
                "Accepting paid stale ApiPay invoice %s for request %s; current invoice is %s",
                invoice_id,
                request.id,
                request.payment_provider_invoice_id,
            )
        elif provider_status in {"cancelled", "expired", "error"}:
            logger.info(
                "Ignoring stale terminal ApiPay invoice %s for request %s; current invoice is %s",
                invoice_id,
                request.id,
                request.payment_provider_invoice_id,
            )
            return ApiPayWebhookResult(
                event=event,
                request_id=request.id,
                status=provider_status,
                ignored=True,
            )
        else:
            raise ValueError("ID счета ApiPay не совпадает с заявкой")

    request.payment_provider = "apipay"
    request.payment_provider_invoice_id = invoice_id
    request.payment_provider_status = provider_status
    request.payment_provider_updated_at = datetime.now(UTC)

    deliver_report = False
    payment_received = False
    if provider_status == "paid":
        try:
            paid_amount = Decimal(str(invoice.get("amount")))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("ApiPay webhook содержит некорректную сумму") from exc
        expected_amount = Decimal(
            request.payment_amount_kzt or settings.platform_access_price_kzt
        )
        if paid_amount != expected_amount:
            raise ValueError(
                f"Сумма ApiPay {paid_amount} не совпадает с ожидаемой {expected_amount}"
            )
        if not approved_candidates(request):
            raise ValueError("У оплаченной заявки нет проверенных кандидатов")
        payment_received = request.payment_status != PaymentStatus.paid.value
        if payment_received:
            request.payment_status = PaymentStatus.paid.value
            request.payment_confirmed_at = datetime.now(UTC)
            request.payment_confirmed_by = f"apipay:{invoice_id}"
        request.access_expires_at = next_platform_access_expiry(
            request.access_expires_at,
            now=request.payment_confirmed_at or datetime.now(UTC),
        )
        deliver_report = request.status != SearchStatus.delivered.value
    notify_payment_retry = False
    if provider_status in {"cancelled", "expired", "error"}:
        if request.payment_status != PaymentStatus.paid.value:
            notify_payment_retry = request.payment_status != PaymentStatus.rejected.value
            request.payment_status = PaymentStatus.rejected.value

    session.commit()
    if payment_received:
        notify_admin_payment_received(
            external_order_id=request.id,
            invoice_id=invoice_id,
            amount_kzt=request.payment_amount_kzt or settings.platform_access_price_kzt,
            telegram_user_id=request.telegram_user_id,
        )
    return ApiPayWebhookResult(
        event=event,
        request_id=request.id,
        status=provider_status,
        deliver_report=deliver_report,
        notify_payment_retry=notify_payment_retry,
        payment_received=payment_received,
    )


def deliver_pending_platform_reports(
    session: Session,
    telegram_user_id: str | None,
) -> list[str]:
    if not telegram_user_id or not has_paid_access(session, telegram_user_id):
        return []
    request_ids = session.scalars(
        select(SearchRequest.id).where(
            SearchRequest.telegram_user_id == telegram_user_id,
            SearchRequest.telegram_chat_id.is_not(None),
            SearchRequest.status.in_(
                [SearchStatus.ready.value, SearchStatus.review.value]
            ),
            SearchRequest.status != SearchStatus.delivered.value,
        )
    ).all()
    delivered: list[str] = []
    for request_id in request_ids:
        request = get_request_with_candidates(session, request_id)
        if request is None or not approved_candidates(request):
            continue
        try:
            deliver_request(session, request_id)
        except Exception:
            logger.exception("Could not deliver pending platform request %s", request_id)
            continue
        delivered.append(request_id)
    return delivered


def deliver_apipay_report(request_id: str) -> None:
    from app.db import SessionLocal

    try:
        with SessionLocal() as session:
            request = get_request_with_candidates(session, request_id)
            if request is None or request.payment_status != PaymentStatus.paid.value:
                return
            if request.status != SearchStatus.delivered.value:
                deliver_request(session, request_id)
    except Exception:
        logger.exception("Could not deliver ApiPay-paid request %s", request_id)


def notify_apipay_payment_retry(request_id: str) -> None:
    from app.db import SessionLocal

    try:
        with SessionLocal() as session:
            request = get_request_with_candidates(session, request_id)
            if request is None or not request.telegram_chat_id:
                return
            text = (
                "Төлем сілтемесі енді жарамсыз\n\n"
                "Бұл қалыпты жағдай: QR-сілтеменің қолданылу мерзімі шектеулі.\n\n"
                "Қазір жаңа төлем сілтемесін жіберемін. Қосымша сома қосылмайды."
                if request.language == "kz"
                else "Ссылка на оплату больше не действует\n\n"
                "Это нормально: QR-ссылка имеет ограниченный срок.\n\n"
                "Сейчас отправлю новую ссылку оплаты. Повторная сумма не добавится."
            )
            telegram_request(
                "sendMessage",
                {
                    "chat_id": request.telegram_chat_id,
                    "text": text,
                },
            )
            _prepare_apipay_invoice_refresh(request)
            session.commit()
            request_payment(session, request.id, message_language=request.language)
    except Exception:
        logger.exception("Could not notify about expired ApiPay invoice %s", request_id)


def dispatch_apipay_reconciliation(request_id: str) -> None:
    if (
        not settings.apipay_enabled
        or not settings.apipay_polling_enabled
        or settings.run_tasks_inline
    ):
        return
    from app.tasks import reconcile_apipay_invoice_task

    reconcile_apipay_invoice_task.apply_async(
        args=[request_id],
        countdown=settings.apipay_poll_interval_seconds,
    )


def request_payment(
    session: Session,
    request_id: str,
    *,
    message_language: str | None = None,
) -> SearchRequest:
    if not settings.paid_search_enabled:
        raise ValueError("Прием платных отчетов временно отключен")
    request = get_request_with_candidates(session, request_id)
    if request is None:
        raise LookupError("Заявка не найдена")
    language = "kz" if (message_language or request.language) == "kz" else "ru"
    if not request.telegram_chat_id:
        raise ValueError("Заявка создана не через Telegram")
    if has_paid_access(session, request.telegram_user_id):
        if request.status != SearchStatus.delivered.value:
            deliver_request(session, request.id)
        return request
    platform_pending = find_pending_platform_invoice(
        session,
        request.telegram_user_id,
        exclude_search_request_id=request.id,
    )
    if platform_pending is not None and platform_pending.source == "auction":
        if (
            platform_pending.payment_status == PaymentStatus.awaiting_transfer.value
            and platform_pending.payment_provider == "apipay"
            and platform_pending.payment_provider_url
        ):
            amount = platform_pending.payment_amount_kzt or settings.platform_access_price_kzt
            formatted_amount = f"{amount:,} ₸".replace(",", " ")
            text = (
                "Сізде бірыңғай қолжетімділік бойынша күтіп тұрған төлем бар.\n\n"
                "Қайта төлеудің қажеті жоқ. Төмендегі ағымдағы төлем сілтемесін ашыңыз."
                if language == "kz"
                else (
                    "У вас уже есть ожидающая оплата единого доступа.\n\n"
                    "Повторно платить не нужно. Откройте текущую ссылку оплаты ниже."
                )
            )
            telegram_request(
                "sendMessage",
                {
                    "chat_id": request.telegram_chat_id,
                    "text": text,
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": (
                                        f"Толық есепті Kaspi арқылы ашу - {formatted_amount}"
                                        if language == "kz"
                                        else (
                                            "Разблокировать отчет через Kaspi - "
                                            f"{formatted_amount}"
                                        )
                                    ),
                                    "url": platform_pending.payment_provider_url,
                                }
                            ],
                            [
                                {
                                    "text": (
                                        "Сілтемені жаңарту"
                                        if language == "kz"
                                        else "Обновить ссылку"
                                    ),
                                    "callback_data": "auction:pay:refresh",
                                }
                            ],
                        ]
                    },
                },
            )
            track_funnel_event(
                session,
                "invoice_reused",
                telegram_user_id=request.telegram_user_id,
                telegram_chat_id=request.telegram_chat_id,
                request_id=request.id,
                language=language,
                metadata={
                    "source": platform_pending.source,
                    "source_id": platform_pending.object_id,
                    "amount_kzt": amount,
                },
            )
            return request
        raise ValueError(
            "У вас уже есть ожидающая оплата единого доступа. "
            "Завершите ее; повторно платить не нужно."
        )
    if (
        platform_pending is not None
        and platform_pending.payment_status == PaymentStatus.awaiting_transfer.value
        and platform_pending.payment_provider == "apipay"
        and platform_pending.payment_provider_url
    ):
        if platform_pending.source == "search":
            if funnel_v2_enabled():
                telegram_request(
                    "sendMessage",
                    {
                        "chat_id": request.telegram_chat_id,
                        "text": existing_invoice_message(language),
                    },
                )
            return request_payment(
                session,
                platform_pending.object_id,
                message_language=language,
            )
        payment_url = platform_pending.payment_provider_url
        amount = platform_pending.payment_amount_kzt or settings.platform_access_price_kzt
        formatted_amount = f"{amount:,} в‚ё".replace(",", " ")
        lines = payment_link_message(language, request.id).splitlines()
        keyboard = [
            [
                {
                    "text": (
                        f"Kaspi Р°СЂТ›С‹Р»С‹ С‚У©Р»РµСѓ вЂ” {formatted_amount}"
                        if language == "kz"
                        else f"РћРїР»Р°С‚РёС‚СЊ С‡РµСЂРµР· Kaspi вЂ” {formatted_amount}"
                    ),
                    "url": payment_url,
                }
            ]
        ]
        request.payment_status = PaymentStatus.awaiting_transfer.value
        request.payment_amount_kzt = amount
        request.payment_requested_at = datetime.now(UTC)
        request.payment_provider = "apipay"
        request.payment_provider_invoice_id = None
        request.payment_provider_status = platform_pending.payment_provider_status
        request.payment_provider_url = payment_url
        request.payment_provider_updated_at = datetime.now(UTC)
        session.commit()
        track_funnel_event(
            session,
            "invoice_reused",
            telegram_user_id=request.telegram_user_id,
            telegram_chat_id=request.telegram_chat_id,
            request_id=request.id,
            language=language,
            metadata={
                "source": platform_pending.source,
                "source_id": platform_pending.object_id,
                "amount_kzt": amount,
            },
        )
        telegram_request(
            "sendMessage",
            {
                "chat_id": request.telegram_chat_id,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )
        session.refresh(request)
        return request
    outstanding = session.scalar(
        select(SearchRequest)
        .where(
            SearchRequest.telegram_user_id == request.telegram_user_id,
            SearchRequest.id != request.id,
            SearchRequest.payment_status.in_(
                [
                    PaymentStatus.awaiting_transfer.value,
                    PaymentStatus.pending_confirmation.value,
                ]
            ),
        )
        .limit(1)
    )
    if outstanding:
        if (
            settings.apipay_enabled
            and outstanding.payment_status == PaymentStatus.awaiting_transfer.value
            and outstanding.payment_provider == "apipay"
            and outstanding.payment_provider_url
        ):
            if funnel_v2_enabled():
                telegram_request(
                    "sendMessage",
                    {
                        "chat_id": request.telegram_chat_id,
                        "text": existing_invoice_message(language),
                    },
                )
            return request_payment(
                session,
                outstanding.id,
                message_language=language,
            )
        raise ValueError(
            "У вас уже есть ожидающая оплата по заявке "
            f"{outstanding.id}. Завершите ее; повторно платить не нужно."
        )
    card_number = "".join(re.findall(r"\d", settings.payment_card_number))
    if not card_number and not settings.apipay_enabled:
        raise ValueError("Заполните PAYMENT_CARD_NUMBER в .env")
    if card_number and not 12 <= len(re.sub(r"\D", "", card_number)) <= 19:
        raise ValueError("Проверьте PAYMENT_CARD_NUMBER в .env")
    approved = approved_candidates(request)
    if not approved:
        raise ValueError("Нет кандидатов, прошедших все обязательные проверки")
    if (
        request.status == SearchStatus.delivered.value
        and request.free_preview_status != FreePreviewStatus.delivered.value
    ):
        raise ValueError("Результат по заявке уже отправлен")
    if request.payment_status == PaymentStatus.paid.value:
        raise ValueError("Оплата уже подтверждена; используйте повторную отправку отчета")
    if request.payment_status == PaymentStatus.pending_confirmation.value:
        raise ValueError("Клиент уже сообщил об оплате; проверьте уведомление в Telegram")
    if request.payment_status == PaymentStatus.awaiting_transfer.value and not (
        settings.apipay_enabled
        and request.payment_provider == "apipay"
        and request.payment_provider_url
    ):
        return request

    amount = settings.platform_access_price_kzt
    recipient = settings.payment_recipient.strip()
    bank_name = settings.payment_bank_name.strip()
    payment_url = settings.payment_url.strip()
    provider_invoice = None
    if settings.apipay_enabled:
        if (
            request.payment_status == PaymentStatus.awaiting_transfer.value
            and request.payment_provider == "apipay"
            and request.payment_provider_invoice_id
            and request.payment_provider_url
        ):
            payment_url = request.payment_provider_url
        else:
            provider_invoice = create_qr_invoice(
                request_id=request.id,
                amount_kzt=amount,
                description="Жертап: полный доступ на 1 месяц",
                idempotency_key=_apipay_invoice_idempotency_key(request),
            )
            payment_url = provider_invoice.payment_url
    if payment_url and not payment_url.startswith("https://"):
        raise ValueError("PAYMENT_URL должен начинаться с https://")
    display_card_number = (
        " ".join(card_number[index : index + 4] for index in range(0, len(card_number), 4))
        if card_number
        else ""
    )
    display_bank_name = escape(
        "ApiPay / Kaspi" if settings.apipay_enabled else bank_name or "Kaspi Bank"
    )
    formatted_amount = f"{amount:,} ₸".replace(",", " ")
    if funnel_v2_enabled() and settings.apipay_enabled:
        lines = payment_link_message(language, request.id).splitlines()
    elif language == "kz":
        lines = [
            "✅ <b>Есеп дайын</b>",
            "",
            f"🧭 Табылған нұсқалар: <b>{len(approved)}</b>",
            f"💳 Бір реттік қолжетімділік: <b>{formatted_amount}</b>",
            "♾ Төлем расталғаннан кейін келесі іздеулерге қосымша төлем алынбайды.",
        ]
    else:
        lines = [
            "✅ <b>Отчет готов</b>",
            "",
            f"🧭 Найдено вариантов: <b>{len(approved)}</b>",
            f"💳 Разовая активация доступа: <b>{formatted_amount}</b>",
            "♾ После подтверждения платежа последующие поиски доступны без доплаты.",
        ]
    if request.free_preview_status == FreePreviewStatus.delivered.value:
        if language == "kz":
            lines.append("🔎 Алдын ала нұсқалар көрсетілді. Толық есеп төлемнен кейін ашылады.")
        else:
            lines.append(
                "🔎 Предварительные варианты уже показаны. Полный отчет откроется после оплаты."
            )
    lines.extend(["", "────────────"])
    if language == "kz":
        lines.extend(
            [
                f"💳 <b>{display_bank_name.upper()} АРҚЫЛЫ ТӨЛЕУ</b>",
                "Төмендегі толық есепті ашу батырмасын басыңыз.",
            ]
        )
        if settings.apipay_enabled:
            lines.append("Төлем мәртебесін бот автоматты түрде алады.")
    else:
        lines.extend(
            [
                f"💳 <b>ОПЛАТА ЧЕРЕЗ {display_bank_name.upper()}</b>",
                "Нажмите кнопку разблокировки полного отчета ниже.",
            ]
        )
        if settings.apipay_enabled:
            lines.append("Бот получит подтверждение платежа автоматически.")
    if display_card_number and not settings.apipay_enabled:
        if language == "kz":
            lines.extend(
                [
                    "",
                    "Карта нөмірі:",
                    f"<code>{escape(display_card_number)}</code>",
                    "Нөмірді басып ұстап тұруға немесе көшіру батырмасын пайдалануға болады.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "Номер карты:",
                    f"<code>{escape(display_card_number)}</code>",
                    "Номер можно удержать пальцем или скопировать отдельной кнопкой.",
                ]
            )
    if recipient and not settings.apipay_enabled:
        recipient_label = "Алушы" if language == "kz" else "Получатель"
        lines.append(f"{recipient_label}: {escape(recipient)}")
    if language == "kz" and settings.apipay_enabled:
        lines.extend(
            [
                "",
                "1. Төлем бетіне өтіп, Kaspi арқылы төлеңіз.",
                "2. Төлем расталғаннан кейін толық есеп автоматты түрде келеді.",
                "",
                f"Өтінім: <code>{request.id}</code>",
                "⚖️ Толық шарттар: /offer",
            ]
        )
    elif language == "kz":
        lines.extend(
            [
                "",
                "1. Kaspi арқылы төлем жасаңыз.",
                "2. Ботқа оралып, «Төледім» батырмасын басыңыз.",
                "3. Төлемді растағаннан кейін толық есеп автоматты түрде келеді.",
                "",
                f"Өтінім: <code>{request.id}</code>",
                "⚖️ Толық шарттар: /offer",
            ]
        )
    elif settings.apipay_enabled:
        lines.extend(
            [
                "",
                "1. Перейдите на страницу оплаты и оплатите через Kaspi.",
                "2. После подтверждения платежа полный отчет придет автоматически.",
                "",
                f"Заявка: <code>{request.id}</code>",
                "⚖️ Полные условия: /offer",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "1. Оплатите через Kaspi.",
                "2. Вернитесь в бот и нажмите «Я оплатил».",
                "3. После подтверждения поступления полный отчет придет автоматически.",
                "",
                f"Заявка: <code>{request.id}</code>",
                "⚖️ Полные условия: /offer",
            ]
        )
    keyboard: list[list[dict]] = []
    if payment_url:
        keyboard.append(
            [
                {
                    "text": (
                        (
                            f"Толық есепті Kaspi арқылы ашу — {formatted_amount}"
                            if language == "kz"
                            else f"Разблокировать отчет через Kaspi — {formatted_amount}"
                        )
                        if funnel_v2_enabled()
                        else (
                            "💳 Kaspi арқылы төлеу"
                            if language == "kz"
                            else "💳 Оплатить через Kaspi"
                        )
                    ),
                    "url": payment_url,
                }
            ]
        )
    active_invoice_id = (
        provider_invoice.invoice_id
        if provider_invoice is not None
        else request.payment_provider_invoice_id
    )
    if settings.apipay_enabled and active_invoice_id:
        keyboard.append(
            [
                {
                    "text": (
                        (
                            "🔄 Сілтемені жаңарту"
                            if language == "kz"
                            else "🔄 Обновить ссылку"
                        )
                        if funnel_v2_enabled()
                        else (
                            "🔄 QR ашылмаса, жаңарту"
                            if language == "kz"
                            else "🔄 Обновить QR-ссылку"
                        )
                    ),
                    "callback_data": (
                        f"pay:refresh:{request.id}:{active_invoice_id}"
                    ),
                }
            ]
        )
    if card_number and not settings.apipay_enabled:
        keyboard.append(
            [
                {
                    "text": (
                        f"📋 {display_card_number} көшіру"
                        if language == "kz"
                        else f"📋 Скопировать {display_card_number}"
                    ),
                    "copy_text": {"text": card_number},
                }
            ]
        )
    if not settings.apipay_enabled:
        keyboard.append(
            [
                {
                    "text": "✅ Төледім" if language == "kz" else "✅ Я оплатил",
                    "callback_data": f"pay:claim:{request.id}",
                }
            ]
        )
    request.payment_status = PaymentStatus.awaiting_transfer.value
    request.payment_amount_kzt = amount
    request.payment_requested_at = datetime.now(UTC)
    if settings.apipay_enabled:
        request.payment_provider = "apipay"
        if provider_invoice is not None:
            request.payment_provider_invoice_id = provider_invoice.invoice_id
            request.payment_provider_status = provider_invoice.status
            request.payment_provider_url = provider_invoice.payment_url
        request.payment_provider_updated_at = datetime.now(UTC)
    session.commit()
    track_funnel_event(
        session,
        "invoice_created",
        telegram_user_id=request.telegram_user_id,
        telegram_chat_id=request.telegram_chat_id,
        request_id=request.id,
        language=language,
        metadata={
            "amount_kzt": amount,
            "provider": request.payment_provider or "manual",
            "invoice_id": request.payment_provider_invoice_id,
        },
    )
    telegram_request(
        "sendMessage",
        {
            "chat_id": request.telegram_chat_id,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": keyboard},
        },
    )
    dispatch_apipay_reconciliation(request.id)
    session.refresh(request)
    return request


def refresh_apipay_payment(
    session: Session,
    request_id: str,
    *,
    expected_invoice_id: str,
    telegram_user_id: str,
    telegram_chat_id: str,
) -> tuple[SearchRequest, bool]:
    request = get_request_with_candidates(session, request_id)
    if request is None:
        raise LookupError("Заявка не найдена")
    if request.telegram_chat_id != telegram_chat_id or (
        request.telegram_user_id and request.telegram_user_id != telegram_user_id
    ):
        raise PermissionError("Эта кнопка относится к другой заявке")
    if has_paid_access(session, telegram_user_id):
        if request.status != SearchStatus.delivered.value:
            deliver_request(session, request.id)
        return request, False
    if not settings.apipay_enabled:
        raise ValueError("Автоматическая оплата временно отключена")

    current_invoice_id = request.payment_provider_invoice_id or ""
    if expected_invoice_id != current_invoice_id:
        request_payment(session, request.id)
        return request, True
    if request.payment_status == PaymentStatus.rejected.value:
        _prepare_apipay_invoice_refresh(request)
        session.commit()
        request_payment(session, request.id)
        return request, True
    if (
        request.payment_status != PaymentStatus.awaiting_transfer.value
        or request.payment_provider != "apipay"
        or not current_invoice_id
    ):
        raise ValueError("Для этой заявки нет активной ссылки оплаты")

    invoice = get_invoice(current_invoice_id)
    invoice["external_order_id"] = request.id
    provider_status = str(invoice.get("status") or "")
    if provider_status == "paid":
        result = apply_apipay_webhook(
            session,
            {"event": "invoice.status_changed", "invoice": invoice, "source": "refresh"},
        )
        if result.deliver_report:
            deliver_request(session, request.id)
        return request, False
    if provider_status in {"cancelled", "expired", "error"}:
        apply_apipay_webhook(
            session,
            {"event": "invoice.status_changed", "invoice": invoice, "source": "refresh"},
        )
        _prepare_apipay_invoice_refresh(request)
        session.commit()
        request_payment(session, request.id)
        return request, True
    if provider_status not in {"pending", "processing"}:
        raise ValueError("ApiPay еще обрабатывает предыдущий счет. Повторите через минуту")

    try:
        cancellation = cancel_invoice(current_invoice_id)
        request.payment_provider_status = cancellation.status
    except Exception:
        logger.exception("Could not cancel stale ApiPay invoice %s", current_invoice_id)
        request.payment_provider_status = "cancel_failed"
    request.payment_provider_updated_at = datetime.now(UTC)
    _prepare_apipay_invoice_refresh(request)
    session.commit()
    request_payment(session, request.id)
    return request, True


def start_payment(
    session: Session,
    request_id: str,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
) -> SearchRequest:
    request = get_request_with_candidates(session, request_id)
    if request is None:
        raise LookupError("Заявка не найдена")
    if request.telegram_chat_id != telegram_chat_id or (
        request.telegram_user_id and request.telegram_user_id != telegram_user_id
    ):
        raise PermissionError("Эта кнопка относится к другой заявке")
    if has_paid_access(session, telegram_user_id):
        if request.status != SearchStatus.delivered.value:
            deliver_request(session, request.id)
        return request
    return request_payment(session, request_id)


def claim_payment(
    session: Session,
    request_id: str,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
    client_label: str,
) -> SearchRequest:
    request = get_request_with_candidates(session, request_id)
    if request is None:
        raise LookupError("Заявка не найдена")
    if request.telegram_chat_id != telegram_chat_id or (
        request.telegram_user_id and request.telegram_user_id != telegram_user_id
    ):
        raise PermissionError("Эта кнопка относится к другой заявке")
    if has_paid_access(session, telegram_user_id):
        request.payment_status = PaymentStatus.not_requested.value
        request.payment_amount_kzt = None
        session.commit()
        if request.status != SearchStatus.delivered.value:
            deliver_request(session, request.id)
        return request
    if not approved_candidates(request):
        raise ValueError(
            "Отчет не прошел обязательную проверку генплана/ПДП. Не переводите деньги; "
            "оплата по этой заявке не принимается."
        )
    if request.payment_status == PaymentStatus.paid.value:
        return request
    if request.payment_status not in {
        PaymentStatus.awaiting_transfer.value,
        PaymentStatus.rejected.value,
        PaymentStatus.pending_confirmation.value,
    }:
        raise ValueError("Оплата по этой заявке еще не запрошена")
    if request.payment_status == PaymentStatus.pending_confirmation.value:
        return request
    if not settings.telegram_admin_chat_id:
        raise ValueError("TELEGRAM_ADMIN_CHAT_ID не заполнен")

    amount = request.payment_amount_kzt or settings.platform_access_price_kzt
    telegram_request(
        "sendMessage",
        {
            "chat_id": settings.telegram_admin_chat_id,
            "text": (
                "Клиент сообщил об оплате\n"
                f"Заявка: {request.id}\n"
                f"Клиент: {client_label} · Telegram ID {telegram_user_id}\n"
                f"Поиск: {request.district}, {request.locality or 'населенный пункт не указан'}\n"
                f"Сумма: {amount:,} ₸\n".replace(",", " ")
                + "Проверьте фактическое поступление в банковском приложении."
            ),
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "Подтвердить поступление",
                            "callback_data": f"pay:confirm:{request.id}",
                        },
                        {
                            "text": "Платеж не найден",
                            "callback_data": f"pay:reject:{request.id}",
                        },
                    ]
                ]
            },
        },
    )
    request.payment_status = PaymentStatus.pending_confirmation.value
    request.payment_claimed_at = datetime.now(UTC)
    session.commit()
    session.refresh(request)
    return request


def confirm_payment(
    session: Session, request_id: str, *, confirmed_by: str
) -> tuple[SearchRequest, str | None]:
    request = get_request_with_candidates(session, request_id)
    if request is None:
        raise LookupError("Заявка не найдена")
    if not approved_candidates(request):
        raise ValueError(
            "Отчет не прошел обязательную проверку генплана/ПДП. Не подтверждайте "
            "поступление; при уже выполненном переводе урегулируйте возврат с клиентом."
        )
    if request.payment_status == PaymentStatus.paid.value:
        if request.status == SearchStatus.delivered.value:
            return request, None
        message = deliver_request(session, request_id)
        session.refresh(request)
        return request, message
    if has_paid_access(
        session,
        request.telegram_user_id,
        exclude_request_id=request.id,
    ):
        request.payment_status = PaymentStatus.not_requested.value
        request.payment_amount_kzt = None
        session.commit()
        message = None
        if request.status != SearchStatus.delivered.value:
            message = deliver_request(session, request_id)
        session.refresh(request)
        return request, message
    if request.payment_status != PaymentStatus.pending_confirmation.value:
        raise ValueError("Клиент еще не сообщил об оплате")
    request.payment_status = PaymentStatus.paid.value
    request.payment_confirmed_at = datetime.now(UTC)
    request.payment_confirmed_by = confirmed_by
    request.access_expires_at = next_platform_access_expiry(
        request.access_expires_at,
        now=request.payment_confirmed_at,
    )
    session.commit()
    notify_admin_payment_received(
        external_order_id=request.id,
        invoice_id=confirmed_by,
        amount_kzt=request.payment_amount_kzt or settings.platform_access_price_kzt,
        telegram_user_id=request.telegram_user_id,
    )
    message = deliver_request(session, request_id)
    session.refresh(request)
    return request, message


def reject_payment(session: Session, request_id: str) -> SearchRequest:
    request = get_request_with_candidates(session, request_id)
    if request is None:
        raise LookupError("Заявка не найдена")
    if request.payment_status == PaymentStatus.paid.value:
        raise ValueError("Оплата уже подтверждена")
    previous_status = request.payment_status
    request.payment_status = PaymentStatus.rejected.value
    session.commit()
    if request.telegram_chat_id:
        if previous_status == PaymentStatus.awaiting_transfer.value and request.language == "kz":
            text = (
                "Оператор осы өтінім бойынша төлемді күтуді тоқтатты. Төлем жасалмады "
                "деп есептеледі. Қажет болса, төлемді қайта бастауға болады."
            )
            button_text = "Төлемді қайта бастау"
            callback_data = f"pay:start:{request.id}"
        elif previous_status == PaymentStatus.awaiting_transfer.value:
            text = (
                "Оператор отменил ожидание оплаты по этой заявке. Платеж считается "
                "несовершенным. При необходимости оплату можно начать заново."
            )
            button_text = "Начать оплату заново"
            callback_data = f"pay:start:{request.id}"
        elif request.language == "kz":
            text = (
                "Өтінім бойынша қаражаттың түскені әзірге табылмады. Сома мен карта "
                "нөмірін тексеріп, батырманы қайта басыңыз."
            )
            button_text = "Төлемді қайта тексеру"
            callback_data = f"pay:claim:{request.id}"
        else:
            text = (
                "Поступление по заявке пока не найдено. Проверьте сумму и реквизиты, "
                "затем нажмите кнопку повторно."
            )
            button_text = "Проверить оплату еще раз"
            callback_data = f"pay:claim:{request.id}"
        telegram_request(
            "sendMessage",
            {
                "chat_id": request.telegram_chat_id,
                "text": text,
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": button_text,
                                "callback_data": callback_data,
                            }
                        ]
                    ]
                },
            },
        )
    session.refresh(request)
    return request


def split_telegram_message(message: str, limit: int = 3900) -> list[str]:
    if len(message) <= limit:
        return [message]
    parts: list[str] = []
    current = ""
    for block in message.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:
            parts.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        parts.append(current)
    return parts
