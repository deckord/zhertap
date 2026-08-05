import json
from collections.abc import Callable
from importlib.resources import files

from app.config import settings
from app.live_search import LiveSearchEngine, coordinates_excluded
from app.providers.osm import OsmProvider
from app.schemas import SearchCreate
from app.search_types import CandidateResult


class SearchEngine:
    def __init__(
        self,
        osm_provider: OsmProvider | None = None,
        *,
        mode: str | None = None,
        live_engine: LiveSearchEngine | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.osm_provider = osm_provider or OsmProvider()
        self.mode = mode or settings.search_mode
        self.live_engine = live_engine or LiveSearchEngine(osm_provider=self.osm_provider)
        self.progress_callback = progress_callback

    def search(self, query: SearchCreate) -> list[CandidateResult]:
        if self.mode in {"live", "hybrid"}:
            try:
                live_results = (
                    self.live_engine.search(
                        query,
                        progress_callback=self.progress_callback,
                    )
                    if self.progress_callback
                    else self.live_engine.search(query)
                )
                if live_results or self.mode == "live":
                    return live_results
            except Exception:
                if self.mode == "live":
                    raise
        return self._search_demo(query)

    def _search_demo(self, query: SearchCreate) -> list[CandidateResult]:
        if self.progress_callback:
            self.progress_callback("boundaries")
        if not settings.demo_data_enabled:
            return []

        rows = json.loads(
            files("app.data").joinpath("demo_candidates.json").read_text(encoding="utf-8")
        )
        district_key = query.district.lower().replace("ё", "е")
        locality_key = (query.locality or "").lower().replace("ё", "е")
        candidates: list[CandidateResult] = []

        for row in rows:
            if district_key not in {"не указан", "казахстан"}:
                row_district = row["district"].lower().replace("ё", "е")
                if district_key not in row_district and row_district not in district_key:
                    continue
            if locality_key and locality_key not in row["locality"].lower().replace("ё", "е"):
                continue

            cemetery_distance = row["cemetery_distance_m"]
            live_distance = self.osm_provider.nearest_cemetery(row["lat"], row["lon"], 2000)
            if live_distance is not None:
                cemetery_distance = round(live_distance)

            score = float(row["base_score"])
            if (
                row["road_distance_m"] is not None
                and row["road_distance_m"] > query.max_road_distance_m
            ):
                score -= min(15, (row["road_distance_m"] - query.max_road_distance_m) / 20)
            if cemetery_distance is not None and cemetery_distance < 500:
                score -= 25
            elif cemetery_distance is not None and cemetery_distance < 1000:
                score -= 12
            elif cemetery_distance is not None and cemetery_distance < 1500:
                score -= 5
            elif cemetery_distance is None:
                score -= 3

            candidates.append(
                CandidateResult(
                    region_chain=row["region_chain"],
                    locality=row["locality"],
                    latitude=row["lat"],
                    longitude=row["lon"],
                    nearby_cadastre=row["cadastre"],
                    nearby_distance_m=row["nearby_distance_m"],
                    cemetery_distance_m=cemetery_distance,
                    road_distance_m=row["road_distance_m"],
                    score=round(score, 1),
                    risk_notes=f"Резервный каталог: {row['risk_notes']}",
                    power_evidence=(
                        "Жилая застройка рядом; наличие сетей и свободной мощности "
                        "не подтверждено."
                    ),
                    water_evidence="Официальных данных нет; запросить сведения и техусловия.",
                    sewer_evidence=(
                        "Индивидуальный септик возможен только после обследования участка."
                    ),
                )
            )

        if self.progress_callback:
            self.progress_callback("objects")
        candidates.sort(key=lambda item: item.score, reverse=True)
        candidates = [
            item
            for item in candidates
            if not coordinates_excluded(
                item.latitude, item.longitude, query.excluded_coordinates
            )
        ]
        if self.progress_callback:
            self.progress_callback("area")
        return candidates[: query.result_limit]
