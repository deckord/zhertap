from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Candidate, SearchRequest, UrbanPlanStatus
from app.schemas import ReviewUpdate
from app.services import split_telegram_message, update_candidate_review


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def add_candidate(session: Session) -> Candidate:
    request = SearchRequest(region="Акмолинская область", district="Бурабайский район")
    session.add(request)
    session.flush()
    candidate = Candidate(
        request_id=request.id,
        rank=1,
        region_chain="Акмолинская область (01) → р-н Бурабайский (01-171)",
        locality="с. Златополье",
        latitude=52.862091,
        longitude=69.948735,
        nearby_cadastre="01171008003",
        nearby_distance_m=36,
        nearby_land_use="ЛПХ",
        requested_area_ha=0.25,
        road_distance_m=45,
        power_evidence="Предварительно",
        water_evidence="Нет данных",
        sewer_evidence="Не подтверждено",
        cemetery_distance_m=None,
        score=94,
        risk_notes="Предварительный кандидат",
        google_maps_url="https://www.google.com/maps/@52.862091,69.948735,19z",
        urban_plan_status=UrbanPlanStatus.passed.value,
    )
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    return candidate


def test_candidate_can_be_approved_from_egkn_geometry() -> None:
    with build_session() as session:
        candidate = add_candidate(session)
        updated = update_candidate_review(
            session,
            candidate.id,
            ReviewUpdate(status="approved", google_checked=False),
        )

        assert updated.google_checked is False
        assert updated.review_status == "approved"


def test_operator_can_add_note_to_approved_candidate() -> None:
    with build_session() as session:
        candidate = add_candidate(session)
        updated = update_candidate_review(
            session,
            candidate.id,
            ReviewUpdate(
                status="approved_with_note",
                notes="Геометрия подходит; проверить юридический статус в акимате.",
            ),
        )

        assert updated.google_checked is False
        assert updated.review_status == "approved_with_note"


def test_long_telegram_message_is_split_on_blocks() -> None:
    message = "\n\n".join(["Кандидат " + ("x" * 800) for _ in range(10)])

    parts = split_telegram_message(message, limit=1000)

    assert len(parts) > 1
    assert all(len(part) <= 1000 for part in parts)
    assert "".join(parts).replace("\n\n", "") == message.replace("\n\n", "")
