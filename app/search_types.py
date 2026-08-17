from dataclasses import dataclass

from app.map_links import google_maps_place_url


@dataclass(slots=True)
class CandidateResult:
    region_chain: str
    locality: str
    latitude: float
    longitude: float
    nearby_cadastre: str
    nearby_distance_m: float | None
    cemetery_distance_m: float | None
    road_distance_m: float | None
    score: float
    risk_notes: str
    power_evidence: str
    water_evidence: str
    sewer_evidence: str
    nearby_land_use: str | None = None
    nearby_category_id: str | None = None

    @property
    def google_maps_url(self) -> str:
        return google_maps_place_url(self.latitude, self.longitude)
