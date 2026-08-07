from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.main as main
import app.rate_limit as rate_limit
import app.web as web
from app.db import Base, get_db
from app.models import Account, SearchRequest


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

    main.app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(main.app) as client:
            client.headers.update(
                {"x-csrf-token": web.csrf_token_value("", "testclient")}
            )
            yield client
    finally:
        main.app.dependency_overrides.clear()


def authorize_client(client: TestClient, session: Session, account: Account) -> None:
    token = "test-web-session"
    session.add(
        web.WebSession(
            account_id=account.id,
            token_hash=web._hash(token),
            expires_at=web._now() + web.timedelta(days=1),
        )
    )
    session.commit()
    client.cookies.set(web.SESSION_COOKIE, token)
    client.headers.update(
        {"x-csrf-token": web.csrf_token_value(token, "testclient")}
    )


def reset_rate_limit_state(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit, "_store", rate_limit._InMemoryRateLimiter())
    if hasattr(rate_limit, "_FALLBACK_STORE"):
        monkeypatch.setattr(
            rate_limit,
            "_FALLBACK_STORE",
            rate_limit._InMemoryRateLimiter(),
        )


def test_api_rate_limit_blocks_excess_requests(monkeypatch) -> None:
    reset_rate_limit_state(monkeypatch)
    monkeypatch.setattr(main, "API_RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(main, "API_RATE_LIMIT_WINDOW_SECONDS", 60)
    monkeypatch.setattr(main, "list_auction_lots", lambda *_args, **_kwargs: ([], 0))
    with client_for(build_session()) as client:
        assert client.get("/api/auctions").status_code == 200
        assert client.get("/api/auctions").status_code == 200
        response = client.get("/api/auctions")
        assert response.status_code == 429
        assert "Retry-After" in response.headers


def test_login_rate_limit_blocks_ip_flooding(monkeypatch) -> None:
    reset_rate_limit_state(monkeypatch)
    monkeypatch.setattr(web, "LOGIN_RATE_LIMIT_IP_PER_MINUTE", 1)
    monkeypatch.setattr(web, "LOGIN_RATE_LIMIT_IP_WINDOW_SECONDS", 60)
    with client_for(build_session()) as client:
        first = client.post(
            "/login",
            data={"phone": "+7 700 000 00 01", "password": "x"},
        )
        second = client.post(
            "/login",
            data={"phone": "+7 700 000 00 01", "password": "x"},
        )
    assert first.status_code == 400
    assert second.status_code == 429


def test_register_request_code_rate_limit_blocks_repeated_requests(monkeypatch) -> None:
    send_calls: list[str] = []
    monkeypatch.setattr(web, "send_login_code", lambda phone, code: send_calls.append(phone))
    monkeypatch.setattr(web, "SMS_REQUEST_RATE_LIMIT_PER_PHONE_PER_HOUR", 1)
    monkeypatch.setattr(web, "SMS_REQUEST_RATE_LIMIT_PER_IP_PER_HOUR", 1)
    monkeypatch.setattr(web, "SMS_REQUEST_RATE_LIMIT_WINDOW_SECONDS", 3600)
    reset_rate_limit_state(monkeypatch)
    with client_for(build_session()) as client:
        first = client.post(
            "/register/request-code",
            data={"phone": "7000000002", "offer_accepted": "yes"},
        )
        second = client.post(
            "/register/request-code",
            data={"phone": "7000000002", "offer_accepted": "yes"},
        )
    assert first.status_code == 200
    assert second.status_code == 429
    assert len(send_calls) == 1


def test_password_reset_request_code_rate_limit_blocks_repeated_requests(monkeypatch) -> None:
    send_calls: list[str] = []
    monkeypatch.setattr(web, "send_login_code", lambda phone, code: send_calls.append(phone))
    monkeypatch.setattr(web, "SMS_REQUEST_RATE_LIMIT_PER_PHONE_PER_HOUR", 1)
    monkeypatch.setattr(web, "SMS_REQUEST_RATE_LIMIT_PER_IP_PER_HOUR", 1)
    monkeypatch.setattr(web, "SMS_REQUEST_RATE_LIMIT_WINDOW_SECONDS", 3600)
    reset_rate_limit_state(monkeypatch)

    with build_session() as session:
        account = Account(
            phone="+77000000003",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password"),
        )
        session.add(account)
        session.commit()

        with client_for(session) as client:
            first = client.post(
                "/password/reset/request-code",
                data={"phone": "+7 700 000 00 03"},
            )
            second = client.post(
                "/password/reset/request-code",
                data={"phone": "+7 700 000 00 03"},
            )

    assert first.status_code == 200
    assert second.status_code == 429
    assert len(send_calls) == 1


def test_cabinet_search_rate_limit_blocks_flooding(monkeypatch) -> None:
    reset_rate_limit_state(monkeypatch)
    monkeypatch.setattr(web, "CABINET_SEARCH_RATE_LIMIT_PER_ACCOUNT_PER_MINUTE", 1)
    monkeypatch.setattr(web, "CABINET_SEARCH_RATE_LIMIT_PER_IP_PER_MINUTE", 1)
    monkeypatch.setattr(
        web,
        "CABINET_SEARCH_RATE_LIMIT_WINDOW_SECONDS",
        60,
    )
    monkeypatch.setattr(web, "dispatch_search", lambda *_args, **_kwargs: None)

    with build_session() as session:
        account = Account(
            phone="+7700000004",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password"),
        )
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            first = client.post(
                "/cabinet/search",
                data={
                    "region": "Region",
                    "district": "District",
                    "locality": "",
                    "purpose": "Индивидуальное строительство",
                    "area_ha": 0.12,
                },
                follow_redirects=False,
            )
            second = client.post(
                "/cabinet/search",
                data={
                    "region": "Region",
                    "district": "District",
                    "locality": "",
                    "purpose": "Индивидуальное строительство",
                    "area_ha": 0.12,
                },
                follow_redirects=False,
            )

    assert first.status_code == 303
    assert second.status_code == 429


def test_cabinet_search_status_rate_limit_blocks_polling(monkeypatch) -> None:
    reset_rate_limit_state(monkeypatch)
    monkeypatch.setattr(web, "CABINET_SEARCH_STATUS_RATE_LIMIT_PER_ACCOUNT_PER_MINUTE", 1)
    monkeypatch.setattr(web, "CABINET_SEARCH_STATUS_RATE_LIMIT_PER_IP_PER_MINUTE", 1)
    monkeypatch.setattr(web, "CABINET_SEARCH_STATUS_WINDOW_SECONDS", 60)

    with build_session() as session:
        account = Account(
            phone="+7700000005",
            phone_verified_at=web._now(),
            password_hash=web._hash_password("password"),
        )
        session.add(account)
        session.commit()
        request = SearchRequest(
            language="ru",
            region="Region",
            district="District",
            locality="",
            purpose="Индивидуальное строительство",
            area_ha=0.12,
            raw_query=f"web-cabinet:{account.id}",
        )
        session.add(request)
        session.commit()
        request.web_account_id = account.id
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            first = client.get(f"/cabinet/searches/{request.id}/status")
            second = client.get(f"/cabinet/searches/{request.id}/status")

    assert first.status_code == 200
    assert second.status_code == 429
