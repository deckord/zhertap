from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class BoundingBox:
    west: float
    south: float
    east: float
    north: float
    source: str
    label: str = ""

    def __post_init__(self) -> None:
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError("Invalid bbox longitude range")
        if not (-85.051129 <= self.south < self.north <= 85.051129):
            raise ValueError("Invalid bbox latitude range")

    def padded(self, ratio: float) -> BoundingBox:
        if ratio < 0:
            raise ValueError("Padding ratio cannot be negative")
        width = self.east - self.west
        height = self.north - self.south
        return BoundingBox(
            west=max(-180.0, self.west - width * ratio),
            south=max(-85.051129, self.south - height * ratio),
            east=min(180.0, self.east + width * ratio),
            north=min(85.051129, self.north + height * ratio),
            source=self.source,
            label=self.label,
        )


@dataclass(frozen=True, slots=True)
class ProposedGCP:
    plan_x: float
    plan_y: float
    longitude: float
    latitude: float
    reference_x: float
    reference_y: float
    residual_px: float
    confidence: float


@dataclass(frozen=True, slots=True)
class DiagnosticAnchorPoint:
    id: str
    rank: int
    scope: str
    plan_pixel: dict[str, float]
    reference_pixel: dict[str, float]
    reference_lonlat: dict[str, float]
    residual_px: float
    diagnostic_score: float
    source: str
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class MatchMetrics:
    detector: str = ""
    plan_keypoints: int = 0
    reference_keypoints: int = 0
    candidate_matches: int = 0
    inliers: int = 0
    inlier_ratio: float = 0.0
    reprojection_rmse_px: float | None = None
    plan_coverage: float = 0.0
    reference_coverage: float = 0.0
    homography_condition: float | None = None


@dataclass(slots=True)
class AutoregResult:
    status: Literal["needs_manual"] = "needs_manual"
    confidence: float = 0.0
    confidence_label: Literal["none", "low", "medium"] = "none"
    source_sha256: str = ""
    source_path: str = ""
    locality: str = ""
    bbox: BoundingBox | None = None
    basemap_source: str = ""
    basemap_attribution: str = ""
    metrics: MatchMetrics = field(default_factory=MatchMetrics)
    proposed_gcps: list[ProposedGCP] = field(default_factory=list)
    diagnostic_anchor_points: list[DiagnosticAnchorPoint] = field(default_factory=list)
    diagnostic_anchor_guardrails: dict[str, bool] = field(default_factory=dict)
    diagnostic_anchor_summary: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
