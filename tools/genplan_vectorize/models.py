from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

LAYER_KINDS = ("allowed", "prohibited", "red_line")
LEGEND_LAYER_KINDS = (*LAYER_KINDS, "unknown", "ignore")
REVIEW_STATUSES = ("needs_review", "approved", "rejected")


class VectorizeConfigError(ValueError):
    """Raised when legend.json fails validation for vectorization."""


class LegendEntry(BaseModel):
    """One reviewed legend color, matching `app.models.GenplanLegendEntry`."""

    model_config = ConfigDict(extra="forbid")

    color_hex: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    red: int = Field(ge=0, le=255)
    green: int = Field(ge=0, le=255)
    blue: int = Field(ge=0, le=255)
    source: str = Field(default="dominant_color", min_length=1, max_length=48)
    label_ru: str = Field(default="", max_length=240)
    label_kz: str = Field(default="", max_length=240)
    target_category: str = Field(default="unknown", min_length=1, max_length=32)
    layer_kind: str = Field(default="unknown", pattern=r"^(" + "|".join(LEGEND_LAYER_KINDS) + ")$")
    confidence_score: float = Field(default=0.25, ge=0, le=1)
    review_status: str = Field(
        default="needs_review", pattern=r"^(" + "|".join(REVIEW_STATUSES) + ")$"
    )
    pixel_count: int | None = Field(default=None, ge=0)
    notes: str = Field(default="", max_length=1_000)
    tolerance: int = Field(default=10, ge=0, le=255)

    @field_validator("color_hex")
    @classmethod
    def _normalize_color_hex(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def _check_color_matches_rgb(self) -> LegendEntry:
        expected = f"#{self.red:02x}{self.green:02x}{self.blue:02x}"
        if self.color_hex != expected:
            raise ValueError(
                f"color_hex {self.color_hex!r} does not match red/green/blue "
                f"({self.red}, {self.green}, {self.blue}); expected {expected!r}"
            )
        return self

    @property
    def is_approved(self) -> bool:
        return self.review_status == "approved"

    @property
    def is_usable(self) -> bool:
        return self.is_approved and self.layer_kind in LAYER_KINDS


class LegendDocument(BaseModel):
    """An operator-approved legend, ready to drive color segmentation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "genplan-legend/v1"
    record_id: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    source_title: str = Field(default="", max_length=320)
    reviewer_id: str = Field(min_length=1, max_length=200)
    reviewed_at_utc: datetime
    min_area_px: int = Field(default=16, ge=0)
    entries: list[LegendEntry] = Field(min_length=1, max_length=200)

    @field_validator("source_sha256")
    @classmethod
    def _normalize_source_sha256(cls, value: str) -> str:
        return value.lower()

    @field_validator("entries")
    @classmethod
    def _unique_colors(cls, value: list[LegendEntry]) -> list[LegendEntry]:
        seen: set[str] = set()
        for entry in value:
            if entry.color_hex in seen:
                raise ValueError(f"duplicate color_hex {entry.color_hex!r} in legend entries")
            seen.add(entry.color_hex)
        return value

    def usable_entries_by_layer_kind(self) -> dict[str, list[LegendEntry]]:
        grouped: dict[str, list[LegendEntry]] = {kind: [] for kind in LAYER_KINDS}
        for entry in self.entries:
            if entry.is_usable:
                grouped[entry.layer_kind].append(entry)
        return grouped


def load_legend_document(path: Path) -> LegendDocument:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VectorizeConfigError(f"Could not read legend.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise VectorizeConfigError("legend.json root must be an object")
    try:
        return LegendDocument.model_validate(payload)
    except ValidationError as exc:
        raise VectorizeConfigError(f"legend.json failed validation: {exc}") from exc
