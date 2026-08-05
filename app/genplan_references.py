from dataclasses import dataclass
from typing import Protocol

from app.manual_genplans import manual_genplan_payload
from app.providers.egkn import normalize_name


class HasSearchScope(Protocol):
    region: str
    region_label: str | None
    district: str
    district_label: str | None
    locality: str | None
    locality_label: str | None


@dataclass(frozen=True, slots=True)
class GenplanReference:
    region: str
    district: str
    locality: str
    title_ru: str
    title_kz: str
    url: str


GENPLAN_REFERENCES: tuple[GenplanReference, ...] = (
    GenplanReference(
        region="г. Астана",
        district="*",
        locality="*",
        title_ru="Генеральный план города Астаны",
        title_kz="Астана қаласының бас жоспары",
        url="https://adilet.zan.kz/rus/docs/P2400000033",
    ),
    GenplanReference(
        region="г. Шымкент",
        district="*",
        locality="*",
        title_ru="РГИС города Шымкента: карта генплана и функциональных зон",
        title_kz="Шымкент қаласының РГАЖ: бас жоспар және функционалдық аймақтар картасы",
        url="https://geo-shym.kz/map/?access_token=&lang=ru",
    ),
    GenplanReference(
        region="Северо-Казахстанская область",
        district="*",
        locality="Петропавловск",
        title_ru="Генеральный план города Петропавловска",
        title_kz="Петропавл қаласының бас жоспары",
        url="https://adilet.zan.kz/rus/docs/P2200000722",
    ),
    GenplanReference(
        region="Актюбинская область",
        district="*",
        locality="Актобе",
        title_ru="Генеральный план города Актобе",
        title_kz="Ақтөбе қаласының бас жоспары",
        url="https://adilet.zan.kz/rus/docs/P2400000461",
    ),
    GenplanReference(
        region="Акмолинская область",
        district="*",
        locality="Кокшетау",
        title_ru="Генеральный план города Кокшетау",
        title_kz="Көкшетау қаласының бас жоспары",
        url="https://adilet.zan.kz/rus/docs/P080000986_",
    ),
    GenplanReference(
        region="Мангистауская область",
        district="*",
        locality="Актау",
        title_ru="Генеральный план города Актау",
        title_kz="Ақтау қаласының бас жоспары",
        url="https://adilet.zan.kz/rus/docs/P2500000609",
    ),
    GenplanReference(
        region="г. Алматы",
        district="*",
        locality="*",
        title_ru="Геоинформационная карта города Алматы",
        title_kz="Алматы қаласының геоақпараттық картасы",
        url="https://alag.kz/",
    ),
    GenplanReference(
        region="Алматинская область",
        district="*",
        locality="*",
        title_ru="Геопортал Алматинской области",
        title_kz="Алматы облысының геопорталы",
        url="https://map.almobl.kz/",
    ),
    GenplanReference(
        region="Акмолинская область",
        district="*",
        locality="*",
        title_ru="Геопортал Акмолинской области",
        title_kz="Ақмола облысының геопорталы",
        url="https://map.iaqmola.kz/",
    ),
)

GGK_REFERENCE = GenplanReference(
    region="*",
    district="*",
    locality="*",
    title_ru="Геопортал РГП Госградкадастр",
    title_kz="Мемқалақұрылыскадастр геопорталы",
    url="https://gov.ggk.kz/",
)


def _is_wildcard(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"", "*", "all"}


def _same(left: str | None, right: str | None) -> bool:
    if _is_wildcard(left):
        return True
    left_key = normalize_name(left or "")
    right_key = normalize_name(right or "")
    return bool(
        left_key
        and right_key
        and (left_key == right_key or left_key in right_key or right_key in left_key)
    )


def _scope_value(request: HasSearchScope, field: str) -> str | None:
    label = getattr(request, f"{field}_label", None)
    value = getattr(request, field, None)
    return label or value


def _specificity(reference: GenplanReference) -> int:
    return sum(
        1
        for value in (reference.region, reference.district, reference.locality)
        if not _is_wildcard(value)
    )


def genplan_reference_for_request(
    request: HasSearchScope,
    *,
    include_legal_documents: bool = False,
) -> GenplanReference:
    region = _scope_value(request, "region")
    district = _scope_value(request, "district")
    locality = _scope_value(request, "locality")
    matches = [
        reference
        for reference in GENPLAN_REFERENCES
        if _same(reference.region, region)
        and _same(reference.district, district)
        and (
            _same(reference.locality, locality)
            or (not locality and _same(reference.locality, district))
        )
    ]
    if not include_legal_documents:
        non_legal_matches = [
            reference
            for reference in matches
            if _reference_source_kind(reference) != "legal_document"
        ]
        matches = non_legal_matches
    if not matches:
        return GGK_REFERENCE
    return max(matches, key=_specificity)


def genplan_reference_payload(
    request: HasSearchScope,
    *,
    language: str | None = None,
    base_url: str | None = None,
    manual_files_root: str | None = None,
) -> dict[str, str]:
    manual_payload = manual_genplan_payload(
        request,
        language=language,
        base_url=base_url,
        configured_root=manual_files_root,
    )
    if manual_payload:
        return manual_payload
    reference = genplan_reference_for_request(request)
    selected = "kz" if language == "kz" else "ru"
    source_kind = _reference_source_kind(reference)
    return {
        "title": reference.title_kz if selected == "kz" else reference.title_ru,
        "url": reference.url,
        "source_kind": source_kind,
        "action_text": _reference_action_text(source_kind, selected),
    }


def _reference_source_kind(reference: GenplanReference) -> str:
    url = reference.url.casefold()
    title = f"{reference.title_ru} {reference.title_kz}".casefold()
    if "adilet.zan.kz" in url:
        return "legal_document"
    if "geo-shym.kz" in url or "ргис" in title or "ргаж" in title:
        return "interactive_map"
    if "geo" in url or "map." in url or "alag.kz" in url or "ggk.kz" in url:
        return "geoportal"
    return "official_source"


def _reference_action_text(source_kind: str, language: str) -> str:
    if language == "kz":
        return {
            "interactive_map": "Бас жоспар қабаттары бар картаны ашу",
            "legal_document": "Бас жоспар/ЕЖЖ ресми құжатын ашу",
            "geoportal": "Қолмен тексеру үшін геопорталды ашу",
            "official_source": "Ресми дереккөзді ашу",
        }.get(source_kind, "Ресми дереккөзді ашу")
    return {
        "interactive_map": "Открыть карту с генплан-слоями",
        "legal_document": "Открыть официальный документ генплана/ПДП",
        "geoportal": "Открыть геопортал для ручной проверки",
        "official_source": "Открыть официальный источник",
    }.get(source_kind, "Открыть официальный источник")
