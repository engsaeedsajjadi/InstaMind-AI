"""Inbox, comments, CRM and analytics APIs backed by the existing models.

All records are tenant-scoped. Meta write operations use the official Graph API
client and never fabricate a successful external action.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import desc, select

from app.api.deps import CurrentUser, DBSession, TenantScopeDep, require
from app.api.schemas import (
    CommentRead, CommentReplyRequest, ConversationRead, CustomerRead, CustomerUpdate,
    MessageRead, SendMessageRequest, AnalyticsRead,
)
from app.core.exceptions import ConflictError, ValidationError
from app.core.permissions import Permission
from app.modules.audit.service import AuditService
from app.modules.inbox.models import AnalyticsSnapshot, Comment, CommentReply, Conversation, Customer, Message
from app.modules.instagram.models import SocialAccount
from app.modules.instagram.service import InstagramConnectService
from app.providers.meta.client import MetaGraphClient

router = APIRouter(prefix="/engagement", tags=["engagement"])


@router.post("/sync/conversations/{social_account_id}", response_model=dict, dependencies=[Depends(require(Permission.INBOX_READ))])
async def sync_conversations(social_account_id: uuid.UUID, session: DBSession, scope: TenantScopeDep):
    account = await scope.get_one(SocialAccount, social_account_id)
    if not account.capabilities.get("messages_manage"):
        raise ValidationError("This Instagram connection does not grant messaging capability.")
    token = await InstagramConnectService(session, scope).get_active_token(account)
    client = MetaGraphClient(access_token=token)
    try:
        payload = await client.list_conversations(account.external_account_id)
    finally:
        await client.aclose()
    created = updated = 0
    for raw in payload.get("data", []):
        ext_id = str(raw.get("id") or raw.get("conversation_id") or "")
        if not ext_id:
            continue
        row = (await session.execute(select(Conversation).where(
            Conversation.workspace_id == scope.workspace_id,
            Conversation.social_account_id == account.id,
            Conversation.external_conversation_id == ext_id,
        ))).scalar_one_or_none()
        participants = raw.get("participants", {}).get("data", []) if isinstance(raw.get("participants"), dict) else []
        participant = participants[0] if participants else {}
        messages = raw.get("messages", {}).get("data", []) if isinstance(raw.get("messages"), dict) else []
        last = messages[-1] if messages else {}
        if row is None:
            row = Conversation(workspace_id=scope.workspace_id, social_account_id=account.id, external_conversation_id=ext_id, created_by=scope.context.user_id)
            session.add(row); created += 1
        else:
            updated += 1
        row.participant_ig_id = str(participant.get("id")) if participant.get("id") else row.participant_ig_id
        row.participant_username = participant.get("username") or row.participant_username
        row.participant_name = participant.get("name") or row.participant_name
        row.status = row.status or "OPEN"
        row.last_message_at = datetime.fromisoformat(last["created_time"].replace("Z", "+00:00")) if last.get("created_time") else row.last_message_at
        await session.flush()
    return {"created": created, "updated": updated, "received": len(payload.get("data", []))}


@router.post("/sync/comments/{social_account_id}", response_model=dict, dependencies=[Depends(require(Permission.COMMENTS_READ))])
async def sync_comments(social_account_id: uuid.UUID, session: DBSession, scope: TenantScopeDep, media_id: str = Query(..., min_length=1, max_length=128)):
    account = await scope.get_one(SocialAccount, social_account_id)
    if not account.capabilities.get("comments_manage"):
        raise ValidationError("This Instagram connection does not grant comments capability.")
    token = await InstagramConnectService(session, scope).get_active_token(account)
    client = MetaGraphClient(access_token=token)
    try:
        payload = await client.list_media_comments(media_id)
    finally:
        await client.aclose()
    created = 0
    for raw in payload.get("data", []):
        ext_id = str(raw.get("id") or "")
        if not ext_id:
            continue
        existing = (await session.execute(select(Comment).where(Comment.workspace_id == scope.workspace_id, Comment.social_account_id == account.id, Comment.external_comment_id == ext_id))).scalar_one_or_none()
        if existing:
            existing.text = raw.get("text", existing.text); existing.like_count = raw.get("like_count", existing.like_count); continue
        author = raw.get("from") or {}
        row = Comment(workspace_id=scope.workspace_id, social_account_id=account.id, media_id=media_id, external_comment_id=ext_id,
                      from_username=author.get("username"), from_ig_id=str(author.get("id")) if author.get("id") else None,
                      text=raw.get("text", ""), like_count=int(raw.get("like_count") or 0), posted_at=datetime.fromisoformat(raw["timestamp"].replace("Z", "+00:00")) if raw.get("timestamp") else datetime.now(UTC), created_by=scope.context.user_id)
        session.add(row); created += 1
    await session.flush()
    return {"created": created, "received": len(payload.get("data", []))}


@router.post("/sync/analytics/{social_account_id}", response_model=dict, dependencies=[Depends(require(Permission.ANALYTICS_READ))])
async def sync_analytics(social_account_id: uuid.UUID, session: DBSession, scope: TenantScopeDep, since: int | None = None, until: int | None = None):
    account = await scope.get_one(SocialAccount, social_account_id)
    if not account.capabilities.get("insights_read"):
        raise ValidationError("This Instagram connection does not grant Insights capability.")
    token = await InstagramConnectService(session, scope).get_active_token(account)
    client = MetaGraphClient(access_token=token)
    metrics = ["reach", "views", "accounts_engaged", "total_interactions"]
    try:
        payload = await client.get_insights(account.external_account_id, metric=metrics, period="day", since=since, until=until)
    finally:
        await client.aclose()
    now = datetime.now(UTC); count = 0
    for raw in payload.get("data", []):
        metric = str(raw.get("name") or raw.get("metric") or "unknown")
        for value in raw.get("values", []):
            end_raw = value.get("end_time")
            end = datetime.fromisoformat(end_raw.replace("Z", "+00:00")) if end_raw else now
            start = end - timedelta(days=1)
            row = (await session.execute(select(AnalyticsSnapshot).where(
                AnalyticsSnapshot.workspace_id == scope.workspace_id,
                AnalyticsSnapshot.social_account_id == account.id,
                AnalyticsSnapshot.metric == metric,
                AnalyticsSnapshot.period == "DAY",
                AnalyticsSnapshot.since == start,
                AnalyticsSnapshot.until == end,
            ))).scalar_one_or_none()
            if row is None:
                row = AnalyticsSnapshot(workspace_id=scope.workspace_id, social_account_id=account.id, metric=metric, period="DAY", since=start, until=end, created_by=scope.context.user_id)
                session.add(row); count += 1
            row.value = float(value.get("value")) if isinstance(value.get("value"), (int,float)) else None
            row.breakdown = value.get("breakdown") or {}
            row.is_available = "value" in value
            row.source = "INSIGHTS_API"
            row.fetched_at = now
    await session.flush()
    return {"created": count, "metrics": metrics}


@router.get("/conversations", response_model=list[ConversationRead], dependencies=[Depends(require(Permission.INBOX_READ))])
async def list_conversations(scope: TenantScopeDep, limit: int = Query(50, ge=1, le=200)):
    return await scope.list(Conversation, order_by=desc(Conversation.last_message_at), limit=limit)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead], dependencies=[Depends(require(Permission.INBOX_READ))])
async def conversation_messages(conversation_id: uuid.UUID, scope: TenantScopeDep, limit: int = Query(100, ge=1, le=500)):
    conversation = await scope.get_one(Conversation, conversation_id)
    return await scope.list(Message, Message.conversation_id == conversation.id, order_by=Message.sent_at.asc(), limit=limit)


@router.post("/conversations/{conversation_id}/messages", response_model=MessageRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require(Permission.INBOX_REPLY))])
async def send_message(conversation_id: uuid.UUID, payload: SendMessageRequest, session: DBSession, scope: TenantScopeDep, user: CurrentUser):
    conversation = await scope.get_one(Conversation, conversation_id)
    if not conversation.participant_ig_id:
        raise ValidationError("Conversation has no Instagram participant ID.")
    account = await scope.get_one(SocialAccount, conversation.social_account_id)
    if not account.capabilities.get("messages_manage"):
        raise ValidationError("This Instagram connection does not grant messaging capability.")
    now = datetime.now(UTC)
    if not conversation.messaging_window_expires_at or conversation.messaging_window_expires_at <= now:
        raise ConflictError("The Instagram 24-hour messaging window is closed.")
    token = await InstagramConnectService(session, scope).get_active_token(account)
    client = MetaGraphClient(access_token=token)
    try:
        result = await client.send_message(conversation.participant_ig_id, message={"text": payload.text}, tag="HUMAN_AGENT" if payload.use_human_agent_tag else None)
    finally:
        await client.aclose()
    external_id = str(result.get("message_id") or result.get("id") or "")
    if not external_id:
        raise ValidationError("Meta accepted the request but returned no message identifier; local state was not recorded.")
    message = Message(workspace_id=scope.workspace_id, conversation_id=conversation.id, external_message_id=external_id,
                      direction="OUTBOUND", sender_kind="HUMAN", message_type="TEXT", text=payload.text,
                      sent_by=user.id, message_tag="HUMAN_AGENT" if payload.use_human_agent_tag else None,
                      status="SENT", sent_at=now, created_by=user.id)
    session.add(message)
    conversation.is_read = True
    conversation.last_message_at = now
    scope.touch(conversation)
    await AuditService(session).record(action="inbox.message_sent", workspace_id=scope.workspace_id, actor_id=user.id, resource_type="conversation", resource_id=str(conversation.id))
    await session.flush()
    return message


@router.patch("/conversations/{conversation_id}/read", response_model=ConversationRead, dependencies=[Depends(require(Permission.INBOX_READ))])
async def mark_read(conversation_id: uuid.UUID, scope: TenantScopeDep):
    conversation = await scope.get_one(Conversation, conversation_id)
    conversation.is_read = True
    scope.touch(conversation)
    await scope.flush()
    return conversation


@router.get("/comments", response_model=list[CommentRead], dependencies=[Depends(require(Permission.COMMENTS_READ))])
async def list_comments(scope: TenantScopeDep, status_filter: str | None = Query(None, alias="status"), limit: int = Query(100, ge=1, le=500)):
    conditions = [Comment.status == status_filter] if status_filter else []
    return await scope.list(Comment, *conditions, order_by=desc(Comment.posted_at), limit=limit)


@router.post("/comments/{comment_id}/reply", response_model=CommentRead, dependencies=[Depends(require(Permission.COMMENTS_REPLY))])
async def reply_comment(comment_id: uuid.UUID, payload: CommentReplyRequest, session: DBSession, scope: TenantScopeDep, user: CurrentUser):
    comment = await scope.get_one(Comment, comment_id)
    account = await scope.get_one(SocialAccount, comment.social_account_id)
    if not account.capabilities.get("comments_manage"):
        raise ValidationError("This Instagram connection does not grant comments capability.")
    token = await InstagramConnectService(session, scope).get_active_token(account)
    client = MetaGraphClient(access_token=token)
    try:
        result = await client.reply_to_comment(comment.external_comment_id, message=payload.text)
    finally:
        await client.aclose()
    reply = CommentReply(workspace_id=scope.workspace_id, comment_id=comment.id, text=payload.text,
                         external_reply_id=str(result.get("id") or ""), created_by=user.id, sender_kind="HUMAN", status="SENT")
    session.add(reply)
    comment.status = "REPLIED"
    comment.replied_at = datetime.now(UTC)
    comment.replied_by = user.id
    scope.touch(comment)
    await AuditService(session).record(action="comments.replied", workspace_id=scope.workspace_id, actor_id=user.id, resource_type="comment", resource_id=str(comment.id))
    await session.flush()
    return comment


@router.post("/comments/{comment_id}/hide", response_model=CommentRead, dependencies=[Depends(require(Permission.COMMENTS_HIDE))])
async def hide_comment(comment_id: uuid.UUID, session: DBSession, scope: TenantScopeDep, user: CurrentUser):
    comment = await scope.get_one(Comment, comment_id)
    account = await scope.get_one(SocialAccount, comment.social_account_id)
    if not account.capabilities.get("comments_manage"):
        raise ValidationError("This Instagram connection does not grant comments capability.")
    token = await InstagramConnectService(session, scope).get_active_token(account)
    client = MetaGraphClient(access_token=token)
    try:
        await client.hide_comment(comment.external_comment_id, hide=True)
    finally:
        await client.aclose()
    comment.is_hidden = True
    comment.status = "HIDDEN"
    scope.touch(comment)
    await AuditService(session).record(action="comments.hidden", workspace_id=scope.workspace_id, actor_id=user.id, resource_type="comment", resource_id=str(comment.id))
    await session.flush()
    return comment


@router.get("/customers", response_model=list[CustomerRead], dependencies=[Depends(require(Permission.CRM_READ))])
async def list_customers(scope: TenantScopeDep, limit: int = Query(100, ge=1, le=500)):
    return await scope.list(Customer, order_by=desc(Customer.last_interaction_at), limit=limit)


@router.get("/customers/{customer_id}", response_model=CustomerRead, dependencies=[Depends(require(Permission.CRM_READ))])
async def get_customer(customer_id: uuid.UUID, scope: TenantScopeDep):
    return await scope.get_one(Customer, customer_id)


@router.patch("/customers/{customer_id}", response_model=CustomerRead, dependencies=[Depends(require(Permission.CRM_WRITE))])
async def update_customer(customer_id: uuid.UUID, payload: CustomerUpdate, scope: TenantScopeDep):
    customer = await scope.get_one(Customer, customer_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(customer, key, value)
    scope.touch(customer)
    await scope.flush()
    return customer


@router.get("/analytics", response_model=list[AnalyticsRead], dependencies=[Depends(require(Permission.ANALYTICS_READ))])
async def analytics(scope: TenantScopeDep, metric: str | None = None, limit: int = Query(200, ge=1, le=1000)):
    conditions = [AnalyticsSnapshot.metric == metric] if metric else []
    return await scope.list(AnalyticsSnapshot, *conditions, order_by=desc(AnalyticsSnapshot.until), limit=limit)
