from app.providers.egkn import normalize_name
from app.purposes import GARDENING, normalize_purpose

KOKSHETAU_GARDENING_NOTICE = (
    "https://www.gov.kz/memleket/entities/ozo-saulet/press/article/details/236719"
)


def legal_restriction_reason(
    *,
    region: str,
    district: str,
    locality: str | None,
    purpose: str,
    language: str = "ru",
) -> str | None:
    if normalize_purpose(purpose) != GARDENING:
        return None
    scope = " ".join(
        [normalize_name(region), normalize_name(district), normalize_name(locality or "")]
    )
    if "кокшетау" not in scope:
        return None
    if language == "kz":
        return (
            "Көкшетау қаласында 2026 жылғы 17 маусымнан бастап бас жоспарда бағбандық "
            "аймақтары болмауына байланысты бағбандық үшін жер учаскелерін беру тоқтатылды. "
            f"Ресми хабарлама: {KOKSHETAU_GARDENING_NOTICE}"
        )
    return (
        "В Кокшетау с 17 июня 2026 года предоставление земельных участков для "
        "садоводства приостановлено, поскольку в графической части генерального плана "
        "нет зон садоводства. "
        f"Официальное сообщение: {KOKSHETAU_GARDENING_NOTICE}"
    )
