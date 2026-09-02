import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as transform_geometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

from app.config import settings
from app.legal_rules import legal_restriction_reason
from app.provider_guard import ProviderCallDeferred
from app.providers.egkn import (
    DistrictInfo,
    EgknProvider,
    EgknProviderError,
    ParcelRecord,
    SettlementInfo,
)
from app.providers.osm import OsmProvider, OsmProviderError, Surroundings
from app.purposes import (
    GARDENING,
    LPH_NEW,
    normalize_purpose,
    parcel_matches_purpose,
)
from app.schemas import ALL_DISTRICTS, SearchCreate
from app.search_types import CandidateResult

logger = logging.getLogger(__name__)
LAND_OSM_TIME_BUDGET_SECONDS = 45
LAND_OSM_DEADLINE_RESERVE_SECONDS = 20


@dataclass(slots=True)
class DraftCandidate:
    point: Point
    plot: Polygon
    nearest: ParcelRecord
    nearest_distance_m: float
    anchor_density: int
    score: float
    planning_first: bool = False
    latitude: float = 0
    longitude: float = 0


def polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        result: list[Polygon] = []
        for part in geometry.geoms:
            result.extend(polygon_parts(part))
        return result
    return []


def district_search_tiles(
    area: SettlementInfo,
    *,
    tile_size_m: float = 2500,
) -> list[SettlementInfo]:
    minx, miny, maxx, maxy = area.geometry.bounds
    tiles: list[SettlementInfo] = []
    row = 0
    y = miny
    while y < maxy:
        column = 0
        x = minx
        while x < maxx:
            geometry = area.geometry.intersection(
                box(x, y, min(x + tile_size_m, maxx), min(y + tile_size_m, maxy))
            )
            if not geometry.is_empty and geometry.area >= 1000:
                tiles.append(
                    SettlementInfo(
                        gid=f"{area.gid}:{row}:{column}",
                        name=area.name,
                        kato=area.kato,
                        district_id=area.district_id,
                        geometry=geometry,
                    )
                )
            x += tile_size_m
            column += 1
        y += tile_size_m
        row += 1
    return tiles


def is_lph(parcel: ParcelRecord) -> bool:
    return parcel_matches_purpose(parcel.land_use, "ЛПХ")


def is_gardening(parcel: ParcelRecord) -> bool:
    return parcel_matches_purpose(parcel.land_use, GARDENING)


