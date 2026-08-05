from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReviewDecision(StrEnum):
    strict = "STRICT"
    warning = "WARNING"
    reject = "REJECT"


class CheckStatus(StrEnum):
    pass_ = "pass"
    warning = "warning"
    fail = "fail"


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    pixel_x: float = Field(ge=0)
    pixel_y: float = Field(ge=0)
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    source: str = Field(min_length=1, max_length=300)
    feature: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=1_000)


class CheckpointSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "genplan-checkpoints/v1"
    record_id: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    reviewer_id: str = Field(min_length=1, max_length=200)
    reviewed_at_utc: datetime
    selected_before_a1_residuals: bool
    points: list[Checkpoint] = Field(min_length=1, max_length=500)

    @field_validator("points")
    @classmethod
    def unique_ids(cls, value: list[Checkpoint]) -> list[Checkpoint]:
        ids = [point.id for point in value]
        if len(ids) != len(set(ids)):
            raise ValueError("checkpoint IDs must be unique")
        return value


class ProvenanceStatus(StrEnum):
    verified_official = "verified_official"
    official_copy_unverified_version = "official_copy_unverified_version"
    secondary_source = "secondary_source"
    unknown = "unknown"


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "genplan-provenance/v1"
    record_id: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    status: ProvenanceStatus
    document_title: str = Field(default="", max_length=500)
    document_type: str = Field(default="", max_length=100)
    approving_authority: str = Field(default="", max_length=500)
    approval_number: str = Field(default="", max_length=200)
    approval_date: str = Field(default="", max_length=50)
    official_url: str = Field(default="", max_length=2_000)
    publication_reference: str = Field(default="", max_length=1_000)
    source_checked_at_utc: datetime | None = None
    territory: str = Field(default="", max_length=500)
    revision: str = Field(default="", max_length=200)
    current_version_confirmed: bool = False
    identity_status: str = Field(default="resolved", max_length=100)


class VisualSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    area: str = Field(pattern=r"^(edge|interior|boundary|critical)$")
    result: str = Field(pattern=r"^(pass|warning|fail)$")
    source: str = Field(min_length=1, max_length=300)
    observation: str = Field(default="", max_length=1_000)


class LayerEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    status: str = Field(pattern=r"^(strict|warning|unavailable|reject)$")
    categories: list[str] = Field(default_factory=list, max_length=500)
    note: str = Field(default="", max_length=1_000)


class LegendEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "genplan-legend-evidence/v1"
    record_id: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    reviewer_id: str = Field(min_length=1, max_length=200)
    legend_status: str = Field(pattern=r"^(readable|not_present|unreadable)$")
    interpretation_confirmed: bool
    scale_denominator: int = Field(gt=0)
    orientation_status: str = Field(
        pattern=r"^(correct|explained_deviation|reflected|wrong_axes|wrong_territory)$"
    )
    anisotropy_percent: float | None = Field(default=None, ge=0)
    anisotropy_explained: bool = False
    visual_samples: list[VisualSample] = Field(default_factory=list, max_length=1_000)
    layers: list[LayerEvidence] = Field(default_factory=list, max_length=500)
    notes: str = Field(default="", max_length=5_000)


class ReviewReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: CheckStatus
    message: str


class ReviewCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    status: CheckStatus
    message: str
    observed: Any = None
    strict_requirement: Any = None


class ErrorMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    rmse_m: float
    p95_m: float
    max_m: float


class PointDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    quadrants: list[str]
    edge_count: int
    interior_count: int
    feature_type_count: int | None = None
    reference_source_count: int | None = None
    max_empty_radius_width_ratio: float | None = None


class CheckpointResidual(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    dx_m: float
    dy_m: float
    error_m: float
    source: str
    feature: str


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "genplan-review/v1"
    record_id: str
    source_sha256: str
    decision: ReviewDecision
    reviewed_at_utc: datetime
    a1_operator_id: str
    a2_reviewer_id: str
    transform_type: str
    transform_sha256: str
    metrics: ErrorMetrics
    a1_distribution: PointDistribution
    checkpoint_distribution: PointDistribution
    checkpoint_residuals: list[CheckpointResidual]
    provenance_status: ProvenanceStatus
    checks: list[ReviewCheck]
    reasons: list[ReviewReason]
    guardrails: dict[str, Any]
