"""Small production monitor for the web readiness endpoints and Telegram alerts."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonitorResult:
    ok: bool
    detail: str


def check_application(client: httpx.Client, base_url: str) -> MonitorResult:
    base_url = base_url.rstrip("/")
    try:
        health = client.get(f"{base_url}/health")
        if health.status_code != 200:
            return MonitorResult(False, f"/health returned HTTP {health.status_code}")
        ready = client.get(f"{base_url}/ready")
        if ready.status_code != 200:
            return MonitorResult(False, f"/ready returned HTTP {ready.status_code}")
        return MonitorResult(True, "health and readiness checks passed")
    except httpx.HTTPError as exc:
        return MonitorResult(False, f"application check failed: {type(exc).__name__}")


def send_telegram_alert(client: httpx.Client, message: str) -> bool:
    token = settings.telegram_bot_token.strip()
    chat_id = settings.telegram_admin_chat_id.strip()
    if not token or not chat_id:
        logger.warning("Telegram monitoring alert is not configured")
        return False
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
        )
        return response.is_success
    except httpx.HTTPError:
        logger.warning("Could not send Telegram monitoring alert")
        return False


def run_monitor(*, once: bool = False) -> None:
    interval = settings.monitor_interval_seconds
    base_url = os.getenv("MONITOR_BASE_URL", settings.monitor_base_url)
    previous: bool | None = None
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            result = check_application(client, base_url)
            if previous is False and result.ok:
                send_telegram_alert(
                    client, "Жертап: сервис восстановился. /health и /ready работают."
                )
            elif previous is True and not result.ok:
                send_telegram_alert(client, f"Жертап: проблема production-сервиса. {result.detail}")
            if not result.ok:
                logger.error(result.detail)
            else:
                logger.info(result.detail)
            previous = result.ok
            if once:
                return
            time.sleep(interval)


if __name__ == "__main__":
    run_monitor()
