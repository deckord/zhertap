import json
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from app.providers.egkn import normalize_name

MANIFEST_PATH = Path("app/data/manual_genplans.json")
DEFAULT_ROOT_CANDIDATES = (
    Path("C:/Users/medadmin/Documents/Codex/genplan/extracted"),
    Path("../../../genplan/extracted"),
    Path("/opt/land-scout/manual-genplans/extracted"),
)
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}


@dataclass(frozen=True, slots=True)
class ManualGenplanRecord:
    asset_id: str
    region: str
    district: str
    locality: str
    title: str
    relative_path: str
    filename: str
    extension: str
    media_type: str
    size_bytes: int
    confidence: str

    @property
    def url(self) -> str:
        return f"/manual-genplans/{self.asset_id}/{quote(self.filename)}"


class HasManualGenplanScope:
    region: str
    region_label: str | None
    district: str
    district_label: str | None
    locality: str | None
    locality_label: str | None


def _scope_value(request: HasManualGenplanScope, field: str) -> str | None:
    label = getattr(request, f"{field}_label", None)
    value = getattr(request, field, None)
    return label or value


def _same(left: str | None, right: str | None) -> bool:
    left_key = normalize_name(left or "")
    right_key = normalize_name(right or "")
    return bool(
        left_key
        and right_key
        and (left_key == right_key or left_key in right_key or right_key in left_key)
    )


def _title_from_record(record: ManualGenplanRecord) -> str:
    if record.locality:
        return f"Генплан/ПДП: {record.locality}"
    if record.district:
        return f"Генплан/ПДП: {record.district}"
    return f"Генплан/ПДП: {record.region}"


def _confidence_score(value: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get((value or "").casefold(), 0)


def _extension_score(value: str) -> int:
    extension = (value or "").casefold()
    if extension in {".jpg", ".jpeg", ".png"}:
        return 3
    if extension == ".pdf":
        return 2
    return 1


@lru_cache(maxsize=1)
def manual_genplan_records() -> tuple[ManualGenplanRecord, ...]:
    if not MANIFEST_PATH.exists():
        return ()
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    records = []
    for item in raw.get("records", []):
        extension = (item.get("extension") or "").casefold()
        if extension not in ALLOWED_EXTENSIONS:
            continue
        records.append(
            ManualGenplanRecord(
                asset_id=item["asset_id"],
                region=item.get("region") or "",
                district=item.get("district") or "",
                locality=item.get("locality") or "",
                title=item.get("title") or "",
                relative_path=item["relative_path"],
                filename=item["filename"],
                extension=extension,
                media_type=item.get("media_type") or "application/octet-stream",
                size_bytes=int(item.get("size_bytes") or 0),
                confidence=item.get("confidence") or "",
            )
        )
    return tuple(records)


def manual_genplan_for_request(request: HasManualGenplanScope) -> ManualGenplanRecord | None:
    region = _scope_value(request, "region")
    district = _scope_value(request, "district")
    locality = _scope_value(request, "locality")
    matches: list[tuple[int, ManualGenplanRecord]] = []
    for record in manual_genplan_records():
        if not _same(record.region, region):
            continue
        score = 10 + _confidence_score(record.confidence)
        if record.district and _same(record.district, district):
            score += 20
        elif record.district and district:
            continue
        if locality and record.locality and _same(record.locality, locality):
            score += 50
        elif locality and record.locality:
            continue
        elif not locality and record.locality and district and not _same(record.locality, district):
            score -= 8
        score += _extension_score(record.extension)
        score -= min(record.size_bytes // (100 * 1024 * 1024), 8)
        matches.append((score, record))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def manual_genplan_by_asset_id(asset_id: str) -> ManualGenplanRecord | None:
    for record in manual_genplan_records():
        if record.asset_id == asset_id:
            return record
    return None


def manual_genplan_roots(configured_root: str | None = None) -> list[Path]:
    candidates: list[Path]
    if configured_root:
        candidates = [Path(configured_root)]
    else:
        candidates = list(DEFAULT_ROOT_CANDIDATES)
    resolved: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            path = candidate.expanduser().resolve()
        except OSError:
            continue
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        if path.exists() and path.is_dir():
            resolved.append(path)
    return resolved


def resolve_manual_genplan_file(
    record: ManualGenplanRecord,
    *,
    configured_root: str | None = None,
) -> Path | None:
    relative = PurePosixPath(record.relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    for root in manual_genplan_roots(configured_root):
        for candidate in _manual_genplan_path_candidates(root, relative):
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.exists() and candidate.is_file():
                return candidate
    return None


def _manual_genplan_path_candidates(root: Path, relative: PurePosixPath) -> Iterator[Path]:
    direct = (root / Path(*relative.parts)).resolve()
    yield direct
    try:
        encoded_parts = [
            part.encode("cp1251").decode("utf-8", errors="surrogateescape")
            for part in relative.parts
        ]
    except (UnicodeEncodeError, UnicodeDecodeError):
        return
    encoded = (root / Path(*encoded_parts)).resolve()
    if encoded != direct:
        yield encoded


def manual_genplan_payload(
    request: HasManualGenplanScope,
    *,
    base_url: str | None = None,
    configured_root: str | None = None,
    language: str | None = None,
) -> dict[str, str] | None:
    record = manual_genplan_for_request(request)
    if record is None:
        return None
    if resolve_manual_genplan_file(record, configured_root=configured_root) is None:
        return None
    title = record.title or _title_from_record(record)
    url = record.url
    if base_url:
        url = base_url.rstrip("/") + url
    if language == "kz":
        action_text = "Бас жоспар/ЕЖЖ картасын ашу"
    else:
        action_text = "Открыть карту генплана/ПДП"
    return {
        "title": title,
        "url": url,
        "source_kind": "manual_plan_file",
        "action_text": action_text,
        "asset_id": record.asset_id,
    }
