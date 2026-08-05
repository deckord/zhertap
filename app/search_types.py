from dataclasses import dataclass


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
        return (
            f"https://www.google.com/maps/@{self.latitude:.7f},"
            f"{self.longitude:.7f},19z/data=!3m1!1e3"
        )
