"""Publishing engine models."""

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


class PublishingJob(TenantModel):
    """One publish request for one (content, account) pair.

    * ``idempotency_key`` is unique per workspace, so a retried HTTP request or
      a duplicate Celery delivery cannot publish twice.
    * ``lease_expires_at`` + ``lease_owner`` implement an atomic claim so two
      workers never publish the same job (see ``claim_next_job``).
    """

    __tablename__ = "publishing_jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_publishing_jobs_ws_idem"),
        Index("ix_publishing_jobs_ws_state", "workspace_id", "state", "scheduled_for"),
        Index("ix_publishing_jobs_due", "state", "scheduled_for"),
    )

    content_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("contents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    trigger: Mapped[str] = mapped_column(String(24), nullable=False, default="SCHEDULED")
    # IMMEDIATE | SCHEDULED | MANUAL
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="QUEUED")
    # QUEUED | RUNNING | SUCCEEDED | FAILED | CANCELLED | DEAD
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(96))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    is_retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # e.g. {"media_id": "...", "permalink": "https://instagram.com/p/..."}


class PublishingAttempt(TenantModel):
    """Immutable log of one API interaction — including the raw Meta response,
    which is what support needs when a customer says "it didn't post"."""

    __tablename__ = "publishing_attempts"
    __table_args__ = (Index("ix_publishing_attempts_ws_job", "workspace_id", "job_id"),)

    job_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("publishing_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[str] = mapped_column(String(32), nullable=False, default="PUBLISH")
    # CONTAINER | PUBLISH | POLL | CAROUSEL_CHILD
    request_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    meta_error_code: Mapped[int | None] = mapped_column(Integer)
    meta_error_subcode: Mapped[int | None] = mapped_column(Integer)
    meta_error_type: Mapped[str | None] = mapped_column(String(96))
    meta_error_message: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, default="UNKNOWN")
    # SUCCESS | RETRYABLE_ERROR | PERMANENT_ERROR | RATE_LIMITED
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublishingQuota(TenantModel):
    """Rolling 24h publish counter per account. Meta's documented ceiling is
    50–100 API-published posts per rolling 24h; we enforce our own guard below
    that so a tenant can never hit Meta's wall silently."""

    __tablename__ = "publishing_quota"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "social_account_id", "window_start",
            name="uq_publishing_quota_ws_account_window",
        ),
    )

    social_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limit: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
