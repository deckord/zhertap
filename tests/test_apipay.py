import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import app.apipay as apipay
import app.db as db
import app.services as services
from app.access import has_platform_paid_access
from app.apipay import (
    ApiPayQrInvoice,
    cancel_invoice,
    create_qr_invoice,
    get_invoice,
    verify_webhook_signature,
)
from app.config import settings
from app.main import app
from app.models import Account, AccountPayment, PaymentStatus, SearchStatus
from tests.test_payments import add_paid_search_candidate, build_session


def test_webhook_signature_uses_raw_request_body() -> None:
    secret = "webhook-secret"
    raw_body = b'{"event":"webhook.test","timestamp":"2026-07-24T10:00:00Z"}'
    signature = "sha256=" + hmac.new(
        secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    assert verify_webhook_signature(raw_body, signature, secret)
    assert not verify_webhook_signature(raw_body + b" ", signature, secret)
    assert not verify_webhook_signature(raw_body, "sha256=wrong", secret)


def test_apipay_api_calls_send_key_in_header(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, body: dict) -> None:
            self.body = body

        def json(self) -> dict:
            return self.body

    class FakeClient:
        def __init__(self, **_: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def post(
            self,
            url: str,
            *,
            headers: dict,
            json: dict | None = None,
        ) -> FakeResponse:
            calls.append(("POST", url, headers))
            if url.endswith("/cancel"):
                return FakeResponse({"message": "Invoice cancelled"})
            return FakeResponse(
                {
                    "id": 601,
                    "status": "pending",
                    "qr_token_url": "https://qr.kaspi.kz/test",
                }
            )

        def get(self, url: str, *, headers: dict) -> FakeResponse:
            calls.append(("GET", url, headers))
            return FakeResponse(
                {
                    "id": 601,
                    "amount": "1490.00",
                    "status": "pending",
                }
            )

    monkeypatch.setattr(settings, "apipay_api_key", "test-api-key")
    monkeypatch.setattr(apipay.httpx, "Client", FakeClient)

    created = create_qr_invoice(
        request_id="request-1",
        amount_kzt=1490,
        description="Test",
    )
    checked = get_invoice(created.invoice_id)
    cancellation = cancel_invoice(created.invoice_id)

    assert checked["status"] == "pending"
    assert cancellation.status == "cancelled"
    assert [call[:2] for call in calls] == [
        ("POST", "https://api.apipay.kz/api/v1/invoices/qr"),
        ("GET", "https://api.apipay.kz/api/v1/invoices/601"),
        ("POST", "https://api.apipay.kz/api/v1/invoices/601/cancel"),
    ]
    assert all(call[2]["X-API-Key"] == "test-api-key" for call in calls)


def test_apipay_invoice_replaces_manual_payment_controls(monkeypatch) -> None:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(settings, "payment_card_number", "")
    monkeypatch.setattr(
        services,
        "create_qr_invoice",
        lambda **_: ApiPayQrInvoice(
            invoice_id="501",
            status="pending",
            payment_url="https://qr.kaspi.kz/example",
        ),
    )
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        request = add_paid_search_candidate(session)

        services.request_payment(session, request.id)

        assert request.payment_status == PaymentStatus.awaiting_transfer.value
        assert request.payment_amount_kzt == 4990
        assert request.payment_provider == "apipay"
        assert request.payment_provider_invoice_id == "501"
        assert request.payment_provider_status == "pending"
        assert request.payment_provider_url == "https://qr.kaspi.kz/example"
        assert "автоматически" in sent[0][1]["text"]
        keyboard = sent[0][1]["reply_markup"]["inline_keyboard"]
        assert keyboard == [
            [
                {
                    "text": "💳 Оплатить через Kaspi",
                    "url": "https://qr.kaspi.kz/example",
                }
            ],
            [
                {
                    "text": "🔄 Обновить QR-ссылку",
                    "callback_data": f"pay:refresh:{request.id}:501",
                }
            ],
        ]


def test_existing_apipay_invoice_is_resent_instead_of_blocking_new_payment(
    monkeypatch,
) -> None:
    sent: list[tuple[str, dict]] = []
    invoices_created: list[str] = []
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(settings, "payment_card_number", "")

    def fake_create_qr_invoice(**kwargs) -> ApiPayQrInvoice:
        invoices_created.append(kwargs["request_id"])
        return ApiPayQrInvoice(
            invoice_id="701",
            status="pending",
            payment_url="https://qr.kaspi.kz/existing",
        )

    monkeypatch.setattr(services, "create_qr_invoice", fake_create_qr_invoice)
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        first = add_paid_search_candidate(session)
        services.request_payment(session, first.id)
        second = add_paid_search_candidate(session)

        returned = services.request_payment(session, second.id)

        assert returned.id == first.id
        assert invoices_created == [first.id]
        assert second.payment_status == PaymentStatus.not_requested.value
        assert len(sent) == 2
        assert sent[1][1]["reply_markup"]["inline_keyboard"][0][0] == {
            "text": "💳 Оплатить через Kaspi",
            "url": "https://qr.kaspi.kz/existing",
        }
        assert sent[1][1]["reply_markup"]["inline_keyboard"][1][0][
            "callback_data"
        ] == f"pay:refresh:{first.id}:701"


def test_existing_apipay_invoice_uses_current_request_language_and_hides_card(
    monkeypatch,
) -> None:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(settings, "payment_card_number", "4400430373806295")
    monkeypatch.setattr(settings, "payment_recipient", "Даурен К")
    monkeypatch.setattr(
        services,
        "create_qr_invoice",
        lambda **_: ApiPayQrInvoice(
            invoice_id="702",
            status="pending",
            payment_url="https://qr.kaspi.kz/kz",
        ),
    )
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    with build_session() as session:
        first = add_paid_search_candidate(session, language="ru")
        services.request_payment(session, first.id)
        second = add_paid_search_candidate(session, language="kz")

        services.request_payment(
            session,
            second.id,
            message_language=second.language,
        )

        repeated = sent[-1][1]
        assert "<b>Есеп дайын</b>" in repeated["text"]
        assert "Kaspi арқылы" in repeated["text"]
        assert "Номер карты" not in repeated["text"]
        assert "Карта нөмірі" not in repeated["text"]
        assert "4400 4303 7380 6295" not in repeated["text"]
        assert "Даурен К" not in repeated["text"]
        keyboard = repeated["reply_markup"]["inline_keyboard"]
        assert "Kaspi арқылы" in keyboard[0][0]["text"]
        assert len(keyboard) == 2


def test_refresh_pending_apipay_invoice_sends_new_qr_without_waiting_for_cancel(
    monkeypatch,
) -> None:
    sent: list[tuple[str, dict]] = []
    created: list[dict] = []
    cancelled: list[str] = []
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(settings, "payment_card_number", "")
    monkeypatch.setattr(
        services,
        "get_invoice",
        lambda invoice_id: {
            "id": invoice_id,
            "external_order_id": "",
            "amount": "4990.00",
            "status": "pending",
        },
    )
    monkeypatch.setattr(
        services,
        "cancel_invoice",
        lambda invoice_id: cancelled.append(invoice_id)
        or apipay.ApiPayCancellation(status="cancelling"),
    )

    def fake_create_qr_invoice(**kwargs) -> ApiPayQrInvoice:
        created.append(kwargs)
        return ApiPayQrInvoice(
            invoice_id="802",
            status="pending",
            payment_url="https://qr.kaspi.kz/new",
        )

    monkeypatch.setattr(services, "create_qr_invoice", fake_create_qr_invoice)
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )
    monkeypatch.setattr(services, "dispatch_apipay_reconciliation", lambda _: None)

    with build_session() as session:
        request = add_paid_search_candidate(session)
        request.payment_status = PaymentStatus.awaiting_transfer.value
        request.payment_amount_kzt = 4990
        request.payment_provider = "apipay"
        request.payment_provider_invoice_id = "801"
        request.payment_provider_status = "pending"
        request.payment_provider_url = "https://qr.kaspi.kz/old"
        session.commit()

        returned, link_sent = services.refresh_apipay_payment(
            session,
            request.id,
            expected_invoice_id="801",
            telegram_user_id=request.telegram_user_id,
            telegram_chat_id=request.telegram_chat_id,
        )

        assert returned.id == request.id
        assert link_sent is True
        assert cancelled == ["801"]
        assert request.payment_provider_invoice_id == "802"
        assert request.payment_provider_url == "https://qr.kaspi.kz/new"
        assert created[0]["idempotency_key"].startswith(
            f"land-scout:{request.id}:refresh:"
        )
        assert sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["url"] == (
            "https://qr.kaspi.kz/new"
        )


def test_expired_apipay_polling_automatically_sends_new_payment_link(
    monkeypatch,
) -> None:
    sent: list[tuple[str, dict]] = []
    created: list[dict] = []
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(settings, "payment_card_number", "")

    def fake_create_qr_invoice(**kwargs) -> ApiPayQrInvoice:
        created.append(kwargs)
        return ApiPayQrInvoice(
            invoice_id="812",
            status="pending",
            payment_url="https://qr.kaspi.kz/auto-new",
        )

    monkeypatch.setattr(services, "create_qr_invoice", fake_create_qr_invoice)
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )
    monkeypatch.setattr(services, "dispatch_apipay_reconciliation", lambda _: None)

    class SameSession:
        def __init__(self, session):
            self.session = session

        def __enter__(self):
            return self.session

        def __exit__(self, exc_type, exc, traceback):
            return False

    with build_session() as session:
        request = add_paid_search_candidate(session)
        request.payment_status = PaymentStatus.rejected.value
        request.payment_amount_kzt = 4990
        request.payment_provider = "apipay"
        request.payment_provider_invoice_id = "811"
        request.payment_provider_status = "expired"
        request.payment_provider_url = "https://qr.kaspi.kz/expired"
        session.commit()
        request_id = request.id
        monkeypatch.setattr(db, "SessionLocal", lambda: SameSession(session))

        services.notify_apipay_payment_retry(request_id)

        session.refresh(request)
        assert request.payment_status == PaymentStatus.awaiting_transfer.value
        assert request.payment_provider_invoice_id == "812"
        assert request.payment_provider_url == "https://qr.kaspi.kz/auto-new"
        assert len(sent) == 2
        assert "Сейчас отправлю новую ссылку оплаты" in sent[0][1]["text"]
        assert sent[1][1]["reply_markup"]["inline_keyboard"][0][0]["url"] == (
            "https://qr.kaspi.kz/auto-new"
        )
        assert created[0]["idempotency_key"].startswith(
            f"land-scout:{request_id}:refresh:"
        )


