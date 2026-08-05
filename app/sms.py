import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def send_login_code(phone: str, code: str) -> None:
    if not settings.smsc_enabled:
        logger.info("SMSC is disabled; login code for %s was not sent", phone)
        return
    if not settings.smsc_login or not settings.smsc_password:
        raise RuntimeError("SMSC credentials are not configured")

    payload = {
        "login": settings.smsc_login,
        "psw": settings.smsc_password,
        "phones": phone,
        "mes": f"Код входа Жертап: {code}. Никому не сообщайте этот код.",
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
