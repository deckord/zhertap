from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import UrbanPlanSource
from tools.genplan_ggk import list_ggk_documents
from tools.genplan_ggk.builder import CATALOG_URL, PROFILE_CONFIG, SOURCE_AUTHORITY

GGK_PLATFORM = "ggk_wfs"
GGK_API_BASE_URL = "https://gov.ggk.kz/geoserver/ows"
SMART_GEOHUB_PLATFORM = "smart_geohub"
SMART_GEOHUB_GEOMETRY_STATUS = "geometry_found"
SMART_GEOHUB_NO_FEATURES_STATUS = "no_features"
SMART_GEOHUB_PORTALS: tuple[dict[str, str], ...] = (
    {"region": "Акмолинская область", "base_url": "https://map.iaqmola.kz/"},
    {"region": "Алматинская область", "base_url": "https://map.almobl.kz/"},
    {"region": "Западно-Казахстанская область", "base_url": "https://map.e-batys.kz/"},
    {"region": "Область Жетісу", "base_url": "https://map.e-zhetisu.kz/"},
    {"region": "Кызылординская область", "base_url": "https://orda.geoportal.kz/"},
    {"region": "Мангистауская область", "base_url": "https://map.e-mangistau.kz/"},
    {"region": "Улытауская область", "base_url": "https://map.iulytau.kz/"},
    {"region": "Туркестанская область", "base_url": "https://map.iturkistan.kz/"},
)
URBAN_PLAN_COLLECTION_PREFIXES = (
    "gpzone",
    "pdpzone",
    "gpreg",
    "pdpreg",
    "gpgr",
    "genplan",
)
URBAN_PLAN_COLLECTION_TERMS = (
    "генплан",
    "пдп",
    "красн",
    "желтые линии",
    "redline",
)


def sync_ggk_urban_plan_sources(
    session: Session,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    documents = rows if rows is not None else list_ggk_documents()
    now = datetime.now(UTC)
    stats = {"seen": len(documents), "created": 0, "updated": 0, "skipped": 0}
    for document in documents:
        external_id = str(document.get("id") or "").strip()
        if not external_id:
            stats["skipped"] += 1
            continue
        source = session.scalar(
            select(UrbanPlanSource).where(
                UrbanPlanSource.platform == GGK_PLATFORM,
                UrbanPlanSource.external_id == external_id,
            )
        )
        created = source is None
        if source is None:
            source = UrbanPlanSource(
                platform=GGK_PLATFORM,
                external_id=external_id,
                source_type="digital_vector",
            )
            session.add(source)
        _apply_ggk_document(source, document, checked_at=now)
        stats["created" if created else "updated"] += 1
    session.commit()
    return stats


def sync_smart_geohub_urban_plan_sources(
    session: Session,
    *,
    portals: Iterable[dict[str, str]] | None = None,
    catalogs: dict[str, Any] | None = None,
) -> dict[str, int]:
    now = datetime.now(UTC)
    stats = {"portals": 0, "seen": 0, "created": 0, "updated": 0, "failed": 0}
    processed_external_ids: set[str] = set()
    for portal in portals or SMART_GEOHUB_PORTALS:
        base_url = portal["base_url"].rstrip("/") + "/"
        region = portal.get("region", "")
        stats["portals"] += 1
        try:
            catalog = (
                catalogs[base_url]
                if catalogs is not None and base_url in catalogs
                else _fetch_smart_geohub_catalog(base_url)
            )
        except Exception:
            stats["failed"] += 1
            continue
        for item in _smart_geohub_collections(catalog):
            collection = _clean(item.get("collection"))
            if not collection:
                continue
            external_id = f"{_host(base_url)}:{collection}"
            if external_id in processed_external_ids:
                continue
            processed_external_ids.add(external_id)
            source = session.scalar(
                select(UrbanPlanSource).where(
                    UrbanPlanSource.platform == SMART_GEOHUB_PLATFORM,
                    UrbanPlanSource.external_id == external_id,
                )
            )
            created = source is None
            if source is None:
                source = UrbanPlanSource(
                    platform=SMART_GEOHUB_PLATFORM,
                    external_id=external_id,
                    source_type="digital_vector",
                )
                session.add(source)
            _apply_smart_geohub_collection(
                source,
                item,
                base_url=base_url,
                region=region,
                checked_at=now,
            )
            stats["seen"] += 1
            stats["created" if created else "updated"] += 1
    session.commit()
    return stats


def probe_smart_geohub_urban_plan_sources(
    session: Session,
    *,
    limit: int = 50,
    client: Any | None = None,
) -> dict[str, int]:
    """Check whether Smart GeoHub catalog entries expose feature geometry.

    This is intentionally a probe, not an import. A geometry_found source still
    needs semantic mapping, release preparation, and QA before it can be used in
    client search.
    """

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=25.0, follow_redirects=True)
    stats = {
        "checked": 0,
        "geometry_found": 0,
        "no_features": 0,
        "failed": 0,
        "skipped": 0,
    }
    now = datetime.now(UTC)
    sources = session.scalars(
        select(UrbanPlanSource)
        .where(
            UrbanPlanSource.platform == SMART_GEOHUB_PLATFORM,
            UrbanPlanSource.import_status != "imported",
        )
        .order_by(UrbanPlanSource.last_checked_at.asc(), UrbanPlanSource.id.asc())
        .limit(limit)
    ).all()
    try:
        for source in sources:
            base_url = _clean(source.source_url) or _source_base_from_api(source.api_base_url)
            collection = _source_collection(source)
            if not base_url or not collection:
                stats["skipped"] += 1
                continue
            stats["checked"] += 1
            try:
                probe = _probe_smart_geohub_collection(
                    client,
                    base_url=base_url,
                    collection=collection,
                )
            except Exception as exc:
                source.last_checked_at = now
                source.last_error = str(exc)[:1000]
                stats["failed"] += 1
                continue
            source.last_checked_at = now
            source.last_error = None
            source.layer_count = probe["total"]
            if probe["geometry_type"]:
                source.coverage_status = SMART_GEOHUB_GEOMETRY_STATUS
                source.notes = (
                    "Smart GeoHub collection exposes feature geometry. "
                    "Next step: semantic mapping, release build, independent QA, then import."
                )
                stats["geometry_found"] += 1
            else:
                source.coverage_status = SMART_GEOHUB_NO_FEATURES_STATUS
                source.notes = (
                    "Smart GeoHub collection was found in catalog, but no sample feature "
                    "with geometry was returned during probe."
                )
                stats["no_features"] += 1
            source.raw_payload_json = _merge_raw_probe(source.raw_payload_json, probe)
        session.commit()
    finally:
        if own_client:
            client.close()
    return stats


