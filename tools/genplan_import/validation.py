from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.providers.urban_plan import normalize_geojson

LAYER_KINDS = ("allowed", "prohibited", "red_line")
STRICT_STATUSES = {"STRICT", "VERIFIED_STRICT"}
IMPORTABLE_STATUSES = STRICT_STATUSES | {"WARNING"}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_METADATA_BYTES = 5 * 1024 * 1024
MAX_GEOJSON_BYTES = 100 * 1024 * 1024


class ReleaseValidationError(ValueError):
    """Raised when a release does not satisfy the safe import gate."""


@dataclass(frozen=True, slots=True)
class ValidatedLayer:
    layer_kind: str
    path: Path
    sha256: str
    raw: bytes
    normalized_geojson: str
    zone_name: str | None


@dataclass(frozen=True, slots=True)
class ValidatedRelease:
    manifest_path: Path
    manifest_sha256: str
    release_id: str
    release_mode: str
    source_sha256: str
    source_version: str
    source_epsg: int
    region: str
    district: str
    locality: str
    purpose: str
    title: str
    approval_document: str
    approval_date: date | None
    source_authority: str
    source_url: str
    released_by: str
    provenance_status: str
    identity_status: str
    qa_status: str
    independent_review: bool
    review: dict[str, Any]
    provenance: dict[str, Any]
    review_sha256: str
    provenance_sha256: str
    approved_for_search: bool
    active: bool
    layers: tuple[ValidatedLayer, ...]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_release(manifest_path: Path) -> ValidatedRelease:
    manifest_file = _existing_file(Path(manifest_path), "release manifest")
    manifest_raw = _read_limited(manifest_file, MAX_METADATA_BYTES, "release manifest")
    manifest = _load_json_object(manifest_raw, "release manifest")
    manifest_sha = sha256_bytes(manifest_raw)
    base_dir = manifest_file.parent.resolve()

    if _required_text(manifest, "schema_version", "release manifest") != "1.0":
        raise ReleaseValidationError("release manifest schema_version must be '1.0'")
    release_id = _required_text(manifest, "release_id", "release manifest")
    release_mode = _required_text(manifest, "release_mode", "release manifest").lower()
    if release_mode not in {"search", "shadow"}:
        raise ReleaseValidationError("release_mode must be 'search' or 'shadow'")

    source_sha = _required_sha(manifest, "source_sha256", "release manifest")
    source_version = _required_text(manifest, "source_version", "release manifest")
    source_epsg = manifest.get("source_epsg", 4326)
    if not isinstance(source_epsg, int) or isinstance(source_epsg, bool):
        raise ReleaseValidationError("release manifest source_epsg must be an integer")

    scope = _required_object(manifest, "scope", "release manifest")
    region = _required_text(scope, "region", "release manifest scope")
    district = _required_text(scope, "district", "release manifest scope")
    locality = _required_text(scope, "locality", "release manifest scope")
    purpose = _required_text(manifest, "purpose", "release manifest")

    document = _required_object(manifest, "document", "release manifest")
    title = _required_text(document, "title", "release manifest document")
    approval_document = _required_text(
        document, "approval_document", "release manifest document"
    )
    approval_date = _optional_date(document.get("approval_date"))
    source_authority = _required_text(
        document, "source_authority", "release manifest document"
    )
    source_url = _official_https_url(
        _required_text(document, "source_url", "release manifest document")
    )
    released_by = _required_text(manifest, "released_by", "release manifest")

    review_path, review_sha, review = _load_artifact_object(
        manifest,
        "review",
        base_dir,
        MAX_METADATA_BYTES,
    )
    provenance_path, provenance_sha, provenance = _load_artifact_object(
        manifest,
        "provenance",
        base_dir,
        MAX_METADATA_BYTES,
    )
    del review_path, provenance_path

    _require_same_release_ids(release_id, review, provenance)
    _require_hash_value(review, "source_sha256", source_sha, "review.json")
    _require_hash_value(provenance, "source_sha256", source_sha, "provenance.json")
    _require_hash_value(provenance, "review_sha256", review_sha, "provenance.json")

    provenance_status = _required_text(
        provenance, "provenance_status", "provenance.json"
    ).lower()
    if provenance_status != "verified_official":
        raise ReleaseValidationError(
            "provenance_status must be 'verified_official'"
        )
    identity_status = _required_text(
        provenance, "identity_status", "provenance.json"
    ).lower()
    if identity_status != "matched":
        raise ReleaseValidationError("identity_status must be 'matched'")
    provenance_url = _official_https_url(
        _required_text(provenance, "official_url", "provenance.json")
    )
    if provenance_url != source_url:
        raise ReleaseValidationError(
            "provenance official_url must equal document source_url"
        )

    qa_status = _review_status(review)
    if qa_status not in IMPORTABLE_STATUSES:
        allowed = ", ".join(sorted(IMPORTABLE_STATUSES))
        raise ReleaseValidationError(f"review status must be one of: {allowed}")
    if review.get("independent_review") is not True:
        raise ReleaseValidationError("review.json independent_review must be true")
    reviewer_role = _required_text(review, "reviewer_role", "review.json").upper()
    if reviewer_role != "A2":
        raise ReleaseValidationError("review.json reviewer_role must be 'A2'")
    reviewer = _required_text(review, "reviewer", "review.json")
    operator = _required_text(review, "operator", "review.json")
    if reviewer.casefold() == operator.casefold():
        raise ReleaseValidationError("A2 reviewer must differ from the vector operator")
    _validate_timestamp(
        _required_text(review, "reviewed_at_utc", "review.json"),
        "review.json reviewed_at_utc",
    )

    if qa_status == "WARNING":
        if release_mode != "shadow":
            raise ReleaseValidationError("WARNING may only be imported in shadow mode")
        if review.get("allow_shadow") is not True:
            raise ReleaseValidationError(
                "WARNING shadow import requires review.json allow_shadow=true"
            )
    elif review.get("allow_shadow") not in (None, False):
        raise ReleaseValidationError(
            "STRICT/VERIFIED_STRICT review must not set allow_shadow=true"
        )

    layer_specs = _required_object(manifest, "layers", "release manifest")
    if set(layer_specs) != set(LAYER_KINDS):
        expected = ", ".join(LAYER_KINDS)
        raise ReleaseValidationError(
            f"release manifest layers must contain exactly: {expected}"
        )
    review_hashes = _required_object(review, "layer_sha256", "review.json")
    provenance_layers = _required_object(
        provenance, "layers", "provenance.json"
    )

    validated_layers: list[ValidatedLayer] = []
    for layer_kind in LAYER_KINDS:
        spec = layer_specs[layer_kind]
        if not isinstance(spec, dict):
            raise ReleaseValidationError(
                f"release manifest layers.{layer_kind} must be an object"
            )
        layer_path, declared_sha = _artifact_reference(
            spec, f"layers.{layer_kind}", base_dir
        )
        raw = _read_limited(
            layer_path, MAX_GEOJSON_BYTES, f"{layer_kind} GeoJSON"
        )
        actual_sha = sha256_bytes(raw)
        _require_equal_hash(
            declared_sha,
            actual_sha,
            f"release manifest layers.{layer_kind} sha256",
        )
        _require_hash_value(
            review_hashes,
            layer_kind,
            actual_sha,
            "review.json layer_sha256",
        )
        provenance_layer = provenance_layers.get(layer_kind)
        if not isinstance(provenance_layer, dict):
            raise ReleaseValidationError(
                f"provenance.json layers.{layer_kind} must be an object"
            )
        _require_hash_value(
            provenance_layer,
            "sha256",
            actual_sha,
            f"provenance.json layers.{layer_kind}",
        )
        try:
            normalized = normalize_geojson(raw, layer_kind, source_epsg)
        except ValueError as exc:
            raise ReleaseValidationError(
                f"{layer_kind} GeoJSON failed normalization: {exc}"
            ) from exc
        zone_name = _optional_text(spec.get("zone_name"))
        validated_layers.append(
            ValidatedLayer(
                layer_kind=layer_kind,
                path=layer_path,
                sha256=actual_sha,
                raw=raw,
                normalized_geojson=normalized,
                zone_name=zone_name,
            )
        )

    approved_for_search = qa_status in STRICT_STATUSES and release_mode == "search"
    active = approved_for_search
    return ValidatedRelease(
        manifest_path=manifest_file,
        manifest_sha256=manifest_sha,
        release_id=release_id,
        release_mode=release_mode,
        source_sha256=source_sha,
        source_version=source_version,
        source_epsg=source_epsg,
        region=region,
        district=district,
        locality=locality,
        purpose=purpose,
        title=title,
        approval_document=approval_document,
        approval_date=approval_date,
        source_authority=source_authority,
        source_url=source_url,
        released_by=released_by,
        provenance_status=provenance_status,
        identity_status=identity_status,
        qa_status=qa_status,
        independent_review=True,
        review=review,
        provenance=provenance,
        review_sha256=review_sha,
        provenance_sha256=provenance_sha,
        approved_for_search=approved_for_search,
        active=active,
        layers=tuple(validated_layers),
    )


