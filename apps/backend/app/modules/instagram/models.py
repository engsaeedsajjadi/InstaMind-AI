"""Instagram integration models.

Only official Meta credentials are stored here, and only encrypted. The raw
access token is never written to the database, never logged, and never returned
by the API — callers get ``access_token_status`` and ``token_fingerprint``.
"""

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


class SocialAccount(TenantModel):
    """A connected professional Instagram account.

    ``api_path`` records which official Meta configuration the account was
    authorised through, because the granted permission sets differ:

    * ``FACEBOOK_LOGIN`` — Business/Creator **linked to a Facebook Page**.
      Superset: Business Discovery, hashtag search, product tagging.
    * ``INSTAGRAM_LOGIN`` — Business Login for Instagram. No Page required,
      no ads/tagging.
    """

    __tablename__ = "social_accounts"
    __table_args__ = (
        UniqueConstraint("platform", "external_account_id", name="uq_social_accounts_platform_ext"),
        Index("ix_social_accounts_ws_platform", "workspace_id", "platform"),
    )

    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="INSTAGRAM")
    api_path: Mapped[str] = mapped_column(String(32), nullable=False, default="INSTAGRAM_LOGIN")
    external_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(160))
    profile_picture_url: Mapped[str | None] = mapped_column(Text)
    account_type: Mapped[str | None] = mapped_column(String(32))  # BUSINESS | CREATOR
    media_count: Mapped[int | None] = mapped_column(Integer)
    followers_count: Mapped[int | None] = mapped_column(Integer)
    follows_count: Mapped[int | None] = mapped_column(Integer)

    # Only Business accounts may publish Stories; Creator accounts may not.
    # Persisted so the UI can disable unsupported actions instead of failing.
    capabilities: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    granted_scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Facebook-login-only linkage
    facebook_page_id: Mapped[str | None] = mapped_column(String(64))
    facebook_page_name: Mapped[str | None] = mapped_column(String(160))

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="CONNECTED")
    # CONNECTED | TOKEN_EXPIRED | DISCONNECTED | ERROR | REVOKED
    status_reason: Mapped[str | None] = mapped_column(String(255))
    connected_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the user asked for erasure of cached platform data.
    purge_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def can_publish_stories(self) -> bool:
        return bool(self.capabilities.get("stories_publish"))

    @property
    def is_usable(self) -> bool:
        return self.status == "CONNECTED" and self.deleted_at is None


class OAuthToken(TenantModel):
    """Encrypted Meta OAuth credentials for a :class:`SocialAccount`."""

    __tablename__ = "oauth_tokens"

    social_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("social_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="META")
    token_type: Mapped[str] = mapped_column(String(32), nullable=False, default="USER")
    # Fernet envelope (v1:key_id:payload) with AAD bound to this row's id.
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    # Non-secret fingerprint (first 16 chars of sha256) for support triage.
    access_token_fingerprint: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    token_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="SHORT_LIVED")
    # SHORT_LIVED | LONG_LIVED | PAGE | SYSTEM_USER
    obtained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class OAuthState(TenantModel):
    """Short-lived CSRF/continuity record for the OAuth round trip.

    ``state`` is a single-use random value; ``code_verifier`` supports PKCE on
    the Instagram Login path.
    """

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    api_path: Mapped[str] = mapped_column(String(32), nullable=False)
    code_verifier: Mapped[str | None] = mapped_column(String(128))
    requested_scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    ip_address: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(String(32))  # SUCCESS | FAILED
    error: Mapped[str | None] = mapped_column(String(255))


class MetaWebhookEvent(TenantModel):
    """Raw inbound webhook event with idempotency + replay protection."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        # A platform event may legitimately be delivered more than once; the
        # unique key makes the duplicate a no-op instead of a double action.
        UniqueConstraint("platform", "event_key", name="uq_webhook_events_platform_key"),
        Index("ix_webhook_events_ws_status", "workspace_id", "processing_status"),
    )

    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="META")
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(64))
    change_type: Mapped[str | None] = mapped_column(String(64))
    entry_id: Mapped[str | None] = mapped_column(String(64), index=True)
    social_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    signature: Mapped[str | None] = mapped_column(String(128))
    # sha256 of the raw body — catches tampering and exact-body duplicates.
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    # PENDING | PROCESSING | PROCESSED | SKIPPED | FAILED
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    # True when the event was older than the replay-protection window.
    is_replay_suspected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class MetaAPIUsage(TenantModel):
    """Rate-limit / quota accounting per account, per rolling window."""

    __tablename__ = "meta_api_usage"
    __table_args__ = (
        Index("ix_meta_api_usage_ws_window", "workspace_id", "window_start"),
    )

    social_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    metric: Mapped[str] = mapped_column(String(48), nullable=False)
    # publish | calls | private_replies | messaging
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta_reported_quota_used: Mapped[int | None] = mapped_column(Integer)
    meta_reported_quota_total: Mapped[int | None] = mapped_column(Integer)
