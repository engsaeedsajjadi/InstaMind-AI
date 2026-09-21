"""API schemas (Pydantic v2).

Conventions:
* IDs are UUIDs, timestamps are timezone-aware ISO-8601 UTC.
* Responses never expose secrets. ``SocialAccountRead`` intentionally has no
  token field — only a fingerprint and a status.
* Pagination is cursor-free but bounded (``limit`` is capped server-side).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.permissions import Permission


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(default="", max_length=160)
    locale: str = Field(default="fa-IR", max_length=10)
    timezone: str = Field(default="Asia/Tehran", max_length=64)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    device_label: str | None = Field(default=None, max_length=160)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    session_id: uuid.UUID | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class UserRead(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    locale: str
    timezone: str
    is_active: bool
    is_superuser: bool
    email_verified_at: datetime | None
    mfa_enabled: bool
    created_at: datetime


class SessionRead(ORMModel):
    id: uuid.UUID
    device_label: str | None
    ip_address: str | None
    user_agent: str | None
    is_active: bool
    mfa_verified: bool
    last_seen_at: datetime | None
    expires_at: datetime
    created_at: datetime


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=256)


# --------------------------------------------------------------------------- #
# Workspaces
# --------------------------------------------------------------------------- #
class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(default=None, max_length=80)
    default_locale: str = Field(default="fa-IR", max_length=10)
    default_timezone: str = Field(default="Asia/Tehran", max_length=64)
    calendar_system: Literal["jalali", "gregorian"] = "jalali"


class WorkspaceRead(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    owner_id: uuid.UUID
    default_locale: str
    default_timezone: str
    calendar_system: str
    is_active: bool
    created_at: datetime


class MembershipRead(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    role_code: str
    status: str
    joined_at: datetime | None
    created_at: datetime


class InviteRequest(BaseModel):
    email: EmailStr
    role_code: str = Field(default="VIEWER", max_length=64)


class RoleUpdateRequest(BaseModel):
    role_code: str = Field(max_length=64)


class RoleCreateRequest(BaseModel):
    code: str = Field(max_length=64)
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=500)
    permissions: list[str] = Field(min_length=1)

    @field_validator("permissions")
    @classmethod
    def _validate(cls, value: list[str]) -> list[str]:
        unknown = [item for item in value if item not in set(Permission)]
        if unknown:
            raise ValueError(f"Unknown permissions: {', '.join(unknown)}")
        return value


class RoleRead(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    code: str
    name: str
    description: str
    permissions: list[str]
    is_builtin: bool


class ContextRead(BaseModel):
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    roles: list[str]
    permissions: list[str]
    is_superuser: bool


# --------------------------------------------------------------------------- #
# Social accounts
# --------------------------------------------------------------------------- #
class ConnectStartRequest(BaseModel):
    api_path: Literal["INSTAGRAM_LOGIN", "FACEBOOK_LOGIN"] = "INSTAGRAM_LOGIN"
    requested_scopes: list[str] | None = None


class ConnectStartResponse(BaseModel):
    authorize_url: str
    state: str
    api_path: str


class SocialAccountRead(ORMModel):
    """No token material is ever serialised — by construction."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    platform: str
    api_path: str
    external_account_id: str
    username: str
    display_name: str | None
    account_type: str | None
    media_count: int | None
    followers_count: int | None
    capabilities: dict[str, Any]
    granted_scopes: list[str]
    status: str
    status_reason: str | None
    connected_at: datetime
    last_synced_at: datetime | None
    created_at: datetime


class DisconnectRequest(BaseModel):
    purge_platform_data: bool = True


# --------------------------------------------------------------------------- #
# Media / content
# --------------------------------------------------------------------------- #
class MediaAssetRead(ORMModel):
    id: uuid.UUID
    filename: str
    content_type: str
    detected_mime: str | None
    size_bytes: int
    kind: str
    width: int | None
    height: int | None
    duration_ms: int | None
    status: str
    created_at: datetime


class ContentCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    caption: str = Field(default="", max_length=2200)
    hashtags: list[str] = Field(default_factory=list)
    content_type: Literal["IMAGE", "CAROUSEL", "REELS", "STORIES"] = "IMAGE"
    media_asset_ids: list[uuid.UUID] = Field(default_factory=list)
    media_order: list[uuid.UUID] = Field(default_factory=list)
    campaign_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    brand_profile_id: uuid.UUID | None = None
    alt_text: str | None = Field(default=None, max_length=1000)
    scheduled_at: datetime | None = None
    timezone: str = Field(default="Asia/Tehran", max_length=64)
    cover_asset_id: uuid.UUID | None = None
    thumb_offset_ms: int | None = None
    share_to_feed: bool = True
    collaborators: list[str] = Field(default_factory=list, max_length=3)
    location_id: str | None = Field(default=None, max_length=64)
    notes: str = Field(default="", max_length=2000)

    @field_validator("hashtags")
    @classmethod
    def _clean_hashtags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            tag = item.strip().lstrip("#")
            if tag and tag not in cleaned:
                cleaned.append(tag)
        return cleaned[:30]


class ContentUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    caption: str | None = Field(default=None, max_length=2200)
    hashtags: list[str] | None = None
    content_type: Literal["IMAGE", "CAROUSEL", "REELS", "STORIES"] | None = None
    media_asset_ids: list[uuid.UUID] | None = None
    media_order: list[uuid.UUID] | None = None
    scheduled_at: datetime | None = None
    status: Literal["DRAFT", "IN_REVIEW", "APPROVED", "SCHEDULED", "ARCHIVED", "CANCELLED"] | None = None
    alt_text: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=2000)
    collaborators: list[str] | None = Field(default=None, max_length=3)


class ContentRead(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    caption: str
    hashtags: list[str]
    content_type: str
    media_asset_ids: list[Any]
    media_order: list[Any]
    status: str
    scheduled_at: datetime | None
    timezone: str
    published_at: datetime | None
    version: int
    ai_generated: bool
    campaign_id: uuid.UUID | None
    category_id: uuid.UUID | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class ApprovalRequest(BaseModel):
    status: Literal["APPROVED", "REJECTED", "CHANGES_REQUESTED"]
    comment: str = Field(default="", max_length=1000)


class PublishRequest(BaseModel):
    content_id: uuid.UUID
    social_account_id: uuid.UUID
    idempotency_key: str | None = Field(default=None, max_length=128)
    scheduled_at: datetime | None = None
    trigger: Literal["IMMEDIATE", "SCHEDULED", "MANUAL"] = "IMMEDIATE"
    requires_approval: bool = True


class PublishingJobRead(ORMModel):
    id: uuid.UUID
    content_id: uuid.UUID
    social_account_id: uuid.UUID
    idempotency_key: str
    state: str
    trigger: str
    scheduled_for: datetime
    attempts: int
    max_attempts: int
    next_retry_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    is_retryable: bool
    result: dict[str, Any]
    created_at: datetime


class PublishingAttemptRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    attempt_number: int
    step: str
    outcome: str
    meta_error_code: int | None
    meta_error_subcode: int | None
    meta_error_type: str | None
    meta_error_message: str | None
    response_status: int | None
    latency_ms: int
    started_at: datetime


class CalendarEventRead(ORMModel):
    id: uuid.UUID
    content_id: uuid.UUID | None
    event_type: str
    title: str
    starts_at: datetime
    ends_at: datetime | None
    timezone: str
    meta: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# AI
# --------------------------------------------------------------------------- #
class CaptionGenerateRequest(BaseModel):
    brand_profile_id: uuid.UUID | None = None
    social_account_id: uuid.UUID | None = None
    topic: str = Field(min_length=1, max_length=500)
    language: Literal["fa-IR", "en-US"] = "fa-IR"
    tone: list[str] = Field(default_factory=list, max_length=8)
    max_hashtags: int = Field(default=10, ge=0, le=30)
    include_cta: bool = True
    product_names: list[str] = Field(default_factory=list, max_length=10)
    content_id: uuid.UUID | None = None


class CaptionGenerateResponse(BaseModel):
    generation_id: uuid.UUID
    caption: str
    hashtags: list[str]
    cta: str | None
    model: str
    cost_cents: int
    warnings: list[str] = Field(default_factory=list)


class ContentPlanRequest(BaseModel):
    brand_profile_id: uuid.UUID | None = None
    horizon_days: int = Field(default=7, ge=1, le=60)
    posts_per_week: int = Field(default=3, ge=1, le=21)
    language: Literal["fa-IR", "en-US"] = "fa-IR"
    goals: list[str] = Field(default_factory=list, max_length=8)


class ContentPlanItem(BaseModel):
    day_index: int
    content_type: str
    idea: str
    caption_draft: str
    hashtags: list[str]


class ContentPlanResponse(BaseModel):
    generation_id: uuid.UUID
    items: list[ContentPlanItem]
    model: str
    cost_cents: int
    status: str = "SUCCEEDED"
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
class WebhookVerifyQuery(BaseModel):
    mode: str
    verify_token: str
    challenge: str


class WebhookIngestResponse(BaseModel):
    accepted: int
    duplicates: int
    rejected: int


# --------------------------------------------------------------------------- #
# Billing / health
# --------------------------------------------------------------------------- #
class PlanRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    description: str
    currency: str
    price_monthly: int
    price_yearly: int
    interval: str
    limits: dict[str, Any]
    features: list[Any]
    is_active: bool


class SubscriptionRead(ORMModel):
    id: uuid.UUID
    plan_code: str
    status: str
    interval: str
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool


class UsageRead(BaseModel):
    metric: str
    used: int
    limit: int | None
    period_start: datetime
    period_end: datetime


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    checks: dict[str, str]
    utc_time: datetime
