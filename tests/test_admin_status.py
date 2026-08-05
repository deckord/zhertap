from app.main import admin_search_status
from app.models import SearchStatus
from tests.test_free_preview import add_request, build_session


def test_ready_web_request_status_does_not_claim_telegram_delivery() -> None:
    with build_session() as session:
        request = add_request(session, user_id="web-status", candidate_count=2)
        request.telegram_user_id = None
        request.telegram_chat_id = None
        request.web_account_id = "web-account-1"
        request.status = SearchStatus.ready.value
        session.commit()

        status = admin_search_status(request)

        assert status["label"] == "Результат готов"
        assert "отправка в Telegram не требуется" in status["detail"]