class LiveSearchEngine:
    def __init__(
        self,
        egkn_provider: EgknProvider | None = None,
        osm_provider: OsmProvider | None = None,
    ) -> None:
        self.egkn = egkn_provider or EgknProvider()
        self.osm = osm_provider or OsmProvider()

    def search(
        self,
        query: SearchCreate,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[CandidateResult]:
        if progress_callback:
            progress_callback("boundaries")
        deadline = time.monotonic() + settings.live_search_time_budget_seconds
        if query.district == ALL_DISTRICTS:
            return self._search_all_districts(query, deadline, progress_callback)
        return self._search_district(
            query,
            deadline,
            progress_callback=progress_callback,
        )

    def _search_all_districts(
        self,
        query: SearchCreate,
        deadline: float,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[CandidateResult]:
        results: list[CandidateResult] = []
        for district in self.egkn.districts(query.region):
            if len(results) >= query.result_limit or time.monotonic() >= deadline:
                break
            if legal_restriction_reason(
                region=query.region,
                district=district.display_name,
                locality=None,
                purpose=query.purpose,
                language=query.language,
            ):
                continue
            district_query = query.model_copy(
                update={
                    "district": district.name,
                    "district_label": district.display_name,
                    "locality": None,
                    "locality_label": district.display_name,
                    "result_limit": 1,
                }
            )
            try:
                district_results = self._search_district(
                    district_query,
                    deadline,
                    district=district,
                    candidate_target=8,
                    progress_callback=progress_callback,
                )
            except (EgknProviderError, OsmProviderError):
                logger.warning(
                    "District-wide search skipped %s",
                    district.display_name,
                    exc_info=True,
                )
                continue
            if district_results:
                results.append(district_results[0])
        results.sort(key=lambda item: item.score, reverse=True)
        return results[: query.result_limit]

    def _search_district(
        self,
        query: SearchCreate,
        deadline: float,
        *,
        district: DistrictInfo | None = None,
        candidate_target: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[CandidateResult]:
        district = district or self.egkn.find_district(query.region, query.district)
        settlement = (
            self.egkn.find_settlement(district.id, query.locality)
            if query.locality
            else self.egkn.district_search_area(district)
        )
        settlement = self._restrict_to_allowed_urban_plan(query, district, settlement)
        areas = [settlement] if query.locality else district_search_tiles(settlement)
        drafts: list[DraftCandidate] = []
        searched_batches: list[tuple[SettlementInfo, list[ParcelRecord], list[ParcelRecord]]] = []
        parcel_count = 0
        anchor_count = 0
        target_count = candidate_target or max(
            (query.result_limit + len(query.excluded_coordinates)) * 4, 8
        )
        for area in areas:
            if time.monotonic() >= deadline:
                break
            for parcel_area, parcels in self._parcel_batches(district, area, deadline):
                if time.monotonic() >= deadline:
                    break
                anchor_parcels = [
                    parcel
                    for parcel in parcels
                    if parcel_matches_purpose(parcel.land_use, query.purpose)
                ]
                searched_batches.append((parcel_area, parcels, anchor_parcels))
                parcel_count += len(parcels)
                anchor_count += len(anchor_parcels)
                if not anchor_parcels:
                    continue
                drafts.extend(
                    self._build_drafts(
                        query,
                        parcel_area,
                        parcels,
                        anchor_parcels,
                        target_count=target_count,
                    )
                )
                if len(drafts) >= target_count:
                    break
            if len(drafts) >= target_count:
                break
        if not drafts and query.urban_plan_allowed_geojsons:
            logger.info(
                "No same-purpose EGKN anchors found a candidate; trying genplan-first "
                "vacancy search inside allowed urban plan geometry"
            )
            for parcel_area, parcels, anchor_parcels in searched_batches:
                if time.monotonic() >= deadline:
                    break
                drafts.extend(
                    self._build_drafts(
                        query,
                        parcel_area,
                        parcels,
                        anchor_parcels,
                        target_count=target_count,
                        require_purpose_anchor=False,
                    )
                )
                if len(drafts) >= target_count:
                    break
        if not drafts:
            return []
        drafts.sort(key=lambda item: item.score, reverse=True)
        drafts = drafts[:target_count]

        transformer = Transformer.from_crs(f"EPSG:{district.srs}", "EPSG:4326", always_xy=True)
        for draft in drafts:
            draft.longitude, draft.latitude = transformer.transform(draft.point.x, draft.point.y)

        if progress_callback:
            progress_callback("objects")
        surroundings = self._surroundings(drafts, deadline=deadline)
        osm_checked = any(context.checked for context in surroundings)
        if not osm_checked:
            logger.warning(
                "OSM did not check any candidate; continuing without automatic "
                "road/object filtering"
            )
        if False and not osm_checked:
            raise OsmProviderError(
                "OSM не подтвердил проверку дорог и объектов ни для одной координаты"
            )
        if progress_callback:
            progress_callback("area")
        plot_radius = math.sqrt(query.area_ha * 10_000 / 2)
        checked_pairs = (
            [
                (draft, context)
                for draft, context in zip(drafts, surroundings, strict=True)
                if context.checked and not mapped_obstacle_intersects_plot(context, plot_radius)
            ]
            if osm_checked
            else list(zip(drafts, surroundings, strict=True))
        )
        ranked = [
            self._to_result(
                query,
                district,
                settlement,
                draft,
                context,
                parcel_count,
                anchor_count,
            )
            for draft, context in checked_pairs
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        ranked = [
            item
            for item in ranked
            if not coordinates_excluded(item.latitude, item.longitude, query.excluded_coordinates)
        ]
        return ranked[: query.result_limit]

    def _restrict_to_allowed_urban_plan(
        self,
        query: SearchCreate,
        district: DistrictInfo,
        settlement: SettlementInfo,
    ) -> SettlementInfo:
        if not query.urban_plan_allowed_geojsons:
            return settlement

        transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{district.srs}", always_xy=True)
        intersections: list[BaseGeometry] = []
        for payload in query.urban_plan_allowed_geojsons:
            try:
                allowed_geometry = make_valid(
                    transform_geometry(transformer.transform, shape(payload))
                )
            except Exception:
                logger.exception("Invalid allowed urban plan search geometry")
                continue
            intersection = make_valid(settlement.geometry.intersection(allowed_geometry))
            if not intersection.is_empty and intersection.area >= query.area_ha * 10_000:
                intersections.append(intersection)

        if not intersections:
            return settlement

        restricted = make_valid(unary_union(intersections))
        if restricted.is_empty:
            return settlement
        logger.info(
            "Search area was restricted to allowed urban plan geometry: %s -> %.0f m2",
            settlement.name,
            restricted.area,
        )
        return SettlementInfo(
            gid=f"{settlement.gid}:urban-plan",
            name=settlement.name,
            kato=settlement.kato,
            district_id=settlement.district_id,
            geometry=restricted,
        )

    def _parcel_batches(
        self,
        district: DistrictInfo,
        area: SettlementInfo,
        deadline: float,
    ) -> list[tuple[SettlementInfo, list[ParcelRecord]]]:
        try:
            return [(area, self.egkn.parcels(district, area))]
        except EgknProviderError as exc:
            if "превысил лимит объектов" not in str(exc).lower():
                raise
            logger.info(
                "EGKN parcel layer is too large for %s; retrying by smaller tiles",
                area.name,
            )
        batches: list[tuple[SettlementInfo, list[ParcelRecord]]] = []
        for tile in district_search_tiles(area, tile_size_m=1000):
            if time.monotonic() >= deadline:
                break
            try:
                parcels = self.egkn.parcels(district, tile)
            except EgknProviderError as exc:
                if "превысил лимит объектов" in str(exc).lower():
                    logger.info(
                        "EGKN tile is still too large and was skipped: %s %s",
                        area.name,
                        tile.gid,
                    )
                    continue
                raise
            if parcels:
                batches.append((tile, parcels))
        if not batches:
            raise EgknProviderError(
                "Слой ЕГКН слишком большой даже после деления территории на части; "
                "выберите более конкретный населенный пункт или меньшую территорию"
            )
        return batches

    def _build_drafts(
        self,
        query: SearchCreate,
        settlement: SettlementInfo,
        parcels: list[ParcelRecord],
        anchor_parcels: list[ParcelRecord],
        *,
        target_count: int | None = None,
        require_purpose_anchor: bool = True,
    ) -> list[DraftCandidate]:
        if require_purpose_anchor and not anchor_parcels:
            return []
        plot_area_m2 = query.area_ha * 10_000
        side = math.sqrt(plot_area_m2)
        half = side / 2
        clearance = math.sqrt(2) * half + 1.5

        all_geometries = [parcel.geometry for parcel in parcels]
        if not all_geometries:
            return []
        anchor_geometries = [parcel.geometry for parcel in anchor_parcels]
        occupied = unary_union(all_geometries).buffer(0.5)
        if require_purpose_anchor:
            anchor_union = unary_union(anchor_geometries)
            candidate_zone = settlement.geometry.intersection(
                anchor_union.buffer(settings.live_search_radius_m)
            )
        else:
            candidate_zone = settlement.geometry
        vacant = candidate_zone.difference(occupied)

        nearest_parcels = anchor_parcels if require_purpose_anchor else parcels
        nearest_geometries = [parcel.geometry for parcel in nearest_parcels]
        if not nearest_geometries:
            return []
        tree = STRtree(nearest_geometries)
        anchor_tree = STRtree(anchor_geometries) if anchor_geometries else None
        parts = sorted(polygon_parts(vacant), key=lambda item: item.area, reverse=True)
        points: list[Point] = []
        drafts: list[DraftCandidate] = []
        target_count = target_count or max(
            (query.result_limit + len(query.excluded_coordinates)) * 4, 8
        )
        grid_step = max(side * 0.65, 25)

        for part in parts:
            if part.area < plot_area_m2 * 1.05:
                continue
            inner = part.buffer(-clearance)
            for inner_part in polygon_parts(inner):
                trial_points = [inner_part.representative_point()]
                minx, miny, maxx, maxy = inner_part.bounds
                x = minx
                grid_count = 0
                while x <= maxx and grid_count < 4000:
                    y = miny
                    while y <= maxy and grid_count < 4000:
                        point = Point(x, y)
                        if inner_part.covers(point):
                            trial_points.append(point)
                        y += grid_step
                        grid_count += 1
                    x += grid_step

                for point in trial_points:
                    if any(point.distance(existing) < side * 1.8 for existing in points):
                        continue
                    plot = box(point.x - half, point.y - half, point.x + half, point.y + half)
                    if not part.covers(plot):
                        continue
                    nearest_index = int(tree.nearest(point))
                    nearest = nearest_parcels[nearest_index]
                    nearest_distance = plot.distance(nearest.geometry)
                    if (
                        require_purpose_anchor
                        and nearest_distance > settings.max_lph_neighbor_distance_m
                    ):
                        continue
                    nearby_indexes = (
                        anchor_tree.query(point.buffer(220), predicate="intersects")
                        if anchor_tree is not None
                        else []
                    )
                    density = len(nearby_indexes)
                    if require_purpose_anchor:
                        score = 58 + min(20, density * 0.8) + max(0, 14 - nearest_distance / 10)
                    else:
                        score = 50 + min(12, density * 0.6) + max(0, 8 - nearest_distance / 50)
                    points.append(point)
                    drafts.append(
                        DraftCandidate(
                            point=point,
                            plot=plot,
                            nearest=nearest,
                            nearest_distance_m=round(nearest_distance, 1),
                            anchor_density=density,
                            score=score,
                            planning_first=not require_purpose_anchor,
                        )
                    )
                    if len(drafts) >= target_count:
                        return drafts
        return drafts

    def _surroundings(
        self, drafts: list[DraftCandidate], *, deadline: float
    ) -> list[Surroundings]:
        remaining = deadline - time.monotonic() - LAND_OSM_DEADLINE_RESERVE_SECONDS
        if remaining < 5:
            logger.warning(
                "Skipping OSM surroundings: only %.1fs remain before search deadline",
                remaining + LAND_OSM_DEADLINE_RESERVE_SECONDS,
            )
            return [Surroundings() for _ in drafts]
        points = [(draft.latitude, draft.longitude) for draft in drafts]
        try:
            return self.osm.analyze_points(
                points,
                radius_m=2000,
                time_budget_seconds=min(LAND_OSM_TIME_BUDGET_SECONDS, remaining),
            )
        except ProviderCallDeferred as exc:
            # OSM is supplementary evidence. A provider circuit/rate-limit must
            # not prevent the primary EGKN search from returning candidates.
            logger.warning("Skipping OSM surroundings after deferred provider call: %s", exc)
            return [Surroundings() for _ in drafts]
        except TypeError as exc:
            if "time_budget_seconds" not in str(exc):
                raise
            return self.osm.analyze_points(points, radius_m=2000)

    def _to_result(
        self,
        query: SearchCreate,
        district: DistrictInfo,
        settlement: SettlementInfo,
        draft: DraftCandidate,
        context: Surroundings,
        parcel_count: int,
        anchor_count: int,
    ) -> CandidateResult:
        score = draft.score
        purpose = normalize_purpose(query.purpose)
        # The 15/25 choice is only the requested search area. It is not verified
        # irrigation or a legally confirmed type of allotment.
        is_field = False
        is_irrigated = False
        if context.road_distance_m is not None and context.road_distance_m <= 200:
            score += 6
        if (
            not is_field
            and context.power_distance_m is not None
            and context.power_distance_m <= 300
        ):
            score += 5
        if context.water_distance_m is not None and context.water_distance_m <= 500:
            score += 8 if is_field or is_irrigated else 3
        if context.cemetery_distance_m is not None and context.cemetery_distance_m < 500:
            score -= 25
        elif context.cemetery_distance_m is not None and context.cemetery_distance_m < 1000:
            score -= 12
        elif context.cemetery_distance_m is not None and context.cemetery_distance_m < 1500:
            score -= 5

        power_evidence = (
            "Для полевого надела электроснабжение не является признаком допустимости; "
            "жилой дом на таком наделе не предусмотрен."
            if is_field
            else f"Объект электросети отмечен в OSM примерно в {context.power_distance_m:.0f} м; "
            "свободная мощность не подтверждена."
            if context.power_distance_m is not None
            else "В открытых данных электросети рядом не определены; требуется проверка на месте."
        )
        if context.water_distance_m is not None:
            water_evidence = (
                f"Водный объект или объект водоснабжения отмечен в OSM примерно в "
                f"{context.water_distance_m:.0f} м; наличие оросительной сети, право "
                "водопользования и обеспеченность водой не подтверждены."
                if purpose == LPH_NEW
                else f"Объект водоснабжения отмечен в OSM примерно в "
                f"{context.water_distance_m:.0f} м; подключение не подтверждено."
            )
        else:
            water_evidence = (
                "Открытых данных об оросительной сети или источнике воды рядом нет; "
                "выбранный профиль орошения не подтвержден."
                if purpose == LPH_NEW
                else "Открытых данных о водоснабжении рядом нет; запросить сведения и техусловия."
            )
        cemetery_note = (
            f" Ближайшее кладбище по OSM: около {context.cemetery_distance_m:.0f} м."
            if context.cemetery_distance_m is not None
            else " Кладбища в подключенных открытых данных не определены."
        )
        purpose_short = "садоводства" if purpose == GARDENING else "ЛПХ"
        profile_note = ""
        if purpose == LPH_NEW:
            profile_note = (
                f" Геометрический расчет выполнен для {query.area_ha:.2f} га. "
                "Правовой вид надела и наличие орошения подтверждает акимат."
            )
        category_note = (
            f" Категория соседнего участка по ЕГКН: {draft.nearest.category_id}."
            if draft.nearest.category_id
            else " Категория соседнего участка в ответе ЕГКН не указана."
        )
        if draft.planning_first:
            purpose_anchor_note = (
                f"рядом найдено {draft.anchor_density} участков назначения «{purpose_short}»"
                if draft.anchor_density
                else f"рядом не найдено участков назначения «{purpose_short}»"
            )
            search_basis = (
                "Живой ЕГКН + генплан: найден геометрический промежуток для "
                f"предварительного квадрата {query.area_ha:.2f} га внутри подключенной "
                f"разрешающей зоны генплана/ПДП; {purpose_anchor_note}. "
            )
        else:
            search_basis = (
                "Живой ЕГКН: найден геометрический промежуток для предварительного квадрата "
                f"{query.area_ha:.2f} га рядом с {draft.anchor_density} участками "
                f"назначения «{purpose_short}». "
            )
        risk_notes = (
            search_basis
            + f"Проанализировано {parcel_count} участков, из них подходящего назначения "
            f"{anchor_count}. "
            "Пустота на публичном слое не подтверждает государственную собственность или "
            "возможность предоставления; обязательна проверка акиматом."
            + category_note
            + profile_note
            + cemetery_note
            + (
                " Квадрат проверен на пересечение с нанесенными в OSM дорогами и объектами."
                if context.checked
                else " Данные OSM об объектах не подключены."
            )
        )
        return CandidateResult(
            region_chain=f"{district.region_name} → {district.display_name}",
            locality=settlement.name,
            latitude=draft.latitude,
            longitude=draft.longitude,
            nearby_cadastre=draft.nearest.cadastre,
            nearby_distance_m=draft.nearest_distance_m,
            cemetery_distance_m=(
                round(context.cemetery_distance_m)
                if context.cemetery_distance_m is not None
                else None
            ),
            road_distance_m=(
                round(context.road_distance_m) if context.road_distance_m is not None else None
            ),
            score=round(min(100, score), 1),
            risk_notes=risk_notes,
            power_evidence=power_evidence,
            water_evidence=water_evidence,
            sewer_evidence=(
                "Для полевого надела канализация и септик не оцениваются: жилой дом на нем "
                "не предусмотрен."
                if is_field
                else "Центральная канализация не подтверждена; возможность септика "
                "определяется после обследования и с учетом санитарных требований."
            ),
            nearby_land_use=draft.nearest.land_use,
            nearby_category_id=draft.nearest.category_id,
        )


def mapped_obstacle_intersects_plot(context: Surroundings, plot_radius_m: float) -> bool:
    if (
        context.road_distance_m is not None
        and context.road_distance_m <= plot_radius_m + settings.osm_road_clearance_m
    ):
        return True
    if context.cemetery_distance_m is not None and context.cemetery_distance_m <= plot_radius_m:
        return True
    if context.power_distance_m is not None and context.power_distance_m <= plot_radius_m:
        return True
    if (
        context.open_water_distance_m is not None
        and context.open_water_distance_m
        <= plot_radius_m + settings.osm_open_water_clearance_m
    ):
        return True
    return context.object_distance_m is not None and context.object_distance_m <= plot_radius_m


def coordinates_excluded(
    latitude: float,
    longitude: float,
    excluded: list[tuple[float, float]],
    *,
    tolerance_m: float = 5,
) -> bool:
    for excluded_latitude, excluded_longitude in excluded:
        north_m = (latitude - excluded_latitude) * 111_320
        east_m = (longitude - excluded_longitude) * 111_320 * math.cos(math.radians(latitude))
        if math.hypot(north_m, east_m) <= tolerance_m:
            return True
    return False
