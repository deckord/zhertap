import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
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
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True)
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


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    request_ip: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship()


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


class AuctionLandObject(Base):
    """Canonical land identity shared by repeated E-Qazyna auction lots."""

    __tablename__ = "auction_land_objects"
    __table_args__ = (
        UniqueConstraint("canonical_key", name="uq_auction_land_object_canonical_key"),
        Index("ix_auction_land_objects_egkn_id", "egkn_id"),
        Index("ix_auction_land_objects_cadastre_number", "cadastre_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    canonical_key: Mapped[str] = mapped_column(String(128), nullable=False)
    egkn_id: Mapped[str | None] = mapped_column(String(64))
    cadastre_number: Mapped[str | None] = mapped_column(String(64))
    jerler_object_id: Mapped[str | None] = mapped_column(String(64), index=True)
    identity_confidence: Mapped[str] = mapped_column(String(16), default="unverified")
    boundary_geojson: Mapped[str | None] = mapped_column(Text)
    boundary_source: Mapped[str | None] = mapped_column(String(120))
    boundary_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    lots: Mapped[list["AuctionLot"]] = relationship(back_populates="land_object")

    @classmethod
    def from_identifiers(
        cls,
        *,
        egkn_id: str | None = None,
        cadastre_number: str | None = None,
        jerler_object_id: str | None = None,
    ) -> "AuctionLandObject":
        egkn = (egkn_id or "").strip()
        cadastre = (cadastre_number or "").strip()
        jerler = (jerler_object_id or "").strip()
        if not egkn and not cadastre and not jerler:
            raise ValueError("At least one official land identifier is required")
        canonical_key = (
            f"egkn:{egkn}" if egkn else f"cadastre:{cadastre}" if cadastre else f"jerler:{jerler}"
        )
        return cls(
            canonical_key=canonical_key,
            egkn_id=egkn or None,
            cadastre_number=cadastre or None,
            jerler_object_id=jerler or None,
            identity_confidence="official" if egkn else "cadastre" if cadastre else "jerler",
        )


class AuctionLandIdentityBackfillCursor(Base):
    """Durable keyset checkpoint for conservative canonical-land reconciliation."""

    __tablename__ = "auction_land_identity_backfill_cursors"

    cursor_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    after_lot_id: Mapped[str | None] = mapped_column(String(36))
    high_water_lot_id: Mapped[str | None] = mapped_column(String(36))
    cycle_count: Mapped[int] = mapped_column(Integer, default=0)
    scanned_count: Mapped[int] = mapped_column(BigInteger, default=0)
    linked_count: Mapped[int] = mapped_column(BigInteger, default=0)
    conflict_count: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuctionTerritoryObservation(Base):
    """Immutable revision of one structured fact from an official authority."""

    __tablename__ = "auction_territory_observations"
    __table_args__ = (
        UniqueConstraint(
            "identity_key", "source_revision", name="uq_auction_territory_identity_revision"
        ),
        CheckConstraint(
            "record_kind IN ('event','demographic')",
            name="ck_auction_territory_record_kind",
        ),
        CheckConstraint(
            "length(content_hash) = 64 AND "
            "(geometry_sha256 IS NULL OR length(geometry_sha256) = 64)",
            name="ck_auction_territory_hashes",
        ),
        Index(
            "ix_auction_territory_provider_record",
            "provider_id",
            "source_record_id",
            "source_revision",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    identity_key: Mapped[str] = mapped_column(String(71), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(160), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    record_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    authority_name: Mapped[str] = mapped_column(String(240), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    territory_code: Mapped[str | None] = mapped_column(String(64))
    geometry_geojson: Mapped[str | None] = mapped_column(Text)
    geometry_sha256: Mapped[str | None] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(96), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionTerritoryApplicability(Base):
    """Boundary-versioned relation between an official fact and an auction lot."""

    __tablename__ = "auction_territory_applicability"
    __table_args__ = (
        UniqueConstraint(
            "observation_id", "lot_id", name="uq_auction_territory_observation_lot"
        ),
        CheckConstraint(
            "status IN ('applicable','not_applicable','manual_required')",
            name="ck_auction_territory_applicability_status",
        ),
        CheckConstraint(
            "scope IN ('parcel','territory','unknown')",
            name="ck_auction_territory_applicability_scope",
        ),
        CheckConstraint(
            "parcel_boundary_sha256 IS NULL OR length(parcel_boundary_sha256) = 64",
            name="ck_auction_territory_boundary_hash",
        ),
        Index("ix_auction_territory_applicability_lot", "lot_id", "status"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    observation_id: Mapped[int] = mapped_column(
        ForeignKey("auction_territory_observations.id", ondelete="CASCADE"), nullable=False
    )
    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    basis: Mapped[str] = mapped_column(String(64), nullable=False)
    overlap_ratio: Mapped[float | None] = mapped_column(Float)
    parcel_boundary_sha256: Mapped[str | None] = mapped_column(String(64))
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionLot(Base):
    __tablename__ = "auction_lots"
    __table_args__ = (
        UniqueConstraint("source", "source_lot_id", name="uq_auction_lot_source_id"),
        Index("ix_auction_lots_history_snapshot", "object_type", "created_at", "id"),
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
    land_object_id: Mapped[str | None] = mapped_column(String(64), index=True)
    land_object_ref_id: Mapped[str | None] = mapped_column(
        ForeignKey("auction_land_objects.id"), index=True
    )
    area_ha: Mapped[float | None] = mapped_column(Float, index=True)
    land_rights: Mapped[str | None] = mapped_column(String(240))
    lease_term_years: Mapped[float | None] = mapped_column(Float, index=True)
    divisible: Mapped[bool | None] = mapped_column(Boolean)
    additional_payment_kzt: Mapped[float | None] = mapped_column(Float)
    annual_rent_kzt: Mapped[float | None] = mapped_column(Float)
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

    land_object: Mapped[AuctionLandObject | None] = relationship(back_populates="lots")

    documents: Mapped[list["AuctionDocument"]] = relationship(
        back_populates="lot", cascade="all, delete-orphan", order_by="AuctionDocument.id"
    )
    history: Mapped[list["AuctionLotHistory"]] = relationship(
        back_populates="lot", cascade="all, delete-orphan", order_by="AuctionLotHistory.observed_at"
    )
    changes: Mapped[list["AuctionLotChange"]] = relationship(
        back_populates="lot", cascade="all, delete-orphan", order_by="AuctionLotChange.changed_at"
    )


class AuctionDueDiligenceRequest(Base):
    __tablename__ = "auction_due_diligence_requests"
    __table_args__ = (
        Index(
            "ix_auction_dd_requests_account_lot_status",
            "account_id",
            "lot_id",
            "status",
        ),
        CheckConstraint(
            "status IN ('draft', 'prepared', 'sent', 'waiting', 'received', 'verified', "
            "'risk', 'cancelled')",
            name="ck_auction_dd_request_status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"), index=True
    )
    check_code: Mapped[str] = mapped_column(String(32), index=True)
    authority: Mapped[str] = mapped_column(String(240))
    question: Mapped[str] = mapped_column(Text)
    why: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    external_reference: Mapped[str | None] = mapped_column(String(160))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[Account] = relationship()
    lot: Mapped[AuctionLot] = relationship()
    attachments: Mapped[list["AuctionDueDiligenceAttachment"]] = relationship(
        cascade="all, delete-orphan", order_by="AuctionDueDiligenceAttachment.created_at"
    )


class AuctionDueDiligenceAttachment(Base):
    __tablename__ = "auction_due_diligence_attachments"
    __table_args__ = (
        Index("ix_auction_dd_attachments_request", "request_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id: Mapped[str] = mapped_column(
        ForeignKey("auction_due_diligence_requests.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(320))
    content_type: Mapped[str] = mapped_column(String(128))
    local_path: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    extraction_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    extraction_json: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionDocument(Base):
    __tablename__ = "auction_documents"
    __table_args__ = (
        UniqueConstraint("lot_id", "source_url", name="uq_auction_document_lot_url"),
        Index("ix_auction_documents_downloaded_id", "downloaded_at", "id"),
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


class AuctionDocumentExtractionState(Base):
    """Durable worker state; immutable extraction facts remain AuctionEvidence rows."""

    __tablename__ = "auction_document_extraction_states"
    __table_args__ = (
        Index(
            "ix_auction_document_extraction_state_work",
            "status",
            "next_attempt_at",
            "document_id",
        ),
        Index(
            "ix_auction_document_extraction_state_validation",
            "status",
            "last_validated_at",
            "document_id",
        ),
        Index(
            "ix_auction_document_extraction_state_claim",
            "status",
            "claim_expires_at",
            "document_id",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'terminal', 'retryable')",
            name="ck_auction_document_extraction_state_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND attempts <= 10000",
            name="ck_auction_document_extraction_state_attempts",
        ),
    )

    document_id: Mapped[int] = mapped_column(
        ForeignKey("auction_documents.id", ondelete="CASCADE"), primary_key=True
    )
    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"), index=True
    )
    document_signature: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    document_path: Mapped[str] = mapped_column(String(2048))
    extractor_version: Mapped[str] = mapped_column(String(64))
    writer_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    current_evidence_id: Mapped[int | None] = mapped_column(
        ForeignKey("auction_evidence.id", ondelete="SET NULL")
    )
    current_evidence_hash: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(36))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuctionDocumentExtractionCursor(Base):
    """Singleton durable checkpoint for bounded legacy/new-document reconciliation."""

    __tablename__ = "auction_document_extraction_cursors"

    cursor_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    backfill_document_id: Mapped[int] = mapped_column(Integer, default=0)
    backfill_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    watermark_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark_document_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


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


class AuctionHistoryGeneration(Base):
    __tablename__ = "auction_history_generations"
    __table_args__ = (
        Index(
            "uq_auction_history_generations_one_building",
            "status",
            unique=True,
            postgresql_where=text("status = 'building'"),
            sqlite_where=text("status = 'building'"),
        ),
        Index(
            "uq_auction_history_generations_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    normalization_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    source_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_high_water_lot_id: Mapped[str | None] = mapped_column(String(36))
    expected_count: Mapped[int] = mapped_column(BigInteger, default=0)
    processed_count: Mapped[int] = mapped_column(BigInteger, default=0)
    error_count: Mapped[int] = mapped_column(BigInteger, default=0)
    checkpoint_lot_id: Mapped[str | None] = mapped_column(String(36))
    scan_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuctionHistoryGenerationLot(Base):
    __tablename__ = "auction_history_generation_lots"
    __table_args__ = (
        Index("ix_auction_history_generation_lots_lot_id", "lot_id"),
    )

    generation: Mapped[int] = mapped_column(
        ForeignKey("auction_history_generations.generation", ondelete="CASCADE"),
        primary_key=True,
    )
    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"),
        primary_key=True,
    )


_AUCTION_HISTORY_ELIGIBLE = text(
    "right_status = 'found' AND purpose_status = 'found' AND area_status = 'found'"
)


class AuctionHistoryNormalized(Base):
    __tablename__ = "auction_history_normalized"
    __table_args__ = (
        Index(
            "ix_auction_history_norm_locality_dims",
            "generation",
            "locality_key",
            "right_kind",
            "purpose_group",
            "lease_band",
            postgresql_where=_AUCTION_HISTORY_ELIGIBLE,
            sqlite_where=_AUCTION_HISTORY_ELIGIBLE,
        ),
        Index(
            "ix_auction_history_norm_district_dims",
            "generation",
            "district_key",
            "right_kind",
            "purpose_group",
            "lease_band",
            postgresql_where=_AUCTION_HISTORY_ELIGIBLE,
            sqlite_where=_AUCTION_HISTORY_ELIGIBLE,
        ),
        Index(
            "ix_auction_history_norm_region_dims",
            "generation",
            "region_key",
            "right_kind",
            "purpose_group",
            "lease_band",
            postgresql_where=_AUCTION_HISTORY_ELIGIBLE,
            sqlite_where=_AUCTION_HISTORY_ELIGIBLE,
        ),
        Index(
            "ix_auction_history_norm_area_date",
            "generation",
            "area_ha",
            "event_date",
            postgresql_where=_AUCTION_HISTORY_ELIGIBLE,
            sqlite_where=_AUCTION_HISTORY_ELIGIBLE,
        ),
        Index(
            "ix_auction_history_norm_outcome_date",
            "generation",
            "outcome",
            "event_date",
        ),
    )

    generation: Mapped[int] = mapped_column(
        ForeignKey("auction_history_generations.generation", ondelete="CASCADE"),
        primary_key=True,
    )
    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"),
        primary_key=True,
    )
    normalization_version: Mapped[str] = mapped_column(String(64))
    normalization_key: Mapped[str] = mapped_column(String(64), index=True)
    right_kind: Mapped[str] = mapped_column(String(16))
    right_status: Mapped[str] = mapped_column(String(16))
    purpose_group: Mapped[str] = mapped_column(String(32))
    purpose_status: Mapped[str] = mapped_column(String(16))
    lease_band: Mapped[str] = mapped_column(String(24))
    lease_status: Mapped[str] = mapped_column(String(16))
    event_date: Mapped[date | None] = mapped_column(Date)
    event_date_status: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str] = mapped_column(String(16))
    outcome_status: Mapped[str] = mapped_column(String(16))
    area_ha: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    area_status: Mapped[str] = mapped_column(String(16))
    start_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    start_price_status: Mapped[str] = mapped_column(String(16))
    sale_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    sale_price_status: Mapped[str] = mapped_column(String(16))
    sale_to_start_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    start_price_per_ha_kzt: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    sale_price_per_ha_kzt: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    region_key: Mapped[str | None] = mapped_column(String(160))
    district_key: Mapped[str | None] = mapped_column(String(160))
    locality_key: Mapped[str | None] = mapped_column(String(160))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issues_json: Mapped[str] = mapped_column(Text, default="[]")
    normalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionDecisionSnapshot(Base):
    __tablename__ = "auction_decision_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "lot_id",
            "engine_version",
            "rules_version",
            "input_hash",
            name="uq_auction_decision_snapshot_input",
        ),
        Index(
            "uq_auction_decision_snapshot_current",
            "lot_id",
            "engine_version",
            "rules_version",
            unique=True,
            postgresql_where=text("is_current = true"),
            sqlite_where=text("is_current = 1"),
        ),
        Index(
            "ix_auction_decision_snapshot_verdict_current",
            "verdict",
            "is_current",
            "checked_at",
        ),
        Index(
            "ix_auction_decision_snapshot_readiness_current",
            "data_readiness",
            "is_current",
            "checked_at",
        ),
        Index(
            "ix_auction_decision_snapshot_scenario_current",
            "scenario_key",
            "is_current",
            "checked_at",
        ),
        Index(
            "ix_auction_decision_snapshot_repeat_current",
            "has_repeat",
            "repeat_attempt_count",
            "is_current",
        ),
        Index(
            "ix_auction_decision_snapshot_stale_current",
            "stale",
            "is_current",
            "checked_at",
        ),
        CheckConstraint(
            "bid_ceiling_kzt IS NULL OR "
            "(bid_ceiling_kzt >= 0 AND bid_ceiling_kzt <= 1000000000000000)",
            name="ck_auction_decision_snapshot_bid_bounds",
        ),
        CheckConstraint(
            "fair_value_low_kzt IS NULL OR "
            "(fair_value_low_kzt >= 0 AND fair_value_low_kzt <= 1000000000000000)",
            name="ck_auction_decision_snapshot_fair_low_bounds",
        ),
        CheckConstraint(
            "fair_value_high_kzt IS NULL OR "
            "(fair_value_high_kzt >= 0 AND fair_value_high_kzt <= 1000000000000000)",
            name="ck_auction_decision_snapshot_fair_high_bounds",
        ),
        CheckConstraint(
            "fair_value_low_kzt IS NULL OR fair_value_high_kzt IS NULL OR "
            "fair_value_low_kzt <= fair_value_high_kzt",
            name="ck_auction_decision_snapshot_fair_order",
        ),
        CheckConstraint(
            "(verdict = 'participate_up_to' AND bid_ceiling_kzt IS NOT NULL) OR "
            "(verdict <> 'participate_up_to' AND bid_ceiling_kzt IS NULL)",
            name="ck_auction_decision_snapshot_bid_verdict",
        ),
        CheckConstraint(
            "verdict IN ('participate', 'participate_up_to', 'requires_check', "
            "'high_risk', 'do_not_participate')",
            name="ck_auction_decision_snapshot_verdict",
        ),
        CheckConstraint(
            "data_readiness IN ('complete', 'partial', 'insufficient', 'error')",
            name="ck_auction_decision_snapshot_readiness",
        ),
        CheckConstraint(
            "repeat_attempt_count >= 0 AND repeat_attempt_count <= 10000",
            name="ck_auction_decision_snapshot_repeat_bounds",
        ),
        CheckConstraint(
            "validated_evidence_id >= 0",
            name="ck_auction_decision_snapshot_validated_evidence_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"), index=True
    )
    engine_version: Mapped[str] = mapped_column(String(64))
    rules_version: Mapped[str] = mapped_column(String(64))
    verdict_engine_version: Mapped[str] = mapped_column(String(64))
    scenario_engine_version: Mapped[str | None] = mapped_column(String(64))
    price_engine_version: Mapped[str | None] = mapped_column(String(64))
    formula_version: Mapped[str | None] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    verdict: Mapped[str] = mapped_column(String(32))
    data_readiness: Mapped[str] = mapped_column(String(24))
    scenario_key: Mapped[str] = mapped_column(String(64))
    repeat_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    has_repeat: Mapped[bool] = mapped_column(Boolean, default=False)
    bid_ceiling_kzt: Mapped[int | None] = mapped_column(BigInteger)
    fair_value_low_kzt: Mapped[int | None] = mapped_column(BigInteger)
    fair_value_high_kzt: Mapped[int | None] = mapped_column(BigInteger)
    evidence_generation_ids_json: Mapped[str] = mapped_column(Text, default="{}")
    source_freshness_json: Mapped[str] = mapped_column(Text, default="{}")
    stale_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    payload_json: Mapped[str] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_validated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    validated_evidence_id: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lot: Mapped[AuctionLot] = relationship()


class AuctionDecisionInputState(Base):
    """Mutable worker checkpoint; assembled decision evidence remains immutable."""

    __tablename__ = "auction_decision_input_states"
    __table_args__ = (
        Index(
            "ix_auction_decision_input_state_work",
            "status",
            "next_attempt_at",
            "updated_at",
        ),
        Index(
            "ix_auction_decision_input_state_watermark",
            "source_watermark_id",
            "validated_at",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'insufficient', 'error')",
            name="ck_auction_decision_input_state_status",
        ),
        CheckConstraint(
            "source_watermark_id >= 0 AND market_watermark_id >= 0 AND "
            "market_row_count >= 0 AND document_watermark_id >= 0 AND "
            "document_row_count >= 0 AND retry_count >= 0 AND retry_count <= 20",
            name="ck_auction_decision_input_state_counters",
        ),
    )

    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    source_watermark_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    source_watermark_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    lot_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_generation: Mapped[int | None] = mapped_column(BigInteger)
    market_signature: Mapped[str | None] = mapped_column(String(64))
    market_watermark_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    market_watermark_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    market_row_count: Mapped[int] = mapped_column(Integer, default=0)
    document_signature: Mapped[str | None] = mapped_column(String(64))
    document_watermark_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    document_watermark_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    document_row_count: Mapped[int] = mapped_column(Integer, default=0)
    input_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    assembler_version: Mapped[str] = mapped_column(String(64))
    spatial_assembler_version: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(64))
    claim_token: Mapped[str | None] = mapped_column(String(36), index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(500))
    validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    lot: Mapped[AuctionLot] = relationship()


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


class AuctionSpatialFeedState(Base):
    """Current durable worker state for one signed spatial feed identity."""

    __tablename__ = "auction_spatial_feed_states"
    __table_args__ = (
        UniqueConstraint(
            "lot_id",
            "module",
            "provider_id",
            "feed_id",
            name="uq_auction_spatial_feed_identity",
        ),
        UniqueConstraint("identity_key", name="uq_auction_spatial_feed_identity_key"),
        CheckConstraint(
            "module IN ('restrictions','site','planning')",
            name="ck_auction_spatial_feed_module",
        ),
        CheckConstraint(
            "status IN ('pending','processing','ready','conflict','retryable',"
            "'terminal','quarantined','expired')",
            name="ck_auction_spatial_feed_status",
        ),
        CheckConstraint(
            "attempts BETWEEN 0 AND 10000",
            name="ck_auction_spatial_feed_attempts",
        ),
        CheckConstraint(
            "length(identity_key) = 64 AND length(input_signature) = 64 AND "
            "(current_generation_id IS NULL OR length(current_generation_id) = 64) AND "
            "(current_payload_hash IS NULL OR length(current_payload_hash) = 64)",
            name="ck_auction_spatial_feed_hashes",
        ),
        CheckConstraint(
            "status != 'processing' OR "
            "(claim_token IS NOT NULL AND claim_expires_at IS NOT NULL "
            "AND claimed_from_status IS NOT NULL)",
            name="ck_auction_spatial_feed_claim",
        ),
        Index("ix_auction_spatial_feed_lot_module", "lot_id", "module", "id"),
        Index("ix_auction_spatial_feed_pending", "status", "id"),
        Index(
            "ix_auction_spatial_feed_retry_due",
            "status",
            "next_attempt_at",
            "id",
        ),
        Index(
            "ix_auction_spatial_feed_claim_due",
            "status",
            "claim_expires_at",
            "id",
        ),
        Index(
            "ix_auction_spatial_feed_validation_due",
            "status",
            "next_validation_at",
            "expires_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE")
    )
    module: Mapped[str] = mapped_column(String(16))
    provider_id: Mapped[str] = mapped_column(String(128))
    feed_id: Mapped[str] = mapped_column(String(128))
    identity_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    input_signature: Mapped[str] = mapped_column(String(64))
    current_evidence_id: Mapped[int | None] = mapped_column(
        ForeignKey("auction_evidence.id", ondelete="SET NULL")
    )
    current_generation_id: Mapped[str | None] = mapped_column(String(64))
    current_payload_hash: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_validation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(128))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_from_status: Mapped[str | None] = mapped_column(String(24))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuctionSpatialManifestExpectation(Base):
    """Versioned authoritative feed checklist used for atomic lot reconciliation."""

    __tablename__ = "auction_spatial_manifest_expectations"
    __table_args__ = (
        CheckConstraint(
            "length(checklist_hash) = 64 AND length(required_feed_keys_json) <= 16384",
            name="ck_auction_spatial_expectation_bounds",
        ),
    )

    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[str] = mapped_column(String(128))
    checklist_hash: Mapped[str] = mapped_column(String(64))
    required_feed_keys_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuctionSpatialGenerationManifest(Base):
    """One current atomic restriction/site/planning generation per lot."""

    __tablename__ = "auction_spatial_generation_manifests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('complete','incomplete','conflict')",
            name="ck_auction_spatial_manifest_status",
        ),
        CheckConstraint(
            "length(manifest_hash) = 64 AND length(module_generations_json) <= 8192 "
            "AND length(missing_feed_keys_json) <= 8192 "
            "AND length(blocking_feed_keys_json) <= 8192",
            name="ck_auction_spatial_manifest_bounds",
        ),
        CheckConstraint("watermark >= 1", name="ck_auction_spatial_manifest_watermark"),
        Index("ix_auction_spatial_manifest_status", "status", "settled", "updated_at"),
        Index("ix_auction_spatial_manifest_expiry", "expires_at", "lot_id"),
    )

    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16))
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    module_generations_json: Mapped[str] = mapped_column(Text, default="{}")
    missing_feed_keys_json: Mapped[str] = mapped_column(Text, default="[]")
    blocking_feed_keys_json: Mapped[str] = mapped_column(Text, default="[]")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[str] = mapped_column(String(128))
    watermark: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionSpatialDecisionSignal(Base):
    """Transactional outbox; dispatcher schedules W14 only after commit."""

    __tablename__ = "auction_spatial_decision_signals"
    __table_args__ = (
        UniqueConstraint(
            "lot_id", "manifest_watermark", name="uq_auction_spatial_signal_watermark"
        ),
        CheckConstraint(
            "status IN ('pending','dispatched','failed')",
            name="ck_auction_spatial_signal_status",
        ),
        CheckConstraint(
            "length(manifest_hash) = 64 AND manifest_watermark >= 1 "
            "AND attempts BETWEEN 0 AND 10000",
            name="ck_auction_spatial_signal_bounds",
        ),
        Index("ix_auction_spatial_signal_due", "status", "next_attempt_at", "id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE")
    )
    manifest_hash: Mapped[str] = mapped_column(String(64))
    manifest_watermark: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


