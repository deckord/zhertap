import json
from dataclasses import dataclass
from math import ceil, sqrt
from typing import Any
from urllib.parse import urlencode

from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.map_links import google_maps_place_url
from app.models import PlanningCandidateReview, PlanningCandidateStatus, UrbanPlanLayer
from app.planning_service import (
    PLANNING_REQUEST_TO_PURPOSE,
    PlanningScope,
    _is_search_layer,
    _matches_planning_scope,
)
from app.providers.egkn import (
    DistrictInfo,
    EgknProvider,
    EgknProviderError,
    ParcelRecord,
    SettlementInfo,
)
from app.purposes import GARDENING, LPH, parcel_matches_purpose

GOOD_ORIENTATION_DISTANCE_M = 300
MAX_ORIENTATION_DISTANCE_M = 800
REVIEW_EXCLUSION_RADIUS_M = 70
REVIEW_EMPTY_REUSE_RADIUS_M = 45
BAD_REVIEW_STATUSES = {
    PlanningCandidateStatus.built.value,
    PlanningCandidateStatus.road.value,
    PlanningCandidateStatus.garden.value,
    PlanningCandidateStatus.unclear.value,
}
GOOD_REVIEW_STATUSES = {PlanningCandidateStatus.empty.value}


@dataclass(frozen=True, slots=True)
class PlanningCandidatePoint:
    rank: int
    latitude: float
    longitude: float
    google_maps_url: str
    distance_to_restriction_m: float | None
    trust_level: str
    genplan_check_url: str = ""
    nearby_cadastre: str | None = None
    nearby_distance_m: float | None = None
    nearby_land_use: str | None = None
    candidate_area_ha: float | None = None
    selection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EgknPlanningContext:
    parcels: list[ParcelRecord]
    parcel_geometries_m: list[BaseGeometry]
    anchor_parcels: list[ParcelRecord]
    anchor_geometries_m: list[BaseGeometry]
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningReviewFeedback:
    reviewed_points: int
    confirmed_empty_points: int
    excluded_bad_points: int
    positive_points_m: list[tuple[PlanningCandidateReview, Point]]
    bad_points_m: list[Point]
    blocked_area_m: BaseGeometry


