import uuid
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class SearchStatus(StrEnum):
    queued = "queued"
    processing = "processing"
    review = "review"
    ready = "ready"
    delivered = "delivered"
    failed = "failed"


class ReviewStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    approved_with_note = "approved_with_note"
    rejected = "rejected"


class PaymentStatus(StrEnum):
    not_requested = "not_requested"
    awaiting_transfer = "awaiting_transfer"
    pending_confirmation = "pending_confirmation"
    paid = "paid"
    rejected = "rejected"


class FreePreviewStatus(StrEnum):
    not_requested = "not_requested"
    pending = "pending"
    delivered = "delivered"
    rejected = "rejected"


class UrbanPlanStatus(StrEnum):
    pending = "pending"
    passed = "passed"
    unavailable = "unavailable"
    blocked = "blocked"
    waived = "waived"


class PlanningCandidateStatus(StrEnum):
    queued = "queued"
    empty = "empty"
    built = "built"
    road = "road"
    garden = "garden"
    unclear = "unclear"


class GenplanPipelineStatus(StrEnum):
    ingested = "ingested"
    missing_file = "missing_file"
    needs_pdf_page_selection = "needs_pdf_page_selection"
    ready_for_vector_extraction = "ready_for_vector_extraction"
    ready_for_legend_extraction = "ready_for_legend_extraction"
    legend_draft_ready = "legend_draft_ready"
    needs_review = "needs_review"
    failed = "failed"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_hash: Mapped[str | None] = mapped_column(String(220))
    password_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_user_id: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32))
    telegram_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_access: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auction_plan: Mapped[str] = mapped_column(
        String(24), default="observer", index=True
    )
    access_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    offer_version: Mapped[str | None] = mapped_column(String(32))
    offer_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offer_accepted_ip: Mapped[str | None] = mapped_column(String(64))
    offer_accepted_user_agent: Mapped[str | None] = mapped_column(Text)
    onboarding_tour_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    onboarding_tour_dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuctionWorkspace(Base):
    __tablename__ = "auction_workspaces"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(160))
    owner_account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id"), unique=True, index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    owner: Mapped[Account] = relationship(foreign_keys=[owner_account_id])


class AuctionWorkspaceMember(Base):
    __tablename__ = "auction_workspace_members"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "account_id", name="uq_auction_workspace_member"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("auction_workspaces.id"), index=True
    )
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    role: Mapped[str] = mapped_column(String(24), default="analyst", index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    invited_by_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    workspace: Mapped[AuctionWorkspace] = relationship()
    account: Mapped[Account] = relationship(foreign_keys=[account_id])
    invited_by: Mapped[Account | None] = relationship(
        foreign_keys=[invited_by_account_id]
    )


class WebLoginCode(Base):
    __tablename__ = "web_login_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    phone: Mapped[str] = mapped_column(String(32), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(24), default="login")
    password_hash: Mapped[str | None] = mapped_column(String(220))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebSession(Base):
    __tablename__ = "web_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship()


class TelegramLinkToken(Base):
    __tablename__ = "telegram_link_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    telegram_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship()


class AccountPayment(Base):
    __tablename__ = "account_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    payment_status: Mapped[str] = mapped_column(
        String(32), default=PaymentStatus.awaiting_transfer.value, index=True
    )
    payment_amount_kzt: Mapped[int | None] = mapped_column(Integer)
    target_plan: Mapped[str] = mapped_column(String(24), default="investor", index=True)
    payment_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_confirmed_by: Mapped[str | None] = mapped_column(String(64))
    payment_provider: Mapped[str | None] = mapped_column(String(32))
    payment_provider_invoice_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payment_provider_status: Mapped[str | None] = mapped_column(String(32))
    payment_provider_url: Mapped[str | None] = mapped_column(Text)
    payment_provider_qr_image_url: Mapped[str | None] = mapped_column(Text)
    payment_provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[Account] = relationship()