def test_paid_stale_apipay_invoice_after_refresh_still_activates_access() -> None:
    with build_session() as session:
        request = add_paid_search_candidate(session)
        request.payment_status = PaymentStatus.awaiting_transfer.value
        request.payment_amount_kzt = 4990
        request.payment_provider = "apipay"
        request.payment_provider_invoice_id = "822"
        request.payment_provider_status = "pending"
        session.commit()

        result = services.apply_apipay_webhook(
            session,
            {
                "event": "invoice.status_changed",
                "invoice": {
                    "id": 821,
                    "external_order_id": request.id,
                    "amount": "4990.00",
                    "status": "paid",
                },
            },
        )

        assert result.deliver_report
        assert request.payment_status == PaymentStatus.paid.value
        assert request.payment_provider_invoice_id == "821"
        assert request.payment_confirmed_by == "apipay:821"


def test_paid_webhook_marks_access_and_is_idempotent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "search_price_kzt", 1490)
    with build_session() as session:
        request = add_paid_search_candidate(session)
        request.payment_status = PaymentStatus.awaiting_transfer.value
        request.payment_amount_kzt = 1490
        request.payment_provider = "apipay"
        request.payment_provider_invoice_id = "502"
        session.commit()
        payload = {
            "event": "invoice.status_changed",
            "invoice": {
                "id": 502,
                "external_order_id": request.id,
                "amount": "1490.00",
                "status": "paid",
            },
        }

        first = services.apply_apipay_webhook(session, payload)

        assert first.deliver_report
        assert request.payment_status == PaymentStatus.paid.value
        assert request.payment_confirmed_by == "apipay:502"

        request.status = SearchStatus.delivered.value
        session.commit()
        second = services.apply_apipay_webhook(session, payload)

        assert not second.deliver_report
        assert request.payment_status == PaymentStatus.paid.value
        assert request.payment_confirmed_by == "apipay:502"


