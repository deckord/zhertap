from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base, get_db
from app.main import app
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

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            client.headers.update(
                {"x-csrf-token": web.csrf_token_value("", "testclient")}
            )
            yield client
    finally:
        app.dependency_overrides.clear()


def test_landing_leads_with_search_form_and_lph_message() -> None:
    with build_session() as session, client_for(session) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Найдите участок под ЛПХ или садоводство за несколько минут" in response.text
    assert 'action="/guest-search"' in response.text
    assert 'name="region"' in response.text
    assert 'name="district"' in response.text
    assert 'name="purpose"' in response.text
    assert "НАЙТИ УЧАСТКИ" in response.text
    assert "без регистрации" in response.text
    assert "1490 ₸" in response.text
    assert "Аукционы" not in response.text


def test_public_catalog_endpoints_do_not_require_account(monkeypatch) -> None:
    monkeypatch.setattr(
        web,
        "_egkn_region_rows",
        lambda: [{"value": "Акмолинская область", "label": "Акмолинская область"}],
    )
    monkeypatch.setattr(
        web,
        "_egkn_district_rows",
        lambda region: [{"value": "Бурабайский район", "label": "Бурабайский район", "id": 7}],
    )
    with build_session() as session, client_for(session) as client:
        regions = client.get("/catalog/regions")
        districts = client.get(
            "/catalog/districts", params={"region": "Акмолинская область"}
        )

    assert regions.status_code == 200
    assert regions.json()[0]["value"] == "Акмолинская область"
    assert districts.status_code == 200
    assert districts.json()[0]["id"] == 7


def test_guest_search_starts_without_account_and_is_cookie_scoped(monkeypatch) -> None:
    dispatched: list[str] = []
    monkeypatch.setattr(web, "dispatch_search", dispatched.append)

    with build_session() as session, client_for(session) as client:
        response = client.post(
            "/guest-search",
            data={
                "region": "Акмолинская область",
                "district": "Бурабайский район",
                "locality": "Бурабай",
                "purpose": "Садоводство",
                "area_ha": "0.12",
            },
            follow_redirects=False,
        )
        search = session.scalar(select(SearchRequest))

        assert response.status_code == 303
        assert search is not None
        assert search.web_account_id is None
        assert search.raw_query.startswith("web-guest:")
        assert response.headers["location"] == f"/guest-searches/{search.id}"
        assert dispatched == [search.id]

        status = client.get(f"/guest-searches/{search.id}/status")
        detail = client.get(f"/guest-searches/{search.id}")

        other_client = TestClient(app)
        forbidden = other_client.get(f"/guest-searches/{search.id}/status")
        other_client.close()

    assert status.status_code == 200
    assert status.json()["status"] == search.status
    assert "Акмолинская область" in detail.text
    assert "Получить результаты" in detail.text
    assert forbidden.status_code == 404


def test_guest_search_is_claimed_when_user_logs_in() -> None:
    with build_session() as session:
        account = Account(phone="+77010000000")
        search = SearchRequest(
            region="Акмолинская область",
            district="Бурабайский район",
            purpose="ЛПХ(новый поиск)",
            raw_query=f"web-guest:{web._hash('guest-token')}",
        )
        session.add_all([account, search])
        session.commit()

        request = type(
            "RequestStub",
            (),
            {"cookies": {web.GUEST_SEARCH_COOKIE: "guest-token"}},
        )()
        claimed = web._claim_guest_search(request, session, account)
        session.commit()

        assert claimed is not None
        assert claimed.id == search.id
        assert search.web_account_id == account.id
        assert search.raw_query == f"web-cabinet:{account.id}:claimed-guest"