class SearchRequest(Base):
    __tablename__ = "search_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    web_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), index=True
    )
    telegram_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32))
    funnel_session_id: Mapped[str | None] = mapped_column(String(36), index=True)
    language: Mapped[str] = mapped_column(String(2), default="ru")
    region: Mapped[str] = mapped_column(String(160))
    region_label: Mapped[str | None] = mapped_column(String(160))
    district: Mapped[str] = mapped_column(String(160), index=True)
    district_label: Mapped[str | None] = mapped_column(String(160))
    locality: Mapped[str | None] = mapped_column(String(160))
    locality_label: Mapped[str | None] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(String(80), default="ЛПХ")
    allotment_type: Mapped[str | None] = mapped_column(String(32))
    irrigation_type: Mapped[str | None] = mapped_column(String(32))
    area_ha: Mapped[float] = mapped_column(Float, default=0.10)
    result_limit: Mapped[int] = mapped_column(Integer, default=10)
    cemetery_buffer_m: Mapped[int] = mapped_column(Integer, default=0)
    max_road_distance_m: Mapped[int] = mapped_column(Integer, default=200)
    max_power_distance_m: Mapped[int] = mapped_column(Integer, default=300)
    raw_query: Mapped[str | None] = mapped_column(Text)
    retry_of_request_id: Mapped[str | None] = mapped_column(String(36), index=True)
    continuation_of_request_id: Mapped[str | None] = mapped_column(String(36), index=True)
    batch_number: Mapped[int] = mapped_column(Integer, default=1)
    terms_version: Mapped[str | None] = mapped_column(String(32))
    terms_text_snapshot: Mapped[str | None] = mapped_column(Text)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default=SearchStatus.queued.value, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    progress_message_id: Mapped[int | None] = mapped_column(Integer)
    search_completed_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    search_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_outcome: Mapped[str | None] = mapped_column(String(48), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    urban_plan_status: Mapped[str] = mapped_column(
        String(32), default=UrbanPlanStatus.pending.value, index=True
    )
    urban_plan_message: Mapped[str | None] = mapped_column(Text)
    urban_plan_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    urban_plan_override_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    urban_plan_override_user_id: Mapped[str | None] = mapped_column(String(32))
    urban_plan_override_text: Mapped[str | None] = mapped_column(Text)
    urban_plan_waiver_kind: Mapped[str | None] = mapped_column(String(32))
    urban_plan_auto_waive_reason: Mapped[str | None] = mapped_column(Text)
    urban_plan_coverage_status: Mapped[str | None] = mapped_column(String(32), index=True)
    urban_plan_coverage_id: Mapped[int | None] = mapped_column(Integer)
    payment_status: Mapped[str] = mapped_column(
        String(32), default=PaymentStatus.not_requested.value, index=True
    )
    payment_amount_kzt: Mapped[int | None] = mapped_column(Integer)
    payment_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_confirmed_by: Mapped[str | None] = mapped_column(String(64))
    payment_confirmation_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    payment_provider: Mapped[str | None] = mapped_column(String(32))
    payment_provider_invoice_id: Mapped[str | None] = mapped_column(
        String(64), index=True
    )
    payment_provider_status: Mapped[str | None] = mapped_column(String(32))
    payment_provider_url: Mapped[str | None] = mapped_column(Text)
    payment_provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    free_preview_status: Mapped[str] = mapped_column(
        String(32), default=FreePreviewStatus.not_requested.value, index=True
    )
    free_preview_count: Mapped[int] = mapped_column(Integer, default=0)
    free_preview_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    free_preview_approved_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    candidates: Mapped[list["Candidate"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="Candidate.rank"
    )


class FunnelEvent(Base):
    __tablename__ = "funnel_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_name: Mapped[str] = mapped_column(String(64), index=True)
    funnel_version: Mapped[str] = mapped_column(String(8), default="v2", index=True)
    telegram_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32))
    request_id: Mapped[str | None] = mapped_column(String(36), index=True)
    funnel_session_id: Mapped[str | None] = mapped_column(String(36), index=True)
    language: Mapped[str] = mapped_column(String(2), default="ru")
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FeedbackConversation(Base):
    __tablename__ = "feedback_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    telegram_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32), index=True)
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    language: Mapped[str] = mapped_column(String(2), default="ru", index=True)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_client_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_admin_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[Account | None] = relationship()
    messages: Mapped[list["FeedbackMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="FeedbackMessage.created_at",
    )


class FeedbackBroadcast(Base):
    __tablename__ = "feedback_broadcasts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(160))
    ru_text: Mapped[str] = mapped_column(Text)
    kz_text: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(120))
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    responded_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recipients: Mapped[list["FeedbackBroadcastRecipient"]] = relationship(
        back_populates="broadcast", cascade="all, delete-orphan"
    )


