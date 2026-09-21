"""Publishing engine.

State machine
-------------
``QUEUED → RUNNING → SUCCEEDED``
``QUEUED → RUNNING → QUEUED`` (retryable error, backoff applied)
``QUEUED → RUNNING → FAILED``  (permanent error)
``QUEUED → RUNNING → DEAD``    (attempts exhausted)
``QUEUED → CANCELLED``         (cancelled before execution — always possible
                                while the job is still QUEUED)

Guarantees
----------
* **Idempotency**: ``(workspace_id, idempotency_key)`` is unique. Re-enqueueing
  the same key returns the existing job instead of publishing twice.
* **Single execution**: a worker claims a job by atomically writing a lease
  (``lease_owner`` + ``lease_expires_at``). A second worker cannot claim it.
* **Meta's own limits are respected**: our rolling-24h counter is checked
  before publishing, and ``content_publishing_limit`` is consulted for
  authoritative quota when available.
* **Every API exchange is recorded** in ``publishing_attempts``, including the
  raw Meta error, so a failure can be diagnosed after the fact.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    ConflictError,
    IllegalStateTransition,
    MetaAPIError,
    QuotaExceeded,
    ValidationError,
)
from app.core.logging import logger
from app.modules.audit.service import AuditService
from app.modules.content.models import Content, ContentApproval, MediaAsset
from app.modules.instagram.media_rules import MediaType, raise_for_issues, validate_publish_request
from app.modules.instagram.models import SocialAccount
from app.modules.instagram.service import InstagramConnectService
from app.modules.publishing.models import PublishingAttempt, PublishingJob, PublishingQuota
from app.modules.tenants.scope import TenantScope

TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD"}
LEASE_SECONDS = 300
CONTAINER_POLL_INTERVAL = settings.REELS_POLL_INTERVAL_SECONDS
CONTAINER_POLL_TIMEOUT = settings.REELS_POLL_MAX_WAIT_SECONDS


@dataclass(frozen=True)
class PublishResult:
    job: PublishingJob
    media_id: str | None
    permalink: str | None
    state: str


class PublishingService:
    def __init__(
        self,
        session: AsyncSession,
        scope: TenantScope,
        *,
        connect_service: InstagramConnectService,
        client_factory,
        audit: AuditService | None = None,
        sleeper=asyncio.sleep,
    ) -> None:
        self.session = session
        self.scope = scope
        self.connect = connect_service
        self._make_client = client_factory
        self.audit = audit or AuditService(session)
        # Injectable so tests do not actually sleep during polling.
        self._sleep = sleeper

    # ------------------------------------------------------------------ queue
    async def enqueue(
        self,
        *,
        content_id: uuid.UUID,
        social_account_id: uuid.UUID,
        idempotency_key: str | None = None,
        trigger: str = "IMMEDIATE",
        scheduled_for: datetime | None = None,
        requires_approval: bool = True,
    ) -> PublishingJob:
        content = await self.scope.get_one(Content, content_id)
        account = await self.scope.get_one(SocialAccount, social_account_id)
        if not account.is_usable:
            raise ConflictError(
                f"Account “{account.username}” is not connected ({account.status}).",
                context={"account_status": account.status},
            )

        key = idempotency_key or f"{content.id}:{account.id}"
        existing = (
            await self.session.execute(
                select(PublishingJob).where(
                    PublishingJob.workspace_id == self.scope.workspace_id,
                    PublishingJob.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Idempotent: same key => same job, never a second publish.
            return existing

        job = PublishingJob(
            workspace_id=self.scope.workspace_id,
            content_id=content.id,
            social_account_id=account.id,
            idempotency_key=key,
            trigger=trigger,
            state="QUEUED",
            scheduled_for=scheduled_for or datetime.now(UTC),
            max_attempts=settings.PUBLISH_RETRY_MAX_ATTEMPTS,
            requires_approval=requires_approval,
            created_by=self.scope.context.user_id,
        )
        self.session.add(job)
        await self.session.flush()
        await self.audit.record(
            action="publishing.queued",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="publishing_job",
            resource_id=str(job.id),
            metadata={"trigger": trigger, "account": account.username},
        )
        return job

    async def cancel(self, job_id: uuid.UUID) -> PublishingJob:
        job = await self.scope.get_one(PublishingJob, job_id)
        if job.state != "QUEUED":
            raise IllegalStateTransition(
                f"Only queued jobs can be cancelled (current state: {job.state})."
            )
        job.state = "CANCELLED"
        job.finished_at = datetime.now(UTC)
        await self.audit.record(
            action="publishing.cancelled",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="publishing_job",
            resource_id=str(job.id),
        )
        await self.session.flush()
        return job

    async def claim_next(self, *, worker_id: str, limit: int = 1) -> list[PublishingJob]:
        """Atomically lease due jobs. The ``WHERE state='QUEUED'`` + ``UPDATE``
        pair makes double-claiming impossible even with concurrent workers."""
        now = datetime.now(UTC)
        stmt = (
            select(PublishingJob.id)
            .where(
                PublishingJob.workspace_id == self.scope.workspace_id,
                PublishingJob.state == "QUEUED",
                PublishingJob.scheduled_for <= now,
                PublishingJob.deleted_at.is_(None),
            )
            .order_by(PublishingJob.scheduled_for.asc())
            .limit(limit)
        )
        job_ids = [row[0] for row in (await self.session.execute(stmt)).all()]
        if not job_ids:
            return []

        result = await self.session.execute(
            update(PublishingJob)
            .where(
                PublishingJob.id.in_(job_ids),
                PublishingJob.state == "QUEUED",
            )
            .values(
                state="RUNNING",
                started_at=now,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
            )
            .returning(PublishingJob.id)
        )
        claimed = [row[0] for row in result.all()]
        await self.session.flush()
        if not claimed:
            return []
        jobs = (
            await self.session.execute(
                select(PublishingJob).where(PublishingJob.id.in_(claimed))
            )
        ).scalars().all()
        return list(jobs)

    # ---------------------------------------------------------------- execute
    async def process_job(self, job: PublishingJob, *, worker_id: str = "worker") -> PublishResult:
        content = await self.scope.get_one(Content, job.content_id)
        account = await self.scope.get_one(SocialAccount, job.social_account_id)

        if job.requires_approval and not await self._is_approved(content.id):
            await self._transition(job, "QUEUED", error=("approval_required", "Content is awaiting approval."))
            raise IllegalStateTransition("Content must be approved before publishing.")

        await self._check_daily_quota(account)
        issues = validate_publish_request(
            media_type=MediaType(content.content_type),
            caption=content.caption,
            assets=await self._asset_payloads(content),
            account={
                "capabilities": account.capabilities,
                "account_type": account.account_type,
            },
            collaborators=content.collaborators,
            user_tags=content.user_tags,
            alt_text=content.alt_text,
            location_id=content.location_id,
            share_to_feed=content.share_to_feed if content.content_type == "REELS" else None,
        )
        if issues:
            raise_for_issues(issues)

        await self.connect.refresh_if_needed(account)
        access_token = await self.connect.get_active_token(account)
        client = self._make_client(access_token=access_token)

        started = datetime.now(UTC)
        attempt_number = job.attempts + 1
        try:
            media_id, permalink = await self._publish(
                client=client, account=account, content=content, job=job, attempt_number=attempt_number
            )
        except MetaAPIError as exc:
            await self._record_attempt(
                job=job,
                attempt_number=attempt_number,
                step="PUBLISH",
                exc=exc,
                started=started,
            )
            if exc.is_transient:
                await self._schedule_retry(job, exc)
            else:
                await self._fail(job, exc)
            raise
        except ValidationError as exc:
            job.state = "FAILED"
            job.finished_at = datetime.now(UTC)
            job.last_error_code = "validation_error"
            job.last_error_message = exc.detail
            job.is_retryable = False
            content.status = "FAILED"
            content.failure_reason = exc.detail
            await self.session.flush()
            raise

        job.state = "SUCCEEDED"
        job.finished_at = datetime.now(UTC)
        job.attempts = attempt_number
        job.result = {"media_id": media_id, "permalink": permalink}
        job.lease_owner = None
        job.lease_expires_at = None
        content.status = "PUBLISHED"
        content.published_at = datetime.now(UTC)
        await self._increment_quota(account)
        await self.audit.record(
            action="publishing.succeeded",
            workspace_id=self.scope.workspace_id,
            actor_type="SYSTEM",
            resource_type="publishing_job",
            resource_id=str(job.id),
            after={"media_id": media_id, "permalink": permalink},
        )
        await self.session.flush()
        return PublishResult(job=job, media_id=media_id, permalink=permalink, state=job.state)

    # ---------------------------------------------------------------- publish
    async def _publish(self, *, client, account: SocialAccount, content: Content, job: PublishingJob, attempt_number: int) -> tuple[str, str]:
        media_type = MediaType(content.content_type)
        assets = await self._asset_payloads(content)
        base_params: dict[str, object] = {}
        if content.location_id:
            base_params["location_id"] = content.location_id
        if content.user_tags:
            base_params["user_tags"] = content.user_tags
        if content.collaborators and media_type is not MediaType.STORIES:
            base_params["collaborators"] = content.collaborators

        if media_type is MediaType.IMAGE:
            return await self._publish_image(client, account, content, assets[0], base_params)
        if media_type is MediaType.REELS:
            return await self._publish_reel(
                client, account, content, assets[0], base_params, job=job, attempt_number=attempt_number
            )
        if media_type is MediaType.STORIES:
            return await self._publish_story(client, account, content, assets[0], base_params)
        if media_type is MediaType.CAROUSEL:
            return await self._publish_carousel(
                client, account, content, assets, base_params, job=job, attempt_number=attempt_number
            )
        raise ValidationError(f"Unsupported content type: {content.content_type}")

    async def _publish_image(self, client, account: SocialAccount, content: Content, asset: dict, base_params: dict) -> tuple[str, str]:
        params = {
            "image_url": asset["public_url"],
            **base_params,
        }
        if content.caption:
            params["caption"] = content.caption
        if content.alt_text:
            params["alt_text"] = content.alt_text
        container = await client.create_media_container(account.external_account_id, params=params)
        return await self._finalize(client, account, container)

    async def _publish_reel(self, client, account: SocialAccount, content: Content, asset: dict, base_params: dict, *, job: PublishingJob, attempt_number: int) -> tuple[str, str]:
        params: dict[str, object] = {
            "media_type": "REELS",
            "video_url": asset["public_url"],
            **base_params,
        }
        if content.caption:
            params["caption"] = content.caption
        if content.thumb_offset_ms is not None:
            params["thumb_offset"] = content.thumb_offset_ms
        params["share_to_feed"] = bool(content.share_to_feed)
        if content.cover_asset_id:
            cover = await self._asset_public_url(content.cover_asset_id)
            if cover:
                params["cover_url"] = cover

        container = await client.create_media_container(account.external_account_id, params=params)
        container_id = container["id"]
        await self._poll_container(client, container_id, job=job, attempt_number=attempt_number)
        return await self._finalize(client, account, container)

    async def _publish_story(self, client, account: SocialAccount, content: Content, asset: dict, base_params: dict) -> tuple[str, str]:
        params: dict[str, object] = {"media_type": "STORIES", **base_params}
        # Stories take no caption/alt text — Meta silently ignores them.
        if asset["kind"] == "VIDEO":
            params["video_url"] = asset["public_url"]
        else:
            params["image_url"] = asset["public_url"]
        container = await client.create_media_container(account.external_account_id, params=params)
        return await self._finalize(client, account, container)

    async def _publish_carousel(self, client, account: SocialAccount, content: Content, assets: list[dict], base_params: dict, *, job: PublishingJob, attempt_number: int) -> tuple[str, str]:
        children: list[str] = []
        for asset in assets:
            child_params: dict[str, object] = {"is_carousel_item": True}
            if asset["kind"] == "VIDEO":
                child_params.update({"media_type": "REELS", "video_url": asset["public_url"]})
                child = await client.create_media_container(account.external_account_id, params=child_params)
                await self._poll_container(client, child["id"], job=job, attempt_number=attempt_number)
            else:
                child_params["image_url"] = asset["public_url"]
                if content.alt_text:
                    child_params["alt_text"] = content.alt_text
                child = await client.create_media_container(account.external_account_id, params=child_params)
            children.append(child["id"])

        parent_params: dict[str, object] = {
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            **base_params,
        }
        if content.caption:
            parent_params["caption"] = content.caption
        container = await client.create_media_container(account.external_account_id, params=parent_params)
        return await self._finalize(client, account, container)

    async def _finalize(self, client, account: SocialAccount, container: dict) -> tuple[str, str]:
        creation_id = container["id"]
        published = await client.publish_container(account.external_account_id, creation_id=creation_id)
        media_id = str(published.get("id") or creation_id)
        permalink = str(published.get("permalink") or "")
        if not permalink:
            try:
                detail = await client.get_media(media_id, fields="id,permalink,media_type,timestamp")
                permalink = str(detail.get("permalink") or "")
            except MetaAPIError:  # pragma: no cover - permalink is best-effort
                logger.warning("permalink_lookup_failed", media_id=media_id)
        return media_id, permalink

    async def _poll_container(self, client, container_id: str, *, job: PublishingJob, attempt_number: int) -> None:
        """Reels/carousel-video containers must reach ``FINISHED`` before the
        parent is created or published."""
        deadline = datetime.now(UTC) + timedelta(seconds=CONTAINER_POLL_TIMEOUT)
        while True:
            status = await client.get_container_status(container_id)
            code = str(status.get("status_code") or "").upper()
            if code == "FINISHED":
                return
            if code in {"ERROR", "EXPIRED", "FAILED"}:
                raise MetaAPIError(
                    status.get("status") or f"Media container {code.lower()}.",
                    meta_code=9007,
                    error_type="MediaContainerError",
                    is_transient=False,
                )
            if datetime.now(UTC) >= deadline:
                raise MetaAPIError(
                    "Timed out waiting for Instagram to finish processing the video.",
                    meta_code=9008,
                    error_type="MediaProcessingTimeout",
                    is_transient=True,
                )
            await self._sleep(CONTAINER_POLL_INTERVAL)

    # ---------------------------------------------------------------- helpers
    async def _asset_payloads(self, content: Content) -> list[dict]:
        ids = content.media_order or content.media_asset_ids
        if not ids:
            raise ValidationError("This content has no media attached.", context={"code": "media_required"})
        assets = await self.scope.list(MediaAsset, MediaAsset.id.in_([uuid.UUID(str(i)) for i in ids]))
        by_id = {str(asset.id): asset for asset in assets}
        ordered = [by_id[str(i)] for i in ids if str(i) in by_id]
        if len(ordered) != len(ids):
            raise ValidationError("One or more media files are missing.", context={"code": "media_missing"})
        payloads: list[dict] = []
        for asset in ordered:
            payloads.append(
                {
                    "id": str(asset.id),
                    "kind": asset.kind,
                    "mime": asset.detected_mime or asset.content_type,
                    "size_bytes": asset.size_bytes,
                    "width": asset.width,
                    "height": asset.height,
                    "duration_ms": asset.duration_ms,
                    "public_url": asset.public_url,
                }
            )
        if any(not payload["public_url"] for payload in payloads):
            raise ValidationError(
                "Media is not publicly reachable. Instagram must be able to download it.",
                context={"code": "media_not_public"},
            )
        return payloads

    async def _asset_public_url(self, asset_id: uuid.UUID) -> str | None:
        asset = await self.scope.find(MediaAsset, asset_id)
        return asset.public_url if asset else None

    async def _is_approved(self, content_id: uuid.UUID) -> bool:
        latest = (
            await self.session.execute(
                select(ContentApproval)
                .where(
                    ContentApproval.workspace_id == self.scope.workspace_id,
                    ContentApproval.content_id == content_id,
                )
                .order_by(ContentApproval.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return latest is not None and latest.status == "APPROVED"

    # ------------------------------------------------------------------ quota
    async def _check_daily_quota(self, account: SocialAccount) -> None:
        window_start = datetime.now(UTC) - timedelta(hours=24)
        total = (
            await self.session.execute(
                select(func.coalesce(func.sum(PublishingQuota.used), 0)).where(
                    PublishingQuota.workspace_id == self.scope.workspace_id,
                    PublishingQuota.social_account_id == account.id,
                    PublishingQuota.window_start >= window_start,
                )
            )
        ).scalar_one()
        guard = min(settings.PUBLISH_DAILY_GUARD_LIMIT, 100)
        if int(total) >= guard:
            raise QuotaExceeded(
                f"Daily publishing limit reached for @{account.username} ({guard} posts / 24h).",
                context={"limit": guard, "used": int(total), "account": account.username},
            )

    async def _increment_quota(self, account: SocialAccount) -> None:
        window_start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        existing = (
            await self.session.execute(
                select(PublishingQuota).where(
                    PublishingQuota.workspace_id == self.scope.workspace_id,
                    PublishingQuota.social_account_id == account.id,
                    PublishingQuota.window_start == window_start,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            self.session.add(
                PublishingQuota(
                    workspace_id=self.scope.workspace_id,
                    social_account_id=account.id,
                    window_start=window_start,
                    used=1,
                    limit=settings.PUBLISH_DAILY_GUARD_LIMIT,
                )
            )
        else:
            existing.used += 1
        await self.session.flush()

    # ------------------------------------------------------------- transitions
    async def _transition(
        self, job: PublishingJob, state: str, *, error: tuple[str, str] | None = None
    ) -> None:
        job.state = state
        if error:
            job.last_error_code, job.last_error_message = error
        await self.session.flush()

    def _backoff_seconds(self, attempt: int) -> float:
        base = settings.PUBLISH_BACKOFF_BASE_SECONDS
        delay = base * (2 ** max(attempt - 1, 0))
        delay = min(delay, settings.PUBLISH_BACKOFF_MAX_SECONDS)
        # ±20 % jitter so a burst of failures does not retry in lockstep.
        return delay * (0.8 + random.random() * 0.4)

    async def _schedule_retry(self, job: PublishingJob, exc: MetaAPIError) -> None:
        job.attempts += 1
        if job.attempts >= job.max_attempts:
            await self._mark_dead(job, exc)
            return
        retry_after = self._backoff_seconds(job.attempts)
        job.state = "QUEUED"
        job.next_retry_at = datetime.now(UTC) + timedelta(seconds=retry_after)
        job.scheduled_for = job.next_retry_at
        job.is_retryable = True
        job.last_error_code = f"meta_{exc.meta_code}"
        job.last_error_message = exc.detail[:1000]
        job.lease_owner = None
        job.lease_expires_at = None
        await self.audit.record(
            action="publishing.retry_scheduled",
            workspace_id=self.scope.workspace_id,
            actor_type="SYSTEM",
            resource_type="publishing_job",
            resource_id=str(job.id),
            metadata={"attempt": job.attempts, "retry_after_seconds": round(retry_after, 1)},
        )
        await self.session.flush()

    async def _mark_dead(self, job: PublishingJob, exc: MetaAPIError) -> None:
        job.state = "DEAD"
        job.finished_at = datetime.now(UTC)
        job.is_retryable = False
        job.last_error_code = f"meta_{exc.meta_code}"
        job.last_error_message = exc.detail[:1000]
        job.lease_owner = None
        content = await self.scope.find(Content, job.content_id)
        if content is not None:
            content.status = "FAILED"
            content.failure_reason = exc.detail[:1000]
        await self.audit.record(
            action="publishing.dead",
            workspace_id=self.scope.workspace_id,
            actor_type="SYSTEM",
            resource_type="publishing_job",
            resource_id=str(job.id),
            outcome="FAILED",
            metadata={"meta_code": exc.meta_code},
        )
        await self.session.flush()

    async def _fail(self, job: PublishingJob, exc: MetaAPIError) -> None:
        job.state = "FAILED"
        job.finished_at = datetime.now(UTC)
        job.is_retryable = False
        job.last_error_code = f"meta_{exc.meta_code}"
        job.last_error_message = exc.detail[:1000]
        job.lease_owner = None
        content = await self.scope.find(Content, job.content_id)
        if content is not None:
            content.status = "FAILED"
            content.failure_reason = exc.detail[:1000]
        await self.audit.record(
            action="publishing.failed",
            workspace_id=self.scope.workspace_id,
            actor_type="SYSTEM",
            resource_type="publishing_job",
            resource_id=str(job.id),
            outcome="FAILED",
            metadata={"meta_code": exc.meta_code, "subcode": exc.meta_subcode},
        )
        await self.session.flush()

    async def _record_attempt(
        self,
        *,
        job: PublishingJob,
        attempt_number: int,
        step: str,
        exc: MetaAPIError,
        started: datetime,
    ) -> None:
        retryable = exc.is_transient
        outcome = "RATE_LIMITED" if exc.meta_code in {4, 17, 32, 613, 80004} else (
            "RETRYABLE_ERROR" if retryable else "PERMANENT_ERROR"
        )
        self.session.add(
            PublishingAttempt(
                workspace_id=self.scope.workspace_id,
                job_id=job.id,
                attempt_number=attempt_number,
                step=step,
                response_status=exc.http_status,
                response_body={"code": exc.meta_code, "subcode": exc.meta_subcode, "type": exc.error_type},
                meta_error_code=exc.meta_code,
                meta_error_subcode=exc.meta_subcode,
                meta_error_type=exc.error_type,
                meta_error_message=exc.detail[:2000],
                outcome=outcome,
                started_at=started,
                finished_at=datetime.now(UTC),
                latency_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            )
        )
        await self.session.flush()

    # ------------------------------------------------------------------ reads
    async def list_jobs(
        self,
        *,
        content_id: uuid.UUID | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[PublishingJob]:
        conditions = []
        if content_id is not None:
            conditions.append(PublishingJob.content_id == content_id)
        if state:
            conditions.append(PublishingJob.state == state)
        return await self.scope.list(
            PublishingJob,
            *conditions,
            limit=limit,
            order_by=PublishingJob.created_at.desc(),
        )

    async def get_job(self, job_id: uuid.UUID) -> PublishingJob:
        return await self.scope.get_one(PublishingJob, job_id)

    async def list_attempts(self, job_id: uuid.UUID) -> list[PublishingAttempt]:
        return await self.scope.list(
            PublishingAttempt,
            PublishingAttempt.job_id == job_id,
            order_by=PublishingAttempt.attempt_number.asc(),
        )


def compute_backoff(attempt: int, *, base: float | None = None, cap: float | None = None) -> float:
    """Pure function version of the backoff policy (used by tests and Celery)."""
    base = base if base is not None else settings.PUBLISH_BACKOFF_BASE_SECONDS
    cap = cap if cap is not None else settings.PUBLISH_BACKOFF_MAX_SECONDS
    return min(base * (2 ** max(attempt - 1, 0)), cap)


__all__ = ["PublishingService", "PublishResult", "TERMINAL_STATES", "compute_backoff"]