def test_account_apipay_webhook_activates_web_and_linked_telegram_access() -> None:
    with build_session() as session:
        account = Account(
            phone="+77026669475",
            telegram_user_id="70557953",
            telegram_chat_id="70557953",
        )
        session.add(account)
        session.flush()
        payment = AccountPayment(
            account_id=account.id,
            payment_status=PaymentStatus.awaiting_transfer.value,
            payment_amount_kzt=4990,
            payment_provider="apipay",
            payment_provider_invoice_id="700",
            payment_provider_url="https://qr.kaspi.kz/web",
        )
        session.add(payment)
        session.commit()

        result = services.apply_apipay_webhook(
            session,
            {
                "event": "invoice.status_changed",
                "invoice": {
                    "id": 700,
                    "external_order_id": f"account-{payment.id}",
                    "amount": "4990.00",
                    "status": "paid",
                },
            },
        )

        assert result.activate_account_access
        assert payment.payment_status == PaymentStatus.paid.value
        assert payment.payment_confirmed_by == "apipay:700"
        assert account.paid_access is True
        assert has_platform_paid_access(session, "70557953") is True


def test_paid_webhook_rejects_wrong_amount(monkeypatch) -> None:
    monkeypatch.setattr(settings, "search_price_kzt", 1490)
    with build_session() as session:
        request = add_paid_search_candidate(session)
        request.payment_status = PaymentStatus.awaiting_transfer.value
        request.payment_amount_kzt = 1490
        request.payment_provider = "apipay"
        request.payment_provider_invoice_id = "503"
        session.commit()

        with pytest.raises(ValueError, match="не совпадает"):
            services.apply_apipay_webhook(
                session,
                {
                    "event": "invoice.status_changed",
                    "invoice": {
                        "id": 503,
                        "external_order_id": request.id,
                        "amount": "1.00",
                        "status": "paid",
                    },
                },
            )

        session.rollback()
        session.refresh(request)
        assert request.payment_status == PaymentStatus.awaiting_transfer.value


