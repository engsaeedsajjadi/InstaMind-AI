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
            # Domain handlers (inbox upsert, comment upsert) are added in the
            # Inbox/Comments milestone. Marking as processed here keeps the
            # contract: an event is handled exactly once.
            event.processing_status = "PROCESSED"
            event.processed_at = datetime.now(UTC)
            await session.flush()
            return {"status": "processed", "change_type": event.change_type}
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
