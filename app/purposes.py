LPH = "ЛПХ"
LPH_NEW = "ЛПХ(новый поиск)"
GARDENING = "Садоводство"
ALL_PURPOSES = "all"
LPH_HOUSEHOLD_LAYER = "ЛПХ:household"
LPH_FIELD_LAYER = "ЛПХ:field"
SUPPORTED_PURPOSES = {LPH, LPH_NEW, GARDENING}

HOUSEHOLD = "household"
FIELD = "field"
IRRIGATED = "irrigated"
NON_IRRIGATED = "non_irrigated"
GARDENING_SMALL_AREA_HA = 0.06
GARDENING_STANDARD_AREA_HA = 0.12
GARDENING_ALLOWED_AREAS_HA = {GARDENING_SMALL_AREA_HA, GARDENING_STANDARD_AREA_HA}


def normalize_purpose(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace("ё", "е")
    if "садов" in normalized or "бағбан" in normalized:
        return GARDENING
    if "нов" in normalized or "жаңа" in normalized:
        return LPH_NEW
    return LPH


def purpose_family(purpose: str) -> str:
    return GARDENING if normalize_purpose(purpose) == GARDENING else LPH


def normalize_allotment_type(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return FIELD if normalized in {FIELD, "полевой", "далалық"} else HOUSEHOLD


def normalize_irrigation_type(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return IRRIGATED if normalized in {IRRIGATED, "орошаемый", "суармалы"} else NON_IRRIGATED


def purpose_area_ha(
    purpose: str,
    irrigation_type: str | None = None,
    requested_area_ha: float | None = None,
) -> float:
    normalized = normalize_purpose(purpose)
    if normalized == GARDENING:
        if requested_area_ha is not None and requested_area_ha <= GARDENING_SMALL_AREA_HA:
            return GARDENING_SMALL_AREA_HA
        return GARDENING_STANDARD_AREA_HA
    if normalized == LPH_NEW:
        return 0.15 if normalize_irrigation_type(irrigation_type) == IRRIGATED else 0.25
    return 0.10


def purpose_sotok(purpose: str, irrigation_type: str | None = None) -> int:
    return round(purpose_area_ha(purpose, irrigation_type) * 100)


def purpose_label(purpose: str, language: str = "ru") -> str:
    normalized = normalize_purpose(purpose)
    if language == "kz":
        if normalized == GARDENING:
            return "Бағбандық"
        if normalized == LPH_NEW:
            return "ЖҚШ (жаңа іздеу)"
        return "Жеке қосалқы шаруашылық (ЖҚШ)"
    return normalized


def allotment_label(value: str | None, language: str = "ru") -> str:
    field = normalize_allotment_type(value) == FIELD
    if language == "kz":
        return "далалық телім" if field else "үй іргесіндегі телім"
    return "полевой надел" if field else "приусадебный надел"


def irrigation_label(value: str | None, language: str = "ru") -> str:
    irrigated = normalize_irrigation_type(value) == IRRIGATED
    if language == "kz":
        return "суармалы жер" if irrigated else "суарылмайтын жер"
    return "орошаемая земля" if irrigated else "неорошаемая земля"


def purpose_activity_phrase(
    purpose: str,
    language: str = "ru",
    allotment_type: str | None = None,
) -> str:
    normalized = normalize_purpose(purpose)
    if language == "kz":
        if normalized == GARDENING:
            return "бағбандық жүргізу"
        if normalized == LPH_NEW:
            return f"ЖҚШ жүргізу ({allotment_label(allotment_type, language)})"
        return "жеке қосалқы шаруашылық жүргізу"
    if normalized == GARDENING:
        return "ведения садоводства"
    if normalized == LPH_NEW:
        return "ведения личного подсобного хозяйства"
    return "ведения личного подсобного хозяйства"


def parcel_matches_purpose(land_use: str, purpose: str) -> bool:
    normalized = (land_use or "").lower().replace("ё", "е")
    if purpose_family(purpose) == GARDENING:
        return "садовод" in normalized
    return "подсоб" in normalized or "лпх" in normalized
