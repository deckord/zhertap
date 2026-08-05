from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

REGIONS: dict[str, tuple[str, str]] = {
    "акмолинская область": ("Акмолинская область", "01"),
    "актюбинская область": ("Актюбинская область", "02"),
    "алматинская область": ("Алматинская область", "03"),
    "атырауская область": ("Атырауская область", "04"),
    "восточно-казахстанская область": ("Восточно-Казахстанская область", "05"),
    "вко": ("Восточно-Казахстанская область", "05"),
    "жамбылская область": ("Жамбылская область", "06"),
    "западно-казахстанская область": ("Западно-Казахстанская область", "08"),
    "зко": ("Западно-Казахстанская область", "08"),
    "карагандинская область": ("Карагандинская область", "09"),
    "кызылординская область": ("Кызылординская область", "10"),
    "костанайская область": ("Костанайская область", "12"),
    "мангистауская область": ("Мангистауская область", "13"),
    "павлодарская область": ("Павлодарская область", "14"),
    "северо-казахстанская область": ("Северо-Казахстанская область", "15"),
    "туркестанская область": ("Туркестанская область", "19"),
    "г. алматы": ("г. Алматы", "20"),
    "г.алматы": ("г. Алматы", "20"),
    "город алматы": ("г. Алматы", "20"),
    "г. астана": ("г. Астана", "21"),
    "г.астана": ("г. Астана", "21"),
    "город астана": ("г. Астана", "21"),
    "г. шымкент": ("г. Шымкент", "22"),
    "г.шымкент": ("г. Шымкент", "22"),
    "город шымкент": ("г. Шымкент", "22"),
    "абай область": ("Область Абай", "23"),
    "область абай": ("Область Абай", "23"),
    "область жетісу": ("Область Жетісу", "24"),
    "жетысуская область": ("Область Жетісу", "24"),
    "жетісу облысы": ("Область Жетісу", "24"),
    "улытауская область": ("Область Ұлытау", "25"),
    "область ұлытау": ("Область Ұлытау", "25"),
}

REGION_SUFFIX_RE = re.compile(r"\s*\(\d{2}\)\s*$")
DOCUMENT_WORDS_RE = re.compile(
    r"(?i)\b(генеральн(?:ый|ого)\s+план|ген\s*план|гп|пдп|основной\s+чертеж)\b"
)
YEAR_AND_COPY_RE = re.compile(r"(?i)(?:\s*[\(\[]?\d{4}[\)\]]?|\s*\(\d+\)|\s*копия)+\s*$")


@dataclass(slots=True)
class LocationInfo:
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
    confidence: str = "low"
    notes: list[str] = field(default_factory=list)


