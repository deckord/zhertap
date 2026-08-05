from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.i18n import normalize_language


class SearchLike(Protocol):
    language: str
    status: str
    search_outcome: str | None
    error_message: str | None
    urban_plan_status: str
    urban_plan_message: str | None
    area_ha: float
    purpose: str
    district: str
    locality: str | None


@dataclass(frozen=True, slots=True)
class SearchExplanation:
    title: str
    body: str
    next_step: str
    next_step_title: str = "Что делать дальше"

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}\n\n{self.next_step_title}:\n{self.next_step}"


def _sotok(search: SearchLike) -> int:
    return round((search.area_ha or 0) * 100)


def _has_large_egkn_error(message: str) -> bool:
    lowered = message.lower()
    return "превысил лимит объектов" in lowered or "слишком большой слой" in lowered


def _has_missing_settlement_error(message: str) -> bool:
    return "населенный пункт не найден" in message.lower()


def explain_search_result(search: SearchLike) -> SearchExplanation:
    language = normalize_language(search.language)
    message = search.error_message or ""
    sotok = _sotok(search)

    if language == "kz":
        if _has_large_egkn_error(message):
            return SearchExplanation(
                title="Іздеу аяқталмады: аумақ тым үлкен",
                body=(
                    "ЖМБМК/ЕГКН бір сұрауда өте көп тіркелген учаске қайтарды. "
                    "Жүйе аумақты бөліктерге бөліп тексеруге тырысады, бірақ кейбір "
                    "ірі қалалық аудандар бәрібір тым ауыр болуы мүмкін."
                ),
                next_step=(
                    "Бүкіл ауданның орнына нақты қала/ауылды немесе кішірек аумақты "
                    "таңдаңыз. Егер мүмкіндік болса, жақын маңдағы басқа елді мекенді "
                    "тексеріңіз."
                ),
                next_step_title="Әрі қарай не істеу керек",
            )
        if _has_missing_settlement_error(message):
            return SearchExplanation(
                title="Елді мекен ЕГКН анықтамалығынан табылмады",
                body=(
                    "Таңдалған атау ЕГКН-дағы ресми атаумен сәйкес келмеді немесе бұл "
                    "аумақ бөлек елді мекен ретінде берілмейді."
                ),
                next_step=(
                    "Артқа қайтып, анықтамалықтан жақын атауды таңдаңыз немесе бүкіл "
                    "ауданды тексеріп көріңіз."
                ),
                next_step_title="Әрі қарай не істеу керек",
            )
        if search.search_outcome == "no_candidates":
            return SearchExplanation(
                title="Бұл параметрлер бойынша орын табылмады",
                body=(
                    f"Жүйе ЕГКН қабатынан {sotok} сотық жер толық орналасатын "
                    "геометриялық аралық таппады. Генплан тексерісі мұнда басталмайды: "
                    "алдымен кадастрлық картадан ықтимал орын табылуы керек."
                ),
                next_step=(
                    "Басқа елді мекенді таңдаңыз немесе мақсат/ауданды өзгертіп көріңіз. "
                    "Бұл нәтиже үшін төлем қажет емес."
                ),
                next_step_title="Әрі қарай не істеу керек",
            )
        if search.urban_plan_status == "unavailable":
            return SearchExplanation(
                title="Генплан/ЕЖЖ цифрлық қабаты жоқ",
                body=(
                    "Кадастрлық карта бойынша ықтимал орындар табылды, бірақ осы аумақ "
                    "үшін жүйеде қызыл сызықтар мен қала құрылысы шектеулерін автоматты "
                    "тексеретін жарамды цифрлық генплан/ЕЖЖ қабаты жоқ."
                ),
                next_step=(
                    "Алдын ала нәтижені генплансыз алуға болады, бірақ оны әкімдікте "
                    "немесе ресми генплан бойынша қолмен тексеру керек."
                ),
                next_step_title="Әрі қарай не істеу керек",
            )
        if search.urban_plan_status == "blocked":
            return SearchExplanation(
                title="Бас жоспар/ЕЖЖ табылған орындарды растаған жоқ",
                body=(
                    "ЕГКН бойынша ықтимал орындар табылды, бірақ қосылған цифрлық "
                    "бас жоспар/ЕЖЖ қабаты оларды таңдалған мақсатқа растаған жоқ. "
                    "Әдетте бұл квадрат рұқсат етілген аймаққа толық кірмегенін "
                    "немесе қала құрылысы шектеуіне түскенін білдіреді."
                ),
                next_step="Басқа елді мекенді немесе аудандық аумақты таңдаңыз. Төлем алынбайды.",
                next_step_title="Әрі қарай не істеу керек",
            )
        return SearchExplanation(
            title="Іздеу аяқталмады",
            body=message or "Жария сервистердің бірі уақытша жауап бермеді.",
            next_step="Іздеуді қайталап көріңіз немесе басқа аумақты таңдаңыз.",
            next_step_title="Әрі қарай не істеу керек",
        )

    if _has_large_egkn_error(message):
        return SearchExplanation(
            title="Поиск не завершился: территория слишком большая",
            body=(
                "ЕГКН вернул слишком много зарегистрированных участков для одного "
                "запроса. Система пытается обрабатывать такие зоны по частям, но "
                "крупные городские районы все равно могут быть слишком тяжелыми."
            ),
            next_step=(
                "Выберите конкретный населенный пункт или более узкую территорию вместо "
                "крупного района. Если есть несколько похожих названий, попробуйте "
                "соседний вариант из справочника."
            ),
        )
    if _has_missing_settlement_error(message):
        return SearchExplanation(
            title="Населенный пункт не найден в справочнике ЕГКН",
            body=(
                "Выбранное название не совпало с официальным названием в ЕГКН или эта "
                "территория не отдается как отдельный населенный пункт."
            ),
            next_step=(
                "Вернитесь к выбору населенного пункта и выберите ближайшее название из "
                "справочника. Если не уверены, попробуйте поиск по всему району."
            ),
        )
    if search.search_outcome == "no_candidates":
        return SearchExplanation(
            title="Подходящее место не найдено",
            body=(
                f"Система проверила кадастровую карту, но не нашла промежуток, куда "
                f"полностью помещается участок {sotok} соток рядом с участками нужного "
                "назначения. Проверка генплана здесь не запускалась: сначала должно "
                "найтись место по ЕГКН."
            ),
            next_step=(
                "Попробуйте другой населенный пункт, район или другую цель анализа. "
                "Оплата за такой результат не требуется."
            ),
        )
    if search.urban_plan_status == "unavailable":
        return SearchExplanation(
            title="Нет цифрового слоя генплана/ПДП",
            body=(
                "По кадастровой карте возможные места найдены, но для выбранной "
                "территории в системе нет пригодного цифрового слоя генплана/ПДП, "
                "по которому можно автоматически проверить красные линии и "
                "градостроительные ограничения."
            ),
            next_step=(
                "Можно получить предварительный результат без проверки генплана, но "
                "после этого обязательно сверить место в акимате или по официальному "
                "генплану населенного пункта."
            ),
        )
    if search.urban_plan_status == "blocked":
        return SearchExplanation(
            title="Генплан/ПДП не подтвердил найденные места",
            body=(
                "ЕГКН показал возможные промежутки, но подключенный цифровой слой "
                "генплана/ПДП не подтвердил их для выбранной цели. Обычно это значит, "
                "что квадрат не попал целиком в разрешенную зону либо пересек "
                "градостроительное ограничение."
            ),
            next_step="Выберите другой населенный пункт или район. Оплата не требуется.",
        )
    return SearchExplanation(
        title="Поиск не завершился",
        body=message or "Один из публичных сервисов временно не ответил.",
        next_step="Повторите поиск позже или выберите другую территорию.",
    )
