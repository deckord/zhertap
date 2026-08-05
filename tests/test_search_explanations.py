from app.models import SearchRequest
from app.search_explanations import explain_search_result


def test_explains_large_egkn_layer_with_next_step() -> None:
    search = SearchRequest(
        language="ru",
        region="Актюбинская область",
        district="Актобе",
        locality="район Алматы",
        purpose="Садоводство",
        area_ha=0.12,
        status="failed",
        search_outcome="egkn_unavailable",
        error_message="Слой ЕГКН превысил лимит объектов; сузьте поиск",
    )

    explanation = explain_search_result(search)

    assert "территория слишком большая" in explanation.title
    assert "ЕГКН вернул слишком много" in explanation.body
    assert "конкретный населенный пункт" in explanation.next_step


def test_explains_no_candidates_without_genplan_confusion() -> None:
    search = SearchRequest(
        language="ru",
        region="Актюбинская область",
        district="Каргалинский район",
        locality="земли с.Петропавловка",
        purpose="ЛПХ(новый поиск)",
        area_ha=0.15,
        status="ready",
        search_outcome="no_candidates",
        urban_plan_status="pending",
    )

    explanation = explain_search_result(search)

    assert "Подходящее место не найдено" == explanation.title
    assert "15 соток" in explanation.body
    assert "Проверка генплана здесь не запускалась" in explanation.body
    assert "Оплата" in explanation.next_step


def test_explains_missing_urban_plan_layer() -> None:
    search = SearchRequest(
        language="ru",
        region="Актюбинская область",
        district="Мартукский район",
        locality="с.Каратогай",
        purpose="ЛПХ(новый поиск)",
        area_ha=0.15,
        status="ready",
        urban_plan_status="unavailable",
    )

    explanation = explain_search_result(search)

    assert "Нет цифрового слоя генплана" in explanation.title
    assert "красные линии" in explanation.body
    assert "акимате" in explanation.next_step