def clean_label(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = value.replace("_", " ").replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip(" .-/\\")
    value = re.sub(r"\s+([,;])", r"\1", value)
    return value


def match_key(value: str) -> str:
    value = REGION_SUFFIX_RE.sub("", clean_label(value))
    value = value.casefold()
    value = re.sub(r"\s*\.\s*", ".", value)
    return value


def canonical_region(value: str, aliases: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    key = match_key(value)
    direct = aliases.get(key)
    if direct:
        return direct
    if key.endswith(" область"):
        return aliases.get(key)
    return None


def _looks_like_city(value: str) -> bool:
    key = match_key(value)
    return key.startswith(("г.", "город "))


def _looks_like_district(value: str) -> bool:
    key = match_key(value)
    return "район" in key or "р-н" in key


def _locality_from_stem(stem: str) -> str:
    value = clean_label(stem)
    value = DOCUMENT_WORDS_RE.sub(" ", value)
    value = re.sub(r"(?i)\b(корректировка|карта|схема)\b.*$", "", value)
    value = YEAR_AND_COPY_RE.sub("", value)
    return clean_label(value)


def load_aliases(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Alias file must contain a JSON object")
    return payload


def _district_match_key(value: str) -> str:
    value = re.sub(r"\(\d{2}-\d{3}\)", "", clean_label(value))
    value = re.sub(r"(?i)^(?:р-н\.?|район|г\.?|город|п\.)\s*", "", value)
    value = re.sub(r"(?i)\s+(?:район|р-н\.?)$", "", value)
    return match_key(value)


def load_egkn_catalog(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"regions": {}, "districts": {}}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("EGKN catalog must contain a JSON array")

    regions: dict[str, tuple[str, str]] = {}
    districts: dict[tuple[str, str], dict[str, str]] = {}
    for region in payload:
        if not isinstance(region, dict):
            continue
        code = str(region.get("code", "")).zfill(2)
        name = clean_label(str(region.get("name") or region.get("nameRu") or ""))
        name = REGION_SUFFIX_RE.sub("", name)
        if not code or not name:
            continue
        for candidate in {name, str(region.get("nameRu", ""))}:
            if candidate:
                regions[match_key(candidate)] = (name, code)
        for district in region.get("districts", []):
            if not isinstance(district, dict):
                continue
            display_name = clean_label(str(district.get("nameRu", "")))
            district_code = str(district.get("code", "")).zfill(3)
            district_type = re.sub(r"\s+", " ", str(district.get("type", ""))).strip()
            if not display_name:
                continue
            key = _district_match_key(display_name)
            districts[(code, key)] = {
                "code": district_code,
                "display": clean_label(f"{district_type} {display_name}"),
            }
    return {"regions": regions, "districts": districts}


def apply_egkn_catalog(location: LocationInfo, catalog: dict[str, Any]) -> LocationInfo:
    region = catalog.get("regions", {}).get(match_key(location.normalized_region))
    if region:
        location.normalized_region, location.region_code = region
        location.egkn_region = f"{location.normalized_region} ({location.region_code})"
        location.notes.append("region_matched_egkn_catalog")

    if location.region_code and location.normalized_district:
        district = catalog.get("districts", {}).get(
            (location.region_code, _district_match_key(location.normalized_district))
        )
        if district:
            location.district_code = district["code"]
            location.egkn_district = district["display"]
            location.notes.append("district_matched_egkn_catalog")
        else:
            location.notes.append("district_not_matched_egkn_catalog")
    return location


def build_region_aliases(overrides: dict[str, Any]) -> dict[str, tuple[str, str]]:
    aliases = dict(REGIONS)
    for source, target in overrides.get("regions", {}).items():
        if isinstance(target, str):
            name, _, code = target.partition("|")
        elif isinstance(target, dict):
            name = str(target.get("name", ""))
            code = str(target.get("code", ""))
        else:
            raise ValueError(f"Unsupported region alias value for {source!r}")
        if not name or not code:
            raise ValueError(f"Region alias {source!r} requires name and code")
        aliases[match_key(source)] = (clean_label(name), code.zfill(2))
    return aliases


def _apply_simple_alias(value: str, section: str, overrides: dict[str, Any]) -> str:
    aliases = overrides.get(section, {})
    wanted = match_key(value)
    for source, target in aliases.items():
        if match_key(source) == wanted:
            return clean_label(str(target))
    return clean_label(value)


def infer_location(member_path: str, overrides: dict[str, Any] | None = None) -> LocationInfo:
    overrides = overrides or {}
    aliases = build_region_aliases(overrides)
    parts = [clean_label(part) for part in PurePosixPath(member_path).parts if clean_label(part)]
    if not parts:
        return LocationInfo(notes=["empty_path"])

    filename = parts[-1]
    stem = clean_label(Path(filename).stem)
    directory_parts = parts[:-1]
    region_index = -1
    region_match: tuple[str, str] | None = None
    for index, part in enumerate(directory_parts):
        region_match = canonical_region(part, aliases)
        if region_match:
            region_index = index
            break

    if region_match is None:
        region_match = canonical_region(stem, aliases)
        if region_match:
            region_index = len(directory_parts)

    result = LocationInfo()
    if region_match is None:
        result.original_locality = stem
        result.normalized_locality = _locality_from_stem(stem)
        result.notes.append("region_not_recognized")
        return result

    result.original_region = (
        directory_parts[region_index] if region_index < len(directory_parts) else stem
    )
    result.normalized_region, result.region_code = region_match
    result.egkn_region = f"{result.normalized_region} ({result.region_code})"
    remaining = directory_parts[region_index + 1 :]

    if remaining:
        result.original_district = remaining[0]
        result.normalized_district = _apply_simple_alias(
            result.original_district, "districts", overrides
        )

    if result.normalized_district and _looks_like_city(result.normalized_district):
        result.original_locality = result.original_district
        result.normalized_locality = _apply_simple_alias(
            result.original_district, "localities", overrides
        )
        result.confidence = "high"
        result.notes.append("city_folder_used_as_locality")
    elif len(remaining) > 1:
        result.original_locality = remaining[-1]
        result.normalized_locality = _apply_simple_alias(
            result.original_locality, "localities", overrides
        )
        result.confidence = "high"
        result.notes.append("nested_locality_folder")
    else:
        result.original_locality = stem
        candidate = _locality_from_stem(stem)
        result.normalized_locality = _apply_simple_alias(candidate, "localities", overrides)
        result.confidence = "medium" if result.normalized_district else "low"
        result.notes.append("locality_inferred_from_filename")

    if result.normalized_district and not (
        _looks_like_district(result.normalized_district)
        or _looks_like_city(result.normalized_district)
    ):
        result.notes.append("district_label_requires_review")
        result.confidence = "medium"
    if not result.normalized_locality:
        result.notes.append("locality_not_recognized")
        result.confidence = "low"
    return result