def _load_artifact_object(
    manifest: dict[str, Any],
    field: str,
    base_dir: Path,
    max_bytes: int,
) -> tuple[Path, str, dict[str, Any]]:
    spec = _required_object(manifest, field, "release manifest")
    path, declared_sha = _artifact_reference(spec, field, base_dir)
    raw = _read_limited(path, max_bytes, field)
    actual_sha = sha256_bytes(raw)
    _require_equal_hash(declared_sha, actual_sha, f"release manifest {field} sha256")
    return path, actual_sha, _load_json_object(raw, f"{field}.json")


def _artifact_reference(
    spec: dict[str, Any],
    label: str,
    base_dir: Path,
) -> tuple[Path, str]:
    relative = _required_text(spec, "path", f"release manifest {label}")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ReleaseValidationError(f"{label} path must be relative to the release")
    resolved = (base_dir / candidate).resolve()
    if not resolved.is_relative_to(base_dir):
        raise ReleaseValidationError(f"{label} path escapes the release directory")
    path = _existing_file(resolved, label)
    sha = _required_sha(spec, "sha256", f"release manifest {label}")
    return path, sha


def _existing_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ReleaseValidationError(f"{label} does not exist or is not a file: {resolved}")
    return resolved


def _read_limited(path: Path, max_bytes: int, label: str) -> bytes:
    size = path.stat().st_size
    if size > max_bytes:
        raise ReleaseValidationError(
            f"{label} exceeds the {max_bytes}-byte safety limit"
        )
    return path.read_bytes()