def find_planning_candidate_points(
    session: Session,
    *,
    scope: PlanningScope,
    include_shadow: bool = True,
    limit: int = 25,
    grid_step_m: int = 90,
    restriction_buffer_m: int = 20,
    use_egkn_context: bool = False,
    target_area_ha: float = 0.10,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    grid_step_m = max(30, min(grid_step_m, 500))
    restriction_buffer_m = max(0, min(restriction_buffer_m, 200))
    target_area_ha = max(0.02, min(target_area_ha, 0.50))
    layers = _candidate_layers(session, scope=scope, include_shadow=include_shadow)
    allowed_layers = [row for row in layers if row.layer_kind == "allowed"]
    restriction_layers = [
        row for row in layers if row.layer_kind in {"prohibited", "red_line"}
    ]
    if not allowed_layers:
        return _empty_result(
            layers=layers,
            allowed_layers=allowed_layers,
            restriction_layers=restriction_layers,
            message="Разрешенная зона для такого запроса не найдена.",
        )

    allowed_wgs84 = _safe_union(allowed_layers)
    if allowed_wgs84.is_empty:
        return _empty_result(
            layers=layers,
            allowed_layers=allowed_layers,
            restriction_layers=restriction_layers,
            message="Разрешенная зона пустая или повреждена.",
        )

    to_meters, to_wgs84 = _local_transformers(allowed_wgs84.centroid)
    allowed_m = make_valid(transform(to_meters.transform, allowed_wgs84))
    search_area_m = allowed_m
    restriction_m = Point().buffer(0)
    if restriction_layers:
        restriction_wgs84 = _safe_union(restriction_layers)
        if not restriction_wgs84.is_empty:
            restriction_m = make_valid(transform(to_meters.transform, restriction_wgs84))
            search_area_m = make_valid(
                search_area_m.difference(restriction_m.buffer(restriction_buffer_m))
            )
    if search_area_m.is_empty:
        return _empty_result(
            layers=layers,
            allowed_layers=allowed_layers,
            restriction_layers=restriction_layers,
            message="После вычитания ограничений свободная зона не осталась.",
        )

    egkn_context = (
        _load_egkn_context(scope=scope, to_meters=to_meters)
        if use_egkn_context
        else EgknPlanningContext([], [], [], [], None)
    )
    vacancy_area_m = search_area_m
    if egkn_context.parcel_geometries_m:
        occupied_m = make_valid(unary_union(egkn_context.parcel_geometries_m)).buffer(1.0)
        vacancy_area_m = make_valid(search_area_m.difference(occupied_m))
    if vacancy_area_m.is_empty:
        return _empty_result(
            layers=layers,
            allowed_layers=allowed_layers,
            restriction_layers=restriction_layers,
            message="В разрешенной зоне ЕГКН не оставил свободного пятна нужного размера.",
        )

    review_feedback = _load_review_feedback(session, scope=scope, to_meters=to_meters)
    if not review_feedback.blocked_area_m.is_empty:
        vacancy_area_m = make_valid(vacancy_area_m.difference(review_feedback.blocked_area_m))
    if vacancy_area_m.is_empty:
        return _empty_result(
            layers=layers,
            allowed_layers=allowed_layers,
            restriction_layers=restriction_layers,
            message="После учета ручной проверки Google подходящих пустых мест не осталось.",
            review_feedback=review_feedback,
        )

    review_seed_rows, reserved_review_points = _review_seed_points(
        review_feedback,
        vacancy_area_m=vacancy_area_m,
        restriction_m=restriction_m,
        to_wgs84=to_wgs84,
        limit=limit,
    )
    raw_points = _ranked_vacancy_points(
        vacancy_area_m,
        restriction_m=restriction_m,
        egkn_context=egkn_context,
        search_area_m=search_area_m,
        to_wgs84=to_wgs84,
        limit=max(0, limit - len(review_seed_rows)),
        grid_step_m=grid_step_m,
        target_area_ha=target_area_ha,
        reserved_points_m=reserved_review_points,
    )
    raw_points = [*review_seed_rows, *raw_points][:limit]
    trust_level = "SEARCH" if all(_is_search_layer(row) for row in allowed_layers) else "SHADOW"
    points = [
        PlanningCandidatePoint(
            rank=index,
            latitude=row["latitude"],
            longitude=row["longitude"],
            google_maps_url=_google_maps_url(row["latitude"], row["longitude"]),
            genplan_check_url=_genplan_check_url(
                scope,
                latitude=row["latitude"],
                longitude=row["longitude"],
                include_shadow=include_shadow,
            ),
            distance_to_restriction_m=row["distance_to_restriction_m"],
            trust_level=trust_level,
            nearby_cadastre=row["nearby_cadastre"],
            nearby_distance_m=row["nearby_distance_m"],
            nearby_land_use=row["nearby_land_use"],
            candidate_area_ha=row["candidate_area_ha"],
            selection_reason=row["selection_reason"],
        )
        for index, row in enumerate(raw_points, start=1)
    ]
    return {
        "points": points,
        "message": None if points else "Подходящие точки по выбранной сетке не найдены.",
        "trust_level": trust_level,
        "allowed_layer_count": len(allowed_layers),
        "restriction_layer_count": len(restriction_layers),
        "search_layer_count": sum(1 for row in layers if _is_search_layer(row)),
        "shadow_layer_count": sum(1 for row in layers if not _is_search_layer(row)),
        "allowed_area_ha": round(allowed_m.area / 10_000, 2),
        "candidate_area_ha": round(vacancy_area_m.area / 10_000, 2),
        "target_area_ha": round(target_area_ha, 2),
        "grid_step_m": grid_step_m,
        "restriction_buffer_m": restriction_buffer_m,
        "egkn_enabled": use_egkn_context,
        "egkn_parcel_count": len(egkn_context.parcels),
        "egkn_anchor_count": len(egkn_context.anchor_parcels),
        "egkn_message": egkn_context.message,
        "review_feedback": _review_feedback_payload(
            review_feedback,
            reused_empty_points=len(review_seed_rows),
        ),
        "requested_use": scope.requested_use,
    }


def _empty_result(
    *,
    layers: list[UrbanPlanLayer],
    allowed_layers: list[UrbanPlanLayer],
    restriction_layers: list[UrbanPlanLayer],
    message: str,
    review_feedback: PlanningReviewFeedback | None = None,
) -> dict[str, Any]:
    return {
        "points": [],
        "message": message,
        "trust_level": "NONE",
        "allowed_layer_count": len(allowed_layers),
        "restriction_layer_count": len(restriction_layers),
        "search_layer_count": sum(1 for row in layers if _is_search_layer(row)),
        "shadow_layer_count": sum(1 for row in layers if not _is_search_layer(row)),
        "allowed_area_ha": 0.0,
        "candidate_area_ha": 0.0,
        "review_feedback": _review_feedback_payload(review_feedback, reused_empty_points=0),
    }


def _candidate_layers(
    session: Session,
    *,
    scope: PlanningScope,
    include_shadow: bool,
) -> list[UrbanPlanLayer]:
    statement = select(UrbanPlanLayer).where(
        UrbanPlanLayer.layer_kind.in_(("allowed", "prohibited", "red_line"))
    )
    if scope.region:
        statement = statement.where(UrbanPlanLayer.region == scope.region)
    if scope.district:
        statement = statement.where(UrbanPlanLayer.district == scope.district)
    if scope.locality:
        statement = statement.where(UrbanPlanLayer.locality == scope.locality)
    purpose = PLANNING_REQUEST_TO_PURPOSE.get(
        (scope.requested_use or "").upper(),
        scope.requested_use,
    )
    if purpose:
        statement = statement.where(UrbanPlanLayer.purpose.in_((purpose, "all")))
    rows = session.scalars(statement).all()
    return [
        row
        for row in rows
        if _matches_planning_scope(row, scope)
        and (include_shadow or _is_search_layer(row))
    ]


def _load_review_feedback(
    session: Session,
    *,
    scope: PlanningScope,
    to_meters: Transformer,
) -> PlanningReviewFeedback:
    statement = select(PlanningCandidateReview).where(
        PlanningCandidateReview.region == (scope.region or ""),
        PlanningCandidateReview.district == (scope.district or ""),
        PlanningCandidateReview.locality == (scope.locality or ""),
        PlanningCandidateReview.requested_use == (scope.requested_use or ""),
    )
    reviews = session.scalars(statement).all()
    positive_points_m: list[tuple[PlanningCandidateReview, Point]] = []
    bad_points_m: list[Point] = []
    for review in reviews:
        point_m = transform(
            to_meters.transform,
            Point(review.longitude, review.latitude),
        )
        if review.status in GOOD_REVIEW_STATUSES:
            positive_points_m.append((review, point_m))
        elif review.status in BAD_REVIEW_STATUSES:
            bad_points_m.append(point_m)
    blocked_area_m = (
        make_valid(unary_union([point.buffer(REVIEW_EXCLUSION_RADIUS_M) for point in bad_points_m]))
        if bad_points_m
        else Point().buffer(0)
    )
    return PlanningReviewFeedback(
        reviewed_points=sum(
            1
            for review in reviews
            if review.status != PlanningCandidateStatus.queued.value
        ),
        confirmed_empty_points=len(positive_points_m),
        excluded_bad_points=len(bad_points_m),
        positive_points_m=positive_points_m,
        bad_points_m=bad_points_m,
        blocked_area_m=blocked_area_m,
    )


def _review_seed_points(
    feedback: PlanningReviewFeedback,
    *,
    vacancy_area_m: BaseGeometry,
    restriction_m: BaseGeometry,
    to_wgs84: Transformer,
    limit: int,
) -> tuple[list[dict[str, Any]], list[Point]]:
    selected: list[tuple[PlanningCandidateReview, Point]] = []
    for review, point_m in feedback.positive_points_m:
        if not vacancy_area_m.covers(point_m):
            continue
        if any(
            point_m.distance(existing_point) < REVIEW_EMPTY_REUSE_RADIUS_M
            for _, existing_point in selected
        ):
            continue
        selected.append((review, point_m))
        if len(selected) >= limit:
            break

    rows: list[dict[str, Any]] = []
    for review, point_m in selected:
        lon, lat = to_wgs84.transform(point_m.x, point_m.y)
        restriction_distance = None
        if not restriction_m.is_empty:
            restriction_distance = round(max(0.0, point_m.distance(restriction_m)), 1)
        reason = "Уже проверено по Google: пусто."
        if review.selection_reason:
            reason = f"{reason} {review.selection_reason}"
        rows.append(
            {
                "latitude": round(lat, 6),
                "longitude": round(lon, 6),
                "distance_to_restriction_m": restriction_distance,
                "nearby_cadastre": review.nearby_cadastre,
                "nearby_distance_m": review.nearby_distance_m,
                "nearby_land_use": review.nearby_land_use,
                "candidate_area_ha": review.candidate_area_ha,
                "selection_reason": reason,
            }
        )
    return rows, [point_m for _, point_m in selected]


def _review_feedback_payload(
    feedback: PlanningReviewFeedback | None,
    *,
    reused_empty_points: int,
) -> dict[str, int]:
    if feedback is None:
        return {
            "reviewed_points": 0,
            "confirmed_empty_points": 0,
            "excluded_bad_points": 0,
            "reused_empty_points": 0,
        }
    return {
        "reviewed_points": feedback.reviewed_points,
        "confirmed_empty_points": feedback.confirmed_empty_points,
        "excluded_bad_points": feedback.excluded_bad_points,
        "reused_empty_points": reused_empty_points,
    }


def _safe_union(layers: list[UrbanPlanLayer]):
    geometries = []
    for layer in layers:
        try:
            geometry = make_valid(shape(json.loads(layer.geometry_geojson)))
        except Exception:
            continue
        if not geometry.is_empty:
            geometries.append(geometry)
    if not geometries:
        return Point().buffer(0)
    return make_valid(unary_union(geometries))


def _local_transformers(center: Point) -> tuple[Transformer, Transformer]:
    local_crs = CRS.from_proj4(
        f"+proj=aeqd +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m +no_defs"
    )
    to_meters = Transformer.from_crs("EPSG:4326", local_crs, always_xy=True)
    to_wgs84 = Transformer.from_crs(local_crs, "EPSG:4326", always_xy=True)
    return to_meters, to_wgs84


def _ranked_vacancy_points(
    vacancy_area_m,
    *,
    restriction_m,
    egkn_context: EgknPlanningContext,
    search_area_m,
    to_wgs84: Transformer,
    limit: int,
    grid_step_m: int,
    target_area_ha: float,
    reserved_points_m: list[Point] | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    parcel_geometries = egkn_context.anchor_geometries_m or egkn_context.parcel_geometries_m
    parcel_records = egkn_context.anchor_parcels or egkn_context.parcels
    parcel_tree = STRtree(parcel_geometries) if parcel_geometries else None

    target_area_m2 = target_area_ha * 10_000
    side_m = sqrt(target_area_m2)
    half_side_m = side_m / 2
    clearance_m = sqrt(2) * half_side_m + 1.5
    candidates: list[tuple[float, Point, Polygon, ParcelRecord | None, float | None]] = []

    parts = sorted(_polygon_parts(vacancy_area_m), key=lambda part: part.area, reverse=True)
    for part in parts:
        if part.area < target_area_m2 * 0.65:
            continue
        inner = make_valid(part.buffer(-clearance_m))
        trial_areas = _polygon_parts(inner) or [part]
        for inner_part in trial_areas:
            for point in _trial_points(inner_part, grid_step_m=grid_step_m):
                plot = box(
                    point.x - half_side_m,
                    point.y - half_side_m,
                    point.x + half_side_m,
                    point.y + half_side_m,
                )
                if not part.covers(plot):
                    continue
                nearest, nearest_distance = _nearest_parcel(
                    plot,
                    parcel_tree=parcel_tree,
                    parcel_records=parcel_records,
                )
                score = _candidate_score(
                    part=part,
                    point=point,
                    nearest_distance=nearest_distance,
                    restriction_m=restriction_m,
                    has_egkn=bool(egkn_context.parcel_geometries_m),
                )
                candidates.append((score, point, part, nearest, nearest_distance))

    if not candidates:
        return _plain_grid_points(
            search_area_m,
            restriction_m=restriction_m,
            to_wgs84=to_wgs84,
            limit=limit,
            grid_step_m=grid_step_m,
            message=egkn_context.message,
            reserved_points_m=reserved_points_m,
        )

    candidates.sort(key=lambda row: row[0], reverse=True)
    selected: list[tuple[Point, Polygon, ParcelRecord | None, float | None]] = []
    reserved_points = reserved_points_m or []
    min_spacing = max(side_m * 1.8, grid_step_m * 0.75, 50)
    for _, point, part, nearest, nearest_distance in candidates:
        if any(point.distance(existing_point) < min_spacing for existing_point in reserved_points):
            continue
        if any(point.distance(existing_point) < min_spacing for existing_point, *_ in selected):
            continue
        selected.append((point, part, nearest, nearest_distance))
        if len(selected) >= limit:
            break

    result: list[dict[str, Any]] = []
    for point, part, nearest, nearest_distance in selected:
        lon, lat = to_wgs84.transform(point.x, point.y)
        restriction_distance = None
        if not restriction_m.is_empty:
            restriction_distance = round(max(0.0, point.distance(restriction_m)), 1)
        usable_nearest = (
            nearest
            if nearest is not None
            and nearest_distance is not None
            and nearest_distance <= MAX_ORIENTATION_DISTANCE_M
            else None
        )
        usable_distance = nearest_distance if usable_nearest is not None else None
        result.append(
            {
                "latitude": round(lat, 6),
                "longitude": round(lon, 6),
                "distance_to_restriction_m": restriction_distance,
                "nearby_cadastre": usable_nearest.cadastre if usable_nearest else None,
                "nearby_distance_m": (
                    round(usable_distance, 1) if usable_distance is not None else None
                ),
                "nearby_land_use": usable_nearest.land_use if usable_nearest else None,
                "candidate_area_ha": round(part.area / 10_000, 2),
                "selection_reason": _selection_reason(nearest, nearest_distance),
            }
        )
    return result


def _plain_grid_points(
    search_area_m,
    *,
    restriction_m,
    to_wgs84: Transformer,
    limit: int,
    grid_step_m: int,
    message: str | None,
    reserved_points_m: list[Point] | None = None,
) -> list[dict[str, Any]]:
    min_x, min_y, max_x, max_y = search_area_m.bounds
    prepared_area = prep(search_area_m)
    rows = max(1, ceil((max_y - min_y) / grid_step_m))
    cols = max(1, ceil((max_x - min_x) / grid_step_m))
    selected: list[Point] = []
    reserved_points = reserved_points_m or []
    min_spacing = max(30, grid_step_m * 0.75)
    for row in range(rows):
        y = min_y + (row + 0.5) * grid_step_m
        x_range = range(cols) if row % 2 == 0 else range(cols - 1, -1, -1)
        for col in x_range:
            x = min_x + (col + 0.5) * grid_step_m
            point = Point(x, y)
            if not prepared_area.covers(point):
                continue
            if any(point.distance(existing) < min_spacing for existing in reserved_points):
                continue
            if any(point.distance(existing) < min_spacing for existing in selected):
                continue
            selected.append(point)
            if len(selected) >= limit:
                break
        if len(selected) >= limit:
            break
    if not selected:
        representative = search_area_m.representative_point()
        selected.append(representative)

    result: list[dict[str, Any]] = []
    for point in selected:
        lon, lat = to_wgs84.transform(point.x, point.y)
        restriction_distance = None
        if not restriction_m.is_empty:
            restriction_distance = round(max(0.0, point.distance(restriction_m)), 1)
        reason = "Точка внутри разрешенной зоны генплана."
        if message:
            reason += f" ЕГКН не использован: {message}"
        result.append(
            {
                "latitude": round(lat, 6),
                "longitude": round(lon, 6),
                "distance_to_restriction_m": restriction_distance,
                "nearby_cadastre": None,
                "nearby_distance_m": None,
                "nearby_land_use": None,
                "candidate_area_ha": round(search_area_m.area / 10_000, 2),
                "selection_reason": reason,
            }
        )
    return result


def _trial_points(area: Polygon, *, grid_step_m: int) -> list[Point]:
    points = [area.representative_point()]
    min_x, min_y, max_x, max_y = area.bounds
    x = min_x
    checked = 0
    while x <= max_x and checked < 3500:
        y = min_y
        while y <= max_y and checked < 3500:
            point = Point(x, y)
            if area.covers(point):
                points.append(point)
            y += grid_step_m
            checked += 1
        x += grid_step_m
    return points


def _nearest_parcel(
    geometry: BaseGeometry,
    *,
    parcel_tree: STRtree | None,
    parcel_records: list[ParcelRecord],
) -> tuple[ParcelRecord | None, float | None]:
    if parcel_tree is None or not parcel_records:
        return None, None
    nearest_index = int(parcel_tree.nearest(geometry))
    nearest = parcel_records[nearest_index]
    nearest_geometry = parcel_tree.geometries.take(nearest_index)
    return nearest, geometry.distance(nearest_geometry)


def _candidate_score(
    *,
    part: Polygon,
    point: Point,
    nearest_distance: float | None,
    restriction_m,
    has_egkn: bool,
) -> float:
    score = min(45.0, part.area / 450)
    if has_egkn:
        if nearest_distance is None:
            score -= 20
        elif nearest_distance <= 12:
            score += 4
        elif nearest_distance <= 80:
            score += 35
        elif nearest_distance <= 220:
            score += 22
        elif nearest_distance <= GOOD_ORIENTATION_DISTANCE_M:
            score += 14
        elif nearest_distance <= MAX_ORIENTATION_DISTANCE_M:
            score += 8
        elif nearest_distance <= 1500:
            score -= 35
        else:
            score -= 70
    if not restriction_m.is_empty:
        score += min(18.0, point.distance(restriction_m) / 15)
    return score


def _selection_reason(
    nearest: ParcelRecord | None,
    nearest_distance: float | None,
) -> str:
    if nearest is None or nearest_distance is None:
        return "Пустое пятно внутри разрешенной зоны генплана; кадастровый ориентир не найден."
    if nearest_distance > MAX_ORIENTATION_DISTANCE_M:
        return (
            "Пустое пятно внутри разрешенной зоны генплана; ближайший кадастровый "
            f"участок {nearest.cadastre} слишком далеко, примерно {nearest_distance:.0f} м, "
            "поэтому как ориентир рядом не используется."
        )
    quality = (
        "хороший ориентир"
        if nearest_distance <= GOOD_ORIENTATION_DISTANCE_M
        else "слабый ориентир"
    )
    land_use = f", назначение: {nearest.land_use}" if nearest.land_use else ""
    return (
        "Пустое пятно внутри разрешенной зоны генплана; "
        f"{quality}: {nearest.cadastre} примерно в "
        f"{nearest_distance:.0f} м{land_use}."
    )


def _load_egkn_context(
    *,
    scope: PlanningScope,
    to_meters: Transformer,
) -> EgknPlanningContext:
    if not scope.region or not scope.district:
        return EgknPlanningContext([], [], [], [], "не указаны область или район")
    egkn = EgknProvider()
    try:
        district = egkn.find_district(scope.region, scope.district)
        area = _egkn_search_area(egkn, district, scope.locality)
        parcels = egkn.parcels(district, area)
    except EgknProviderError as exc:
        return EgknPlanningContext([], [], [], [], str(exc))
    except Exception as exc:
        return EgknPlanningContext([], [], [], [], f"ошибка ЕГКН: {exc}")

    parcel_geometries_m: list[BaseGeometry] = []
    anchor_parcels: list[ParcelRecord] = []
    anchor_geometries_m: list[BaseGeometry] = []
    to_wgs84 = Transformer.from_crs(f"EPSG:{district.srs}", "EPSG:4326", always_xy=True)
    requested_purpose = _requested_use_to_purpose(scope.requested_use)
    for parcel in parcels:
        try:
            geometry_wgs84 = make_valid(transform(to_wgs84.transform, parcel.geometry))
            geometry_m = make_valid(transform(to_meters.transform, geometry_wgs84))
        except Exception:
            continue
        if geometry_m.is_empty:
            continue
        parcel_geometries_m.append(geometry_m)
        if parcel_matches_purpose(parcel.land_use, requested_purpose):
            anchor_parcels.append(parcel)
            anchor_geometries_m.append(geometry_m)
    message = None
    if not parcels:
        message = "зарегистрированные участки в выбранной зоне не найдены"
    elif not anchor_parcels:
        message = (
            "участки такого назначения рядом не найдены; "
            "ориентиром служит ближайший кадастровый участок"
        )
    return EgknPlanningContext(
        parcels=parcels,
        parcel_geometries_m=parcel_geometries_m,
        anchor_parcels=anchor_parcels,
        anchor_geometries_m=anchor_geometries_m,
        message=message,
    )


def _egkn_search_area(
    egkn: EgknProvider,
    district: DistrictInfo,
    locality: str | None,
) -> SettlementInfo:
    if locality:
        try:
            return egkn.find_settlement(district.id, locality)
        except EgknProviderError:
            pass
    return egkn.district_search_area(district)


def _requested_use_to_purpose(requested_use: str | None) -> str:
    return GARDENING if (requested_use or "").upper() == "GARDENING" else LPH


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        result: list[Polygon] = []
        for part in geometry.geoms:
            result.extend(_polygon_parts(part))
        return result
    return []


def _google_maps_url(latitude: float, longitude: float) -> str:
    return google_maps_place_url(latitude, longitude)


def _genplan_check_url(
    scope: PlanningScope,
    *,
    latitude: float,
    longitude: float,
    include_shadow: bool,
) -> str:
    params = {
        "planning_probe": "1",
        "planning_region": scope.region or "",
        "planning_district": scope.district or "",
        "planning_locality": scope.locality or "",
        "planning_use": scope.requested_use or "",
        "planning_lat": f"{latitude:.6f}",
        "planning_lon": f"{longitude:.6f}",
        "planning_shadow": "1" if include_shadow else "0",
    }
    return f"/admin/urban-plans?{urlencode(params)}#planning-check"
