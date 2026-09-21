"""Inbox (Direct Messages), comments and CRM models.

Messaging compliance is encoded in the schema, not just in prose:
``Conversation.last_inbound_at`` + ``messaging_window_expires_at`` are what the
reply endpoint checks before allowing a free-form send (24h window), and
``human_agent_until`` records the 7-day HUMAN_AGENT extension for *human*
replies only.
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

from app.db.base import GUID, TenantModel


class Conversation(TenantModel):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("social_account_id", "external_conversation_id", name="uq_conversations_ext"),
        Index("ix_conversations_ws_updated", "workspace_id", "updated_at"),
    )

    social_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    participant_ig_id: Mapped[str | None] = mapped_column(String(64), index=True)
    participant_username: Mapped[str | None] = mapped_column(String(64))
    participant_name: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="OPEN")
    # OPEN | PENDING | ESCALATED | RESOLVED | ARCHIVED
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    labels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Free-form replies are only allowed until this instant (last inbound + 24h).
    messaging_window_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # HUMAN_AGENT tag extension — for human replies only, never for bots.
    human_agent_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ai_intent: Mapped[str | None] = mapped_column(String(64))
    ai_lead_score: Mapped[int | None] = mapped_column(Integer)
    needs_human: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    automation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Message(TenantModel):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "external_message_id", name="uq_messages_ext"),
        Index("ix_messages_ws_conversation", "workspace_id", "conversation_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    # INBOUND | OUTBOUND
    sender_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="PARTICIPANT")
    # PARTICIPANT | HUMAN | AI_SUGGESTED | AI_AUTOMATIC
    message_type: Mapped[str] = mapped_column(String(24), nullable=False, default="TEXT")
    # TEXT | IMAGE | VIDEO | AUDIO | REACTION | STORY_MENTION | STORY_REPLY | SYSTEM
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    media_url: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_internal_note: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    ai_generation_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    message_tag: Mapped[str | None] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="SENT")
    # SENT | DELIVERED | READ | FAILED
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Comment(TenantModel):
    __tablename__ = "comments"
    __table_args__ = (
        UniqueConstraint("external_comment_id", "social_account_id", name="uq_comments_ext"),
        Index("ix_comments_ws_status", "workspace_id", "status"),
    )

    social_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_id: Mapped[str | None] = mapped_column(String(64), index=True)
    external_comment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_comment_id: Mapped[str | None] = mapped_column(String(64), index=True)
    from_username: Mapped[str | None] = mapped_column(String(64), index=True)
    from_ig_id: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="NEW")
    # NEW | REPLIED | ESCALATED | HIDDEN | SPAM | ARCHIVED
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    spam_score: Mapped[float] = mapped_column(nullable=False, default=0.0)
    ai_intent: Mapped[str | None] = mapped_column(String(64))
    needs_human: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    labels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class CommentReply(TenantModel):
    __tablename__ = "comment_replies"

    comment_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("comments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    external_reply_id: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    sender_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="HUMAN")
    is_private_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="SENT")
    error: Mapped[str | None] = mapped_column(Text)


class Customer(TenantModel):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "external_id", "source", name="uq_customers_ws_ext"),
        Index("ix_customers_ws_score", "workspace_id", "lead_score"),
    )

    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="INSTAGRAM")
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    display_name: Mapped[str | None] = mapped_column(String(160))
    # Phone/e-mail only when the customer volunteered it (policy: never inferred).
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320))
    phone_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    lead_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="NEW")
    # NEW | QUALIFIED | PROPOSAL | NEGOTIATION | WON | LOST
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class CustomerNote(TenantModel):
    __tablename__ = "customer_notes"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class CustomerActivity(TenantModel):
    __tablename__ = "customer_activities"
    __table_args__ = (Index("ix_customer_activities_ws_customer", "workspace_id", "customer_id"),)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # MESSAGE_IN | MESSAGE_OUT | COMMENT | FOLLOW | NOTE | STAGE_CHANGE
    reference_id: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # `metadata` is reserved by SQLAlchemy's Declarative API.
    meta: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)


class AnalyticsSnapshot(TenantModel):
    """Metrics pulled from the Insights API. Only fields Meta actually returns
    are stored; missing fields stay NULL and are rendered as "no data"."""

    __tablename__ = "analytics_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "social_account_id", "metric", "period", "since", "until",
            name="uq_analytics_snapshots_key",
        ),
    )

    social_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_id: Mapped[str | None] = mapped_column(String(64), index=True)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    period: Mapped[str] = mapped_column(String(24), nullable=False, default="DAY")
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float | None] = mapped_column(nullable=True)
    breakdown: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # NULL means "the API did not return this", never zero.
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="INSIGHTS_API")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
