from app.models import SearchRequest
from app.search_explanations import explain_search_result
from app.web import _search_explanation_payload, _search_status_message


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


def test_running_provider_backpressure_is_not_rendered_as_final_failure() -> None:
    search = SearchRequest(
        language="ru",
        region="Акмолинская область",
        district="Бурабайский район",
        locality="Бурабай",
        purpose="ЛПХ(новый поиск)",
        area_ha=0.15,
        status="queued",
        progress=20,
        error_message="Публичный сервис egkn временно ограничил запросы; повтор через 30 сек.",
    )

    message = _search_status_message(search)

    assert "Заявка остается в очереди" in message
    assert "автоматически" in message
    assert _search_explanation_payload(search) is None
