from types import SimpleNamespace

import pytest

import app.sms as sms
from app.config import settings


class ResponseStub:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def configure_green_api(monkeypatch) -> None:
    monkeypatch.setattr(settings, "green_api_enabled", True)
    monkeypatch.setattr(settings, "green_api_base_url", "https://7201.api.green-api.com")
    monkeypatch.setattr(settings, "green_api_id_instance", "123456789")
    monkeypatch.setattr(settings, "green_api_token_instance", "secret-token")
    monkeypatch.setattr(settings, "green_api_timeout_seconds", 15)
    monkeypatch.setattr(settings, "green_api_delivery_timeout_seconds", 0)
    monkeypatch.setattr(settings, "green_api_delivery_poll_seconds", 0)
    monkeypatch.setattr(settings, "smsc_enabled", False)


def configure_smsc(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smsc_enabled", True)
    monkeypatch.setattr(settings, "smsc_login", "sms-login")
    monkeypatch.setattr(settings, "smsc_password", "sms-password")
    monkeypatch.setattr(settings, "smsc_sender", "Zhertap")
    monkeypatch.setattr(settings, "smsc_base_url", "https://sms.example/send")
    monkeypatch.setattr(settings, "smsc_timeout_seconds", 15)


def test_green_api_succeeds_only_after_delivered_status(monkeypatch) -> None:
    configure_green_api(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        sms.httpx,
        "get",
        lambda *args, **kwargs: ResponseStub(payload={"stateInstance": "authorized"}),
    )

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        if "/sendMessage/" in url:
            return ResponseStub(payload={"idMessage": "message-1"})
        return ResponseStub(
            payload={
                "idMessage": "message-1",
                "statusMessage": "delivered",
                "sendByApi": True,
            }
        )

    monkeypatch.setattr(sms.httpx, "post", fake_post)

    sms.send_login_code("+7 (701) 234-56-78", "482913")

    assert calls[0]["json"] == {
        "chatId": "77012345678@c.us",
        "message": (
            "Код подтверждения Жертап: 482913. "
            "Никому не сообщайте этот код. Код действует 10 минут."
        ),
    }
    assert "/sendMessage/" in calls[0]["url"]
    assert "/getMessage/" in calls[1]["url"]


def test_green_api_rejects_success_response_without_message_id(monkeypatch) -> None:
    configure_green_api(monkeypatch)
    monkeypatch.setattr(
        sms.httpx,
        "get",
        lambda *args, **kwargs: ResponseStub(payload={"stateInstance": "authorized"}),
    )
    monkeypatch.setattr(
        sms.httpx,
        "post",
        lambda *args, **kwargs: ResponseStub(payload={"unexpected": True}),
    )

    with pytest.raises(RuntimeError, match="did not return a message id"):
        sms.send_login_code("+77012345678", "482913")


def test_green_api_http_error_does_not_expose_token(monkeypatch) -> None:
    configure_green_api(monkeypatch)
    monkeypatch.setattr(
        sms.httpx,
        "get",
        lambda *args, **kwargs: ResponseStub(payload={"stateInstance": "authorized"}),
    )
    monkeypatch.setattr(
        sms.httpx,
        "post",
        lambda *args, **kwargs: ResponseStub(status_code=401, payload={"error": "unauthorized"}),
    )

    with pytest.raises(RuntimeError) as exc_info:
        sms.send_login_code("+77012345678", "482913")

    assert "401" in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)


def test_not_authorized_green_api_falls_back_to_smsc(monkeypatch) -> None:
    configure_green_api(monkeypatch)
    configure_smsc(monkeypatch)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        sms.httpx,
        "get",
        lambda *args, **kwargs: ResponseStub(payload={"stateInstance": "notAuthorized"}),
    )

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"id": "sms-1"},
        )

    monkeypatch.setattr(sms.httpx, "post", fake_post)

    sms.send_login_code("+77012345678", "482913")

    assert [url for url, _ in calls] == ["https://sms.example/send"]


def test_unconfirmed_whatsapp_delivery_falls_back_to_smsc(monkeypatch) -> None:
    configure_green_api(monkeypatch)
    configure_smsc(monkeypatch)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        sms.httpx,
        "get",
        lambda *args, **kwargs: ResponseStub(payload={"stateInstance": "authorized"}),
    )

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if "/sendMessage/" in url:
            return ResponseStub(payload={"idMessage": "message-1"})
        if "/getMessage/" in url:
            return ResponseStub(payload={"statusMessage": "sent"})
        return ResponseStub(payload={"id": "sms-1"})

    monkeypatch.setattr(sms.httpx, "post", fake_post)

    sms.send_login_code("+77012345678", "482913")

    assert [url for url, _ in calls] == [
        "https://7201.api.green-api.com/waInstance123456789/sendMessage/secret-token",
        "https://7201.api.green-api.com/waInstance123456789/getMessage/secret-token",
        "https://sms.example/send",
    ]
