from __future__ import annotations

from dataclasses import dataclass

from app.models import UrbanPlanStatus


@dataclass(frozen=True, slots=True)
class UrbanPlanClientStatus:
    status: str
    title: str
    short: str
    detail: str
    tone: str


def urban_plan_client_status(
    status: str | None,
    *,
    language: str | None = None,
    reference_source_kind: str | None = None,
) -> UrbanPlanClientStatus:
    key = (status or UrbanPlanStatus.pending.value).strip().lower()
    if language == "kz":
        return _status_kz(key, reference_source_kind=reference_source_kind)
    return _status_ru(key, reference_source_kind=reference_source_kind)


def urban_plan_badge_payload(
    status: str | None,
    *,
    language: str | None = None,
    reference_source_kind: str | None = None,
) -> dict[str, str]:
    item = urban_plan_client_status(
        status,
        language=language,
        reference_source_kind=reference_source_kind,
    )
    return {
        "status": item.status,
        "title": item.title,
        "short": item.short,
        "detail": item.detail,
        "tone": item.tone,
    }


def telegram_urban_plan_line(
    status: str | None,
    *,
    language: str | None = None,
    reference_source_kind: str | None = None,
) -> str:
    item = urban_plan_client_status(
        status,
        language=language,
        reference_source_kind=reference_source_kind,
    )
    if language == "kz":
        return f"🏙 Бас жоспар/ЕЖЖ: {item.short}. {item.detail}"
    return f"🏙 Генплан/ПДП: {item.short}. {item.detail}"


def _has_manual_plan_file(reference_source_kind: str | None) -> bool:
    return (reference_source_kind or "").strip().lower() == "manual_plan_file"


def _status_ru(key: str, *, reference_source_kind: str | None = None) -> UrbanPlanClientStatus:
    if key == UrbanPlanStatus.passed.value:
        return UrbanPlanClientStatus(
            status=key,
            title="Генплан/ПДП проверен автоматически",
            short="проверяется автоматически",
            detail=(
                "Для этой территории подключен официальный цифровой слой. "
                "Система сверила разрешенную зону, запретные зоны и красные линии."
            ),
            tone="success",
        )
    if key == UrbanPlanStatus.blocked.value:
        return UrbanPlanClientStatus(
            status=key,
            title="Генплан/ПДП не подтвердил это место",
            short="не прошло генплан",
            detail=(
                "Официальный цифровой слой подключен, но найденное место не попало "
                "в разрешенную зону для выбранной цели или попало под ограничение."
            ),
            tone="danger",
        )
    if key in {UrbanPlanStatus.unavailable.value, UrbanPlanStatus.waived.value}:
        if _has_manual_plan_file(reference_source_kind):
            return UrbanPlanClientStatus(
                status=key,
                title="Есть карта генплана/ПДП для ручной сверки",
                short="есть карта для сверки",
                detail=(
                    "Автоматическая проверка по генплану здесь еще не включена, "
                    "но для выбранной территории есть файл карты генплана/ПДП. "
                    "Откройте его и сверьте найденное место вручную."
                ),
                tone="warning",
            )
        return UrbanPlanClientStatus(
            status=key,
            title="Генплан/ПДП не подключен",
            short="нужна ручная сверка",
            detail=(
                "Анализ выполнен по ЕГКН, дорогам, объектам и открытым данным, "
                "но без автоматической сверки с генпланом/ПДП."
            ),
            tone="warning",
        )
    return UrbanPlanClientStatus(
        status=key,
        title="Генплан/ПДП ожидает проверки",
        short="проверка ожидается",
        detail="Статус появится после обработки заявки.",
        tone="neutral",
    )


def _status_kz(key: str, *, reference_source_kind: str | None = None) -> UrbanPlanClientStatus:
    if key == UrbanPlanStatus.passed.value:
        return UrbanPlanClientStatus(
            status=key,
            title="Бас жоспар/ЕЖЖ автоматты түрде тексерілді",
            short="автоматты түрде тексерілді",
            detail=(
                "Бұл аумаққа ресми цифрлық қабат қосылған. Жүйе рұқсат етілген "
                "аймақты, тыйым салынған аймақтарды және қызыл сызықтарды тексерді."
            ),
            tone="success",
        )
    if key == UrbanPlanStatus.blocked.value:
        return UrbanPlanClientStatus(
            status=key,
            title="Бас жоспар/ЕЖЖ бұл орынды растаған жоқ",
            short="бас жоспардан өтпеді",
            detail=(
                "Ресми цифрлық қабат қосылған, бірақ табылған орын таңдалған мақсатқа "
                "рұқсат етілген аймаққа кірмеді немесе шектеуге түсті."
            ),
            tone="danger",
        )
    if key in {UrbanPlanStatus.unavailable.value, UrbanPlanStatus.waived.value}:
        if _has_manual_plan_file(reference_source_kind):
            return UrbanPlanClientStatus(
                status=key,
                title="Қолмен тексеруге арналған бас жоспар/ЕЖЖ картасы бар",
                short="қолмен сверкаға карта бар",
                detail=(
                    "Бұл аумақта бас жоспар бойынша автоматты тексеру әлі қосылмаған, "
                    "бірақ бас жоспар/ЕЖЖ картасының файлы бар. Табылған орынды картадан "
                    "қолмен салыстырып тексеріңіз."
                ),
                tone="warning",
            )
        return UrbanPlanClientStatus(
            status=key,
            title="Бас жоспар/ЕЖЖ қосылмаған",
            short="қолмен тексеру керек",
            detail=(
                "Талдау ЕГКН, жолдар, объектілер және ашық деректер бойынша жасалды, "
                "бірақ бас жоспар/ЕЖЖ автоматты түрде тексерілген жоқ."
            ),
            tone="warning",
        )
    return UrbanPlanClientStatus(
        status=key,
        title="Бас жоспар/ЕЖЖ тексерісі күтілуде",
        short="тексеріс күтілуде",
        detail="Өтінім өңделгеннен кейін статус көрсетіледі.",
        tone="neutral",
    )