class FeedbackMessage(Base):
    __tablename__ = "feedback_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_conversations.id"), index=True
    )
    broadcast_id: Mapped[str | None] = mapped_column(
        ForeignKey("feedback_broadcasts.id"), index=True
    )
    sender_type: Mapped[str] = mapped_column(String(16), index=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    text: Mapped[str] = mapped_column(Text)
    delivery_status: Mapped[str] = mapped_column(String(24), default="stored", index=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[FeedbackConversation] = relationship(back_populates="messages")
    broadcast: Mapped[FeedbackBroadcast | None] = relationship()


class FeedbackBroadcastRecipient(Base):
    __tablename__ = "feedback_broadcast_recipients"
    __table_args__ = (
        UniqueConstraint("broadcast_id", "telegram_user_id", name="uq_feedback_broadcast_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broadcast_id: Mapped[str] = mapped_column(ForeignKey("feedback_broadcasts.id"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("feedback_conversations.id"), index=True
    )
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    telegram_user_id: Mapped[str] = mapped_column(String(32), index=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), index=True)
    language: Mapped[str] = mapped_column(String(2), default="ru", index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    broadcast: Mapped[FeedbackBroadcast] = relationship(back_populates="recipients")
    conversation: Mapped[FeedbackConversation | None] = relationship()
    account: Mapped[Account | None] = relationship()


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("search_requests.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    region_chain: Mapped[str] = mapped_column(String(260))
    locality: Mapped[str] = mapped_column(String(180))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    nearby_cadastre: Mapped[str] = mapped_column(String(32), index=True)
    nearby_distance_m: Mapped[float | None] = mapped_column(Float)
    nearby_land_use: Mapped[str | None] = mapped_column(String(240))
    nearby_category_id: Mapped[str | None] = mapped_column(String(16))
    requested_area_ha: Mapped[float] = mapped_column(Float, default=0.10)
    road_distance_m: Mapped[float | None] = mapped_column(Float)
    power_evidence: Mapped[str] = mapped_column(Text)
    water_evidence: Mapped[str] = mapped_column(Text)
    sewer_evidence: Mapped[str] = mapped_column(Text)
    cemetery_distance_m: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float)
    risk_notes: Mapped[str] = mapped_column(Text, default="")
    google_maps_url: Mapped[str] = mapped_column(Text)
    egkn_url: Mapped[str] = mapped_column(Text, default="https://map.gov4c.kz/egkn/")
    source_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    review_status: Mapped[str] = mapped_column(String(32), default=ReviewStatus.pending.value)
    review_notes: Mapped[str] = mapped_column(Text, default="")
    google_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    google_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer: Mapped[str | None] = mapped_column(String(120))
    urban_plan_status: Mapped[str] = mapped_column(
        String(32), default=UrbanPlanStatus.pending.value, index=True
    )
    urban_plan_zone: Mapped[str | None] = mapped_column(String(240))
    urban_plan_document: Mapped[str | None] = mapped_column(String(320))
    urban_plan_source_url: Mapped[str | None] = mapped_column(Text)

    request: Mapped[SearchRequest] = relationship(back_populates="candidates")


class UrbanPlanLayer(Base):
    __tablename__ = "urban_plan_layers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region: Mapped[str] = mapped_column(String(160), index=True)
    district: Mapped[str] = mapped_column(String(160), index=True)
    locality: Mapped[str] = mapped_column(String(160), index=True)
    purpose: Mapped[str] = mapped_column(String(32), default="all", index=True)
    layer_kind: Mapped[str] = mapped_column(String(32), index=True)
    zone_name: Mapped[str | None] = mapped_column(String(240))
    title: Mapped[str] = mapped_column(String(320))
    approval_document: Mapped[str] = mapped_column(String(320))
    approval_date: Mapped[date | None] = mapped_column(Date)
    source_authority: Mapped[str] = mapped_column(String(240))
    source_url: Mapped[str] = mapped_column(Text)
    source_epsg: Mapped[int] = mapped_column(Integer, default=4326)
    source_file_name: Mapped[str | None] = mapped_column(String(260))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_version: Mapped[str | None] = mapped_column(String(120))
    provenance_status: Mapped[str] = mapped_column(
        String(64), default="unknown", index=True
    )
    identity_status: Mapped[str] = mapped_column(
        String(64), default="unverified", index=True
    )
    qa_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    independent_review: Mapped[bool] = mapped_column(Boolean, default=False)
    qa_review_json: Mapped[str | None] = mapped_column(Text)
    approved_for_search: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True
    )
    uploaded_by: Mapped[str | None] = mapped_column(String(120))
    geometry_geojson: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class GenplanSourceDocument(Base):
    __tablename__ = "genplan_source_documents"
    __table_args__ = (
        UniqueConstraint("asset_id", name="uq_genplan_source_document_asset_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(96), index=True)
    region: Mapped[str] = mapped_column(String(160), default="", index=True)
    district: Mapped[str] = mapped_column(String(160), default="", index=True)
    locality: Mapped[str] = mapped_column(String(160), default="", index=True)
    title: Mapped[str] = mapped_column(String(320), default="")
    filename: Mapped[str] = mapped_column(String(260))
    relative_path: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    detected_format: Mapped[str] = mapped_column(String(24), index=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    source_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    pdf_route: Mapped[str | None] = mapped_column(String(24), index=True)
    has_text_layer: Mapped[bool] = mapped_column(Boolean, default=False)
    vector_object_count: Mapped[int] = mapped_column(Integer, default=0)
    image_count: Mapped[int] = mapped_column(Integer, default=0)
    max_image_width: Mapped[int | None] = mapped_column(Integer)
    max_image_height: Mapped[int | None] = mapped_column(Integer)
    confidence_score: Mapped[float | None] = mapped_column(Float)
    pipeline_status: Mapped[str] = mapped_column(
        String(48), default=GenplanPipelineStatus.ingested.value, index=True
    )
    next_action: Mapped[str] = mapped_column(String(120), default="")
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_metadata_json: Mapped[str | None] = mapped_column(Text)
    ingested_by: Mapped[str | None] = mapped_column(String(120))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class GenplanLegendEntry(Base):
    __tablename__ = "genplan_legend_entries"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "color_hex",
            "source",
            name="uq_genplan_legend_entry_document_color_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("genplan_source_documents.id", ondelete="CASCADE"),
        index=True,
    )
    color_hex: Mapped[str] = mapped_column(String(7), index=True)
    red: Mapped[int] = mapped_column(Integer)
    green: Mapped[int] = mapped_column(Integer)
    blue: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(48), default="dominant_color", index=True)
    label_ru: Mapped[str | None] = mapped_column(String(240))
    label_kz: Mapped[str | None] = mapped_column(String(240))
    target_category: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    layer_kind: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.25)
    review_status: Mapped[str] = mapped_column(String(32), default="needs_review", index=True)
    pixel_count: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UrbanPlanSource(Base):
    __tablename__ = "urban_plan_sources"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "external_id",
            name="uq_urban_plan_source_platform_external",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="digital_vector", index=True)
    external_id: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(160), default="", index=True)
    district: Mapped[str] = mapped_column(String(160), default="", index=True)
    locality: Mapped[str] = mapped_column(String(160), default="", index=True)
    title: Mapped[str] = mapped_column(String(320))
    approval_document: Mapped[str] = mapped_column(String(320), default="")
    approval_date: Mapped[str] = mapped_column(String(32), default="")
    source_authority: Mapped[str] = mapped_column(String(240), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    api_base_url: Mapped[str] = mapped_column(Text, default="")
    admterr_id: Mapped[str] = mapped_column(String(120), default="")
    profiles_json: Mapped[str | None] = mapped_column(Text)
    collections_json: Mapped[str | None] = mapped_column(Text)
    coverage_status: Mapped[str] = mapped_column(String(32), default="candidate", index=True)
    import_status: Mapped[str] = mapped_column(String(32), default="not_imported", index=True)
    layer_count: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    raw_payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UrbanPlanCoverage(Base):
    __tablename__ = "urban_plan_coverage"
    __table_args__ = (
        UniqueConstraint(
            "region",
            "district",
            "locality",
            "purpose",
            name="uq_urban_plan_coverage_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region: Mapped[str] = mapped_column(String(160), index=True)
    district: Mapped[str] = mapped_column(String(160), index=True)
    locality: Mapped[str] = mapped_column(String(160), default="", index=True)
    purpose: Mapped[str] = mapped_column(String(64), default="all", index=True)
    coverage_status: Mapped[str] = mapped_column(String(32), index=True)
    approved_layer_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PlanningCandidateReview(Base):
    __tablename__ = "planning_candidate_reviews"
    __table_args__ = (
        UniqueConstraint(
            "region",
            "district",
            "locality",
            "requested_use",
            "latitude",
            "longitude",
            name="uq_planning_candidate_review_point",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region: Mapped[str] = mapped_column(String(160), index=True)
    district: Mapped[str] = mapped_column(String(160), index=True)
    locality: Mapped[str] = mapped_column(String(160), default="", index=True)
    requested_use: Mapped[str] = mapped_column(String(64), index=True)
    latitude: Mapped[float] = mapped_column(Float, index=True)
    longitude: Mapped[float] = mapped_column(Float, index=True)
    google_maps_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    trust_level: Mapped[str | None] = mapped_column(String(32))
    allowed_area_ha: Mapped[float | None] = mapped_column(Float)
    nearby_cadastre: Mapped[str | None] = mapped_column(String(64))
    nearby_distance_m: Mapped[float | None] = mapped_column(Float)
    nearby_land_use: Mapped[str | None] = mapped_column(String(240))
    candidate_area_ha: Mapped[float | None] = mapped_column(Float)
    selection_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuctionLot(Base):
    __tablename__ = "auction_lots"
    __table_args__ = (
        UniqueConstraint("source", "source_lot_id", name="uq_auction_lot_source_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source: Mapped[str] = mapped_column(String(32), default="e-qazyna", index=True)
    source_lot_id: Mapped[str] = mapped_column(String(64), index=True)
    source_search_status: Mapped[str | None] = mapped_column(String(64), index=True)
    auction_number: Mapped[str | None] = mapped_column(String(32), index=True)
    object_type: Mapped[str] = mapped_column(String(64), default="land", index=True)
    auction_type: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str | None] = mapped_column(Text, index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(160), index=True)
    district: Mapped[str | None] = mapped_column(String(160), index=True)
    locality: Mapped[str | None] = mapped_column(String(160))
    location_text: Mapped[str | None] = mapped_column(Text)
    cadastre_number: Mapped[str | None] = mapped_column(String(64), index=True)
    area_ha: Mapped[float | None] = mapped_column(Float, index=True)
    land_rights: Mapped[str | None] = mapped_column(String(240))
    functional_purpose_level2: Mapped[str | None] = mapped_column(String(240), index=True)
    functional_purpose_level3: Mapped[str | None] = mapped_column(String(320))
    functional_purpose_level4: Mapped[str | None] = mapped_column(String(320))
    use_goal: Mapped[str | None] = mapped_column(String(160))
    purpose: Mapped[str | None] = mapped_column(Text)
    start_price_kzt: Mapped[float | None] = mapped_column(Float, index=True)
    guarantee_kzt: Mapped[float | None] = mapped_column(Float)
    sale_price_kzt: Mapped[float | None] = mapped_column(Float)
    auction_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[date | None] = mapped_column(Date)
    seller_name: Mapped[str | None] = mapped_column(Text)
    seller_bin: Mapped[str | None] = mapped_column(String(16), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    source_object_url: Mapped[str | None] = mapped_column(Text)
    raw_payload_json: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    documents: Mapped[list["AuctionDocument"]] = relationship(
        back_populates="lot", cascade="all, delete-orphan", order_by="AuctionDocument.id"
    )
    history: Mapped[list["AuctionLotHistory"]] = relationship(
        back_populates="lot", cascade="all, delete-orphan", order_by="AuctionLotHistory.observed_at"
    )
    changes: Mapped[list["AuctionLotChange"]] = relationship(
        back_populates="lot", cascade="all, delete-orphan", order_by="AuctionLotChange.changed_at"
    )


class AuctionDocument(Base):
    __tablename__ = "auction_documents"
    __table_args__ = (
        UniqueConstraint("lot_id", "source_url", name="uq_auction_document_lot_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    title: Mapped[str] = mapped_column(String(320))
    source_url: Mapped[str] = mapped_column(Text)
    file_type: Mapped[str | None] = mapped_column(String(32))
    storage_status: Mapped[str] = mapped_column(String(32), default="linked")
    local_path: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    download_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lot: Mapped[AuctionLot] = relationship(back_populates="documents")


class AuctionLotHistory(Base):
    __tablename__ = "auction_lot_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    status: Mapped[str | None] = mapped_column(Text, index=True)
    start_price_kzt: Mapped[float | None] = mapped_column(Float)
    sale_price_kzt: Mapped[float | None] = mapped_column(Float)
    auction_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lot: Mapped[AuctionLot] = relationship(back_populates="history")


class AuctionLotChange(Base):
    __tablename__ = "auction_lot_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(80), index=True)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    lot: Mapped[AuctionLot] = relationship(back_populates="changes")


class AuctionFavorite(Base):
    __tablename__ = "auction_favorites"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", "lot_id", name="uq_auction_favorite_user_lot"),
        UniqueConstraint("account_id", "lot_id", name="uq_auction_favorite_account_lot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    telegram_user_id: Mapped[str] = mapped_column(String(32), index=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lot: Mapped[AuctionLot] = relationship()


class AuctionSubscription(Base):
    __tablename__ = "auction_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "telegram_user_id",
            "region",
            "district",
            "locality",
            "purpose_query",
            "min_price_kzt",
            "max_price_kzt",
            "min_area_ha",
            "max_area_ha",
            name="uq_auction_subscription_filter_v2",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    telegram_user_id: Mapped[str] = mapped_column(String(32), index=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), index=True)
    language: Mapped[str] = mapped_column(String(2), default="ru")
    region: Mapped[str | None] = mapped_column(String(160), index=True)
    district: Mapped[str | None] = mapped_column(String(160), index=True)
    locality: Mapped[str | None] = mapped_column(String(160), index=True)
    purpose_query: Mapped[str | None] = mapped_column(String(160))
    min_price_kzt: Mapped[float | None] = mapped_column(Float)
    max_price_kzt: Mapped[float | None] = mapped_column(Float)
    min_area_ha: Mapped[float | None] = mapped_column(Float)
    max_area_ha: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuctionNotification(Base):
    __tablename__ = "auction_notifications"
    __table_args__ = (
        UniqueConstraint("subscription_id", "lot_id", name="uq_auction_notification_lot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("auction_subscriptions.id"), index=True
    )
    lot_id: Mapped[str] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionAccess(Base):
    __tablename__ = "auction_access"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    telegram_user_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True
    )
    telegram_chat_id: Mapped[str] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(2), default="ru")
    free_lot_id: Mapped[str | None] = mapped_column(
        ForeignKey("auction_lots.id"), index=True
    )
    paid_access: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    payment_status: Mapped[str] = mapped_column(
        String(32), default=PaymentStatus.not_requested.value, index=True
    )
    payment_amount_kzt: Mapped[int | None] = mapped_column(Integer)
    payment_provider: Mapped[str | None] = mapped_column(String(32))
    payment_provider_invoice_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )
    payment_provider_status: Mapped[str | None] = mapped_column(String(32))
    payment_provider_url: Mapped[str | None] = mapped_column(Text)
    payment_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    payment_provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    payment_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    payment_confirmation_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    free_lot: Mapped[AuctionLot | None] = relationship()


class AuctionSource(Base):
    __tablename__ = "auction_sources"
    __table_args__ = (
        UniqueConstraint("code", name="uq_auction_source_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(80), index=True)
    source_type: Mapped[str] = mapped_column(String(48), index=True)
    name: Mapped[str] = mapped_column(String(220))
    base_url: Mapped[str] = mapped_column(Text)
    region: Mapped[str] = mapped_column(String(160), default="all", index=True)
    parser_kind: Mapped[str] = mapped_column(String(64), default="planned", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True)
    crawl_interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    quality_status: Mapped[str] = mapped_column(String(32), default="planned", index=True)
    legal_status: Mapped[str] = mapped_column(String(32), default="public", index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuctionCrawlRun(Base):
    __tablename__ = "auction_crawl_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("auction_sources.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_created: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_payload_json: Mapped[str | None] = mapped_column(Text)

    source: Mapped[AuctionSource] = relationship()


class AuctionEvidence(Base):
    __tablename__ = "auction_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("auction_sources.id"), index=True)
    evidence_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="found", index=True)
    title: Mapped[str] = mapped_column(String(320), default="")
    value_text: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    raw_payload_json: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    lot: Mapped[AuctionLot] = relationship()
    source: Mapped[AuctionSource | None] = relationship()


class AuctionLotV2Analysis(Base):
    __tablename__ = "auction_lot_v2_analysis"
    __table_args__ = (
        UniqueConstraint("lot_id", name="uq_auction_lot_v2_analysis_lot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    confidence_level: Mapped[str] = mapped_column(String(16), default="low", index=True)
    recommended_action: Mapped[str] = mapped_column(String(64), default="inspect", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    readiness_json: Mapped[str] = mapped_column(Text, default="[]")
    risk_flags_json: Mapped[str] = mapped_column(Text, default="[]")
    source_status_json: Mapped[str] = mapped_column(Text, default="[]")
    max_bid_conservative_kzt: Mapped[float | None] = mapped_column(Float)
    max_bid_market_kzt: Mapped[float | None] = mapped_column(Float)
    max_bid_aggressive_kzt: Mapped[float | None] = mapped_column(Float)
    price_per_sotka: Mapped[float | None] = mapped_column(Float)
    district_average_price_per_sotka: Mapped[float | None] = mapped_column(Float)
    district_difference_percent: Mapped[float | None] = mapped_column(Float)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    lot: Mapped[AuctionLot] = relationship()


class AuctionLotGeoCheck(Base):
    __tablename__ = "auction_lot_geo_checks"
    __table_args__ = (
        UniqueConstraint("lot_id", name="uq_auction_lot_geo_check_lot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    cadastre_status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    boundary_status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    boundary_area_ha: Mapped[float | None] = mapped_column(Float)
    boundary_difference_percent: Mapped[float | None] = mapped_column(Float)
    boundary_source: Mapped[str | None] = mapped_column(String(120))
    coordinate_status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    urban_plan_status: Mapped[str] = mapped_column(
        String(32), default="manual_required", index=True
    )
    red_line_status: Mapped[str] = mapped_column(
        String(32), default="manual_required", index=True
    )
    engineering_status: Mapped[str] = mapped_column(
        String(32), default="manual_required", index=True
    )
    market_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    osm_status: Mapped[str] = mapped_column(String(32), default="not_checked", index=True)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    road_distance_m: Mapped[float | None] = mapped_column(Float)
    power_distance_m: Mapped[float | None] = mapped_column(Float)
    water_distance_m: Mapped[float | None] = mapped_column(Float)
    open_water_distance_m: Mapped[float | None] = mapped_column(Float)
    cemetery_distance_m: Mapped[float | None] = mapped_column(Float)
    object_distance_m: Mapped[float | None] = mapped_column(Float)
    object_kind: Mapped[str | None] = mapped_column(String(80))
    egkn_url: Mapped[str | None] = mapped_column(Text)
    google_maps_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    osm_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    lot: Mapped[AuctionLot] = relationship()


class AuctionMarketComparable(Base):
    __tablename__ = "auction_market_comparables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str | None] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    source_name: Mapped[str] = mapped_column(String(80), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(320), default="")
    region: Mapped[str | None] = mapped_column(String(160), index=True)
    district: Mapped[str | None] = mapped_column(String(160), index=True)
    locality: Mapped[str | None] = mapped_column(String(160), index=True)
    area_ha: Mapped[float | None] = mapped_column(Float)
    price_kzt: Mapped[float | None] = mapped_column(Float)
    price_per_sotka: Mapped[float | None] = mapped_column(Float, index=True)
    listing_status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    raw_payload_json: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    lot: Mapped[AuctionLot | None] = relationship()


class AuctionWatchlist(Base):
    __tablename__ = "auction_watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(160), default="Мой мониторинг")
    region: Mapped[str | None] = mapped_column(String(160), index=True)
    district: Mapped[str | None] = mapped_column(String(160), index=True)
    locality: Mapped[str | None] = mapped_column(String(160), index=True)
    purpose_query: Mapped[str | None] = mapped_column(String(160))
    lot_scope: Mapped[str | None] = mapped_column(String(24), default="active", index=True)
    eqazyna_status: Mapped[str | None] = mapped_column(String(64), index=True)
    min_score: Mapped[int | None] = mapped_column(Integer)
    min_price_kzt: Mapped[float | None] = mapped_column(Float)
    max_price_kzt: Mapped[float | None] = mapped_column(Float)
    min_area_ha: Mapped[float | None] = mapped_column(Float)
    max_area_ha: Mapped[float | None] = mapped_column(Float)
    risk_level: Mapped[str | None] = mapped_column(String(16), index=True)
    confidence_level: Mapped[str | None] = mapped_column(String(16), index=True)
    stage: Mapped[str | None] = mapped_column(String(40), index=True)
    deadline_status: Mapped[str | None] = mapped_column(String(24), index=True)
    geo_status: Mapped[str | None] = mapped_column(String(32), index=True)
    notify_channels_json: Mapped[str] = mapped_column(Text, default='["web","telegram"]')
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[Account] = relationship()


class AuctionWatchlistNotification(Base):
    __tablename__ = "auction_watchlist_notifications"
    __table_args__ = (
        UniqueConstraint(
            "watchlist_id",
            "lot_id",
            "channel",
            "event_key",
            name="uq_auction_watchlist_notification_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(ForeignKey("auction_watchlists.id"), index=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    channel: Mapped[str] = mapped_column(String(24), default="web", index=True)
    event_type: Mapped[str] = mapped_column(String(48), default="new_lot", index=True)
    event_key: Mapped[str] = mapped_column(String(160), default="new_lot", index=True)
    title: Mapped[str | None] = mapped_column(String(240))
    detail: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    watchlist: Mapped[AuctionWatchlist] = relationship()
    lot: Mapped[AuctionLot] = relationship()


class AuctionUserLotPipeline(Base):
    __tablename__ = "auction_user_lot_pipeline"
    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_auction_pipeline_account_lot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("auction_lots.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40), default="watching", index=True)
    decision: Mapped[str | None] = mapped_column(String(40), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    max_bid_kzt: Mapped[float | None] = mapped_column(Float)
    costs_json: Mapped[str] = mapped_column(Text, default="{}")
    investment_json: Mapped[str] = mapped_column(Text, default="{}")
    inspection_json: Mapped[str] = mapped_column(Text, default="{}")
    activity_json: Mapped[str] = mapped_column(Text, default="[]")
    notes: Mapped[str | None] = mapped_column(Text)
    reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[Account] = relationship()
    lot: Mapped[AuctionLot] = relationship()