def test_stale_webhook_cannot_restore_cleared_access() -> None:
    with build_session() as session:
        request = add_paid_search_candidate(session)
        request.payment_status = PaymentStatus.not_requested.value
        request.payment_provider = None
        request.payment_provider_invoice_id = None
        session.commit()

        with pytest.raises(ValueError, match="ID счета"):
            services.apply_apipay_webhook(
                session,
                {
                    "event": "invoice.status_changed",
                    "invoice": {
                        "id": 504,
                        "external_order_id": request.id,
                        "amount": "1490.00",
                        "status": "paid",
                    },
                },
            )

        session.rollback()
        session.refresh(request)
        assert request.payment_status == PaymentStatus.not_requested.value


def test_webhook_test_event_needs_no_invoice() -> None:
    with build_session() as session:
        result = services.apply_apipay_webhook(
            session,
            json.loads('{"event":"webhook.test"}'),
        )

        assert result.event == "webhook.test"
        assert not result.deliver_report


def test_webhook_endpoint_accepts_only_valid_signature(monkeypatch) -> None:
    secret = "endpoint-secret"
    raw_body = b'{"event":"webhook.test"}'
    signature = "sha256=" + hmac.new(
        secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "apipay_webhook_secret", secret)

    with TestClient(app) as client:
        accepted = client.post(
            "/webhooks/apipay",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
            },
        )
        rejected = client.post(
            "/webhooks/apipay",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": "sha256=wrong",
            },
        )

    assert accepted.status_code == 200
    assert accepted.json() == {
        "ok": True,
        "event": "webhook.test",
        "status": None,
        "ignored": False,
    }
    assert rejected.status_code == 401
