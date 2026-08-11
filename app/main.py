import csv
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

import app.web as web
from app.analytics import excluded_analytics_user_ids
from app.apipay import verify_webhook_signature
from app.auction_documents import unique_auction_documents
from app.auction_exports import auction_lot_publication_history
from app.auction_service import (
    AuctionFilters,
    active_auction_lots_geojson,
    auction_category_stats,
    auction_district_stats,
    auction_lot_changes,
    auction_lot_geo_metrics,
    auction_lot_history,
    auction_lot_metrics,
    auction_market_snapshot,
    auction_region_stats,
    auction_stats,
    get_auction_lot,
    list_auction_lots,
)
from app.config import settings
from app.db import engine, get_db, init_db
from app.feedback import (
    DEFAULT_FEEDBACK_KZ,
    DEFAULT_FEEDBACK_RU,
    conversation_access_summary,
    feedback_conversation_is_unread,
    get_feedback_conversation,
    list_feedback_conversations,
    mark_feedback_conversation_read,
    recipient_access_summary,
    send_admin_feedback_reply,
    send_feedback_broadcast,
)
from app.genplan_pipeline import (
    build_document_legend_export,
    extract_next_document_legend_draft,
    legend_entry_stats,
    list_document_legend_entries,
    list_pipeline_documents,
    pipeline_document_stats,
    set_legend_entry_classification,
    sync_manual_genplans_into_pipeline,
)
from app.genplan_references import genplan_reference_payload
from app.genplan_sources import (
    probe_smart_geohub_urban_plan_sources,
    sync_ggk_urban_plan_sources,
    sync_smart_geohub_urban_plan_sources,
)
from app.manual_genplans import manual_genplan_by_asset_id, resolve_manual_genplan_file
from app.models import (
    Account,
    AccountPayment,
    AuctionLot,
    Candidate,
    FeedbackBroadcast,
    FeedbackBroadcastRecipient,
    FunnelEvent,
    GenplanLegendEntry,
    GenplanSourceDocument,
    PaymentStatus,
    PlanningCandidateReview,
    ReviewStatus,
    SearchRequest,
    UrbanPlanCoverage,
    UrbanPlanLayer,
    UrbanPlanSource,
)
from app.planning_candidate_reviews import (
    PLANNING_CANDIDATE_STATUS_LABELS,
    get_next_queued_planning_candidate,
    list_planning_candidate_reviews,
    planning_candidate_review_lookup,
    upsert_planning_candidate_review,
)
from app.planning_completion import (
    build_planning_completion_report,
    queue_planning_candidates_for_all_scopes,
    queue_planning_candidates_for_next_scope_with_egkn,
)
from app.planning_free_space import find_planning_candidate_points
from app.planning_service import PlanningScope, planning_check, planning_coverage
from app.providers.egkn import EgknProvider, EgknProviderError, normalize_name
from app.providers.urban_plan import LAYER_KINDS, UrbanPlanError, normalize_geojson
from app.purposes import (
    ALL_PURPOSES,
    GARDENING,
    LPH,
    LPH_FIELD_LAYER,
    LPH_HOUSEHOLD_LAYER,
    normalize_allotment_type,
    normalize_irrigation_type,
    normalize_purpose,
    purpose_area_ha,
)
from app.rate_limit import consume_rate_limit
from app.request_context import client_ip
from app.schemas import ReviewUpdate, SearchCreate, SearchCreated
from app.security import require_admin, require_api_key
from app.services import (
    apply_apipay_webhook,
    approve_free_preview,
    confirm_payment,
    create_search,
    deliver_apipay_report,
    deliver_request,
    dispatch_search,
    elapsed_seconds,
    free_preview_usage,
    get_request_with_candidates,
    has_paid_access,
    notify_apipay_payment_retry,
    reject_free_preview,
    reject_payment,
    request_payment,
    retry_failed_search,
    search_queue_visible_condition,
    update_candidate_review,
)
from app.urban_plan_labels import urban_plan_badge_payload

logger = logging.getLogger(__name__)

