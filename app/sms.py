import logging
import re
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def login_code_message(code: str) -> str:
    return (
        f"Код подтверждения Жертап: {code}. "
        "Никому не сообщайте этот код. Код действует 10 минут."
    )


def _green_api_chat_id(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if not 10 <= len(digits) <= 15:
        raise ValueError("Invalid phone number for WhatsApp delivery")
    return f"{digits}@c.us"


def _send_green_api_login_code(phone: str, code: str) -> None:
    if not settings.green_api_id_instance or not settings.green_api_token_instance:
        raise RuntimeError("GREEN API credentials are not configured")
    base_url = settings.green_api_base_url.rstrip("/")
    state_url = (
        f"{base_url}/waInstance{settings.green_api_id_instance}"
        f"/getStateInstance/{settings.green_api_token_instance}"
    )
    try:
        state_response = httpx.get(
            state_url,
            timeout=httpx.Timeout(settings.green_api_timeout_seconds, connect=5),
        )
    except httpx.RequestError as exc:
        raise RuntimeError("GREEN API state request failed") from exc
    if state_response.status_code >= 400:
        raise RuntimeError(f"GREEN API state returned HTTP {state_response.status_code}")
    try:
        state = state_response.json().get("stateInstance")
    except ValueError as exc:
        raise RuntimeError("GREEN API returned an invalid state response") from exc
    if state != "authorized":
        raise RuntimeError(f"GREEN API instance is not authorized: {state or 'unknown'}")

    url = (
        f"{base_url}"
        f"/waInstance{settings.green_api_id_instance}"
        f"/sendMessage/{settings.green_api_token_instance}"
    )
    try:
        response = httpx.post(
            url,
            json={
                "chatId": _green_api_chat_id(phone),
                "message": login_code_message(code),
            },
            timeout=httpx.Timeout(settings.green_api_timeout_seconds, connect=5),
        )
    except httpx.RequestError as exc:
        raise RuntimeError("GREEN API request failed") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"GREEN API returned HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("GREEN API returned an invalid response") from exc
    if not result.get("idMessage"):
        raise RuntimeError("GREEN API did not return a message id")

    chat_id = _green_api_chat_id(phone)
    status_url = (
        f"{base_url}/waInstance{settings.green_api_id_instance}"
        f"/getMessage/{settings.green_api_token_instance}"
    )
    deadline = time.monotonic() + settings.green_api_delivery_timeout_seconds
    while True:
        try:
            status_response = httpx.post(
                status_url,
                json={"chatId": chat_id, "idMessage": result["idMessage"]},
                timeout=httpx.Timeout(settings.green_api_timeout_seconds, connect=5),
            )
        except httpx.RequestError as exc:
            raise RuntimeError("GREEN API delivery-status request failed") from exc
        if status_response.status_code == 200:
            try:
                status = status_response.json().get("statusMessage")
            except ValueError as exc:
                raise RuntimeError("GREEN API returned an invalid delivery response") from exc
            if status in {"delivered", "read"}:
                return
            if status in {"failed", "yellowCard", "notInGroup"}:
                raise RuntimeError(f"GREEN API delivery failed with status {status}")
        if time.monotonic() >= deadline:
            break
        time.sleep(settings.green_api_delivery_poll_seconds)
    raise RuntimeError("GREEN API message was not confirmed delivered")


def _send_smsc_login_code(phone: str, code: str) -> None:
    if not settings.smsc_enabled:
        logger.info("SMSC is disabled; login code for %s was not sent", phone)
        return
    if not settings.smsc_login or not settings.smsc_password:
        raise RuntimeError("SMSC credentials are not configured")

    payload = {
        "login": settings.smsc_login,
        "psw": settings.smsc_password,
        "phones": phone,
        "mes": login_code_message(code),
        "fmt": "3",
        "charset": "utf-8",
    }
    if settings.smsc_sender:
        payload["sender"] = settings.smsc_sender

    response = httpx.post(
        settings.smsc_base_url,
        data=payload,
        timeout=httpx.Timeout(settings.smsc_timeout_seconds, connect=5),
    )
    response.raise_for_status()
    result = response.json()
    if "error" in result:
        raise RuntimeError(f"SMSC error {result.get('error_code')}: {result.get('error')}")


def send_login_code(phone: str, code: str) -> None:
    if settings.green_api_enabled:
        try:
            _send_green_api_login_code(phone, code)
            return
        except (RuntimeError, ValueError):
            logger.warning("WhatsApp login-code delivery failed; trying SMS fallback")
            if not settings.smsc_enabled:
                raise
    _send_smsc_login_code(phone, code)