def _apply_ggk_document(
    source: UrbanPlanSource,
    document: dict[str, Any],
    *,
    checked_at: datetime,
) -> None:
    title = _clean(document.get("title")) or f"Генплан ГГК #{document.get('id')}"
    locality = _clean(document.get("locality"))
    number = _clean(document.get("number"))
    date = _clean(document.get("date"))
    deactivation_date = _clean(document.get("deactivation_date"))
    source.region = ""
    source.district = ""
    source.locality = locality
    source.title = title
    source.approval_document = number
    source.approval_date = date
    source.source_authority = SOURCE_AUTHORITY
    source.source_url = CATALOG_URL
    source.api_base_url = GGK_API_BASE_URL
    source.profiles_json = json.dumps(sorted(PROFILE_CONFIG), ensure_ascii=False)
    source.collections_json = json.dumps(
        ["gp_documents", "gp_functional_zones", "gp_red_lines"],
        ensure_ascii=False,
    )
    source.coverage_status = "archived" if deactivation_date else "digital_found"
    source.last_checked_at = checked_at
    source.last_error = None
    source.notes = _notes(document)
    source.raw_payload_json = json.dumps(document, ensure_ascii=False, sort_keys=True)


def _fetch_smart_geohub_catalog(base_url: str) -> Any:
    url = base_url.rstrip("/") + "/api/catalog"
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        response = client.get(url, params={"context[admterr_id]": "kz", "lang": "ru"})
        response.raise_for_status()
        return response.json()


def _probe_smart_geohub_collection(
    client: Any,
    *,
    base_url: str,
    collection: str,
) -> dict[str, Any]:
    api_base = base_url.rstrip("/") + "/api/"
    list_response = client.get(
        api_base + "list",
        params={
            "collection": collection,
            "context[admterr_id]": "kz",
            "lang": "ru",
            "limit": 1,
        },
    )
    list_response.raise_for_status()
    payload = list_response.json()
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        raise ValueError("Smart GeoHub list response has no features array")
    total = _int_or_zero(payload.get("total")) or len(features)
    sample_feature = next((item for item in features if _clean(item.get("id"))), None)
    if sample_feature is None:
        return {
            "collection": collection,
            "total": total,
            "sample_feature_id": "",
            "sample_collection": "",
            "geometry_type": "",
            "geometry_bbox": None,
        }
    feature_id = _clean(sample_feature.get("id"))
    sample_collection = _clean(sample_feature.get("collection")) or collection
    geometry = _fetch_smart_geohub_geometry(
        client,
        api_base=api_base,
        collection=collection,
        feature_id=feature_id,
    )
    if geometry is None and sample_collection != collection:
        geometry = _fetch_smart_geohub_geometry(
            client,
            api_base=api_base,
            collection=sample_collection,
            feature_id=feature_id,
        )
    return {
        "collection": collection,
        "total": total,
        "sample_feature_id": feature_id,
        "sample_collection": sample_collection,
        "geometry_type": _clean((geometry or {}).get("type")),
        "geometry_bbox": (geometry or {}).get("bbox"),
    }