API_RATE_LIMIT_PER_MINUTE = 120
API_RATE_LIMIT_WINDOW_SECONDS = 60
ADMIN_RATE_LIMIT_PER_MINUTE = 60
ADMIN_RATE_LIMIT_WINDOW_SECONDS = 60
CSP_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data: https://*.tile.openstreetmap.org; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(self), camera=(), microphone=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            CSP_POLICY,
        )
        response.headers.setdefault("X-XSS-Protection", "0")
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    _EXEMPT_PATH_PREFIXES = ("/api/", "/webhooks/apipay", "/static/")

    async def dispatch(self, request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            path = request.url.path
            if not any(path.startswith(prefix) for prefix in self._EXEMPT_PATH_PREFIXES):
                content_type = (request.headers.get("content-type") or "").lower()
                if content_type.startswith(
                    "application/x-www-form-urlencoded"
                ) or content_type.startswith(
                    "multipart/form-data",
                ):
                    token_form_value = request.headers.get("x-csrf-token")
                    if token_form_value is None:
                        # Cache the body before parsing the form so downstream FastAPI
                        # Form parameters can read the same request payload.
                        body = await request.body()
                        if content_type.startswith("application/x-www-form-urlencoded"):
                            parsed = parse_qs(body.decode("utf-8", errors="ignore"))
                            token_form_value = (parsed.get("csrf_token") or [None])[0]
                        else:
                            form = await request.form()
                            token_form_value = form.get("csrf_token")

                    expected_token = web.csrf_token(request)
                    if isinstance(token_form_value, str):
                        provided = token_form_value
                    elif token_form_value is None:
                        provided = ""
                    else:
                        provided = str(token_form_value)
                    if not hmac.compare_digest(provided, expected_token):
                        return JSONResponse(
                            status_code=403,
                            content={"detail": "CSRF token is invalid"},
                        )

        return await call_next(request)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CSRFMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["csrf_token"] = web.csrf_token
app.include_router(web.router)
catalog_provider = EgknProvider()
payment_labels = {
    "not_requested": "не запрошена",
    "awaiting_transfer": "ожидается оплата",
    "pending_confirmation": "проверить поступление",
    "paid": "оплачено",
    "rejected": "не найдено",
}
review_status_labels = {
    ReviewStatus.pending.value: "ожидает проверки",
    ReviewStatus.approved.value: "подходит",
    ReviewStatus.approved_with_note.value: "подходит с замечанием",
    ReviewStatus.rejected.value: "не подходит",
}


def _request_client_ip(request: Request) -> str:
    return client_ip(request)


@app.middleware("http")
async def enforce_api_rate_limit(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") or path.startswith("/admin"):
        is_admin = path.startswith("/admin")
        state = consume_rate_limit(
            f"{'admin' if is_admin else 'api'}:ip:{_request_client_ip(request)}",
            limit=ADMIN_RATE_LIMIT_PER_MINUTE if is_admin else API_RATE_LIMIT_PER_MINUTE,
            window_seconds=(
                ADMIN_RATE_LIMIT_WINDOW_SECONDS
                if is_admin
                else API_RATE_LIMIT_WINDOW_SECONDS
            ),
        )
        if not state.allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please retry later."},
                headers={"Retry-After": str(state.retry_after_seconds)},
            )

    return await call_next(request)


@app.get("/manual-genplans/{asset_id}/{filename:path}")
def manual_genplan_file(asset_id: str, filename: str) -> FileResponse:
    record = manual_genplan_by_asset_id(asset_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Генплан не найден")
    path = resolve_manual_genplan_file(
        record,
        configured_root=settings.manual_genplan_files_root,
    )
    if path is None:
        raise HTTPException(status_code=404, detail="Файл генплана не найден")
    return FileResponse(
        path,
        media_type=record.media_type,
        filename=record.filename,
    )


def approved_candidate_count(search: SearchRequest) -> int:
    return sum(
        1
        for candidate in search.candidates
        if candidate.review_status in {"approved", "approved_with_note"}
    )


def admin_candidate_note(candidate: Candidate, search: SearchRequest) -> str:
    if candidate.review_status == ReviewStatus.rejected.value:
        if candidate.urban_plan_status == "blocked":
            return (
                "Система нашла место по кадастровой карте, но подключенный генплан/ПДП "
                "не подтвердил его для выбранной цели: квадрат не попал целиком в "
                "разрешенную зону или пересек ограничение. Такой вариант не выдаем "
                "клиенту как подходящий."
            )
        return (
            "Система нашла место по кадастровой карте, но обязательные проверки его "
            "отсеяли. Клиенту этот вариант лучше не показывать."
        )
    if candidate.urban_plan_status == "waived":
        return (
            "Место прошло расчет по кадастровой карте и открытым объектам, но генплан "
            "не проверен автоматически. Нужна ручная сверка по официальному источнику."
        )
    if candidate.urban_plan_status == "passed":
        return (
            "Место прошло расчет по кадастровой карте и автоматическую сверку с "
            "подключенным генпланом/ПДП."
        )
    return (
        "Место найдено по кадастровой карте. Перед выдачей клиенту проверьте статус "
        "генплана/ПДП и ссылку на официальный источник."
    )


def admin_search_status(search: SearchRequest) -> dict[str, str]:
    approved_count = approved_candidate_count(search)
    if search.status == "queued":
        return {
            "label": "В очереди",
            "detail": "Заявка ждёт запуска поиска.",
            "tone": "pending",
        }
    if search.status == "processing":
        return {
            "label": "Идёт поиск",
            "detail": f"Проверка выполняется, прогресс {search.progress}%.",
            "tone": "pending",
        }
    if search.status == "review":
        return {
            "label": "На проверке",
            "detail": f"Найдено вариантов: {approved_count}. Нужна проверка оператора.",
            "tone": "review",
        }
    if search.status == "failed":
        return {
            "label": "Сбой поиска",
            "detail": search.error_message or "Поиск остановился с технической ошибкой.",
            "tone": "failed",
        }
    if search.status == "delivered":
        if approved_count:
            return {
                "label": "Отчёт отправлен",
                "detail": f"Клиенту отправлено вариантов: {approved_count}.",
                "tone": "delivered",
            }
        return {
            "label": "Ответ отправлен",
            "detail": "Клиенту отправлен ответ без подходящих участков.",
            "tone": "empty",
        }
    if search.status == "ready":
        if search.search_outcome == "no_candidates" or approved_count == 0:
            if search.urban_plan_status == "blocked":
                detail = (
                    "Система нашла предварительные места по кадастровой карте, но "
                    "подключенный генплан/ПДП не подтвердил их для выбранной цели. "
                    "Клиенту оплату не запрашиваем."
                )
            elif search.urban_plan_status in {"unavailable", "waived"}:
                detail = (
                    "Автоматической проверки генплана для этой территории нет. "
                    "Нужна ручная сверка по официальному источнику."
                )
            else:
                detail = (
                    search.error_message
                    or "По заданным параметрам подходящие участки не найдены."
                )
            if search.search_completed_notified_at is None and search.telegram_chat_id:
                detail += " Уведомление клиенту ещё не отмечено как отправленное."
            return {
                "label": "Участки не найдены",
                "detail": detail,
                "tone": "empty",
            }
        if not search.telegram_chat_id:
            location = "в веб-кабинете" if search.web_account_id else "в админке"
            return {
                "label": "Результат готов",
                "detail": (
                    f"Найдено вариантов: {approved_count}. Результат доступен {location}; "
                    "отправка в Telegram не требуется."
                ),
                "tone": "delivered",
            }
        if search.payment_status == PaymentStatus.awaiting_transfer.value:
            return {
                "label": "Ждёт оплаты",
                "detail": f"Найдено вариантов: {approved_count}. Клиенту выставлен счёт.",
                "tone": "pending",
            }
        if search.payment_status == PaymentStatus.paid.value:
            return {
                "label": "Оплачено, не отправлено",
                "detail": f"Найдено вариантов: {approved_count}. Нужно проверить доставку отчёта.",
                "tone": "action",
            }
        return {
            "label": "Найдено, ждёт отправки",
            "detail": f"Найдено вариантов: {approved_count}. Проверьте отправку или оплату.",
            "tone": "action",
        }
    return {
        "label": search.status,
        "detail": "Технический статус заявки.",
        "tone": "pending",
    }


def invalidate_urban_plan_coverage(
    session: Session,
    *,
    region: str,
    district: str,
    locality: str,
    purpose: str,
) -> None:
    session.execute(
        delete(UrbanPlanCoverage).where(
            UrbanPlanCoverage.region == region.strip(),
            UrbanPlanCoverage.district == district.strip(),
            UrbanPlanCoverage.locality == locality.strip(),
            UrbanPlanCoverage.purpose.in_({purpose, ALL_PURPOSES}),
        )
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def readiness() -> dict[str, object]:
    checks: dict[str, bool] = {"database": False, "redis": True}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        logger.exception("Readiness database check failed")

    if not settings.run_tasks_inline:
        checks["redis"] = False
        try:
            import redis

            client = redis.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            checks["redis"] = bool(client.ping())
        except Exception:
            logger.exception("Readiness Redis check failed")

    if not all(checks.values()):
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "checks": checks},
        )
    return {"status": "ready", "checks": checks}


@app.post("/webhooks/apipay")
async def apipay_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db),
) -> dict:
    if not settings.apipay_enabled:
        raise HTTPException(status_code=503, detail="ApiPay integration is disabled")
    secret = settings.apipay_webhook_secret.strip()
    if not secret:
        raise HTTPException(status_code=503, detail="ApiPay webhook is not configured")

    raw_body = await request.body()
    signature = request.headers.get("X-Webhook-Signature", "")
    if not verify_webhook_signature(raw_body, signature, secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    try:
        result = apply_apipay_webhook(session, payload)
    except ValueError as exc:
        logger.warning("Rejected ApiPay webhook: %s", exc)
        raise HTTPException(status_code=409, detail="Webhook data conflicts with order") from exc

    if result.deliver_report and result.request_id:
        background_tasks.add_task(deliver_apipay_report, result.request_id)
    if result.notify_payment_retry and result.request_id:
        background_tasks.add_task(notify_apipay_payment_retry, result.request_id)
    if result.activate_auction_access and result.auction_access_id:
        from app.auction_access import notify_auction_payment_confirmed

        background_tasks.add_task(
            notify_auction_payment_confirmed,
            result.auction_access_id,
        )
    if result.notify_auction_payment_retry and result.auction_access_id:
        from app.auction_access import notify_auction_payment_retry

        background_tasks.add_task(
            notify_auction_payment_retry,
            result.auction_access_id,
        )
    return {
        "ok": True,
        "event": result.event,
        "status": result.status,
        "ignored": result.ignored,
    }


@app.post("/api/searches", response_model=SearchCreated, dependencies=[Depends(require_api_key)])
def api_create_search(payload: SearchCreate, session: Session = Depends(get_db)) -> SearchCreated:
    request, position = create_search(session, payload)
    dispatch_search(request.id)
    session.refresh(request)
    return SearchCreated(id=request.id, status=request.status, position=position)


@app.get("/api/searches/{request_id}", dependencies=[Depends(require_api_key)])
def api_get_search(request_id: str, session: Session = Depends(get_db)) -> dict:
    search = get_request_with_candidates(session, request_id)
    if search is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    return {
        "id": search.id,
        "status": search.status,
        "progress": search.progress,
        "error": search.error_message,
        "candidate_count": len(search.candidates),
    }


@app.get("/api/planning/coverage", dependencies=[Depends(require_api_key)])
def api_planning_coverage(
    lat: float,
    lon: float,
    requested_use: str | None = None,
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    include_shadow: bool = False,
    session: Session = Depends(get_db),
) -> dict:
    try:
        return planning_coverage(
            session,
            latitude=lat,
            longitude=lon,
            include_shadow=include_shadow,
            scope=PlanningScope(
                region=region,
                district=district,
                locality=locality,
                requested_use=requested_use,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/planning/check", dependencies=[Depends(require_api_key)])
def api_planning_check(
    payload: dict = Body(...),
    session: Session = Depends(get_db),
) -> dict:
    geometry = payload.get("geometry")
    if not isinstance(geometry, dict):
        raise HTTPException(status_code=422, detail="geometry must be a GeoJSON object")
    try:
        return planning_check(
            session,
            geometry=geometry,
            include_shadow=bool(payload.get("include_shadow", False)),
            scope=PlanningScope(
                region=payload.get("region"),
                district=payload.get("district"),
                locality=payload.get("locality"),
                requested_use=payload.get("requested_use"),
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/planning/batch-check", dependencies=[Depends(require_api_key)])
def api_planning_batch_check(
    payload: dict = Body(...),
    session: Session = Depends(get_db),
) -> dict:
    items = payload.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="items must be a list")
    if len(items) > 100:
        raise HTTPException(status_code=413, detail="batch limit is 100 geometries")

    results: list[dict] = []
    scope = PlanningScope(
        region=payload.get("region"),
        district=payload.get("district"),
        locality=payload.get("locality"),
        requested_use=payload.get("requested_use"),
    )
    include_shadow = bool(payload.get("include_shadow", False))
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("geometry"), dict):
            raise HTTPException(
                status_code=422,
                detail=f"items[{index}].geometry must be a GeoJSON object",
            )
        try:
            result = planning_check(
                session,
                geometry=item["geometry"],
                include_shadow=include_shadow,
                scope=PlanningScope(
                    region=item.get("region", scope.region),
                    district=item.get("district", scope.district),
                    locality=item.get("locality", scope.locality),
                    requested_use=item.get("requested_use", scope.requested_use),
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result["id"] = item.get("id", index)
        results.append(result)
    return {"count": len(results), "results": results}


@app.get("/api/genplans/catalog", dependencies=[Depends(require_api_key)])
def api_genplan_catalog(
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    session: Session = Depends(get_db),
) -> dict:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    statement = select(GenplanSourceDocument)
    if region:
        statement = statement.where(GenplanSourceDocument.region == region)
    if district:
        statement = statement.where(GenplanSourceDocument.district == district)
    if locality:
        statement = statement.where(GenplanSourceDocument.locality == locality)
    if status:
        statement = statement.where(GenplanSourceDocument.pipeline_status == status)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    documents = session.scalars(
        statement.order_by(GenplanSourceDocument.id.asc()).offset(offset).limit(limit)
    ).all()
    legend_counts: dict[int, int] = {}
    if documents:
        rows = session.execute(
            select(GenplanLegendEntry.document_id, func.count(GenplanLegendEntry.id))
            .where(
                GenplanLegendEntry.document_id.in_(
                    [document.id for document in documents]
                )
            )
            .group_by(GenplanLegendEntry.document_id)
        ).all()
        legend_counts = {int(document_id): int(count) for document_id, count in rows}
    return {
        "stats": {
            "documents": pipeline_document_stats(session),
            "legend_entries": legend_entry_stats(session),
        },
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": document.id,
                "asset_id": document.asset_id,
                "region": document.region,
                "district": document.district,
                "locality": document.locality,
                "title": document.title,
                "filename": document.filename,
                "detected_format": document.detected_format,
                "pdf_route": document.pdf_route,
                "page_count": document.page_count,
                "pipeline_status": document.pipeline_status,
                "next_action": document.next_action,
                "source_sha256": document.source_sha256,
                "confidence_score": document.confidence_score,
                "legend_entry_count": legend_counts.get(document.id, 0),
            }
            for document in documents
        ],
    }


@app.get("/api/genplans/layers/geojson", dependencies=[Depends(require_api_key)])
def api_genplan_layers_geojson(
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    purpose: str | None = None,
    include_shadow: bool = False,
    limit: int = 200,
    session: Session = Depends(get_db),
) -> dict:
    limit = max(1, min(limit, 500))
    statement = select(UrbanPlanLayer).where(UrbanPlanLayer.active.is_(True))
    if not include_shadow:
        statement = statement.where(UrbanPlanLayer.approved_for_search.is_(True))
    if region:
        statement = statement.where(UrbanPlanLayer.region == region)
    if district:
        statement = statement.where(UrbanPlanLayer.district == district)
    if locality:
        statement = statement.where(UrbanPlanLayer.locality == locality)
    if purpose:
        statement = statement.where(UrbanPlanLayer.purpose == purpose)
    layers = session.scalars(statement.order_by(UrbanPlanLayer.id.asc()).limit(limit)).all()
    features = []
    for layer in layers:
        try:
            geometry = json.loads(layer.geometry_geojson)
        except json.JSONDecodeError:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": layer.id,
                    "region": layer.region,
                    "district": layer.district,
                    "locality": layer.locality,
                    "purpose": layer.purpose,
                    "layer_kind": layer.layer_kind,
                    "zone_name": layer.zone_name,
                    "title": layer.title,
                    "approval_document": layer.approval_document,
                    "approval_date": layer.approval_date.isoformat()
                    if layer.approval_date
                    else None,
                    "source_authority": layer.source_authority,
                    "source_url": layer.source_url,
                    "provenance_status": layer.provenance_status,
                    "identity_status": layer.identity_status,
                    "qa_status": layer.qa_status,
                    "approved_for_search": layer.approved_for_search,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def auction_api_item(lot: AuctionLot, *, include_documents: bool = False) -> dict:
    item = {
        "id": lot.id,
        "source": lot.source,
        "source_lot_id": lot.source_lot_id,
        "source_search_status": lot.source_search_status,
        "auction_number": lot.auction_number,
        "title": lot.title,
        "status": lot.status,
        "auction_type": lot.auction_type,
        "region": lot.region,
        "district": lot.district,
        "locality": lot.locality,
        "location": lot.location_text,
        "cadastre_number": lot.cadastre_number,
        "area_ha": lot.area_ha,
        "land_rights": lot.land_rights,
        "functional_purpose_level2": lot.functional_purpose_level2,
        "functional_purpose_level3": lot.functional_purpose_level3,
        "functional_purpose_level4": lot.functional_purpose_level4,
        "use_goal": lot.use_goal,
        "purpose": lot.purpose,
        "start_price_kzt": lot.start_price_kzt,
        "guarantee_kzt": lot.guarantee_kzt,
        "sale_price_kzt": lot.sale_price_kzt,
        "auction_starts_at": lot.auction_starts_at,
        "published_at": lot.published_at,
        "seller_name": lot.seller_name,
        "seller_bin": lot.seller_bin,
        "source_url": lot.source_url,
        "active": lot.active,
        "last_seen_at": lot.last_seen_at,
    }
    if include_documents:
        item["description"] = lot.description
        item["documents"] = [
            {
                "title": document.title,
                "url": document.source_url,
                "file_type": document.file_type,
            }
            for document in unique_auction_documents(lot.documents)
        ]
    return item


def auction_geo_api_item(lot: AuctionLot) -> dict:
    geo = auction_lot_geo_metrics(lot)
    return {
        "status": geo.status,
        "latitude": geo.latitude,
        "longitude": geo.longitude,
        "distance_to_city_m": geo.distance_to_city_m,
        "road_m": geo.road_m,
        "school_m": geo.school_m,
        "hospital_m": geo.hospital_m,
        "fuel_m": geo.fuel_m,
        "railway_m": geo.railway_m,
        "power_line_m": geo.power_line_m,
    }


@app.get("/api/auctions", dependencies=[Depends(require_api_key)])
def api_auction_list(
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    purpose: str | None = None,
    min_price_kzt: float | None = None,
    max_price_kzt: float | None = None,
    min_area_ha: float | None = None,
    max_area_ha: float | None = None,
    offset: int = 0,
    limit: int = 20,
    session: Session = Depends(get_db),
) -> dict:
    offset = max(0, offset)
    limit = min(max(limit, 1), 100)
    lots, total = list_auction_lots(
        session,
        AuctionFilters(
            region=region,
            district=district,
            locality=locality,
            purpose_query=purpose,
            min_price_kzt=min_price_kzt,
            max_price_kzt=max_price_kzt,
            min_area_ha=min_area_ha,
            max_area_ha=max_area_ha,
        ),
        offset=offset,
        limit=limit,
    )
    return {
        "items": [auction_api_item(lot) for lot in lots],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/auctions/stats/market", dependencies=[Depends(require_api_key)])
def api_auction_market_stats(session: Session = Depends(get_db)) -> dict:
    return auction_market_snapshot(session)


@app.get("/api/auctions/map/geojson", dependencies=[Depends(require_api_key)])
def api_auction_map_geojson(
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    purpose: str | None = None,
    min_price_kzt: float | None = None,
    max_price_kzt: float | None = None,
    min_area_ha: float | None = None,
    max_area_ha: float | None = None,
    session: Session = Depends(get_db),
) -> dict:
    return active_auction_lots_geojson(
        session,
        AuctionFilters(
            region=region,
            district=district,
            locality=locality,
            purpose_query=purpose,
            min_price_kzt=min_price_kzt,
            max_price_kzt=max_price_kzt,
            min_area_ha=min_area_ha,
            max_area_ha=max_area_ha,
            active_only=True,
        ),
    )


@app.get("/api/auctions/{lot_id}", dependencies=[Depends(require_api_key)])
def api_auction_detail(lot_id: str, session: Session = Depends(get_db)) -> dict:
    lot = get_auction_lot(session, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Лот не найден")
    item = auction_api_item(lot, include_documents=True)
    metrics = auction_lot_metrics(session, lot)
    item["geo_metrics"] = auction_geo_api_item(lot)
    item["metrics"] = {
        "price_per_sotka": metrics.price_per_sotka,
        "price_per_square_meter": metrics.price_per_square_meter,
        "district_average_price_per_sotka": metrics.district_average_price_per_sotka,
        "district_difference_percent": metrics.district_difference_percent,
        "publication_count": metrics.publication_count,
        "failed_count": metrics.failed_count,
        "document_count": metrics.document_count,
        "district_lot_count": metrics.district_lot_count,
        "district_successful_count": metrics.district_successful_count,
        "district_failed_count": metrics.district_failed_count,
        "district_liquidity_percent": metrics.district_liquidity_percent,
        "rating": metrics.rating,
    }
    item["history_summary"] = (
        auction_lot_publication_history(
            session,
            cadastre_number=lot.cadastre_number,
        ).as_dict()
        if lot.cadastre_number
        else auction_lot_publication_history(
            session,
            source_lot_id=lot.source_lot_id,
        ).as_dict()
    )
    item["changes"] = [
        {
            "field_name": change.field_name,
            "old_value": change.old_value,
            "new_value": change.new_value,
            "changed_at": change.changed_at,
        }
        for change in auction_lot_changes(session, lot.id)[:50]
    ]
    return item


@app.get("/admin", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    searches = session.scalars(
        select(SearchRequest)
        .where(search_queue_visible_condition())
        .order_by(SearchRequest.created_at.desc())
        .limit(100)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "searches": searches,
            "app_name": settings.app_name,
            "payment_labels": payment_labels,
            "admin_search_status": admin_search_status,
        },
    )


@app.get("/admin/land-guide", response_class=HTMLResponse)
def land_guide(
    request: Request,
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        request=request,
        name="land_guide.html",
        context={"app_name": settings.app_name},
    )


@app.get("/admin/auctions", response_class=HTMLResponse)
def auctions_dashboard(
    request: Request,
    region: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    query = select(AuctionLot)
    if region:
        query = query.where(AuctionLot.region == region)
    if status:
        query = query.where(AuctionLot.status == status)
    lots = session.scalars(
        query.order_by(
            AuctionLot.auction_starts_at.is_(None),
            AuctionLot.auction_starts_at,
            AuctionLot.created_at.desc(),
        ).limit(300)
    ).all()
    regions = session.scalars(
        select(AuctionLot.region)
        .where(AuctionLot.region.is_not(None))
        .distinct()
        .order_by(AuctionLot.region)
    ).all()
    statuses = session.scalars(
        select(AuctionLot.status)
        .where(AuctionLot.status.is_not(None))
        .distinct()
        .order_by(AuctionLot.status)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="auctions.html",
        context={
            "app_name": settings.app_name,
            "lots": lots,
            "stats": auction_stats(session),
            "regions": regions,
            "statuses": statuses,
            "selected_region": region or "",
            "selected_status": status or "",
            "region_stats": auction_region_stats(session)[:12],
            "category_stats": auction_category_stats(session)[:12],
        },
    )


@app.get("/admin/auctions/export.csv")
def auctions_export_csv(
    region: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> Response:
    query = select(AuctionLot)
    if region:
        query = query.where(AuctionLot.region == region)
    if status:
        query = query.where(AuctionLot.status == status)
    lots = session.scalars(
        query.order_by(
            AuctionLot.region,
            AuctionLot.district,
            AuctionLot.auction_starts_at.is_(None),
            AuctionLot.auction_starts_at,
        ).limit(10_000)
    ).all()
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "ID",
            "Источник",
            "Номер аукциона",
            "Кадастровый номер",
            "Регион",
            "Район",
            "Населенный пункт",
            "Площадь, га",
            "Назначение",
            "Право на землю",
            "Стартовая цена, ₸",
            "Цена за сотку, ₸",
            "Цена за м2, ₸",
            "Средняя цена района за сотку, ₸",
            "Разница с районом, %",
            "Рейтинг",
            "Статус",
            "Дата торгов",
            "Ссылка",
        ]
    )
    for lot in lots:
        metrics = auction_lot_metrics(session, lot)
        writer.writerow(
            [
                lot.id,
                lot.source,
                lot.auction_number or lot.source_lot_id,
                lot.cadastre_number or "",
                lot.region or "",
                lot.district or "",
                lot.locality or "",
                lot.area_ha or "",
                lot.purpose or lot.title,
                lot.land_rights or "",
                lot.start_price_kzt or "",
                round(metrics.price_per_sotka, 2) if metrics.price_per_sotka is not None else "",
                (
                    round(metrics.price_per_square_meter, 2)
                    if metrics.price_per_square_meter is not None
                    else ""
                ),
                (
                    round(metrics.district_average_price_per_sotka, 2)
                    if metrics.district_average_price_per_sotka is not None
                    else ""
                ),
                (
                    round(metrics.district_difference_percent, 2)
                    if metrics.district_difference_percent is not None
                    else ""
                ),
                metrics.rating,
                lot.status or "",
                lot.auction_starts_at.isoformat() if lot.auction_starts_at else "",
                lot.source_url,
            ]
        )
    return Response(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="land-auctions.csv"'},
    )


@app.get("/admin/auctions/{lot_id}", response_class=HTMLResponse)
def auction_detail(
    request: Request,
    lot_id: str,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    lot = get_auction_lot(session, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Лот не найден")
    return templates.TemplateResponse(
        request=request,
        name="auction_detail.html",
        context={
            "app_name": settings.app_name,
            "lot": lot,
            "metrics": auction_lot_metrics(session, lot),
            "history": auction_lot_history(session, lot.id),
            "changes": auction_lot_changes(session, lot.id),
            "district_stats": auction_district_stats(session, lot.region)[:20],
        },
    )


@app.post("/admin/auctions/sync")
def auctions_sync(
    _: str = Depends(require_admin),
):
    if not settings.auctions_enabled:
        raise HTTPException(status_code=503, detail="Мониторинг аукционов отключен")
    from app.tasks import sync_current_auctions_task

    sync_current_auctions_task.delay()
    return RedirectResponse("/admin/auctions", status_code=303)


@app.get("/admin/analytics", response_class=HTMLResponse)
def analytics_dashboard(
    request: Request,
    days: int = 30,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    days = min(max(days, 1), 90)
    since = datetime.now(UTC) - timedelta(days=days)
    excluded_user_ids = excluded_analytics_user_ids()
    events = session.scalars(
        select(FunnelEvent).where(
            FunnelEvent.created_at >= since,
            FunnelEvent.funnel_session_id.is_not(None),
        )
    ).all()
    searches = session.scalars(
        select(SearchRequest).where(SearchRequest.created_at >= since)
    ).all()
    web_accounts = session.scalars(
        select(Account).where(
            Account.created_at >= since,
            Account.phone_verified_at.is_not(None),
            Account.password_hash.is_not(None),
        )
    ).all()
    web_payments = session.scalars(
        select(AccountPayment).where(AccountPayment.created_at >= since)
    ).all()
    web_paid_payments = session.scalars(
        select(AccountPayment).where(
            AccountPayment.payment_confirmed_at >= since,
            AccountPayment.payment_status == PaymentStatus.paid.value,
        )
    ).all()
    if excluded_user_ids:
        events = [
            item
            for item in events
            if not item.telegram_user_id or item.telegram_user_id not in excluded_user_ids
        ]
        searches = [
            item
            for item in searches
            if not item.telegram_user_id or item.telegram_user_id not in excluded_user_ids
        ]
        web_accounts = [
            item
            for item in web_accounts
            if not item.telegram_user_id or item.telegram_user_id not in excluded_user_ids
        ]

    excluded_account_ids = set()
    if excluded_user_ids:
        excluded_account_ids = set(
            session.scalars(
                select(Account.id).where(Account.telegram_user_id.in_(excluded_user_ids))
            ).all()
        )
        web_payments = [
            item for item in web_payments if item.account_id not in excluded_account_ids
        ]
        web_paid_payments = [
            item for item in web_paid_payments if item.account_id not in excluded_account_ids
        ]

    def sessions_for(event_name: str) -> set[str]:
        return {
            item.funnel_session_id
            for item in events
            if item.event_name == event_name and item.funnel_session_id
        }

    started = sessions_for("start_opened")
    completed_searches = [item for item in searches if item.search_finished_at]
    durations = [
        value
        for item in completed_searches
        if (value := elapsed_seconds(item.search_started_at, item.search_finished_at)) is not None
    ]
    outcome_counts: dict[str, int] = {}
    for item in completed_searches:
        outcome = item.search_outcome or "legacy_or_unknown"
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    paid_searches = [
        item
        for item in searches
        if item.payment_status == PaymentStatus.paid.value and item.payment_confirmed_at
    ]
    invoiced_searches = [
        item
        for item in searches
        if item.payment_provider_invoice_id or item.payment_requested_at
    ]
    delivered_paid_searches = [
        item
        for item in paid_searches
        if item.status == "delivered" or item.search_completed_notified_at
    ]
    rows = [
        ("Открыл бот", len(started)),
        ("Выбрал язык", len(sessions_for("language_selected"))),
        ("Принял условия", len(sessions_for("terms_accepted"))),
        ("Выбрал назначение", len(sessions_for("purpose_selected"))),
        ("Подтвердил поиск", len(sessions_for("search_confirmed"))),
        ("Поиск завершён", len(sessions_for("search_completed"))),
        ("Получил бесплатные варианты", len(sessions_for("free_results_delivered"))),
        ("Увидел предложение открыть остальные", len(sessions_for("paywall_viewed"))),
        ("Нажал открыть остальные", len(sessions_for("payment_button_clicked"))),
        ("Счёт создан", len(invoiced_searches)),
        ("Оплата подтверждена", len(paid_searches)),
        ("Получил полный отчёт", len(delivered_paid_searches)),
    ]
    auction_rows = [
        ("Открыл аукционы", len(sessions_for("auction_opened"))),
        ("Настроил фильтр аукционов", len(sessions_for("auction_filter_completed"))),
        ("Открыл карточку лота", len(sessions_for("auction_lot_viewed"))),
        ("Добавил/убрал избранное", len(sessions_for("auction_favorite_toggled"))),
        ("Создал уведомление о лотах", len(sessions_for("auction_subscription_created"))),
        ("Увидел ограничение аукционов", len(sessions_for("auction_paywall_viewed"))),
        ("Нажал открыть аукционы", len(sessions_for("auction_payment_clicked"))),
        ("Создан счет за аукционы", len(sessions_for("auction_invoice_created"))),
        ("Оплатил доступ к аукционам", len(sessions_for("auction_payment_paid"))),
    ]
    auction_started = sessions_for("auction_opened")
    auction_catalog = auction_stats(session, excluded_user_ids=excluded_user_ids)
    web_searches = [item for item in searches if item.web_account_id]
    web_search_accounts = {item.web_account_id for item in web_searches if item.web_account_id}
    web_completed_searches = [item for item in web_searches if item.search_finished_at]
    web_payment_accounts = {item.account_id for item in web_payments}
    web_paid_accounts = {
        item.account_id
        for item in web_paid_payments
        if (item.payment_amount_kzt or 0) > 0
    }
    active_trial_accounts = [
        item
        for item in session.scalars(
            select(Account).where(
                Account.trial_expires_at > datetime.now(UTC),
            )
        ).all()
        if not excluded_account_ids or item.id not in excluded_account_ids
    ]
    web_rows = [
        ("Зарегистрировались", len(web_accounts)),
        ("С активным тестовым доступом", len(active_trial_accounts)),
        (
            "Привязали Telegram",
            sum(1 for item in web_accounts if item.telegram_user_id),
        ),
        ("Начали поиск", len(web_search_accounts)),
        ("Всего веб-поисков", len(web_searches)),
        ("Поиск завершён", len(web_completed_searches)),
        ("Создали счет Kaspi", len(web_payment_accounts)),
        ("Оплатили", len(web_paid_accounts)),
    ]
    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "app_name": settings.app_name,
            "days": days,
            "rows": rows,
            "auction_rows": auction_rows,
            "web_rows": web_rows,
            "web_registered_count": len(web_accounts),
            "started_count": len(started),
            "auction_started_count": len(auction_started),
            "auction_catalog": auction_catalog,
            "average_duration_seconds": (
                round(sum(durations) / len(durations)) if durations else None
            ),
            "completed_count": len(completed_searches),
            "outcomes": sorted(outcome_counts.items(), key=lambda item: (-item[1], item[0])),
            "technical": {
                "egkn_unavailable": outcome_counts.get("egkn_unavailable", 0),
                "timeout": outcome_counts.get("timeout", 0),
                "technical_error": outcome_counts.get("technical_error", 0),
            },
            "legacy_event_count": session.scalar(
                select(func.count()).select_from(FunnelEvent).where(
                    FunnelEvent.created_at >= since,
                    FunnelEvent.funnel_session_id.is_(None),
                    FunnelEvent.telegram_user_id.not_in(excluded_user_ids)
                    if excluded_user_ids
                    else True,
                )
            ) or 0,
        },
    )


@app.get("/admin/feedback", response_class=HTMLResponse)
def admin_feedback(
    request: Request,
    broadcast_id: str | None = None,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    broadcasts = session.scalars(
        select(FeedbackBroadcast).order_by(FeedbackBroadcast.created_at.desc()).limit(20)
    ).all()
    selected_broadcast = session.get(FeedbackBroadcast, broadcast_id) if broadcast_id else None
    recipients = []
    if selected_broadcast is not None:
        recipients = session.scalars(
            select(FeedbackBroadcastRecipient)
            .where(FeedbackBroadcastRecipient.broadcast_id == selected_broadcast.id)
            .order_by(
                FeedbackBroadcastRecipient.status.asc(),
                FeedbackBroadcastRecipient.sent_at.desc().nullslast(),
            )
        ).all()
    conversations = list_feedback_conversations(session)
    conversation_access = {
        item.id: conversation_access_summary(session, item) for item in conversations
    }
    conversation_unread = {
        item.id: feedback_conversation_is_unread(item) for item in conversations
    }
    unread_count = sum(1 for is_unread in conversation_unread.values() if is_unread)
    recipient_access = {
        item.id: recipient_access_summary(session, item) for item in recipients
    }
    return templates.TemplateResponse(
        request=request,
        name="feedback_admin.html",
        context={
            "app_name": settings.app_name,
            "broadcasts": broadcasts,
            "selected_broadcast": selected_broadcast,
            "recipients": recipients,
            "conversations": conversations,
            "conversation_access": conversation_access,
            "conversation_unread": conversation_unread,
            "unread_count": unread_count,
            "recipient_access": recipient_access,
            "default_ru": DEFAULT_FEEDBACK_RU,
            "default_kz": DEFAULT_FEEDBACK_KZ,
        },
    )


@app.post("/admin/feedback/broadcast")
def admin_feedback_broadcast(
    title: str = Form("Запрос впечатлений"),
    ru_text: str = Form(DEFAULT_FEEDBACK_RU),
    kz_text: str = Form(DEFAULT_FEEDBACK_KZ),
    session: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    broadcast = send_feedback_broadcast(
        session,
        title=title,
        ru_text=ru_text,
        kz_text=kz_text,
        created_by=admin_user,
    )
    return RedirectResponse(f"/admin/feedback?broadcast_id={broadcast.id}", status_code=303)


@app.get("/admin/feedback/{conversation_id}", response_class=HTMLResponse)
def admin_feedback_conversation(
    request: Request,
    conversation_id: str,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    conversation = get_feedback_conversation(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    conversation = mark_feedback_conversation_read(session, conversation)
    access_summary = conversation_access_summary(session, conversation)
    return templates.TemplateResponse(
        request=request,
        name="feedback_conversation.html",
        context={
            "app_name": settings.app_name,
            "conversation": conversation,
            "access_summary": access_summary,
        },
    )


@app.post("/admin/feedback/{conversation_id}/reply")
def admin_feedback_reply(
    conversation_id: str,
    text: str = Form(...),
    session: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    try:
        send_admin_feedback_reply(
            session,
            conversation_id=conversation_id,
            text=text,
            admin_user=admin_user,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/admin/feedback/{conversation_id}", status_code=303)


@app.get("/admin/urban-plans", response_class=HTMLResponse)
def urban_plan_layers(
    request: Request,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    planning_probe_defaults = {
        "region": request.query_params.get("planning_region", "Акмолинская область"),
        "district": request.query_params.get("planning_district", "г.Акколь"),
        "locality": request.query_params.get("planning_locality", "г.Акколь"),
        "requested_use": request.query_params.get("planning_use", "LPH_HOMESTEAD"),
        "latitude": request.query_params.get("planning_lat", "51.992950"),
        "longitude": request.query_params.get("planning_lon", "70.930765"),
        "include_shadow": request.query_params.get("planning_shadow", "1") == "1",
    }
    planning_probe_result = None
    planning_probe_error = None
    candidate_defaults = {
        "region": request.query_params.get("candidate_region", "Акмолинская область"),
        "district": request.query_params.get("candidate_district", "г.Акколь"),
        "locality": request.query_params.get("candidate_locality", "г.Акколь"),
        "requested_use": request.query_params.get("candidate_use", "LPH_HOMESTEAD"),
        "limit": request.query_params.get("candidate_limit", "25"),
        "grid_step_m": request.query_params.get("candidate_step", "90"),
        "restriction_buffer_m": request.query_params.get("candidate_buffer", "20"),
        "include_shadow": request.query_params.get("candidate_shadow", "1") == "1",
        "use_egkn_context": request.query_params.get("candidate_egkn", "1") == "1",
    }
    candidate_result = None
    candidate_error = None
    candidate_scope = PlanningScope(
        region=candidate_defaults["region"].strip() or None,
        district=candidate_defaults["district"].strip() or None,
        locality=candidate_defaults["locality"].strip() or None,
        requested_use=candidate_defaults["requested_use"],
    )
    if request.query_params.get("planning_probe"):
        try:
            planning_probe_result = planning_coverage(
                session,
                latitude=float(planning_probe_defaults["latitude"]),
                longitude=float(planning_probe_defaults["longitude"]),
                scope=PlanningScope(
                    region=planning_probe_defaults["region"].strip() or None,
                    district=planning_probe_defaults["district"].strip() or None,
                    locality=planning_probe_defaults["locality"].strip() or None,
                    requested_use=planning_probe_defaults["requested_use"],
                ),
                include_shadow=planning_probe_defaults["include_shadow"],
            )
        except (TypeError, ValueError) as exc:
            planning_probe_error = str(exc)
    if request.query_params.get("candidate_find"):
        try:
            candidate_result = find_planning_candidate_points(
                session,
                scope=candidate_scope,
                include_shadow=candidate_defaults["include_shadow"],
                limit=int(candidate_defaults["limit"]),
                grid_step_m=int(candidate_defaults["grid_step_m"]),
                restriction_buffer_m=int(candidate_defaults["restriction_buffer_m"]),
                use_egkn_context=candidate_defaults["use_egkn_context"],
            )
        except (TypeError, ValueError) as exc:
            candidate_error = str(exc)
    candidate_reviews = list_planning_candidate_reviews(
        session,
        scope=candidate_scope,
        limit=200,
    )
    candidate_review_lookup = planning_candidate_review_lookup(candidate_reviews)
    planning_completion = build_planning_completion_report(session)
    layers = session.scalars(
        select(UrbanPlanLayer).order_by(
            UrbanPlanLayer.active.desc(), UrbanPlanLayer.created_at.desc()
        )
    ).all()
    sources = session.scalars(
        select(UrbanPlanSource)
        .order_by(
            UrbanPlanSource.coverage_status.desc(),
            UrbanPlanSource.locality.asc(),
            UrbanPlanSource.updated_at.desc(),
        )
        .limit(700)
    ).all()
    source_stats = session.execute(
        select(
            UrbanPlanSource.platform,
            UrbanPlanSource.coverage_status,
            func.count(UrbanPlanSource.id),
        ).group_by(UrbanPlanSource.platform, UrbanPlanSource.coverage_status)
    ).all()
    pipeline_documents = list_pipeline_documents(session, limit=120)
    pipeline_stats = pipeline_document_stats(session)
    legend_stats = legend_entry_stats(session)
    return templates.TemplateResponse(
        request=request,
        name="urban_plans.html",
        context={
            "layers": layers,
            "sources": sources,
            "source_stats": source_stats,
            "pipeline_documents": pipeline_documents,
            "pipeline_stats": pipeline_stats,
            "legend_stats": legend_stats,
            "app_name": settings.app_name,
            "strict_mode": settings.urban_plan_check_mode.lower() == "strict",
            "planning_probe_defaults": planning_probe_defaults,
            "planning_probe_result": planning_probe_result,
            "planning_probe_error": planning_probe_error,
            "candidate_defaults": candidate_defaults,
            "candidate_result": candidate_result,
            "candidate_error": candidate_error,
            "candidate_reviews": candidate_reviews,
            "candidate_review_lookup": candidate_review_lookup,
            "candidate_status_labels": PLANNING_CANDIDATE_STATUS_LABELS,
            "planning_completion": planning_completion,
        },
    )


@app.get("/admin/planning-candidates/review-next", response_class=HTMLResponse)
def review_next_planning_candidate(
    request: Request,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    candidate = get_next_queued_planning_candidate(session)
    queued_count = session.scalar(
        select(func.count(PlanningCandidateReview.id)).where(
            PlanningCandidateReview.status == "queued"
        )
    )
    reviewed_count = session.scalar(
        select(func.count(PlanningCandidateReview.id)).where(
            PlanningCandidateReview.status != "queued"
        )
    )
    return templates.TemplateResponse(
        request=request,
        name="planning_candidate_review_next.html",
        context={
            "app_name": settings.app_name,
            "candidate": candidate,
            "candidate_status_labels": PLANNING_CANDIDATE_STATUS_LABELS,
            "queued_count": queued_count or 0,
            "reviewed_count": reviewed_count or 0,
        },
    )


@app.post("/admin/planning-candidates/review")
def review_planning_candidate(
    candidate_region: str = Form(...),
    candidate_district: str = Form(...),
    candidate_locality: str = Form(""),
    candidate_use: str = Form(...),
    candidate_latitude: float = Form(...),
    candidate_longitude: float = Form(...),
    candidate_status: str = Form(...),
    candidate_note: str = Form(""),
    candidate_trust_level: str = Form(""),
    candidate_allowed_area_ha: float | None = Form(None),
    candidate_nearby_cadastre: str = Form(""),
    candidate_nearby_distance_m: float | None = Form(None),
    candidate_nearby_land_use: str = Form(""),
    candidate_candidate_area_ha: float | None = Form(None),
    candidate_selection_reason: str = Form(""),
    candidate_limit: str = Form("25"),
    candidate_step: str = Form("90"),
    candidate_buffer: str = Form("20"),
    candidate_shadow: str = Form("1"),
    candidate_egkn: str = Form("1"),
    review_return: str = Form("list"),
    session: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    scope = PlanningScope(
        region=candidate_region.strip() or None,
        district=candidate_district.strip() or None,
        locality=candidate_locality.strip(),
        requested_use=candidate_use,
    )
    try:
        upsert_planning_candidate_review(
            session,
            scope=scope,
            latitude=candidate_latitude,
            longitude=candidate_longitude,
            status=candidate_status,
            note=candidate_note,
            trust_level=candidate_trust_level or None,
            allowed_area_ha=candidate_allowed_area_ha,
            nearby_cadastre=candidate_nearby_cadastre,
            nearby_distance_m=candidate_nearby_distance_m,
            nearby_land_use=candidate_nearby_land_use,
            candidate_area_ha=candidate_candidate_area_ha,
            selection_reason=candidate_selection_reason,
            reviewed_by=admin_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if review_return == "next":
        return RedirectResponse(
            "/admin/planning-candidates/review-next",
            status_code=303,
        )
    params = urlencode(
        {
            "candidate_find": "1",
            "candidate_region": candidate_region,
            "candidate_district": candidate_district,
            "candidate_locality": candidate_locality,
            "candidate_use": candidate_use,
            "candidate_limit": candidate_limit,
            "candidate_step": candidate_step,
            "candidate_buffer": candidate_buffer,
            "candidate_shadow": candidate_shadow,
            "candidate_egkn": candidate_egkn,
        }
    )
    return RedirectResponse(
        f"/admin/urban-plans?{params}#planning-candidates",
        status_code=303,
    )


@app.post("/admin/planning-candidates/queue-all")
def queue_all_planning_candidates(
    limit_per_scope: int = Form(5),
    max_scopes: int = Form(160),
    grid_step_m: int = Form(180),
    session: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    stats = queue_planning_candidates_for_all_scopes(
        session,
        limit_per_scope=limit_per_scope,
        max_scopes=max_scopes,
        grid_step_m=grid_step_m,
        reviewed_by=admin_user,
    )
    params = urlencode(
        {
            "queue_scopes": stats["scopes_checked"],
            "queue_with_points": stats["scopes_with_points"],
            "queue_created": stats["points_created"],
            "queue_existing": stats["points_existing"],
            "queue_failed": stats["failed"],
        }
    )
    return RedirectResponse(
        f"/admin/urban-plans?{params}#planning-completion",
        status_code=303,
    )


@app.post("/admin/planning-candidates/queue-next-egkn")
def queue_next_planning_candidates_with_egkn(
    limit_per_scope: int = Form(2),
    grid_step_m: int = Form(180),
    session: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    stats = queue_planning_candidates_for_next_scope_with_egkn(
        session,
        limit_per_scope=limit_per_scope,
        grid_step_m=grid_step_m,
        reviewed_by=admin_user,
    )
    params = urlencode(
        {
            "smart_scope": stats["scope_found"],
            "smart_created": stats["points_created"],
            "smart_failed": stats["failed"],
            "smart_region": stats["region"],
            "smart_district": stats["district"],
            "smart_locality": stats["locality"],
            "smart_message": stats["message"],
        }
    )
    return RedirectResponse(
        f"/admin/urban-plans?{params}#planning-completion",
        status_code=303,
    )


@app.post("/admin/genplan-pipeline/sync-manual")
def sync_manual_genplan_pipeline(
    limit: int = Form(200),
    session: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    stats = sync_manual_genplans_into_pipeline(
        session,
        limit=limit,
        ingested_by=admin_user,
    )
    params = urlencode(
        {
            "pipeline_seen": stats["seen"],
            "pipeline_created": stats["created"],
            "pipeline_updated": stats["updated"],
            "pipeline_missing": stats["missing"],
            "pipeline_pdf": stats["pdf"],
            "pipeline_raster": stats["raster"],
            "pipeline_failed": stats["failed"],
        }
    )
    return RedirectResponse(
        f"/admin/urban-plans?{params}#genplan-pipeline",
        status_code=303,
    )


@app.post("/admin/genplan-pipeline/extract-next-legend")
def extract_next_genplan_legend(
    limit_colors: int = Form(12),
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    stats = extract_next_document_legend_draft(
        session,
        limit_colors=limit_colors,
    )
    params = urlencode(
        {
            "legend_doc": stats["document_id"],
            "legend_file": stats["filename"],
            "legend_colors": stats["colors_created"],
            "legend_status": stats["status"],
            "legend_message": stats["message"],
        }
    )
    return RedirectResponse(
        f"/admin/urban-plans?{params}#genplan-pipeline",
        status_code=303,
    )


@app.get("/admin/genplan-pipeline/documents/{document_id}/legend", response_class=HTMLResponse)
def genplan_document_legend_view(
    request: Request,
    document_id: int,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    document = session.get(GenplanSourceDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    entries = list_document_legend_entries(session, document_id)
    return templates.TemplateResponse(
        request=request,
        name="genplan_legend.html",
        context={
            "app_name": settings.app_name,
            "document": document,
            "entries": entries,
        },
    )


@app.post("/admin/genplan-pipeline/legend/{entry_id}/classify")
def classify_genplan_legend_entry(
    entry_id: int,
    target_category: str = Form(...),
    layer_kind: str = Form(...),
    review_status: str = Form(...),
    notes: str = Form(""),
    document_id: int | None = Form(None),
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    try:
        entry = set_legend_entry_classification(
            session,
            entry_id,
            target_category=target_category,
            layer_kind=layer_kind,
            review_status=review_status,
            notes=notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if document_id is not None:
        return RedirectResponse(
            f"/admin/genplan-pipeline/documents/{document_id}/legend",
            status_code=303,
        )
    params = urlencode({"legend_classified": entry.id, "legend_status": entry.review_status})
    return RedirectResponse(
        f"/admin/urban-plans?{params}#genplan-pipeline",
        status_code=303,
    )


@app.get("/admin/genplan-pipeline/documents/{document_id}/legend-export")
def export_genplan_document_legend(
    document_id: int,
    session: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    try:
        payload = build_document_legend_export(
            session,
            document_id,
            reviewer_id=admin_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{payload["record_id"]}-legend.json"'
        },
    )


@app.post("/admin/urban-plan-sources/sync-ggk")
def sync_ggk_urban_plan_catalog(
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    try:
        stats = sync_ggk_urban_plan_sources(session)
    except Exception as exc:
        logger.exception("Failed to sync GGK urban-plan sources")
        raise HTTPException(
            status_code=502,
            detail="Не удалось получить каталог ГГК: " + str(exc),
        ) from exc
    return RedirectResponse(
        "/admin/urban-plans"
        f"?ggk_seen={stats['seen']}&ggk_created={stats['created']}"
        f"&ggk_updated={stats['updated']}#sources",
        status_code=303,
    )


@app.post("/admin/urban-plan-sources/sync-smart-geohub")
def sync_smart_geohub_urban_plan_catalog(
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    stats = sync_smart_geohub_urban_plan_sources(session)
    return RedirectResponse(
        "/admin/urban-plans"
        f"?smart_seen={stats['seen']}&smart_created={stats['created']}"
        f"&smart_updated={stats['updated']}&smart_failed={stats['failed']}#sources",
        status_code=303,
    )


@app.post("/admin/urban-plan-sources/probe-smart-geohub")
def probe_smart_geohub_urban_plan_catalog(
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    stats = probe_smart_geohub_urban_plan_sources(session, limit=60)
    return RedirectResponse(
        "/admin/urban-plans"
        f"?probe_checked={stats['checked']}&probe_geometry={stats['geometry_found']}"
        f"&probe_empty={stats['no_features']}&probe_failed={stats['failed']}"
        f"&probe_skipped={stats['skipped']}#sources",
        status_code=303,
    )


@app.post("/admin/urban-plans")
async def upload_urban_plan_layer(
    region: str = Form(...),
    district: str = Form(...),
    locality: str = Form(""),
    purpose: str = Form(ALL_PURPOSES),
    layer_kind: str = Form(...),
    zone_name: str = Form(""),
    title: str = Form(...),
    approval_document: str = Form(...),
    approval_date: str = Form(""),
    source_authority: str = Form(...),
    source_url: str = Form(...),
    source_epsg: int = Form(4326),
    geojson_file: UploadFile = File(...),
    session: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    if purpose not in {
        ALL_PURPOSES,
        LPH,
        LPH_HOUSEHOLD_LAYER,
        LPH_FIELD_LAYER,
        GARDENING,
    }:
        raise HTTPException(status_code=400, detail="Недопустимое назначение слоя")
    if layer_kind not in LAYER_KINDS:
        raise HTTPException(status_code=400, detail="Недопустимый тип слоя")
    parsed_source = urlparse(source_url.strip())
    if parsed_source.scheme.lower() != "https" or not parsed_source.hostname:
        raise HTTPException(status_code=400, detail="Нужна HTTPS-ссылка на официальный источник")
    allowed_domains = {
        item.strip().lower()
        for item in settings.urban_plan_source_domains.split(",")
        if item.strip()
    }
    source_host = parsed_source.hostname.lower()
    if not any(
        source_host == domain or source_host.endswith(f".{domain}")
        for domain in allowed_domains
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Домен источника не входит в список официальных: "
                + ", ".join(sorted(allowed_domains))
            ),
        )
    required = {
        "область": region,
        "район": district,
        "название документа": title,
        "акт утверждения": approval_document,
        "орган-источник": source_authority,
    }
    missing = [label for label, value in required.items() if not value.strip()]
    if missing:
        raise HTTPException(
            status_code=400, detail="Не заполнено: " + ", ".join(missing)
        )
    max_bytes = settings.urban_plan_max_upload_mb * 1024 * 1024
    raw = await geojson_file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"GeoJSON больше {settings.urban_plan_max_upload_mb} МБ",
        )
    try:
        normalized = normalize_geojson(raw, layer_kind, source_epsg)
        parsed_date = date.fromisoformat(approval_date) if approval_date else None
    except (UrbanPlanError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    layer = UrbanPlanLayer(
        region=region.strip(),
        district=district.strip(),
        locality=locality.strip(),
        purpose=purpose,
        layer_kind=layer_kind,
        zone_name=zone_name.strip() or None,
        title=title.strip(),
        approval_document=approval_document.strip(),
        approval_date=parsed_date,
        source_authority=source_authority.strip(),
        source_url=source_url.strip(),
        source_epsg=4326,
        source_file_name=Path(geojson_file.filename or "layer.geojson").name,
        source_sha256=sha256(raw).hexdigest(),
        provenance_status="unknown",
        identity_status="unverified",
        qa_status="pending",
        independent_review=False,
        approved_for_search=False,
        uploaded_by=admin_user,
        geometry_geojson=normalized,
        active=False,
    )
    session.add(layer)
    invalidate_urban_plan_coverage(
        session,
        region=layer.region,
        district=layer.district,
        locality=layer.locality,
        purpose=layer.purpose,
    )
    session.commit()
    return RedirectResponse("/admin/urban-plans", status_code=303)


@app.post("/admin/urban-plans/{layer_id}/toggle")
def toggle_urban_plan_layer(
    layer_id: int,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    layer = session.get(UrbanPlanLayer, layer_id)
    if layer is None:
        raise HTTPException(status_code=404, detail="Слой не найден")
    if not layer.active and not (
        layer.approved_for_search
        and layer.provenance_status == "verified_official"
        and layer.identity_status == "matched"
        and layer.qa_status in {"STRICT", "VERIFIED_STRICT"}
        and layer.independent_review
        and layer.source_sha256
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Слой нельзя включить до подтверждения официального источника, "
                "территории, SHA-256 и независимого QA."
            ),
        )
    layer.active = not layer.active
    invalidate_urban_plan_coverage(
        session,
        region=layer.region,
        district=layer.district,
        locality=layer.locality,
        purpose=layer.purpose,
    )
    session.commit()
    return RedirectResponse("/admin/urban-plans", status_code=303)


@app.get("/admin/catalog/regions")
def catalog_regions(_: str = Depends(require_admin)) -> list[dict[str, str]]:
    try:
        return [
            {
                "value": row.get("name") or row.get("nameRu") or "",
                "label": row.get("nameRu") or row.get("name") or "",
            }
            for row in catalog_provider.regions()
            if row.get("name") or row.get("nameRu")
        ]
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Справочник областей ЕГКН недоступен") from exc


@app.get("/admin/catalog/districts")
def catalog_districts(
    region: str,
    _: str = Depends(require_admin),
) -> list[dict[str, str | int]]:
    try:
        rows = catalog_provider.districts(region)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Справочник районов ЕГКН недоступен") from exc
    return [
        {
            "id": row.id,
            "value": (
                f"{row.name} район"
                if normalize_name(row.display_name).startswith(normalize_name("район"))
                or row.display_name.lower().startswith("р-н")
                else row.name
            ),
            "label": row.display_name,
        }
        for row in rows
    ]


@app.get("/admin/catalog/settlements")
def catalog_settlements(
    district_id: int,
    _: str = Depends(require_admin),
) -> list[dict[str, str]]:
    try:
        rows = catalog_provider.settlement_options(district_id)
    except EgknProviderError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Справочник населенных пунктов ЕГКН недоступен"
        ) from exc
    rows.sort(key=lambda row: normalize_name(row.name))
    if not rows:
        return [
            {
                "value": "__district__",
                "label": "Искать по территории выбранного района",
            }
        ]
    return [{"value": row.name, "label": f"{row.name} · КАТО {row.kato}"} for row in rows]


@app.post("/admin/searches")
def admin_create_search(
    region: str = Form("Акмолинская область"),
    district: str = Form(...),
    locality: str = Form(""),
    purpose: str = Form(LPH),
    allotment_type: str = Form("household"),
    irrigation_type: str = Form("non_irrigated"),
    result_limit: int = Form(10),
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    purpose = normalize_purpose(purpose)
    allotment_type = normalize_allotment_type(allotment_type)
    irrigation_type = normalize_irrigation_type(irrigation_type)
    payload = SearchCreate(
        region=region,
        district=district,
        locality=None if locality == "__district__" else locality or None,
        purpose=purpose,
        allotment_type=allotment_type,
        irrigation_type=irrigation_type,
        area_ha=purpose_area_ha(purpose, irrigation_type),
        result_limit=result_limit,
        cemetery_buffer_m=0,
    )
    search, _position = create_search(session, payload)
    dispatch_search(search.id)
    return RedirectResponse(f"/admin/searches/{search.id}", status_code=303)


@app.get("/admin/searches/{request_id}", response_class=HTMLResponse)
def search_detail(
    request: Request,
    request_id: str,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    search = get_request_with_candidates(session, request_id)
    if search is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    free_used = (
        free_preview_usage(session, search.telegram_user_id)
        if search.telegram_user_id
        else 0
    )
    paid_access = has_paid_access(session, search.telegram_user_id)
    return templates.TemplateResponse(
        request=request,
        name="request_detail.html",
        context={
            "search": search,
            "app_name": settings.app_name,
            "payment_labels": payment_labels,
            "review_status_labels": review_status_labels,
            "admin_search_status": admin_search_status,
            "admin_candidate_note": admin_candidate_note,
            "urban_plan_badge": urban_plan_badge_payload,
            "free_preview_limit": settings.free_preview_plot_limit,
            "paid_search_enabled": settings.paid_search_enabled,
            "free_preview_used": free_used,
            "paid_access": paid_access,
            "genplan_reference": genplan_reference_payload(
                search,
                language=search.language,
                manual_files_root=settings.manual_genplan_files_root,
            ),
        },
    )


@app.post("/admin/candidates/{candidate_id}/review")
def review_candidate(
    candidate_id: int,
    status: str = Form(...),
    google_checked: bool = Form(False),
    notes: str = Form(""),
    reviewer: str = Depends(require_admin),
    session: Session = Depends(get_db),
):
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Кандидат не найден")
    request_id = candidate.request_id
    try:
        update_candidate_review(
            session,
            candidate_id,
            ReviewUpdate(
                status=status,
                google_checked=google_checked,
                notes=notes,
                reviewer=reviewer,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/admin/searches/{request_id}", status_code=303)


@app.post("/admin/searches/{request_id}/retry")
def admin_retry_search(
    request_id: str,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    source = session.get(SearchRequest, request_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    try:
        retry, _position, created = retry_failed_search(
            session,
            request_id,
            telegram_user_id=source.telegram_user_id,
            telegram_chat_id=source.telegram_chat_id,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if created:
        dispatch_search(retry.id)
    return RedirectResponse(f"/admin/searches/{retry.id}", status_code=303)


@app.post("/admin/searches/{request_id}/deliver", response_class=HTMLResponse)
def deliver(
    request: Request,
    request_id: str,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    try:
        message = deliver_request(session, request_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request=request,
        name="delivered.html",
        context={"message": message, "request_id": request_id, "app_name": settings.app_name},
    )


@app.post("/admin/searches/{request_id}/request-payment")
def admin_request_payment(
    request_id: str,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    try:
        request_payment(session, request_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/admin/searches/{request_id}", status_code=303)


@app.post("/admin/searches/{request_id}/confirm-payment")
def admin_confirm_payment(
    request_id: str,
    session: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    try:
        confirm_payment(session, request_id, confirmed_by=f"web:{admin_user}")
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/admin/searches/{request_id}", status_code=303)


@app.post("/admin/searches/{request_id}/reject-payment")
def admin_reject_payment(
    request_id: str,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    try:
        reject_payment(session, request_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/admin/searches/{request_id}", status_code=303)


@app.post("/admin/searches/{request_id}/approve-free-preview")
def admin_approve_free_preview(
    request_id: str,
    session: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    try:
        approve_free_preview(session, request_id, approved_by=admin_user)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/admin/searches/{request_id}", status_code=303)


@app.post("/admin/searches/{request_id}/reject-free-preview")
def admin_reject_free_preview(
    request_id: str,
    session: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    try:
        reject_free_preview(session, request_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/admin/searches/{request_id}", status_code=303)
