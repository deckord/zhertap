import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry import Point, box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union
from shapely.prepared import PreparedGeometry, prep
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SearchRequest, UrbanPlanCoverage, UrbanPlanLayer, UrbanPlanSource
from app.providers.egkn import normalize_name
from app.purposes import (
    ALL_PURPOSES,
    FIELD,
    LPH_FIELD_LAYER,
    LPH_HOUSEHOLD_LAYER,
    normalize_allotment_type,
    purpose_family,
)
from app.schemas import ALL_DISTRICTS

logger = logging.getLogger(__name__)

LAYER_KINDS = {"allowed", "prohibited", "red_line"}
POLYGON_KINDS = {"Polygon", "MultiPolygon"}
RED_LINE_KINDS = POLYGON_KINDS | {"LineString", "MultiLineString"}


class UrbanPlanError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UrbanPlanDecision:
    status: str
    message: str
    zone: str | None = None
    document: str | None = None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class UrbanPlanEvaluation:
    coverage_available: bool
    message: str
    decisions: list[UrbanPlanDecision]
    coverage_id: int | None = None
    coverage_status: str | None = None


@dataclass(frozen=True, slots=True)
class UrbanPlanSourceHint:
    message: str
    source_url: str | None


def _geojson_geometries(payload: dict[str, Any]) -> list[BaseGeometry]:
    payload_type = payload.get("type")
    if payload_type == "FeatureCollection":
        rows = [item.get("geometry") for item in payload.get("features", [])]
    elif payload_type == "Feature":
        rows = [payload.get("geometry")]
    else:
        rows = [payload]
    geometries: list[BaseGeometry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            geometry = make_valid(shape(row))
        except Exception as exc:
            raise UrbanPlanError("GeoJSON содержит некорректную геометрию") from exc
        if not geometry.is_empty:
            geometries.append(geometry)
    if not geometries:
        raise UrbanPlanError("GeoJSON не содержит непустых геометрий")
    return geometries


def normalize_geojson(raw: bytes | str, layer_kind: str, source_epsg: int) -> str:
    if layer_kind not in LAYER_KINDS:
        raise UrbanPlanError("Неизвестный тип слоя")
    if not 1024 <= source_epsg <= 999999:
        raise UrbanPlanError("Укажите корректный EPSG исходного файла")
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UrbanPlanError("Файл должен быть корректным GeoJSON в UTF-8") from exc
    if not isinstance(payload, dict):
        raise UrbanPlanError("Корень GeoJSON должен быть объектом")
    geometries = _geojson_geometries(payload)
    allowed_types = RED_LINE_KINDS if layer_kind == "red_line" else POLYGON_KINDS
    invalid = sorted({item.geom_type for item in geometries if item.geom_type not in allowed_types})
    if invalid:
        raise UrbanPlanError("Недопустимый тип геометрии для этого слоя: " + ", ".join(invalid))
    merged = make_valid(unary_union(geometries))
    if merged.is_empty:
        raise UrbanPlanError("После объединения геометрия оказалась пустой")
    try:
        transformer = Transformer.from_crs(source_epsg, 4326, always_xy=True)
        wgs84 = transform(transformer.transform, merged)
    except Exception as exc:
        raise UrbanPlanError("Не удалось преобразовать геометрию из указанного EPSG") from exc
    min_x, min_y, max_x, max_y = wgs84.bounds
    if min_x < -180 or max_x > 180 or min_y < -90 or max_y > 90:
        raise UrbanPlanError("Геометрия после преобразования выходит за границы WGS84")
    return json.dumps(mapping(wgs84), ensure_ascii=False, separators=(",", ":"))


def _matches_scope(layer: UrbanPlanLayer, request: SearchRequest) -> bool:
    def wildcard(value: str) -> bool:
        return value.strip().casefold() in {"*", "all"}

    def same(left: str, right: str) -> bool:
        left_key = normalize_name(left)
        right_key = normalize_name(right)
        return bool(
            left_key
            and right_key
            and (left_key == right_key or left_key in right_key or right_key in left_key)
        )

    layer_purpose = (layer.purpose or ALL_PURPOSES).strip()
    request_is_field = normalize_allotment_type(request.allotment_type) == FIELD
    if layer_purpose == LPH_FIELD_LAYER:
        if purpose_family(request.purpose) != purpose_family(layer_purpose) or not request_is_field:
            return False
    elif layer_purpose in {LPH_HOUSEHOLD_LAYER, "ЛПХ"}:
        if purpose_family(request.purpose) != purpose_family(layer_purpose) or request_is_field:
            return False
    elif layer_purpose != ALL_PURPOSES and purpose_family(layer_purpose) != purpose_family(
        request.purpose
    ):
        return False
    if not same(layer.region, request.region):
        return False
    if request.district == ALL_DISTRICTS:
        return True
    if not wildcard(layer.district) and not same(layer.district, request.district):
        return False
    if wildcard(layer.locality):
        return True
    if not request.locality and same(layer.locality, request.district):
        return True
    layer_locality = normalize_name(layer.locality)
    return not layer_locality or same(layer.locality, request.locality or "")


def _utm_crs(longitude: float, latitude: float) -> CRS:
    zone = max(1, min(60, int(math.floor((longitude + 180) / 6)) + 1))
    return CRS.from_epsg((32600 if latitude >= 0 else 32700) + zone)


def _document_label(layer: UrbanPlanLayer) -> str:
    date_label = layer.approval_date.isoformat() if layer.approval_date else "дата не указана"
    return f"{layer.title}; {layer.approval_document}; {date_label}"


def _coverage_scope(request: SearchRequest) -> tuple[str, str, str, str]:
    return (
        request.region,
        request.district,
        request.locality or "",
        purpose_family(request.purpose) or ALL_PURPOSES,
    )


def _name_tokens(value: str | None) -> set[str]:
    tokens = set(normalize_name(value or "").split())
    return tokens - {"г", "город", "ауданы", "облысы", "обласы"}


def _source_matches_request(source: UrbanPlanSource, request: SearchRequest) -> bool:
    source_tokens = _name_tokens(source.locality or source.district or source.region)
    if not source_tokens:
        return False
    request_tokens: set[str] = set()
    for value in (
        request.locality,
        request.locality_label,
        request.district,
        request.district_label,
        request.region,
        request.region_label,
    ):
        request_tokens |= _name_tokens(value)
    if not request_tokens:
        return False
    return bool(source_tokens & request_tokens)


def _source_hint_for_request(
    session: Session,
    request: SearchRequest,
) -> UrbanPlanSourceHint | None:
    sources = session.scalars(
        select(UrbanPlanSource).where(
            UrbanPlanSource.coverage_status == "digital_found",
            UrbanPlanSource.import_status != "imported",
        )
    ).all()
    source = next((item for item in sources if _source_matches_request(item, request)), None)
    if source is None:
        return None
    message = (
        "Официальный цифровой генплан/ПДП найден в АИС ГГК, но слой еще не "
        "прошел импорт, сопоставление зон и независимое QA. Автоматическая "
        "проверка генплана пока недоступна, результат нужно сверить вручную."
    )
    if source.title:
        message += f" Источник: {source.title}."
    return UrbanPlanSourceHint(message=message, source_url=source.source_url or None)


def _upsert_coverage(
    session: Session,
    request: SearchRequest,
    *,
    status: str,
    approved_layer_count: int,
    message: str,
) -> UrbanPlanCoverage:
    region, district, locality, purpose = _coverage_scope(request)
    coverage = session.scalar(
        select(UrbanPlanCoverage).where(
            UrbanPlanCoverage.region == region,
            UrbanPlanCoverage.district == district,
            UrbanPlanCoverage.locality == locality,
            UrbanPlanCoverage.purpose == purpose,
        )
    )
    if coverage is None:
        coverage = UrbanPlanCoverage(
            region=region,
            district=district,
            locality=locality,
            purpose=purpose,
            coverage_status=status,
            approved_layer_count=approved_layer_count,
            message=message,
        )
        session.add(coverage)
        session.flush()
    else:
        coverage.coverage_status = status
        coverage.approved_layer_count = approved_layer_count
        coverage.message = message
        coverage.checked_at = datetime.now(UTC)
    return coverage


def _cached_unavailable_coverage(
    session: Session,
    request: SearchRequest,
) -> UrbanPlanCoverage | None:
    region, district, locality, purpose = _coverage_scope(request)
    return session.scalar(
        select(UrbanPlanCoverage).where(
            UrbanPlanCoverage.region == region,
            UrbanPlanCoverage.district == district,
            UrbanPlanCoverage.locality == locality,
            UrbanPlanCoverage.purpose == purpose,
            UrbanPlanCoverage.coverage_status == "unavailable",
        )
    )


def allowed_search_area_geojsons(session: Session, request: SearchRequest) -> list[dict[str, Any]]:
    if settings.urban_plan_check_mode.lower() == "off":
        return []
    result: list[dict[str, Any]] = []
    for layer in session.scalars(
        select(UrbanPlanLayer).where(
            UrbanPlanLayer.active.is_(True),
            UrbanPlanLayer.approved_for_search.is_(True),
            UrbanPlanLayer.provenance_status == "verified_official",
            UrbanPlanLayer.identity_status == "matched",
            UrbanPlanLayer.qa_status.in_(["STRICT", "VERIFIED_STRICT"]),
            UrbanPlanLayer.independent_review.is_(True),
            UrbanPlanLayer.source_sha256.is_not(None),
            UrbanPlanLayer.layer_kind == "allowed",
        )
    ).all():
        if not _matches_scope(layer, request):
            continue
        try:
            geometry = shape(json.loads(layer.geometry_geojson))
        except Exception:
            logger.exception("Invalid urban plan layer %s", layer.id)
            continue
        if not geometry.is_empty:
            result.append(mapping(geometry))
    return result


def evaluate_urban_plan(
    session: Session,
    request: SearchRequest,
    candidates: list[Any],
) -> UrbanPlanEvaluation:
    if settings.urban_plan_check_mode.lower() == "off":
        return UrbanPlanEvaluation(
            coverage_available=True,
            message="Проверка генплана/ПДП отключена настройкой сервера.",
            decisions=[
                UrbanPlanDecision(status="passed", message="Проверка отключена.")
                for _ in candidates
            ],
        )

    if settings.urban_plan_auto_waive_unavailable:
        cached = _cached_unavailable_coverage(session, request)
        if cached is not None:
            source_hint = _source_hint_for_request(session, request)
            message = cached.message or (
                "Для выбранной территории в системе нет активного официального "
                "геопривязанного слоя генплана/ПДП."
            )
            if source_hint is not None and "Официальный цифровой генплан" not in message:
                message = source_hint.message
                cached.message = message
                session.flush()
            return UrbanPlanEvaluation(
                coverage_available=False,
                message=message,
                decisions=[
                    UrbanPlanDecision(
                        status="unavailable",
                        message=message,
                        source_url=source_hint.source_url if source_hint else None,
                    )
                    for _ in candidates
                ],
                coverage_id=cached.id,
                coverage_status=cached.coverage_status,
            )

    layers = [
        row
        for row in session.scalars(
            select(UrbanPlanLayer).where(
                UrbanPlanLayer.active.is_(True),
                UrbanPlanLayer.approved_for_search.is_(True),
                UrbanPlanLayer.provenance_status == "verified_official",
                UrbanPlanLayer.identity_status == "matched",
                UrbanPlanLayer.qa_status.in_(["STRICT", "VERIFIED_STRICT"]),
                UrbanPlanLayer.independent_review.is_(True),
                UrbanPlanLayer.source_sha256.is_not(None),
            )
        ).all()
        if _matches_scope(row, request)
    ]
    allowed_layers = [row for row in layers if row.layer_kind == "allowed"]
    if not allowed_layers:
        source_hint = _source_hint_for_request(session, request)
        if source_hint is None:
            message = (
                "Для выбранного населенного пункта в системе нет активного официального "
                "геопривязанного слоя разрешенной территории генплана/ПДП. Координаты не "
                "выдаются, оплата не запрашивается."
            )
        else:
            message = source_hint.message
        coverage = _upsert_coverage(
            session,
            request,
            status="unavailable",
            approved_layer_count=0,
            message=message,
        )
        return UrbanPlanEvaluation(
            coverage_available=False,
            message=message,
            decisions=[
                UrbanPlanDecision(
                    status="unavailable",
                    message=message,
                    source_url=source_hint.source_url if source_hint else None,
                )
                for _ in candidates
            ],
            coverage_id=coverage.id,
            coverage_status=coverage.coverage_status,
        )

    parsed_layers: list[tuple[UrbanPlanLayer, BaseGeometry, PreparedGeometry]] = []
    for layer in layers:
        try:
            geometry = shape(json.loads(layer.geometry_geojson))
            parsed_layers.append((layer, geometry, prep(geometry)))
        except Exception:
            logger.exception("Invalid urban plan layer %s", layer.id)

    if not any(layer.layer_kind == "allowed" for layer, _, _ in parsed_layers):
        message = (
            "Активные градостроительные слои повреждены или имеют неверную систему "
            "координат. Координаты не выдаются, оплата не запрашивается."
        )
        coverage = _upsert_coverage(
            session,
            request,
            status="broken",
            approved_layer_count=len(allowed_layers),
            message=message,
        )
        return UrbanPlanEvaluation(
            coverage_available=False,
            message=message,
            decisions=[
                UrbanPlanDecision(status="unavailable", message=message) for _ in candidates
            ],
            coverage_id=coverage.id,
            coverage_status=coverage.coverage_status,
        )

    if candidates:
        allowed_layer_rows = [
            (layer, prepared_geometry)
            for layer, _, prepared_geometry in parsed_layers
            if layer.layer_kind == "allowed"
        ]
        candidate_points = [
            Point(candidate.longitude, candidate.latitude) for candidate in candidates
        ]
        touches_allowed_layer = any(
            prepared_geometry.covers(point)
            for _, prepared_geometry in allowed_layer_rows
            for point in candidate_points
        )
        if not touches_allowed_layer:
            source = allowed_layers[0]
            message = (
                "Для выбранного района цифровой слой генплана/ПДП не покрывает найденные "
                "по кадастровой карте места. Поэтому система не считает это запретом "
                "генплана и выдает только предварительный результат с обязательной ручной "
                "сверкой."
            )
            coverage = _upsert_coverage(
                session,
                request,
                status="unavailable",
                approved_layer_count=0,
                message=message,
            )
            return UrbanPlanEvaluation(
                coverage_available=False,
                message=message,
                decisions=[
                    UrbanPlanDecision(
                        status="unavailable",
                        message=message,
                        source_url=source.source_url,
                    )
                    for _ in candidates
                ],
                coverage_id=coverage.id,
                coverage_status=coverage.coverage_status,
            )

    decisions: list[UrbanPlanDecision] = []
    for candidate in candidates:
        local_crs = _utm_crs(candidate.longitude, candidate.latitude)
        center_transformer = Transformer.from_crs(4326, local_crs, always_xy=True)
        wgs84_transformer = Transformer.from_crs(local_crs, 4326, always_xy=True)
        center_x, center_y = center_transformer.transform(candidate.longitude, candidate.latitude)
        side_m = math.sqrt(float(request.area_ha) * 10_000)
        half = side_m / 2
        metric_plot = box(
            center_x - half,
            center_y - half,
            center_x + half,
            center_y + half,
        )
        plot = transform(wgs84_transformer.transform, metric_plot)
        red_line_test_area = transform(
            wgs84_transformer.transform,
            metric_plot.buffer(settings.urban_plan_red_line_buffer_m),
        )

        covering = [
            layer
            for layer, _, prepared_geometry in parsed_layers
            if layer.layer_kind == "allowed" and prepared_geometry.covers(plot)
        ]
        if not covering:
            decisions.append(
                UrbanPlanDecision(
                    status="blocked",
                    message=(
                        f"Квадрат {request.area_ha * 100:.0f} соток не помещается целиком "
                        "в загруженную разрешенную территорию официального генплана/ПДП."
                    ),
                )
            )
            continue

        blocker: UrbanPlanLayer | None = None
        for layer, _, prepared_geometry in parsed_layers:
            if layer.layer_kind == "prohibited" and prepared_geometry.intersects(plot):
                blocker = layer
                break
            if layer.layer_kind == "red_line" and prepared_geometry.intersects(red_line_test_area):
                blocker = layer
                break
        if blocker is not None:
            decisions.append(
                UrbanPlanDecision(
                    status="blocked",
                    message=f"Квадрат пересекает исключающий слой: {blocker.title}.",
                    zone=blocker.zone_name,
                    document=_document_label(blocker),
                    source_url=blocker.source_url,
                )
            )
            continue

        source = covering[0]
        zone = ", ".join(dict.fromkeys(row.zone_name or row.title for row in covering))
        decisions.append(
            UrbanPlanDecision(
                status="passed",
                message=(
                    "Квадрат полностью находится в загруженной разрешенной территории "
                    "генплана/ПДП и не пересекает загруженные красные линии и запретные зоны."
                ),
                zone=zone,
                document=_document_label(source),
                source_url=source.source_url,
            )
        )

    passed = sum(item.status == "passed" for item in decisions)
    coverage = _upsert_coverage(
        session,
        request,
        status="available",
        approved_layer_count=len(allowed_layers),
        message=f"Найдено пригодных официальных разрешающих слоев: {len(allowed_layers)}.",
    )
    return UrbanPlanEvaluation(
        coverage_available=True,
        message=(
            f"Проверка официальных градостроительных слоев завершена: прошло {passed} "
            f"из {len(decisions)} кандидатов."
        ),
        decisions=decisions,
        coverage_id=coverage.id,
        coverage_status=coverage.coverage_status,
    )