class AuctionVerifiedComparableObservation(Base):
    """Immutable provider observation for the global verified-comparable inventory."""

    __tablename__ = "auction_verified_comparable_observations"
    __table_args__ = (
        UniqueConstraint(
            "source_identity_key",
            "content_hash",
            name="uq_auction_verified_comparable_observation_content",
        ),
        CheckConstraint(
            "fact_status IN ('found', 'conflict', 'error')",
            name="ck_auction_verified_comparable_observation_status",
        ),
        CheckConstraint(
            "source_sequence_id > 0",
            name="ck_auction_verified_comparable_observation_sequence",
        ),
        CheckConstraint(
            "length(source_identity_key) = 71 AND length(generation_signature) = 64 "
            "AND length(content_hash) = 64",
            name="ck_auction_verified_comparable_observation_hashes",
        ),
        CheckConstraint(
            "price_kind IN ('verified_sale', 'listing')",
            name="ck_auction_verified_comparable_observation_price_kind",
        ),
        CheckConstraint(
            "(price_kind = 'verified_sale' AND source_sale_id IS NOT NULL) OR "
            "(price_kind = 'listing' AND source_listing_id IS NOT NULL)",
            name="ck_auction_verified_comparable_observation_identity",
        ),
        CheckConstraint(
            "fact_status != 'found' OR (source_url IS NOT NULL AND title IS NOT NULL "
            "AND right_type IS NOT NULL AND purpose_group IS NOT NULL AND area_ha IS NOT NULL "
            "AND price_kzt IS NOT NULL AND latitude IS NOT NULL AND longitude IS NOT NULL)",
            name="ck_auction_verified_comparable_observation_found_complete",
        ),
        CheckConstraint(
            "fact_status != 'found' OR price_kind != 'verified_sale' OR "
            "(event_at IS NOT NULL AND verification_status = 'verified' "
            "AND verification_ref IS NOT NULL)",
            name="ck_auction_verified_comparable_observation_verified_sale",
        ),
        CheckConstraint(
            "right_type IS NULL OR right_type IN ('ownership', 'lease')",
            name="ck_auction_verified_comparable_observation_right",
        ),
        CheckConstraint(
            "(right_type IS NULL AND lease_term_years IS NULL AND lease_band IS NULL) OR "
            "(right_type = 'ownership' AND lease_term_years IS NULL AND lease_band IS NULL) OR "
            "(right_type = 'lease' AND lease_term_years > 0 AND lease_term_years <= 99 AND "
            "((lease_term_years <= 3 AND lease_band = 'short_3') OR "
            "(lease_term_years > 3 AND lease_term_years <= 10 AND lease_band = 'medium_10') OR "
            "(lease_term_years > 10 AND lease_band = 'long_99')))",
            name="ck_auction_verified_comparable_observation_lease",
        ),
        CheckConstraint(
            "access_readiness IS NULL OR access_readiness IN "
            "('none', 'partial', 'ready', 'unknown')",
            name="ck_auction_verified_comparable_observation_access",
        ),
        CheckConstraint(
            "infrastructure_readiness IS NULL OR infrastructure_readiness IN "
            "('none', 'partial', 'ready', 'unknown')",
            name="ck_auction_verified_comparable_observation_infrastructure",
        ),
        CheckConstraint(
            "latitude IS NULL OR latitude BETWEEN 40 AND 56",
            name="ck_auction_verified_comparable_observation_latitude",
        ),
        CheckConstraint(
            "longitude IS NULL OR longitude BETWEEN 46 AND 88",
            name="ck_auction_verified_comparable_observation_longitude",
        ),
        CheckConstraint(
            "area_ha IS NULL OR (area_ha >= 0.0001 AND area_ha <= 1000000)",
            name="ck_auction_verified_comparable_observation_area",
        ),
        CheckConstraint(
            "price_kzt IS NULL OR (price_kzt >= 1 AND price_kzt <= 1000000000000000)",
            name="ck_auction_verified_comparable_observation_price",
        ),
        CheckConstraint(
            "length(provenance_json) <= 16384 AND length(conflicts_json) <= 8192 "
            "AND (raw_payload_json IS NULL OR length(raw_payload_json) <= 64000)",
            name="ck_auction_verified_comparable_observation_payload_bounds",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    source_sequence_id: Mapped[int] = mapped_column(BigInteger)
    source_identity_key: Mapped[str] = mapped_column(String(71))
    source_name: Mapped[str] = mapped_column(String(128))
    source_record_id: Mapped[str] = mapped_column(String(128))
    source_sale_id: Mapped[str | None] = mapped_column(String(128))
    source_listing_id: Mapped[str | None] = mapped_column(String(128))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    object_id: Mapped[str | None] = mapped_column(String(128))
    fact_status: Mapped[str] = mapped_column(String(16))
    price_kind: Mapped[str] = mapped_column(String(20))
    verification_status: Mapped[str | None] = mapped_column(String(32))
    verification_ref: Mapped[str | None] = mapped_column(String(512))
    right_type: Mapped[str | None] = mapped_column(String(16))
    purpose_group: Mapped[str | None] = mapped_column(String(160))
    lease_term_years: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    lease_band: Mapped[str | None] = mapped_column(String(16))
    area_ha: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    access_readiness: Mapped[str | None] = mapped_column(String(16))
    infrastructure_readiness: Mapped[str | None] = mapped_column(String(16))
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    title: Mapped[str | None] = mapped_column(String(320))
    locality: Mapped[str | None] = mapped_column(String(160))
    provenance_json: Mapped[str] = mapped_column(Text)
    conflicts_json: Mapped[str] = mapped_column(Text, default="[]")
    raw_payload_json: Mapped[str | None] = mapped_column(Text)
    generation_signature: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    contract_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionVerifiedComparableCurrent(Base):
    """One authoritative latest observation per provider identity, indexed for W9."""

    __tablename__ = "auction_verified_comparable_current"
    __table_args__ = (
        UniqueConstraint(
            "observation_id", name="uq_auction_verified_comparable_current_observation"
        ),
        CheckConstraint(
            "fact_status IN ('found', 'conflict', 'error')",
            name="ck_auction_verified_comparable_current_status",
        ),
        CheckConstraint(
            "source_sequence_id > 0",
            name="ck_auction_verified_comparable_current_sequence",
        ),
        CheckConstraint(
            "length(source_identity_key) = 71 AND length(generation_signature) = 64 "
            "AND length(content_hash) = 64",
            name="ck_auction_verified_comparable_current_hashes",
        ),
        CheckConstraint(
            "price_kind IN ('verified_sale', 'listing')",
            name="ck_auction_verified_comparable_current_price_kind",
        ),
        CheckConstraint(
            "(price_kind = 'verified_sale' AND source_sale_id IS NOT NULL) OR "
            "(price_kind = 'listing' AND source_listing_id IS NOT NULL)",
            name="ck_auction_verified_comparable_current_identity",
        ),
        CheckConstraint(
            "fact_status != 'found' OR (source_url IS NOT NULL AND title IS NOT NULL "
            "AND right_type IS NOT NULL AND purpose_group IS NOT NULL AND area_ha IS NOT NULL "
            "AND price_kzt IS NOT NULL AND latitude IS NOT NULL AND longitude IS NOT NULL)",
            name="ck_auction_verified_comparable_current_found_complete",
        ),
        CheckConstraint(
            "fact_status != 'found' OR price_kind != 'verified_sale' OR "
            "(event_at IS NOT NULL AND verification_status = 'verified' "
            "AND verification_ref IS NOT NULL)",
            name="ck_auction_verified_comparable_current_verified_sale",
        ),
        CheckConstraint(
            "latitude IS NULL OR latitude BETWEEN 40 AND 56",
            name="ck_auction_verified_comparable_current_latitude",
        ),
        CheckConstraint(
            "longitude IS NULL OR longitude BETWEEN 46 AND 88",
            name="ck_auction_verified_comparable_current_longitude",
        ),
        CheckConstraint(
            "area_ha IS NULL OR (area_ha >= 0.0001 AND area_ha <= 1000000)",
            name="ck_auction_verified_comparable_current_area",
        ),
        CheckConstraint(
            "price_kzt IS NULL OR (price_kzt >= 1 AND price_kzt <= 1000000000000000)",
            name="ck_auction_verified_comparable_current_price",
        ),
        CheckConstraint(
            "(right_type IS NULL AND lease_term_years IS NULL AND lease_band IS NULL) OR "
            "(right_type = 'ownership' AND lease_term_years IS NULL AND lease_band IS NULL) OR "
            "(right_type = 'lease' AND lease_term_years > 0 AND lease_term_years <= 99 AND "
            "((lease_term_years <= 3 AND lease_band = 'short_3') OR "
            "(lease_term_years > 3 AND lease_term_years <= 10 AND lease_band = 'medium_10') OR "
            "(lease_term_years > 10 AND lease_band = 'long_99')))",
            name="ck_auction_verified_comparable_current_lease",
        ),
        CheckConstraint(
            "access_readiness IS NULL OR access_readiness IN "
            "('none', 'partial', 'ready', 'unknown')",
            name="ck_auction_verified_comparable_current_access",
        ),
        CheckConstraint(
            "infrastructure_readiness IS NULL OR infrastructure_readiness IN "
            "('none', 'partial', 'ready', 'unknown')",
            name="ck_auction_verified_comparable_current_infrastructure",
        ),
        CheckConstraint(
            "length(provenance_json) <= 16384 AND length(conflicts_json) <= 8192",
            name="ck_auction_verified_comparable_current_payload_bounds",
        ),
        Index(
            "ix_auction_verified_comparable_current_target_geo",
            "right_type",
            "purpose_group",
            "lease_band",
            "latitude",
            "longitude",
            "area_ha",
            "event_at",
            "observed_at",
            "observation_id",
            postgresql_where=text(
                "fact_status = 'found' AND price_kind = 'verified_sale' "
                "AND verification_status = 'verified' AND verification_ref IS NOT NULL "
                "AND conflicts_json = '[]'"
            ),
            sqlite_where=text(
                "fact_status = 'found' AND price_kind = 'verified_sale' "
                "AND verification_status = 'verified' AND verification_ref IS NOT NULL "
                "AND conflicts_json = '[]'"
            ),
        ),
        Index(
            "ix_auction_verified_comparable_current_target_event",
            "right_type",
            "purpose_group",
            "lease_band",
            "event_at",
            "observed_at",
            "observation_id",
            postgresql_where=text(
                "fact_status = 'found' AND price_kind = 'verified_sale' "
                "AND verification_status = 'verified' AND verification_ref IS NOT NULL "
                "AND conflicts_json = '[]'"
            ),
            sqlite_where=text(
                "fact_status = 'found' AND price_kind = 'verified_sale' "
                "AND verification_status = 'verified' AND verification_ref IS NOT NULL "
                "AND conflicts_json = '[]'"
            ),
        ),
        Index(
            "ix_auction_verified_comparable_current_keyset",
            "observed_at",
            "observation_id",
        ),
    )

    source_identity_key: Mapped[str] = mapped_column(String(71), primary_key=True)
    observation_id: Mapped[int] = mapped_column(
        ForeignKey("auction_verified_comparable_observations.id", ondelete="RESTRICT")
    )
    source_sequence_id: Mapped[int] = mapped_column(BigInteger)
    source_name: Mapped[str] = mapped_column(String(128))
    source_record_id: Mapped[str] = mapped_column(String(128))
    source_sale_id: Mapped[str | None] = mapped_column(String(128))
    source_listing_id: Mapped[str | None] = mapped_column(String(128))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    object_id: Mapped[str | None] = mapped_column(String(128))
    fact_status: Mapped[str] = mapped_column(String(16))
    price_kind: Mapped[str] = mapped_column(String(20))
    verification_status: Mapped[str | None] = mapped_column(String(32))
    verification_ref: Mapped[str | None] = mapped_column(String(512))
    right_type: Mapped[str | None] = mapped_column(String(16))
    purpose_group: Mapped[str | None] = mapped_column(String(160))
    lease_term_years: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    lease_band: Mapped[str | None] = mapped_column(String(16))
    area_ha: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    access_readiness: Mapped[str | None] = mapped_column(String(16))
    infrastructure_readiness: Mapped[str | None] = mapped_column(String(16))
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    title: Mapped[str | None] = mapped_column(String(320))
    locality: Mapped[str | None] = mapped_column(String(160))
    provenance_json: Mapped[str] = mapped_column(Text)
    conflicts_json: Mapped[str] = mapped_column(Text, default="[]")
    generation_signature: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    contract_version: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionMarketInventoryGeneration(Base):
    """Immutable cell delta committed with a comparable-current batch."""

    __tablename__ = "auction_market_inventory_generations"
    __table_args__ = (
        CheckConstraint("generation > 0", name="ck_auction_market_generation_positive"),
        CheckConstraint(
            "changed_identity_count BETWEEN 0 AND 1000",
            name="ck_auction_market_generation_identity_count",
        ),
        CheckConstraint(
            "length(generation_signature) = 64 AND length(policy_version) <= 64",
            name="ck_auction_market_generation_signatures",
        ),
        CheckConstraint(
            "length(changed_cells_json) <= 32000",
            name="ck_auction_market_generation_cells_bound",
        ),
    )

    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    generation_signature: Mapped[str] = mapped_column(String(64), unique=True)
    changed_cells_json: Mapped[str] = mapped_column(Text, default="[]")
    global_reconciliation: Mapped[bool] = mapped_column(Boolean, default=False)
    changed_identity_count: Mapped[int] = mapped_column(Integer)
    policy_version: Mapped[str] = mapped_column(String(64))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuctionMarketTargetState(Base):
    """Durable W9 target watermark, claim ownership and retry schedule."""

    __tablename__ = "auction_market_target_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready','insufficient','error','pending','processing')",
            name="ck_auction_market_target_state_status",
        ),
        CheckConstraint(
            "validated_generation >= 0 AND attempts BETWEEN 0 AND 10000",
            name="ck_auction_market_target_state_counters",
        ),
        CheckConstraint(
            "length(target_signature) = 64 AND length(coverage_cells_json) <= 2048",
            name="ck_auction_market_target_state_bounds",
        ),
        CheckConstraint(
            "status != 'processing' OR (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_auction_market_target_state_claim",
        ),
        Index(
            "ix_auction_market_target_state_due",
            "status",
            "next_attempt_at",
            "claim_expires_at",
            "lot_id",
        ),
        Index(
            "ix_auction_market_target_state_watermark",
            "validated_generation",
            "lot_id",
        ),
    )

    lot_id: Mapped[str] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"), primary_key=True
    )
    target_signature: Mapped[str] = mapped_column(String(64))
    coverage_cells_json: Mapped[str] = mapped_column(Text, default="[]")
    validated_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_version: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionMarketScanCursor(Base):
    """Durable independent active-target scan cursor for sparse dirty pages."""

    __tablename__ = "auction_market_scan_cursors"

    policy_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    scan_cursor_lot_id: Mapped[str | None] = mapped_column(String(36))
    high_water_lot_id: Mapped[str | None] = mapped_column(String(36))
    latest_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderSyncRun(Base):
    """One idempotent provider generation and its downstream barrier."""

    __tablename__ = "provider_sync_runs"
    __table_args__ = (
        CheckConstraint(
            "run_kind IN ('current','full','history','sources')",
            name="ck_provider_sync_run_kind",
        ),
        CheckConstraint(
            "status IN ('active','finalizing','complete','error')",
            name="ck_provider_sync_run_status",
        ),
        CheckConstraint(
            "child_count BETWEEN 0 AND 1000 AND completed_children BETWEEN 0 AND child_count "
            "AND detail_limit BETWEEN 0 AND 100000 AND details_enqueued BETWEEN 0 AND detail_limit",
            name="ck_provider_sync_run_counters",
        ),
        CheckConstraint(
            "length(config_json) <= 16000 AND length(policy_version) <= 64",
            name="ck_provider_sync_run_bounds",
        ),
        Index(
            "uq_provider_sync_run_active_kind",
            "run_kind",
            unique=True,
            postgresql_where=text("status IN ('active','finalizing')"),
            sqlite_where=text("status IN ('active','finalizing')"),
        ),
    )

    run_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_kind: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    child_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_children: Mapped[int] = mapped_column(Integer, default=0)
    detail_limit: Mapped[int] = mapped_column(Integer, default=0)
    details_enqueued: Mapped[int] = mapped_column(Integer, default=0)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    downstream_dispatched: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_version: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderWorkflowState(Base):
    """Compact durable cursor for a bounded provider crawl; response bodies are never stored."""

    __tablename__ = "provider_workflow_states"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('eqazyna','egkn','osm_overpass','gov_kz','auction_documents','jerler')",
            name="ck_provider_workflow_provider",
        ),
        CheckConstraint(
            "status IN ('pending','processing','deferred','complete','error')",
            name="ck_provider_workflow_status",
        ),
        CheckConstraint(
            "completed_units >= 0 AND failed_units >= 0 AND attempts BETWEEN 0 AND 10000",
            name="ck_provider_workflow_counters",
        ),
        CheckConstraint(
            "length(cursor_json) <= 16000 AND length(policy_version) <= 64",
            name="ck_provider_workflow_bounds",
        ),
        CheckConstraint(
            "status != 'processing' OR (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_provider_workflow_claim",
        ),
        Index(
            "ix_provider_workflow_due",
            "status",
            "next_attempt_at",
            "claim_expires_at",
            "workflow_key",
        ),
    )

    workflow_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_key: Mapped[str | None] = mapped_column(
        ForeignKey("provider_sync_runs.run_key", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    workflow_kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    cursor_json: Mapped[str] = mapped_column(Text, default="{}")
    completed_units: Mapped[int] = mapped_column(Integer, default=0)
    failed_units: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    policy_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderRunDispatch(Base):
    """Durable broker outbox for provider-run barrier continuations."""

    __tablename__ = "provider_run_dispatches"
    __table_args__ = (
        UniqueConstraint("run_key", "action", name="uq_provider_run_dispatch_action"),
        CheckConstraint(
            "action IN ('start_sources','normalize_history','decision_input')",
            name="ck_provider_run_dispatch_action",
        ),
        CheckConstraint(
            "status IN ('pending','processing','dispatched','error')",
            name="ck_provider_run_dispatch_status",
        ),
        CheckConstraint(
            "attempts BETWEEN 0 AND 10000 AND length(payload_json) <= 4000",
            name="ck_provider_run_dispatch_bounds",
        ),
        Index(
            "ix_provider_run_dispatch_due",
            "status",
            "next_attempt_at",
            "claim_expires_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_key: Mapped[str] = mapped_column(
        ForeignKey("provider_sync_runs.run_key", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderWorkflowUnit(Base):
    """Small request identity/input record; parsed domain data is persisted elsewhere."""

    __tablename__ = "provider_workflow_units"
    __table_args__ = (
        UniqueConstraint("workflow_key", "unit_key", name="uq_provider_workflow_unit_key"),
        CheckConstraint(
            "status IN ('pending','processing','done','error','terminal')",
            name="ck_provider_workflow_unit_status",
        ),
        CheckConstraint(
            "attempts BETWEEN 0 AND 100 AND length(input_json) <= 8000",
            name="ck_provider_workflow_unit_bounds",
        ),
        CheckConstraint(
            "status != 'processing' OR (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_provider_workflow_unit_claim",
        ),
        Index(
            "ix_provider_workflow_unit_due",
            "workflow_key",
            "status",
            "next_attempt_at",
            "claim_expires_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_key: Mapped[str] = mapped_column(
        ForeignKey("provider_workflow_states.workflow_key", ondelete="CASCADE"), index=True
    )
    unit_key: Mapped[str] = mapped_column(String(128))
    unit_kind: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_ref: Mapped[str | None] = mapped_column(String(512))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