def _fetch_smart_geohub_geometry(
    client: Any,
    *,
    api_base: str,
    collection: str,
    feature_id: str,
) -> dict[str, Any] | None:
    response = client.get(
        api_base + "geometry",
        params={
            "collection": collection,
            "feature_id": feature_id,
            "lang": "ru",
        },
    )
    if response.status_code == 400:
        return None
    response.raise_for_status()
    geometry = response.json()
    if isinstance(geometry, dict):
        return geometry
    return None


def _smart_geohub_collections(catalog: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            collection = _clean(value.get("collection"))
            haystack = " ".join(
                _clean(value.get(key))
                for key in ("collection", "name", "name_kk", "title", "label")
            ).casefold()
            if collection and _looks_like_urban_plan_collection(collection, haystack):
                result.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(catalog)
    return result


def _looks_like_urban_plan_collection(collection: str, haystack: str) -> bool:
    collection_key = collection.casefold()
    return collection_key.startswith(URBAN_PLAN_COLLECTION_PREFIXES) or any(
        term in haystack for term in URBAN_PLAN_COLLECTION_TERMS
    )


def _source_collection(source: UrbanPlanSource) -> str:
    try:
        collections = json.loads(source.collections_json or "[]")
    except json.JSONDecodeError:
        collections = []
    if isinstance(collections, list) and collections:
        return _clean(collections[0])
    if ":" in source.external_id:
        return _clean(source.external_id.rsplit(":", 1)[1])
    return ""


def _source_base_from_api(api_base_url: str) -> str:
    parsed = urlparse(_clean(api_base_url))
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/"


def _apply_smart_geohub_collection(
    source: UrbanPlanSource,
    item: dict[str, Any],
    *,
    base_url: str,
    region: str,
    checked_at: datetime,
) -> None:
    collection = _clean(item.get("collection"))
    name = _clean(item.get("name")) or collection
    source.region = region
    source.district = ""
    source.locality = ""
    source.title = name
    source.approval_document = ""
    source.approval_date = ""
    source.source_authority = f"Региональный геопортал Smart GeoHub: {_host(base_url)}"
    source.source_url = base_url
    source.api_base_url = base_url.rstrip("/") + "/api/"
    source.collections_json = json.dumps([collection], ensure_ascii=False)
    source.profiles_json = json.dumps([], ensure_ascii=False)
    source.coverage_status = "catalog_found"
    source.last_checked_at = checked_at
    source.last_error = None
    source.notes = (
        "Найдена коллекция градостроительного каталога Smart GeoHub. "
        "Нужно проверить покрытие, семантику зон и подготовить QA-релиз."
    )
    source.raw_payload_json = json.dumps(item, ensure_ascii=False, sort_keys=True)


def _notes(document: dict[str, Any]) -> str:
    parts = [
        "Найдено в официальном каталоге АИС ГГК.",
        "Для автоматической проверки нужен build релиза и независимое QA.",
    ]
    status_id = document.get("status_id")
    if status_id not in (None, ""):
        parts.append(f"status_id: {status_id}.")
    deactivation_date = _clean(document.get("deactivation_date"))
    if deactivation_date:
        parts.append(f"Есть дата деактивации: {deactivation_date}.")
    return " ".join(parts)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _host(url: str) -> str:
    return urlparse(url).hostname or url


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _merge_raw_probe(raw_payload_json: str | None, probe: dict[str, Any]) -> str:
    try:
        raw = json.loads(raw_payload_json or "{}")
    except json.JSONDecodeError:
        raw = {"catalog": raw_payload_json}
    if not isinstance(raw, dict):
        raw = {"catalog": raw}
    raw["probe"] = probe
    return json.dumps(raw, ensure_ascii=False, sort_keys=True)
