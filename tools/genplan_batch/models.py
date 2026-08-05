from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = "1.0"
ALLOWED_REGISTRATION_STATUSES = {"proposed", "needs_manual"}


@dataclass(frozen=True, slots=True)
class RunRequest:
    asset_id: str
    source_path: str
    source_sha256: str
    locality: str
    region: str
    district: str
    output_dir: str
    basemaps: tuple[str, ...]
    zoom: int
    bbox_padding: float
    max_tiles: int


@dataclass(slots=True)
class AssetStatus:
    asset_id: str
    source_sha256: str
    config_sha256: str
    source_path: str
    detected_format: str
    region: str
    district: str
    locality: str
    workflow_state: str
    registration_status: Literal["proposed", "needs_manual"] | None = None
    action: str = ""
    duplicate_of: str | None = None
    resumed: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BatchResult:
    selected: int
    runnable: int
    duplicate_count: int
    resumed_count: int
    failed_count: int
    output_dir: str
    summary: dict[str, Any]

