from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings


class ApiPayError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApiPayQrInvoice:
    invoice_id: str
    status: str
    payment_url: str
    qr_image_url: str | None = None
    qr_expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class ApiPayCancellation:
    status: str


def _api_headers() -> dict[str, str]:
    api_key = settings.apipay_api_key.strip()
    if not api_key:
        raise ApiPayError("APIPAY_API_KEY не заполнен")
    return {
        "X-API-Key": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def create_qr_invoice(
    *,
    request_id: str,
    amount_kzt: int,
    description: str,
    idempotency_key: str | None = None,
) -> ApiPayQrInvoice:
    base_url = settings.apipay_base_url.rstrip("/")
    payload = {
        "amount": amount_kzt,
        "description": description[:100],
        "external_order_id": request_id,
        "external_order_id_idempotency": idempotency_key or f"land-scout:{request_id}",
    }
    try:
        with httpx.Client(timeout=settings.apipay_timeout_seconds) as client:
            response = client.post(
                f"{base_url}/invoices/qr",
                headers=_api_headers(),
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise ApiPayError("ApiPay временно не ответил при создании счета") from exc

    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise ApiPayError(f"ApiPay вернул некорректный ответ HTTP {response.status_code}") from exc
    if response.status_code not in {200, 201}:
        error = body.get("message") or body.get("error") or body.get("error_code")
        raise ApiPayError(
            f"ApiPay не создал счет: {str(error or response.status_code)[:300]}"
        )

    invoice_id = body.get("id")
    payment_url = body.get("qr_token_url")
    if invoice_id is None or not isinstance(payment_url, str) or not payment_url.startswith(
        "https://"
    ):
        raise ApiPayError("ApiPay не вернул ID счета или безопасную ссылку Kaspi")
    return ApiPayQrInvoice(
        invoice_id=str(invoice_id),
        status=str(body.get("status") or "pending"),
        payment_url=payment_url,
        qr_image_url=(
            str(body.get("qr_image_url"))
            if isinstance(body.get("qr_image_url"), str)
            and str(body.get("qr_image_url")).startswith("https://")
            else None
        ),
        qr_expires_at=str(body.get("qr_expires_at")) if body.get("qr_expires_at") else None,
    )


def get_invoice(invoice_id: str) -> dict[str, Any]:
    if not invoice_id.isdigit():
        raise ApiPayError("Некорректный ID счета ApiPay")
    base_url = settings.apipay_base_url.rstrip("/")
    try:
        with httpx.Client(timeout=settings.apipay_timeout_seconds) as client:
            response = client.get(
                f"{base_url}/invoices/{invoice_id}",
                headers=_api_headers(),
            )
    except httpx.HTTPError as exc:
        raise ApiPayError("ApiPay временно не ответил при проверке счета") from exc

    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise ApiPayError(
            f"ApiPay вернул некорректный ответ HTTP {response.status_code}"
        ) from exc
    if response.status_code != 200:
        error = body.get("message") or body.get("error") or body.get("error_code")
        raise ApiPayError(
            f"ApiPay не вернул счет: {str(error or response.status_code)[:300]}"
        )
    if str(body.get("id") or "") != invoice_id or not body.get("status"):
        raise ApiPayError("Ответ ApiPay не содержит корректный ID или статус счета")
    return body


def cancel_invoice(invoice_id: str) -> ApiPayCancellation:
    if not invoice_id.isdigit():
        raise ApiPayError("Некорректный ID счета ApiPay")
    base_url = settings.apipay_base_url.rstrip("/")
    try:
        with httpx.Client(timeout=settings.apipay_timeout_seconds) as client:
            response = client.post(
                f"{base_url}/invoices/{invoice_id}/cancel",
                headers=_api_headers(),
            )
    except httpx.HTTPError as exc:
        raise ApiPayError("ApiPay временно не ответил при отмене счета") from exc

    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise ApiPayError(
            f"ApiPay вернул некорректный ответ HTTP {response.status_code}"
        ) from exc
    if response.status_code == 200:
        return ApiPayCancellation(status="cancelled")
    if response.status_code == 202:
        return ApiPayCancellation(status="cancelling")
    error = body.get("message") or body.get("error") or body.get("error_code")
    raise ApiPayError(
        f"ApiPay не отменил счет: {str(error or response.status_code)[:300]}"
    )
