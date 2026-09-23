"""Background tasks.

Every task creates its own session and its own :class:`TenantScope`, because a
worker has no request context. The tenant is always resolved from the row being
processed — never from a task argument that a caller could influence.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.logging import configure_logging, logger
from app.db.session import get_session_factory
from app.modules.instagram.models import SocialAccount
from app.modules.publishing.models import PublishingJob
from app.worker.celery_app import celery_app

configure_logging()

WORKER_ID_PREFIX = "celery"


def _run(coro_factory):
    """Run an async unit of work inside a synchronous Celery task."""

    async def _main():
        factory = get_session_factory()
        async with factory() as session:
            try:
                result = await coro_factory(session)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise

    return asyncio.run(_main())


@celery_app.task(name="instamind.publishing.process_due_jobs", bind=True, max_retries=0)
def process_due_jobs(self) -> dict:  # noqa: ANN001
    """Claim and execute due publishing jobs, one workspace at a time.

    Claiming is atomic in the database, so running several workers is safe.
    """

    async def _work(session) -> dict:
        from app.modules.instagram.service import InstagramConnectService
        from app.modules.publishing.service import PublishingService
        from app.modules.tenants.scope import TenantContext, TenantScope
        from app.providers.meta.client import MetaGraphClient

        worker_id = f"{WORKER_ID_PREFIX}-{self.request.id or 'manual'}"
        due = (
            await session.execute(
                select(PublishingJob.workspace_id)
                .where(
                    PublishingJob.state == "QUEUED",
                    PublishingJob.scheduled_for <= datetime.now(UTC),
                    PublishingJob.deleted_at.is_(None),
                )
                .distinct()
                .limit(20)
            )
        ).scalars().all()

        processed = succeeded = failed = 0
        for workspace_id in due:
            # A system actor: no user, permissions are not consulted here
            # because the permission check already happened at enqueue time.
            context = TenantContext(
                user_id=uuid.UUID(int=0),
                workspace_id=workspace_id,
                roles=("SYSTEM",),
                is_superuser=True,
            )
            scope = TenantScope(session, context)
            service = PublishingService(
                session,
                scope,
                connect_service=InstagramConnectService(session, scope),
                client_factory=lambda access_token: MetaGraphClient(access_token=access_token),
            )
            jobs = await service.claim_next(worker_id=worker_id, limit=5)
            for job in jobs:
                processed += 1
                try:
                    result = await service.process_job(job, worker_id=worker_id)
                    succeeded += 1 if result.state == "SUCCEEDED" else 0
                except Exception as exc:  # noqa: BLE001 - one job must not stop the batch
                    failed += 1
                    logger.warning(
                        "publishing_job_error",
                        job_id=str(job.id),
                        workspace_id=str(workspace_id),
                        error=str(exc)[:300],
                    )
        return {"processed": processed, "succeeded": succeeded, "failed": failed}

    return _run(_work)


@celery_app.task(name="instamind.publishing.process_job", bind=True, max_retries=0)
def process_job(self, job_id: str) -> dict:  # noqa: ANN001
    """Execute a single job (used for immediate publishes)."""

    async def _work(session) -> dict:
        from app.modules.instagram.service import InstagramConnectService
        from app.modules.publishing.service import PublishingService
        from app.modules.tenants.scope import TenantContext, TenantScope
        from app.providers.meta.client import MetaGraphClient

        job = await session.get(PublishingJob, uuid.UUID(job_id))
        if job is None:
            return {"status": "missing"}
        context = TenantContext(
            user_id=uuid.UUID(int=0),
            workspace_id=job.workspace_id,
            roles=("SYSTEM",),
            is_superuser=True,
        )
        scope = TenantScope(session, context)
        service = PublishingService(
            session,
            scope,
            connect_service=InstagramConnectService(session, scope),
            client_factory=lambda access_token: MetaGraphClient(access_token=access_token),
        )
        claimed = await service.claim_next(worker_id=f"{WORKER_ID_PREFIX}-{self.request.id}", limit=1)
        if not claimed:
            return {"status": "not_claimable", "state": job.state}
        result = await service.process_job(job, worker_id="celery")
        return {"status": result.state, "media_id": result.media_id}

    return _run(_work)


@celery_app.task(name="instamind.instagram.refresh_tokens")
def refresh_meta_tokens() -> dict:  # noqa: ANN001
    """Refresh Meta tokens before they lapse (daily sweep)."""

    async def _work(session) -> dict:
        from app.modules.instagram.service import InstagramConnectService
        from app.modules.tenants.scope import TenantContext, TenantScope
        from app.providers.meta.client import MetaGraphClient

        accounts = (
            await session.execute(
                select(SocialAccount).where(
                    SocialAccount.status.in_(("CONNECTED", "TOKEN_EXPIRED")),
                    SocialAccount.deleted_at.is_(None),
                )
            )
        ).scalars().all()

        refreshed = errors = 0
        for account in accounts:
            context = TenantContext(
                user_id=uuid.UUID(int=0),
                workspace_id=account.workspace_id,
                roles=("SYSTEM",),
                is_superuser=True,
            )
            scope = TenantScope(session, context)
            service = InstagramConnectService(
                session,
                scope,
                client_factory=lambda access_token: MetaGraphClient(access_token=access_token),
            )
            try:
                if await service.refresh_if_needed(account):
                    refreshed += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.warning("token_refresh_error", account=str(account.id), error=str(exc)[:200])
        return {"accounts": len(accounts), "refreshed": refreshed, "errors": errors}

    return _run(_work)


@celery_app.task(name="instamind.webhooks.process_event")
def process_webhook_event(event_id: str) -> dict:  # noqa: ANN001
    """Turn a stored webhook event into domain changes (inbox, comments)."""

    async def _work(session) -> dict:
        from app.modules.inbox.models import Comment, Conversation, Message
        from app.modules.instagram.models import MetaWebhookEvent

        event = await session.get(MetaWebhookEvent, uuid.UUID(event_id))
        if event is None:
            return {"status": "missing"}
        if event.processing_status == "PROCESSED":
            return {"status": "already_processed"}
        event.processing_status = "PROCESSING"
        event.attempts += 1
        await session.flush()
        try:
            item = event.payload or {}
            change = item.get("change") or {}
            value = change.get("value") or {}
            messaging = item.get("messaging") or {}
            created = 0

            if event.change_type == "comments":
                comment_id = str(value.get("id") or "")
                if comment_id and event.social_account_id:
                    exists = (await session.execute(
                        select(Comment.id).where(
                            Comment.workspace_id == event.workspace_id,
                            Comment.social_account_id == event.social_account_id,
                            Comment.external_comment_id == comment_id,
                        )
                    )).scalar_one_or_none()
                    if exists is None:
                        sender = value.get("from") or {}
                        media = value.get("media") or {}
                        session.add(Comment(
                            workspace_id=event.workspace_id,
                            social_account_id=event.social_account_id,
                            media_id=str(media.get("id") or value.get("media_id") or "") or None,
                            external_comment_id=comment_id,
                            parent_comment_id=str(value.get("parent_id") or "") or None,
                            from_username=sender.get("username"),
                            from_ig_id=str(sender.get("id") or "") or None,
                            text=str(value.get("text") or ""),
                            like_count=int(value.get("like_count") or 0),
                            status="NEW",
                            posted_at=datetime.fromtimestamp(
                                int(value["timestamp"]), UTC
                            ) if value.get("timestamp") else datetime.now(UTC),
                            created_by=uuid.UUID(int=0),
                        ))
                        created = 1

            elif event.change_type in {"messages", "message", "messaging_postbacks", "message_reactions"}:
                message = messaging.get("message") if isinstance(messaging, dict) else None
                message = message or {}
                mid = str(message.get("mid") or "")
                sender = messaging.get("sender") or {}
                recipient = messaging.get("recipient") or {}
                participant_id = str(sender.get("id") or recipient.get("id") or "") or None
                if mid and event.social_account_id and participant_id:
                    conversation_ext = str(
                        messaging.get("thread_id")
                        or messaging.get("conversation_id")
                        or participant_id
                    )
                    conversation = (await session.execute(
                        select(Conversation).where(
                            Conversation.workspace_id == event.workspace_id,
                            Conversation.social_account_id == event.social_account_id,
                            Conversation.external_conversation_id == conversation_ext,
                        )
                    )).scalar_one_or_none()
                    now = datetime.now(UTC)
                    if conversation is None:
                        conversation = Conversation(
                            workspace_id=event.workspace_id,
                            social_account_id=event.social_account_id,
                            external_conversation_id=conversation_ext,
                            participant_ig_id=participant_id,
                            participant_username=sender.get("username"),
                            participant_name=sender.get("name"),
                            status="OPEN",
                            labels=[],
                            created_by=uuid.UUID(int=0),
                        )
                        session.add(conversation)
                        await session.flush()
                    exists = (await session.execute(
                        select(Message.id).where(
                            Message.workspace_id == event.workspace_id,
                            Message.conversation_id == conversation.id,
                            Message.external_message_id == mid,
                        )
                    )).scalar_one_or_none()
                    if exists is None:
                        inbound = participant_id != str(
                            (messaging.get("recipient") or {}).get("id") or ""
                        )
                        session.add(Message(
                            workspace_id=event.workspace_id,
                            conversation_id=conversation.id,
                            external_message_id=mid,
                            direction="INBOUND" if inbound else "OUTBOUND",
                            sender_kind="PARTICIPANT" if inbound else "HUMAN",
                            message_type="TEXT",
                            text=str(message.get("text") or ""),
                            attachments=(message.get("attachments") or {}).get("data", [])
                            if isinstance(message.get("attachments"), dict) else [],
                            status="SENT",
                            sent_at=datetime.fromtimestamp(
                                int(messaging["timestamp"]) / 1000, UTC
                            ) if messaging.get("timestamp") else now,
                            created_by=uuid.UUID(int=0),
                        ))
                        conversation.last_message_at = now
                        if inbound:
                            conversation.last_inbound_at = now
                            conversation.messaging_window_expires_at = now + timedelta(hours=24)
                        created = 1

            event.processing_status = "PROCESSED"
            event.processed_at = datetime.now(UTC)
            await session.flush()
            return {"status": "processed", "change_type": event.change_type, "created": created}
        except Exception as exc:  # noqa: BLE001
            event.processing_status = "FAILED"
            event.last_error = str(exc)[:1000]
            await session.flush()
            raise

    return _run(_work)


@celery_app.task(name="instamind.storage.purge_expired_media")
def purge_expired_media() -> dict:  # noqa: ANN001
    """Delete presigned-URL-expired uploads that were never published."""

    async def _work(session) -> dict:
        from app.modules.content.models import MediaAsset
        from app.modules.storage.backend import get_storage

        cutoff = datetime.now(UTC) - timedelta(days=7)
        rows = (
            await session.execute(
                select(MediaAsset).where(
                    MediaAsset.status == "UPLOADED",
                    MediaAsset.created_at < cutoff,
                    MediaAsset.public_url.isnot(None),
                )
            )
        ).scalars().all()
        storage = get_storage()
        removed = 0
        for asset in rows:
            try:
                await storage.delete(asset.storage_key)
                asset.status = "EXPIRED"
                removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("media_purge_failed", asset=str(asset.id), error=str(exc)[:200])
        await session.flush()
        return {"removed": removed}

    return _run(_work)
