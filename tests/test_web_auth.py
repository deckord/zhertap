import hashlib
import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.account_payments as account_payments
import app.web as web
from app.apipay import ApiPayCancellation, ApiPayQrInvoice
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import (
    Account,
    AccountPayment,
    AuctionFavorite,
    AuctionLot,
    PaymentStatus,
    SearchRequest,
    WebLoginCode,
    WebSession,
)


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


@contextmanager
def client_for(session: Session) -> Iterator[TestClient]:
    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            client.headers.update(
                {"x-csrf-token": web.csrf_token_value("", "testclient")}
            )
            yield client
    finally:
        app.dependency_overrides.clear()


def authorize_client(client: TestClient, session: Session, account: Account) -> None:
    token = "test-web-session"
    session.add(
        WebSession(
            account_id=account.id,
            token_hash=web._hash(token),
            expires_at=web._now() + timedelta(days=1),
        )
    )
    session.commit()
    client.cookies.set("zhertap_session", token)
    client.headers.update(
        {"x-csrf-token": web.csrf_token_value(token, "testclient")}
    )


def test_web_hash_uses_session_secret(monkeypatch) -> None:
    monkeypatch.setattr(settings, "session_secret", "session-secret-1")
    monkeypatch.setattr(settings, "admin_password", "admin-password")
    monkeypatch.setattr(settings, "internal_api_key", "internal-secret")

    expected = hmac.new(
        b"session-secret-1",
        b"raw-value",
        hashlib.sha256,
    ).hexdigest()
    assert web._hash("raw-value") == expected


def test_verify_password_returns_false_for_invalid_hash_format() -> None:
    assert web._verify_password("password", "nonsense") is False
    assert web._verify_password("password", "pbkdf2_sha256$notint$AA==$BBB") is False
    assert (
        web._verify_password("password", "pbkdf2_sha256$210000$@@@!$") is False
    )


