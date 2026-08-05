from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Decision(StrEnum):
    match = "match"
    no_coverage = "no_coverage"
    blocked = "blocked"
    manual_review = "manual_review"


class LayerKind(StrEnum):
    georaster = "georaster"
    geojson_masks = "geojson_masks"


class ProvenanceStatus(StrEnum):
    verified_official = "verified_official"
    official_copy_unverified_version = "official_copy_unverified_version"
    secondary_source = "secondary_source"
    unknown = "unknown"


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=200)
    geometry: dict[str, Any]


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=300)
    source_title: str = Field(min_length=1, max_length=500)
    source_version: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    status: ProvenanceStatus
    identity_status: str = Field(min_length=1, max_length=100)
    official_url: str | None = Field(default=None, max_length=2_000)
    checked_at: datetime
    valid_until: datetime | None = None
    current: bool
    superseded_by: str | None = Field(default=None, max_length=200)


class QAReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(min_length=1, max_length=100)
    review_version: str = Field(min_length=1, max_length=200)
    reviewer_id: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime
    expires_at: datetime | None = None
    independent_review: bool
    ambiguity_resolved: bool = True
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class RasterMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str = Field(min_length=1, max_length=2_000)
    crs: str = Field(min_length=1, max_length=100)
    footprint: dict[str, Any]
    classification_complete: bool = False
    categories_checked: list[str] = Field(default_factory=list)
    masks: dict[str, Any] | None = None


class Layer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str = Field(min_length=1, max_length=200)
    kind: LayerKind
    provenance: Provenance
    qa_review: QAReview
    ambiguous: bool = False
    coverage_geometry: dict[str, Any] | None = None
    masks: dict[str, Any] | None = None
    categories_checked: list[str] = Field(default_factory=list)
    raster: RasterMetadata | None = None

    @model_validator(mode="after")
    def validate_payload_for_kind(self) -> Layer:
        if self.kind == LayerKind.georaster and self.raster is None:
            raise ValueError("georaster layer requires raster metadata")
        if self.kind == LayerKind.geojson_masks and self.coverage_geometry is None:
            raise ValueError("geojson_masks layer requires coverage_geometry")
        return self


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted_qa_decisions: list[str] = Field(
        default_factory=lambda: ["APPROVED", "STRICT", "VERIFIED_STRICT"]
    )
    required_categories: list[str] = Field(
        default_factory=lambda: ["road", "water", "zone"]
    )
    max_source_age_days: int = Field(default=365, ge=1, le=3_650)


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: Candidate
    layers: list[Layer] = Field(min_length=1)
    as_of: datetime
    policy: Policy = Field(default_factory=Policy)


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    layer_id: str | None = None
    source_version: str | None = None
    feature_ids: list[str] = Field(default_factory=list)


class SourceEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str
    source_id: str
    source_version: str
    source_sha256: str
    qa_decision: str
    eligibility: str


class ComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "genplan-shadow/v1"
    candidate_id: str
    decision: Decision
    as_of: datetime
    reasons: list[Reason]
    source_versions: list[SourceEvaluation]
    matched_feature_ids: list[str] = Field(default_factory=list)