def _load_json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseValidationError(f"{label} root must be an object")
    return value


def _required_object(
    payload: dict[str, Any],
    field: str,
    label: str,
) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise ReleaseValidationError(f"{label} {field} must be an object")
    return value


def _required_text(payload: dict[str, Any], field: str, label: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseValidationError(f"{label} {field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReleaseValidationError("zone_name must be a string or null")
    return value.strip() or None


def _required_sha(payload: dict[str, Any], field: str, label: str) -> str:
    value = _required_text(payload, field, label)
    if not SHA256_RE.fullmatch(value):
        raise ReleaseValidationError(f"{label} {field} must be a SHA-256 hex digest")
    return value.lower()


def _require_hash_value(
    payload: dict[str, Any],
    field: str,
    expected: str,
    label: str,
) -> None:
    actual = _required_sha(payload, field, label)
    _require_equal_hash(actual, expected, f"{label} {field}")


def _require_equal_hash(actual: str, expected: str, label: str) -> None:
    if not hmac.compare_digest(actual.lower(), expected.lower()):
        raise ReleaseValidationError(f"{label} does not match the referenced file")


def _require_same_release_ids(
    release_id: str,
    review: dict[str, Any],
    provenance: dict[str, Any],
) -> None:
    for payload, label in ((review, "review.json"), (provenance, "provenance.json")):
        actual = _required_text(payload, "release_id", label)
        if actual != release_id:
            raise ReleaseValidationError(
                f"{label} release_id does not match the release manifest"
            )


def _review_status(review: dict[str, Any]) -> str:
    status = review.get("status")
    decision = review.get("decision")
    if status is not None and decision is not None:
        if str(status).strip().upper() != str(decision).strip().upper():
            raise ReleaseValidationError("review.json status and decision conflict")
    value = status if status is not None else decision
    if not isinstance(value, str) or not value.strip():
        raise ReleaseValidationError("review.json status must be a non-empty string")
    return value.strip().upper()


def _official_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ReleaseValidationError(
            "official source URL must be an HTTPS URL without embedded credentials"
        )
    return value


def _optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ReleaseValidationError("approval_date must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReleaseValidationError("approval_date must use YYYY-MM-DD") from exc


def _validate_timestamp(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseValidationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseValidationError(f"{label} must include a timezone")