def test_register_verification_accepts_legacy_sms_code_hash(monkeypatch) -> None:
    monkeypatch.setattr(settings, "session_secret", "new-session-secret")
    monkeypatch.setattr(settings, "internal_api_key", "legacy-internal-secret")
    monkeypatch.setattr(settings, "admin_password", "legacy-admin-password")
    code = "123456"
    with build_session() as session:
        account = Account(
            phone="+77026669475",
            password_hash=None,
            phone_verified_at=None,
        )
        session.add(account)
        session.flush()
        session.add(
            WebLoginCode(
                phone="+77026669475",
                code_hash=web._legacy_hash(code),
                purpose="register",
                expires_at=web._now() + timedelta(minutes=10),
            )
        )
        session.commit()

        with client_for(session) as client:
            response = client.post(
                "/register/verify",
                data={
                    "phone": "7026669475",
                    "code": code,
                    "password": "password-1",
                    "password_confirm": "password-1",
                },
                follow_redirects=False,
            )

        updated_code = session.scalar(
            select(WebLoginCode).where(WebLoginCode.phone == "+77026669475")
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/cabinet"
        assert updated_code is not None
        assert updated_code.code_hash == web._hash(code)
        assert updated_code.consumed_at is not None


def test_legacy_web_session_hash_is_accepted_after_session_secret_change(monkeypatch) -> None:
    monkeypatch.setattr(settings, "session_secret", "active-session-secret")
    monkeypatch.setattr(settings, "internal_api_key", "legacy-session-secret")
    monkeypatch.setattr(settings, "admin_password", "legacy-admin-password")
    token = "legacy-session-token"
    with build_session() as session:
        account = Account(
            phone="+77026669475",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
        )
        session.add(account)
        session.commit()
        web_session = WebSession(
            account_id=account.id,
            token_hash=web._legacy_hash(token),
            expires_at=web._now() + timedelta(days=1),
        )
        session.add(web_session)
        session.commit()

        with client_for(session) as client:
            client.cookies.set(web.SESSION_COOKIE, token)
            response = client.get("/cabinet")

        refreshed = session.get(WebSession, web_session.id)
        assert refreshed is not None
        assert response.status_code == 200
        assert refreshed.token_hash == web._hash(token)


def test_existing_registered_phone_cannot_request_registration_sms(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(web, "send_login_code", lambda phone, code: sent.append((phone, code)))
    with build_session() as session:
        session.add(
            Account(
                phone="+77026669475",
                phone_verified_at=web._now(),
                password_hash=web._hash_password("old-password"),
            )
        )
        session.commit()

        with client_for(session) as client:
            response = client.post(
                "/register/request-code",
                data={
                    "phone": "7026669475",
                    "offer_accepted": "yes",
                },
            )

        assert response.status_code == 200
        assert "Не удалось отправить SMS-код" in response.text
        assert "уже есть аккаунт" not in response.text.lower()
        assert sent == []
        assert session.scalar(select(WebLoginCode.id)) is None


def test_unknown_phone_cannot_reset_password_revealing_registration() -> None:
    with build_session() as session:
        with client_for(session) as client:
            response = client.post(
                "/password/reset/request-code",
                data={"phone": "7026669475"},
            )

        assert response.status_code == 200
        assert "не найден" not in response.text.lower()
        assert "Если этот номер зарегистрирован" in response.text


def test_existing_phone_fails_registration_verification_without_reveal(monkeypatch) -> None:
    monkeypatch.setattr(web, "send_login_code", lambda phone, code: None)
    with build_session() as session:
        session.add(
            Account(
                phone="+77026669475",
                phone_verified_at=web._now(),
                password_hash=web._hash_password("old-password"),
            )
        )
        session.commit()

        with client_for(session) as client:
            response = client.post(
                "/register/verify",
                data={
                    "phone": "7026669475",
                    "code": "000000",
                    "password": "password-1",
                    "password_confirm": "password-1",
                },
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?invalid=1"


def test_login_unknown_phone_does_not_reveal_account_state() -> None:
    with build_session() as session:
        with client_for(session) as client:
            response = client.post(
                "/login",
                data={"phone": "7026669475", "password": "wrong"},
                follow_redirects=False,
            )

        assert response.status_code == 400
        assert "не найден" not in response.text.lower()
        assert "Неверные учетные данные" in response.text


def test_successful_web_registration_notifies_admin(monkeypatch) -> None:
    sms_codes: list[tuple[str, str]] = []
    admin_messages: list[tuple[str, dict]] = []
    monkeypatch.setattr(settings, "telegram_admin_chat_id", "9001")
    monkeypatch.setattr(settings, "trial_access_enabled", True)
    monkeypatch.setattr(web, "send_login_code", lambda phone, code: sms_codes.append((phone, code)))
    monkeypatch.setattr(
        web,
        "telegram_request",
        lambda method, payload: admin_messages.append((method, payload)) or {"ok": True},
    )
    with build_session() as session:
        with client_for(session) as client:
            request_response = client.post(
                "/register/request-code",
                data={"phone": "7026669475", "offer_accepted": "yes"},
            )
            assert request_response.status_code == 200
            verify_response = client.post(
                "/register/verify",
                data={
                    "phone": "7026669475",
                    "code": sms_codes[0][1],
                    "password": "password-1",
                    "password_confirm": "password-1",
                },
                follow_redirects=False,
            )

        account = session.scalar(select(Account).where(Account.phone == "+77026669475"))
        assert verify_response.status_code == 303
        assert account is not None
        assert account.phone_verified_at is not None
        assert admin_messages
        assert admin_messages[0][0] == "sendMessage"
        assert admin_messages[0][1]["chat_id"] == "9001"
        assert "Новая регистрация на сайте" in admin_messages[0][1]["text"]
        assert "+77026669475" in admin_messages[0][1]["text"]
        assert account.onboarding_tour_available_at is not None
        assert account.onboarding_tour_dismissed_at is None


def test_cabinet_help_page_renders_for_authorized_account() -> None:
    with build_session() as session:
        account = Account(
            phone="+77026669475",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
        )
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/help")

        assert response.status_code == 200
        assert "Как проходит проверка" in response.text
        assert "ЕГКН" in response.text
        assert "генплан" in response.text


def test_onboarding_tour_can_be_dismissed() -> None:
    with build_session() as session:
        account = Account(
            phone="+77026669475",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
            onboarding_tour_available_at=web._now(),
        )
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            cabinet_response = client.get("/cabinet")
            dismiss_response = client.post("/cabinet/onboarding/dismiss")

        session.refresh(account)
        assert cabinet_response.status_code == 200
        assert "data-onboarding-tour" in cabinet_response.text
        assert dismiss_response.status_code == 200
        assert account.onboarding_tour_dismissed_at is not None


def test_web_gardening_search_can_use_six_sotok(monkeypatch) -> None:
    dispatched: list[str] = []
    monkeypatch.setattr(web, "dispatch_search", lambda search_id: dispatched.append(search_id))
    with build_session() as session:
        account = Account(
            phone="+77026669475",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
        )
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.post(
                "/cabinet/search",
                data={
                    "region": "Акмолинская область",
                    "district": "Бурабайский район",
                    "locality": "Щучинск",
                    "purpose": "Садоводство",
                    "area_ha": "0.06",
                },
                follow_redirects=False,
            )

        search = session.scalar(select(SearchRequest))
        assert response.status_code == 303
        assert search is not None
        assert search.area_ha == 0.06
        assert search.irrigation_type is None
        assert dispatched == [search.id]


def test_password_reset_uses_sms_code_and_updates_password(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(web, "send_login_code", lambda phone, code: sent.append((phone, code)))
    with build_session() as session:
        account = Account(
            phone="+77026669475",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("old-password"),
        )
        session.add(account)
        session.commit()

        with client_for(session) as client:
            request_response = client.post(
                "/password/reset/request-code",
                data={"phone": "+7 702 666 94 75"},
            )
            assert request_response.status_code == 200
            assert sent and sent[-1][0] == "+77026669475"

            reset_response = client.post(
                "/password/reset/verify",
                data={
                    "phone": "+77026669475",
                    "code": sent[-1][1],
                    "password": "new-password",
                    "password_confirm": "new-password",
                },
                follow_redirects=False,
            )

        session.refresh(account)
        assert reset_response.status_code == 303
        assert reset_response.headers["location"] == "/cabinet"
        assert "zhertap_session" in reset_response.headers["set-cookie"]
        assert web._verify_password("new-password", account.password_hash)
        assert not web._verify_password("old-password", account.password_hash)
        assert session.scalar(select(WebLoginCode.consumed_at)) is not None


def test_web_payment_status_auto_activates_paid_account(monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(
        account_payments,
        "get_invoice",
        lambda invoice_id: {
            "id": invoice_id,
            "status": "paid",
            "amount": "4990.00",
        },
    )
    with build_session() as session:
        account = Account(
            phone="+77026669475",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
        )
        session.add(account)
        session.flush()
        payment = AccountPayment(
            account_id=account.id,
            payment_status=PaymentStatus.awaiting_transfer.value,
            payment_amount_kzt=4990,
            payment_provider="apipay",
            payment_provider_invoice_id="901",
            payment_provider_url="https://qr.kaspi.kz/old",
        )
        session.add(payment)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/payment/status")

        session.refresh(account)
        session.refresh(payment)
        assert response.status_code == 200
        assert response.json()["paid"] is True
        assert response.json()["payment_status"] == PaymentStatus.paid.value
        assert account.paid_access is True
        assert payment.payment_confirmed_by == "apipay:901"


def test_web_payment_status_regenerates_qr_after_cancelled_invoice(monkeypatch) -> None:
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "apipay_polling_enabled", False)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(
        account_payments,
        "get_invoice",
        lambda invoice_id: {
            "id": invoice_id,
            "status": "cancelled",
            "amount": "4990.00",
        },
    )
    monkeypatch.setattr(
        account_payments,
        "cancel_invoice",
        lambda invoice_id: ApiPayCancellation(status="cancelled"),
    )
    monkeypatch.setattr(
        account_payments,
        "create_qr_invoice",
        lambda **_: ApiPayQrInvoice(
            invoice_id="902",
            status="pending",
            payment_url="https://qr.kaspi.kz/new",
            qr_image_url="https://api.apipay.kz/storage/qr/new.png",
        ),
    )

    with build_session() as session:
        account = Account(
            phone="+77026669475",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
        )
        session.add(account)
        session.flush()
        old_payment = AccountPayment(
            account_id=account.id,
            payment_status=PaymentStatus.awaiting_transfer.value,
            payment_amount_kzt=4990,
            payment_provider="apipay",
            payment_provider_invoice_id="901",
            payment_provider_url="https://qr.kaspi.kz/old",
        )
        session.add(old_payment)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/payment/status")

        payload = response.json()
        new_payment = session.get(AccountPayment, payload["payment_id"])
        session.refresh(old_payment)
        assert response.status_code == 200
        assert payload["paid"] is False
        assert payload["payment_url"] == "https://qr.kaspi.kz/new"
        assert payload["payment_status"] == PaymentStatus.awaiting_transfer.value
        assert old_payment.payment_status == PaymentStatus.rejected.value
        assert len(old_payment.payment_provider_status or "") <= 32
        assert new_payment is not None
        assert new_payment.payment_provider_invoice_id == "902"
        assert new_payment.payment_provider_qr_image_url == "https://api.apipay.kz/storage/qr/new.png"


def test_web_payment_status_regenerates_stale_qr(monkeypatch) -> None:
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "apipay_polling_enabled", False)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(
        account_payments,
        "get_invoice",
        lambda invoice_id: {
            "id": invoice_id,
            "status": "pending",
            "amount": "4990.00",
        },
    )
    monkeypatch.setattr(
        account_payments,
        "cancel_invoice",
        lambda invoice_id: ApiPayCancellation(status="cancelled"),
    )
    monkeypatch.setattr(
        account_payments,
        "create_qr_invoice",
        lambda **_: ApiPayQrInvoice(
            invoice_id="905",
            status="pending",
            payment_url="https://qr.kaspi.kz/stale-new",
            qr_image_url="https://api.apipay.kz/storage/qr/stale-new.png",
        ),
    )

    with build_session() as session:
        account = Account(
            phone="+77026669475",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
        )
        session.add(account)
        session.flush()
        old_payment = AccountPayment(
            account_id=account.id,
            payment_status=PaymentStatus.awaiting_transfer.value,
            payment_amount_kzt=4990,
            payment_provider="apipay",
            payment_provider_invoice_id="904",
            payment_provider_status="pending",
            payment_provider_url="https://qr.kaspi.kz/stale-old",
            payment_provider_updated_at=web._now()
            - timedelta(seconds=account_payments.QR_REFRESH_AFTER_SECONDS + 1),
        )
        session.add(old_payment)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/payment/status")

        payload = response.json()
        session.refresh(old_payment)
        assert response.status_code == 200
        assert payload["payment_url"] == "https://qr.kaspi.kz/stale-new"
        assert old_payment.payment_status == PaymentStatus.rejected.value
        assert len(old_payment.payment_provider_status or "") <= 32


def test_web_payment_refresh_button_forces_new_qr_for_pending_invoice(monkeypatch) -> None:
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "apipay_polling_enabled", False)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(
        account_payments,
        "get_invoice",
        lambda invoice_id: {
            "id": invoice_id,
            "status": "pending",
            "amount": "4990.00",
        },
    )
    monkeypatch.setattr(
        account_payments,
        "cancel_invoice",
        lambda invoice_id: ApiPayCancellation(status="cancelled"),
    )
    monkeypatch.setattr(
        account_payments,
        "create_qr_invoice",
        lambda **_: ApiPayQrInvoice(
            invoice_id="904",
            status="pending",
            payment_url="https://qr.kaspi.kz/manual-new",
            qr_image_url="https://api.apipay.kz/storage/qr/manual-new.png",
        ),
    )

    with build_session() as session:
        account = Account(
            phone="+77026669475",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
        )
        session.add(account)
        session.flush()
        old_payment = AccountPayment(
            account_id=account.id,
            payment_status=PaymentStatus.awaiting_transfer.value,
            payment_amount_kzt=4990,
            payment_provider="apipay",
            payment_provider_invoice_id="903",
            payment_provider_status="pending",
            payment_provider_url="https://qr.kaspi.kz/manual-old",
        )
        session.add(old_payment)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.post("/cabinet/payment/refresh", follow_redirects=False)

        latest_payment = session.scalar(
            select(AccountPayment)
            .where(AccountPayment.account_id == account.id)
            .order_by(AccountPayment.created_at.desc())
            .limit(1)
        )
        session.refresh(old_payment)
        assert response.status_code == 303
        assert response.headers["location"] == "/cabinet/payment?refreshed=1"
        assert old_payment.payment_status == PaymentStatus.rejected.value
        assert len(old_payment.payment_provider_status or "") <= 32
        assert latest_payment is not None
        assert latest_payment.id != old_payment.id
        assert latest_payment.payment_provider_invoice_id == "904"
        assert latest_payment.payment_provider_qr_image_url == (
            "https://api.apipay.kz/storage/qr/manual-new.png"
        )


def test_admin_analytics_shows_web_funnel(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_username", "admin")
    monkeypatch.setattr(settings, "admin_password", "secret")
    with build_session() as session:
        account = Account(
            phone="+77020000010",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
            trial_started_at=web._now(),
            trial_expires_at=web._now() + timedelta(days=1),
        )
        session.add(account)
        session.flush()
        search = SearchRequest(
            web_account_id=account.id,
            language="ru",
            region="Акмолинская область",
            district="Бурабайский район",
            raw_query=f"web-cabinet:{account.id}",
            search_started_at=web._now(),
            search_finished_at=web._now(),
        )
        payment = AccountPayment(
            account_id=account.id,
            payment_status=PaymentStatus.paid.value,
            payment_amount_kzt=1990,
            payment_confirmed_at=web._now(),
        )
        paid_bot_search = SearchRequest(
            telegram_user_id="123456789",
            telegram_chat_id="123456789",
            language="ru",
            region="Акмолинская область",
            district="Косшы",
            payment_status=PaymentStatus.paid.value,
            payment_amount_kzt=1990,
            payment_provider_invoice_id="paid-invoice-1",
            payment_confirmed_at=web._now(),
            status="delivered",
            search_completed_notified_at=web._now(),
            search_started_at=web._now(),
            search_finished_at=web._now(),
        )
        session.add_all([search, payment, paid_bot_search])
        session.commit()

        with client_for(session) as client:
            response = client.get("/admin/analytics", auth=("admin", "secret"))

        assert response.status_code == 200
        assert "Сайт" in response.text
        assert "Зарегистрировались" in response.text
        assert "Начали поиск" in response.text
        assert "Создали счет Kaspi" in response.text
        assert "Оплатили" in response.text
        assert "Оплата подтверждена</td><td><strong>1</strong>" in response.text
        assert "Получил полный отчёт</td><td><strong>1</strong>" in response.text


def test_trial_web_account_without_telegram_can_add_auction_favorite() -> None:
    with build_session() as session:
        account = Account(
            phone="+77018854333",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
            trial_started_at=web._now(),
            trial_expires_at=web._now() + timedelta(hours=1),
        )
        lot = AuctionLot(
            source_lot_id="test-lot-1",
            title="Test auction lot",
            source_url="https://e-qazyna.kz/lot/test-lot-1",
        )
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.post(
                f"/cabinet/auctions/{lot.id}/favorite",
                headers={"referer": "/cabinet/auctions"},
                follow_redirects=False,
            )

        favorite = session.scalar(select(AuctionFavorite))
        assert response.status_code == 303
        assert response.headers["location"] == "/cabinet/auctions?favorite=added"
        assert favorite is not None
        assert favorite.account_id == account.id
        assert favorite.lot_id == lot.id
        assert len(favorite.telegram_user_id) == 32


def test_expired_trial_cannot_add_auction_favorite() -> None:
    with build_session() as session:
        account = Account(
            phone="+77018854333",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
            trial_started_at=web._now() - timedelta(days=2),
            trial_expires_at=web._now() - timedelta(days=1),
        )
        lot = AuctionLot(
            source_lot_id="test-lot-2",
            title="Test auction lot",
            source_url="https://e-qazyna.kz/lot/test-lot-2",
        )
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.post(
                f"/cabinet/auctions/{lot.id}/favorite",
                headers={"referer": "/cabinet/auctions"},
                follow_redirects=False,
            )

        favorite = session.scalar(select(AuctionFavorite))
        assert response.status_code == 303
        assert response.headers["location"] == "/cabinet/auctions?locked=auction_favorite"
        assert favorite is None


def test_auction_filters_accept_empty_numeric_query_values() -> None:
    with build_session() as session:
        account = Account(
            phone="+77018854333",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
            trial_started_at=web._now() - timedelta(days=2),
            trial_expires_at=web._now() - timedelta(days=1),
        )
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get(
                "/cabinet/auctions?region=&district=&locality=&purpose="
                "&min_price_kzt=&max_price_kzt=&min_area_ha=&max_area_ha="
            )

        assert response.status_code == 200
        assert "Анализ земельных аукционов" in response.text
        assert "float_parsing" not in response.text


def test_admin_phone_can_open_hidden_auctions_v2() -> None:
    with build_session() as session:
        account = Account(
            phone="+77026669475",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
        )
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            cabinet_response = client.get("/cabinet")
            response = client.get("/cabinet/auctions-v2")

        assert cabinet_response.status_code == 200
        assert "/cabinet/auctions-v2" in cabinet_response.text
        assert "/cabinet/auctions-v2/analytics" not in cabinet_response.text
        assert response.status_code == 200
        assert "Рабочий стол земельных аукционов" in response.text
        assert "Рабочий режим" in response.text
        assert "/cabinet/auctions-v2/analytics" in response.text


def test_non_admin_phone_cannot_see_or_open_auctions_v2() -> None:
    with build_session() as session:
        account = Account(
            phone="+77018854333",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password-1"),
        )
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            cabinet_response = client.get("/cabinet")
            response = client.get("/cabinet/auctions-v2")

        assert cabinet_response.status_code == 200
        assert "/cabinet/auctions-v2" not in cabinet_response.text
        assert "/cabinet/auctions-v2/analytics" not in cabinet_response.text
        assert response.status_code == 404
