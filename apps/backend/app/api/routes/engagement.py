"""Production engagement APIs: inbox, comments, CRM and Instagram insights.

All platform mutations go through the official Meta Graph client. Database rows are
tenant-scoped and external webhook/API payloads are treated as untrusted input.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.api.deps import DBSession, TenantScopeDep, require
from app.api.schemas import (
    AnalyticsSnapshotRead,
    CommentRead,
    CommentReplyRequest,
    ConversationRead,
    CustomerNoteCreate,
    CustomerNoteRead,
    CustomerRead,
    CustomerUpdateRequest,
    MessageRead,
    MessageSendRequest,
)
from app.core.exceptions import ValidationError
from app.core.permissions import Permission
from app.modules.inbox.models import (
    AnalyticsSnapshot,
    Comment,
    CommentReply,
    Conversation,
    Customer,
    CustomerActivity,
    CustomerNote,
    Message,
)
from app.modules.instagram.models import SocialAccount
from app.modules.instagram.service import InstagramConnectService
from app.providers.meta.client import MetaGraphClient

router = APIRouter(tags=["engagement", "analytics"])


async def _account(scope: TenantScopeDep, account_id: uuid.UUID) -> SocialAccount:
    account = await scope.get_one(SocialAccount, account_id)
    if not account.is_usable:
        raise ValidationError("Instagram account is not connected.")
    return account


async def _client(
    session: DBSession,
    scope: TenantScopeDep,
    account: SocialAccount,
) -> MetaGraphClient:
    token_service = InstagramConnectService(session, scope)
    token = await token_service.get_active_token(account)
    return MetaGraphClient(access_token=token)


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Inbox
# --------------------------------------------------------------------------- #
@router.get(
    "/inbox/conversations",
    response_model=list[ConversationRead],
    dependencies=[Depends(require(Permission.INBOX_READ))],
)
async def list_conversations(
    scope: TenantScopeDep,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ConversationRead]:
    conditions = [Conversation.status == status_filter] if status_filter else []
    rows = await scope.list(
        Conversation, *conditions, limit=limit, offset=offset,
        order_by=Conversation.last_message_at.desc(),
    )
    return [ConversationRead.model_validate(row) for row in rows]


@router.get(
    "/inbox/conversations/{conversation_id}/messages",
    response_model=list[MessageRead],
    dependencies=[Depends(require(Permission.INBOX_READ))],
)
async def list_messages(
    conversation_id: uuid.UUID,
    scope: TenantScopeDep,
    limit: int = Query(100, ge=1, le=500),
) -> list[MessageRead]:
    await scope.get_one(Conversation, conversation_id)
    rows = await scope.list(
        Message, Message.conversation_id == conversation_id,
        limit=limit, order_by=Message.sent_at.asc(),
    )
    return [MessageRead.model_validate(row) for row in rows]


@router.post(
    "/inbox/conversations/{conversation_id}/send",
    response_model=MessageRead,
    dependencies=[Depends(require(Permission.INBOX_REPLY))],
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: MessageSendRequest,
    scope: TenantScopeDep,
    session: DBSession,
) -> MessageRead:
    conversation = await scope.get_one(Conversation, conversation_id)
    if conversation.participant_ig_id is None:
        raise ValidationError("Conversation has no Instagram participant id.")

    now = datetime.now(UTC)
    window_ok = (
        conversation.messaging_window_expires_at is not None
        and conversation.messaging_window_expires_at > now
    )
    human_extension_ok = (
        payload.human_agent
        and conversation.human_agent_until is not None
        and conversation.human_agent_until > now
    )
    if not (window_ok or human_extension_ok):
        raise ValidationError(
            "The Instagram messaging window is closed. A free-form reply cannot be sent."
        )

    account = await _account(scope, conversation.social_account_id)
    client = await _client(session, scope, account)
    tag = "HUMAN_AGENT" if payload.human_agent else None
    result = await client.send_message(
        conversation.participant_ig_id, message={"text": payload.text}, tag=tag
    )
    external_id = str(result.get("message_id") or result.get("id") or uuid.uuid4())
    message = Message(
        workspace_id=scope.workspace_id,
        conversation_id=conversation.id,
        external_message_id=external_id,
        direction="OUTBOUND",
        sender_kind="HUMAN" if payload.human_agent else "PARTICIPANT",
        message_type="TEXT",
        text=payload.text,
        sent_by=scope.context.user_id,
        message_tag=tag,
        status="SENT",
        sent_at=now,
        created_by=scope.context.user_id,
    )
    session.add(message)
    conversation.last_message_at = now
    conversation.is_read = True
    if payload.human_agent:
        conversation.human_agent_until = now + timedelta(days=7)
    scope.touch(conversation)
    await session.flush()
    return MessageRead.model_validate(message)


@router.post(
    "/inbox/accounts/{account_id}/sync",
    response_model=dict[str, int],
    dependencies=[Depends(require(Permission.INBOX_READ))],
)
async def sync_inbox(
    account_id: uuid.UUID,
    scope: TenantScopeDep,
    session: DBSession,
) -> dict[str, int]:
    account = await _account(scope, account_id)
    client = await _client(session, scope, account)
    payload = await client.list_conversations(account.external_account_id)
    conversations = payload.get("data") or []
    created = messages = 0
    for raw in conversations:
        ext_id = str(raw.get("id") or "")
        if not ext_id:
            continue
        existing = (
            await session.execute(
                select(Conversation).where(
                    Conversation.workspace_id == scope.workspace_id,
                    Conversation.social_account_id == account.id,
                    Conversation.external_conversation_id == ext_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            participants = raw.get("participants") or {}
            pdata = participants.get("data") if isinstance(participants, dict) else participants
            participant = (pdata or [{}])[0] if isinstance(pdata, list) else {}
            existing = Conversation(
                workspace_id=scope.workspace_id,
                social_account_id=account.id,
                external_conversation_id=ext_id,
                participant_ig_id=str(participant.get("id")) if participant.get("id") else None,
                participant_username=participant.get("username"),
                participant_name=participant.get("name"),
                status="OPEN",
                labels=[],
                created_by=scope.context.user_id,
            )
            session.add(existing)
            await session.flush()
            created += 1
        msg_data = ((raw.get("messages") or {}).get("data") or [])
        for raw_msg in msg_data:
            msg_id = str(raw_msg.get("id") or "")
            if not msg_id:
                continue
            exists = (
                await session.execute(
                    select(Message.id).where(
                        Message.workspace_id == scope.workspace_id,
                        Message.conversation_id == existing.id,
                        Message.external_message_id == msg_id,
                    )
                )
            ).scalar_one_or_none()
            if exists:
                continue
            sender = raw_msg.get("from") or {}
            inbound = str(sender.get("id") or "") != account.external_account_id
            sent_at = _dt(raw_msg.get("created_time")) or datetime.now(UTC)
            session.add(
                Message(
                    workspace_id=scope.workspace_id,
                    conversation_id=existing.id,
                    external_message_id=msg_id,
                    direction="INBOUND" if inbound else "OUTBOUND",
                    sender_kind="PARTICIPANT" if inbound else "HUMAN",
                    message_type="TEXT",
                    text=str(raw_msg.get("text") or ""),
                    attachments=raw_msg.get("attachments", {}).get("data", [])
                    if isinstance(raw_msg.get("attachments"), dict) else [],
                    status="READ" if raw_msg.get("is_read") else "SENT",
                    sent_at=sent_at,
                    created_by=scope.context.user_id,
                )
            )
            messages += 1
            if inbound:
                existing.last_inbound_at = max(
                    existing.last_inbound_at or sent_at, sent_at
                )
                existing.messaging_window_expires_at = sent_at + timedelta(hours=24)
        existing.last_message_at = _dt(raw.get("updated_time")) or existing.last_message_at
        scope.touch(existing)
    await session.flush()
    return {"conversations_created": created, "messages_created": messages}


# --------------------------------------------------------------------------- #
# Comments
# --------------------------------------------------------------------------- #
@router.get(
    "/comments",
    response_model=list[CommentRead],
    dependencies=[Depends(require(Permission.COMMENTS_READ))],
)
async def list_comments(
    scope: TenantScopeDep,
    status_filter: str | None = Query(None, alias="status"),
    media_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[CommentRead]:
    conditions = []
    if status_filter:
        conditions.append(Comment.status == status_filter)
    if media_id:
        conditions.append(Comment.media_id == media_id)
    rows = await scope.list(Comment, *conditions, limit=limit, order_by=Comment.posted_at.desc())
    return [CommentRead.model_validate(row) for row in rows]


@router.post(
    "/comments/{comment_id}/reply",
    response_model=CommentRead,
    dependencies=[Depends(require(Permission.COMMENTS_REPLY))],
)
async def reply_comment(
    comment_id: uuid.UUID,
    payload: CommentReplyRequest,
    scope: TenantScopeDep,
    session: DBSession,
) -> CommentRead:
    comment = await scope.get_one(Comment, comment_id)
    account = await _account(scope, comment.social_account_id)
    client = await _client(session, scope, account)
    if payload.private:
        result = await client.send_private_reply(comment.external_comment_id, message=payload.text)
    else:
        result = await client.reply_to_comment(comment.external_comment_id, message=payload.text)
    reply = CommentReply(
        workspace_id=scope.workspace_id,
        comment_id=comment.id,
        text=payload.text,
        external_reply_id=str(result.get("id") or result.get("comment_id") or ""),
        created_by=scope.context.user_id,
        status="SENT",
        is_private_reply=payload.private,
    )
    session.add(reply)
    comment.status = "REPLIED"
    comment.replied_at = datetime.now(UTC)
    comment.replied_by = scope.context.user_id
    scope.touch(comment)
    await session.flush()
    return CommentRead.model_validate(comment)


@router.post(
    "/comments/{comment_id}/hide",
    response_model=CommentRead,
    dependencies=[Depends(require(Permission.COMMENTS_HIDE))],
)
async def hide_comment(
    comment_id: uuid.UUID,
    scope: TenantScopeDep,
    session: DBSession,
) -> CommentRead:
    comment = await scope.get_one(Comment, comment_id)
    account = await _account(scope, comment.social_account_id)
    client = await _client(session, scope, account)
    await client.hide_comment(comment.external_comment_id, hide=True)
    comment.is_hidden = True
    comment.status = "HIDDEN"
    scope.touch(comment)
    await session.flush()
    return CommentRead.model_validate(comment)


@router.post(
    "/comments/accounts/{account_id}/media/{media_id}/sync",
    response_model=dict[str, int],
    dependencies=[Depends(require(Permission.COMMENTS_READ))],
)
async def sync_comments(
    account_id: uuid.UUID,
    media_id: str,
    scope: TenantScopeDep,
    session: DBSession,
) -> dict[str, int]:
    account = await _account(scope, account_id)
    client = await _client(session, scope, account)
    payload = await client.list_media_comments(media_id)
    rows = payload.get("data") or []
    created = 0
    for raw in rows:
        ext = str(raw.get("id") or "")
        if not ext:
            continue
        exists = (
            await session.execute(
                select(Comment.id).where(
                    Comment.workspace_id == scope.workspace_id,
                    Comment.social_account_id == account.id,
                    Comment.external_comment_id == ext,
                )
            )
        ).scalar_one_or_none()
        if exists:
            continue
        sender = raw.get("from") or {}
        session.add(
            Comment(
                workspace_id=scope.workspace_id,
                social_account_id=account.id,
                media_id=media_id,
                external_comment_id=ext,
                parent_comment_id=raw.get("parent_id"),
                from_username=sender.get("username") or raw.get("username"),
                from_ig_id=str(sender.get("id")) if sender.get("id") else None,
                text=str(raw.get("text") or ""),
                like_count=int(raw.get("like_count") or 0),
                status="NEW",
                posted_at=_dt(raw.get("timestamp")),
                created_by=scope.context.user_id,
            )
        )
        created += 1
    await session.flush()
    return {"comments_created": created}


# --------------------------------------------------------------------------- #
# CRM
# --------------------------------------------------------------------------- #
@router.get(
    "/crm/customers",
    response_model=list[CustomerRead],
    dependencies=[Depends(require(Permission.CRM_READ))],
)
async def list_customers(
    scope: TenantScopeDep,
    stage: str | None = None,
    search: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[CustomerRead]:
    conditions = []
    if stage:
        conditions.append(Customer.stage == stage)
    if search:
        needle = f"%{search.strip()}%"
        conditions.append(Customer.username.ilike(needle) | Customer.display_name.ilike(needle))
    rows = await scope.list(Customer, *conditions, limit=limit, order_by=Customer.updated_at.desc())
    return [CustomerRead.model_validate(row) for row in rows]


@router.get(
    "/crm/customers/{customer_id}",
    response_model=CustomerRead,
    dependencies=[Depends(require(Permission.CRM_READ))],
)
async def get_customer(customer_id: uuid.UUID, scope: TenantScopeDep) -> CustomerRead:
    return CustomerRead.model_validate(await scope.get_one(Customer, customer_id))


@router.patch(
    "/crm/customers/{customer_id}",
    response_model=CustomerRead,
    dependencies=[Depends(require(Permission.CRM_WRITE))],
)
async def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdateRequest,
    scope: TenantScopeDep,
    session: DBSession,
) -> CustomerRead:
    customer = await scope.get_one(Customer, customer_id)
    before_stage = customer.stage
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(customer, key, value)
    if customer.stage != before_stage:
        session.add(
            CustomerActivity(
                workspace_id=scope.workspace_id,
                customer_id=customer.id,
                kind="STAGE_CHANGE",
                summary=f"{before_stage} -> {customer.stage}",
                meta={"from": before_stage, "to": customer.stage},
                created_by=scope.context.user_id,
            )
        )
    scope.touch(customer)
    await session.flush()
    return CustomerRead.model_validate(customer)


@router.post(
    "/crm/customers/{customer_id}/notes",
    response_model=CustomerNoteRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.CRM_WRITE))],
)
async def add_customer_note(
    customer_id: uuid.UUID,
    payload: CustomerNoteCreate,
    scope: TenantScopeDep,
    session: DBSession,
) -> CustomerNoteRead:
    await scope.get_one(Customer, customer_id)
    note = CustomerNote(
        workspace_id=scope.workspace_id,
        customer_id=customer_id,
        body=payload.body,
        author_id=scope.context.user_id,
        created_by=scope.context.user_id,
    )
    session.add(note)
    await session.flush()
    return CustomerNoteRead.model_validate(note)


@router.get(
    "/crm/customers/{customer_id}/notes",
    response_model=list[CustomerNoteRead],
    dependencies=[Depends(require(Permission.CRM_READ))],
)
async def list_customer_notes(
    customer_id: uuid.UUID,
    scope: TenantScopeDep,
) -> list[CustomerNoteRead]:
    await scope.get_one(Customer, customer_id)
    rows = await scope.list(
        CustomerNote, CustomerNote.customer_id == customer_id,
        order_by=CustomerNote.created_at.desc(), limit=200,
    )
    return [CustomerNoteRead.model_validate(row) for row in rows]


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@router.get(
    "/analytics/snapshots",
    response_model=list[AnalyticsSnapshotRead],
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)
async def list_analytics(
    scope: TenantScopeDep,
    account_id: uuid.UUID | None = None,
    metric: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> list[AnalyticsSnapshotRead]:
    conditions = []
    if account_id:
        conditions.append(AnalyticsSnapshot.social_account_id == account_id)
    if metric:
        conditions.append(AnalyticsSnapshot.metric == metric)
    if since:
        conditions.append(AnalyticsSnapshot.until >= since)
    if until:
        conditions.append(AnalyticsSnapshot.since <= until)
    rows = await scope.list(
        AnalyticsSnapshot, *conditions, limit=limit,
        order_by=AnalyticsSnapshot.until.desc(),
    )
    return [AnalyticsSnapshotRead.model_validate(row) for row in rows]


@router.post(
    "/analytics/accounts/{account_id}/sync",
    response_model=dict[str, int],
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)
async def sync_analytics(
    account_id: uuid.UUID,
    scope: TenantScopeDep,
    session: DBSession,
    days: int = Query(7, ge=1, le=90),
) -> dict[str, int]:
    account = await _account(scope, account_id)
    client = await _client(session, scope, account)
    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    metrics = ["impressions", "reach", "profile_views", "website_clicks"]
    epoch_since = int(since.timestamp())
    epoch_until = int(until.timestamp())
    payload = await client.get_insights(
        account.external_account_id,
        metric=metrics,
        period="day",
        since=epoch_since,
        until=epoch_until,
    )
    data = payload.get("data") or []
    created = 0
    for item in data:
        metric = str(item.get("name") or item.get("metric") or "")
        if not metric:
            continue
        values = item.get("values") or []
        for value in values:
            end_time = _dt(value.get("end_time")) or until
            start_time = _dt(value.get("start_time")) or (end_time - timedelta(days=1))
            numeric = value.get("value")
            if isinstance(numeric, dict):
                # Some Meta metrics are dimensional; keep the raw breakdown and
                # don't coerce it to zero.
                parsed_value = None
                breakdown = numeric
            else:
                try:
                    parsed_value = float(numeric) if numeric is not None else None
                except (TypeError, ValueError):
                    parsed_value = None
                breakdown = {}
            exists = (
                await session.execute(
                    select(AnalyticsSnapshot).where(
                        AnalyticsSnapshot.workspace_id == scope.workspace_id,
                        AnalyticsSnapshot.social_account_id == account.id,
                        AnalyticsSnapshot.metric == metric,
                        AnalyticsSnapshot.period == "DAY",
                        AnalyticsSnapshot.since == start_time,
                        AnalyticsSnapshot.until == end_time,
                    )
                )
            ).scalar_one_or_none()
            if exists:
                exists.value = parsed_value
                exists.breakdown = breakdown
                exists.fetched_at = until
                exists.is_available = True
                continue
            session.add(
                AnalyticsSnapshot(
                    workspace_id=scope.workspace_id,
                    social_account_id=account.id,
                    metric=metric,
                    period="DAY",
                    since=start_time,
                    until=end_time,
                    value=parsed_value,
                    breakdown=breakdown,
                    is_available=True,
                    source="INSIGHTS_API",
                    fetched_at=until,
                    created_by=scope.context.user_id,
                )
            )
            created += 1
    await session.flush()
    return {"snapshots_created": created, "metrics_received": len(data)}
