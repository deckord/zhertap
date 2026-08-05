import re

from app.schemas import SearchCreate

DISTRICT_ALIASES = {
    "бурабай": "Бурабайский район",
    "боровое": "Бурабайский район",
    "бурабайский": "Бурабайский район",
    "зеренда": "Зерендинский район",
    "зерендинский": "Зерендинский район",
    "целиноград": "Целиноградский район",
    "целиноградский": "Целиноградский район",
    "аршалы": "Аршалынский район",
    "аршалынский": "Аршалынский район",
    "шортанды": "Шортандинский район",
    "шортандинский": "Шортандинский район",
}

LOCALITIES = [
    "Златополье",
    "Мадениет",
    "Веденовка",
    "Акылбай",
    "Айдабол",
    "Симферополь",
    "Викторовка",
    "Еленовка",
    "Молодежное",
    "Азат",
    "Зеренда",
    "Бурабай",
    "Боровое",
    "Талапкер",
    "Косшы",
]

LOCALITY_PATTERN = re.compile(
    r"(?:населенный\s+пункт|н\.?\s*п\.?|село|поселок|аул|город|г\.)\s+"
    r"([а-яё-]+(?:\s+[а-яё-]+){0,3})",
    re.IGNORECASE,
)


def parse_search_text(text: str, **telegram_fields: str | None) -> SearchCreate:
    normalized = text.lower().replace("ё", "е")
    district = next(
        (canonical for alias, canonical in DISTRICT_ALIASES.items() if alias in normalized),
        "",
    )
    if not district:
        match = re.search(r"([а-я-]+)\s+(?:район|р-н)", normalized)
        district = f"{match.group(1).title()} район" if match else "Не указан"

    locality_match = LOCALITY_PATTERN.search(text)
    locality = locality_match.group(1).strip().title() if locality_match else None
    if locality is None:
        locality = next((name for name in LOCALITIES if name.lower() in normalized), None)

    if locality is None:
        parts = [part.strip() for part in re.split(r"[,;\n]", text) if part.strip()]
        district_index = next(
            (
                index
                for index, part in enumerate(parts)
                if re.search(r"\b(?:район|р-н)\b", part.lower())
            ),
            None,
        )
        if district_index is not None and district_index + 1 < len(parts):
            candidate = parts[district_index + 1]
            forbidden = r"\b(?:лпх|сот|га|гектар|электр|вода|дорог|кладбищ|септик)"
            if not re.search(forbidden, candidate.lower()) and len(candidate.split()) <= 4:
                locality = candidate.title()

    area_ha = 0.10
    sotka_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:сот|соток|сотки)", normalized)
    hectare_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:га|гектар)", normalized)
    if sotka_match:
        area_ha = float(sotka_match.group(1).replace(",", ".")) / 100
    elif hectare_match:
        area_ha = float(hectare_match.group(1).replace(",", "."))

    return SearchCreate(
        region="Акмолинская область" if "акмол" in normalized else "Казахстан",
        district=district,
        locality=locality,
        area_ha=area_ha,
        cemetery_buffer_m=0,
        raw_query=text,
        **telegram_fields,
    )
