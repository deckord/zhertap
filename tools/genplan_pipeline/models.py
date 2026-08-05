from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"


@dataclass(slots=True)
class ArchiveRecord:
    archive_id: str
    original_path: str
    filename: str
    size_bytes: int
    sha256: str
    member_count: int = 0
    file_count: int = 0
    uncompressed_bytes: int = 0
    extraction_directory: str = ""
    status: str = "pending"
    error_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AssetRecord:
    asset_id: str
    source_kind: str
    source_archive_id: str
    source_archive_name: str
    original_member_path: str
    original_filename: str
    extracted_path: str
    extension: str
    detected_format: str
    media_type: str
    asset_role: str
    size_bytes: int
    sha256: str
    width_px: int | None = None
    height_px: int | None = None
    page_count: int | None = None
    metadata_method: str = ""
    original_region: str = ""
    original_district: str = ""
    original_locality: str = ""
    normalized_region: str = ""
    region_code: str = ""
    egkn_region: str = ""
    normalized_district: str = ""
    district_code: str = ""
    egkn_district: str = ""
    normalized_locality: str = ""
    location_confidence: str = "low"
    normalization_notes: list[str] = field(default_factory=list)
    georef_status: str = "not_checked"
    workflow_status: str = "pending_inventory"
    crs: str = ""
    control_point_count: int = 0
    rms_error_m: float | None = None
    sidecar_files: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ARCHIVE_CSV_FIELDS = list(ArchiveRecord.__dataclass_fields__)
ASSET_CSV_FIELDS = list(AssetRecord.__dataclass_fields__)
