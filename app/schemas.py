from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.purposes import (
    LPH_NEW,
    normalize_allotment_type,
    normalize_irrigation_type,
    normalize_purpose,
    purpose_area_ha,
)

ALL_DISTRICTS = "__all_districts__"


class SearchCreate(BaseModel):
    language: str = "ru"
    region: str = "Акмолинская область"
    region_label: str | None = None
    district: str
    district_label: str | None = None
    locality: str | None = None
    locality_label: str | None = None
    purpose: str = "ЛПХ"
    allotment_type: str | None = None
    irrigation_type: str | None = None
    area_ha: float = Field(default=0.10, ge=0.01, le=10)
    result_limit: int = Field(default=10, ge=1, le=15)
    cemetery_buffer_m: int = Field(default=0, ge=0, le=10000)
    max_road_distance_m: int = Field(default=200, ge=0, le=10000)
    max_power_distance_m: int = Field(default=300, ge=0, le=10000)
    raw_query: str | None = None
    telegram_user_id: str | None = None
    telegram_chat_id: str | None = None
    funnel_session_id: str | None = None
    terms_version: str | None = None
    terms_text_snapshot: str | None = None
    terms_accepted_at: datetime | None = None
    excluded_coordinates: list[tuple[float, float]] = Field(default_factory=list, exclude=True)
    urban_plan_allowed_geojsons: list[dict[str, Any]] = Field(default_factory=list, exclude=True)

    @field_validator("region", "district")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Поле не может быть пустым")
        return value

    @field_validator("purpose")
    @classmethod
    def normalize_search_purpose(cls, value: str) -> str:
        return normalize_purpose(value)

    @model_validator(mode="after")
    def enforce_profile_area(self) -> "SearchCreate":
        if self.purpose == LPH_NEW:
            self.allotment_type = normalize_allotment_type(self.allotment_type)
            self.irrigation_type = normalize_irrigation_type(self.irrigation_type)
        else:
            self.allotment_type = None
            self.irrigation_type = None
        self.area_ha = purpose_area_ha(self.purpose, self.irrigation_type, self.area_ha)
        return self


class SearchCreated(BaseModel):
    id: str
    status: str
    position: int


class ReviewUpdate(BaseModel):
    status: str
    google_checked: bool = False
    notes: str = ""
    reviewer: str = "operator"
