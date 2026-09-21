"""Content management models (Content Studio)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, TenantModel, utcnow


class MediaAsset(TenantModel):
    """An uploaded file. Meta cannot accept an upload — it fetches media from a
    publicly reachable HTTPS URL, so ``public_url`` is the contract with the
    publishing engine."""

    __tablename__ = "media_assets"
    __table_args__ = (Index("ix_media_assets_ws_kind", "workspace_id", "kind"),)

    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    bucket: Mapped[str | None] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # Detected from magic bytes, not from the client's declared header.
    detected_mime: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="IMAGE")
    # IMAGE | VIDEO | AUDIO | DOCUMENT
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="UPLOADED")
    # UPLOADED | SCANNED | REJECTED | EXPIRED
    public_url: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scan_result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class BrandProfile(TenantModel):
    """Facts the AI must obey. Everything here is user-supplied/verified —
    the generator is forbidden from inventing product claims."""

    __tablename__ = "brand_profiles"

    social_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(120))
    target_audience: Mapped[str | None] = mapped_column(Text)
    tone_of_voice: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="fa-IR")
    visual_identity: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    marketing_goals: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    products: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Explicitly verified facts. Prompts must cite these instead of guessing.
    verified_claims: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    prohibited_topics: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    banned_words: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    default_hashtags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Campaign(TenantModel):
    __tablename__ = "campaigns"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    goal: Mapped[str | None] = mapped_column(String(160))
    budget_hint: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class ContentCategory(TenantModel):
    __tablename__ = "content_categories"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_content_categories_ws_name"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#6366F1")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_categories.id", ondelete="SET NULL"), nullable=True
    )


class Hashtag(TenantModel):
    __tablename__ = "hashtags"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_hashtags_ws_name"),)

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="MANUAL")


class Content(TenantModel):
    """A piece of content, independent of the account it will be published to."""

    __tablename__ = "contents"
    __table_args__ = (
        Index("ix_contents_ws_status_scheduled", "workspace_id", "status", "scheduled_at"),
    )

    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    brand_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hashtags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    alt_text: Mapped[str | None] = mapped_column(String(1000))
    # IMAGE | CAROUSEL | REELS | STORIES
    content_type: Mapped[str] = mapped_column(String(24), nullable=False, default="IMAGE")
    media_asset_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    media_order: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Reels / carousel options that Meta actually supports:
    cover_asset_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    thumb_offset_ms: Mapped[int | None] = mapped_column(Integer)
    share_to_feed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    collaborators: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    location_id: Mapped[str | None] = mapped_column(String(64))
    user_tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="DRAFT")
    # DRAFT | IN_REVIEW | APPROVED | SCHEDULED | PUBLISHING | PUBLISHED
    # | FAILED | CANCELLED | ARCHIVED
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tehran")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_generation_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    failure_reason: Mapped[str | None] = mapped_column(Text)


class ContentVersion(TenantModel):
    """Immutable snapshot of a content row before each mutation."""

    __tablename__ = "content_versions"
    __table_args__ = (Index("ix_content_versions_ws_content", "workspace_id", "content_id"),)

    content_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("contents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    change_summary: Mapped[str] = mapped_column(String(255), nullable=False, default="")


class ContentApproval(TenantModel):
    """Review/approval trail. A publish is refused without an APPROVED row when
    the workspace requires approval."""

    __tablename__ = "content_approvals"
    __table_args__ = (Index("ix_content_approvals_ws_content", "workspace_id", "content_id"),)

    content_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("contents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    # PENDING | APPROVED | REJECTED | CHANGES_REQUESTED
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContentCalendarEvent(TenantModel):
    """Calendar projection used by the calendar UI (Jalali + Gregorian)."""

    __tablename__ = "content_calendar"

    content_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("contents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    social_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False, default="CONTENT")
    # CONTENT | HOLIDAY | NOTE
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tehran")
    # `metadata` is reserved by SQLAlchemy's Declarative API.
    meta: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)


class AIGeneration(TenantModel):
    """Every AI call, for cost control, history and regeneration."""

    __tablename__ = "ai_generations"
    __table_args__ = (Index("ix_ai_generations_ws_kind", "workspace_id", "kind"),)

    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    # caption | content_plan | hashtag | reel_script | reply_suggestion ...
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="openai")
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="SUCCEEDED")
    # SUCCEEDED | FAILED | BLOCKED | MODERATED
    moderation_flags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("contents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    brand_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="SET NULL"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text)


class AITask(TenantModel):
    """A unit of work created by the agent orchestrator."""

    __tablename__ = "ai_tasks"

    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    input_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    content_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("contents.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class AgentRun(TenantModel):
    """A user instruction handed to the orchestrator, plus its audit trail."""

    __tablename__ = "agent_runs"

    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    planner: Mapped[str] = mapped_column(String(48), nullable=False, default="PLANNER")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    # PENDING | PLANNING | RUNNING | AWAITING_APPROVAL | SUCCEEDED | FAILED | CANCELLED
    plan: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    budget_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spent_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AgentToolCall(TenantModel):
    """Every tool invocation by an agent. Sensitive tools require approval."""

    __tablename__ = "agent_tool_calls"
    __table_args__ = (Index("ix_agent_tool_calls_ws_run", "workspace_id", "agent_run_id"),)

    agent_run_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_status: Mapped[str] = mapped_column(String(24), nullable=False, default="NOT_REQUIRED")
    # NOT_REQUIRED | PENDING | APPROVED | DENIED
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    # PENDING | EXECUTED | DENIED | FAILED
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
