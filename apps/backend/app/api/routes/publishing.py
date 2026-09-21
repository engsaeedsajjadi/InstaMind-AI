"""Publishing endpoints (queue + status).

Execution itself runs in the Celery worker (``app/worker/tasks.py``); these
routes only enqueue, cancel and report. That split is what makes retries
survive a restart and keeps the API latency independent of Meta's response time.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from app.api.deps import DBSession, TenantScopeDep, require
from app.api.schemas import PublishingAttemptRead, PublishingJobRead, PublishRequest
from app.core.permissions import Permission
from app.modules.instagram.service import InstagramConnectService
from app.modules.publishing.models import PublishingJob
from app.modules.publishing.service import PublishingService
from app.providers.meta.client import MetaGraphClient

router = APIRouter(prefix="/publishing", tags=["publishing"])


def _service(session, scope) -> PublishingService:
    return PublishingService(
        session,
        scope,
        connect_service=InstagramConnectService(session, scope),
        client_factory=lambda access_token: MetaGraphClient(access_token=access_token),
    )


@router.post(
    "/jobs",
    response_model=PublishingJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require(Permission.CONTENT_PUBLISH))],
)
async def enqueue_job(
    payload: PublishRequest, session: DBSession, scope: TenantScopeDep
) -> PublishingJobRead:
    service = _service(session, scope)
    job = await service.enqueue(
        content_id=payload.content_id,
        social_account_id=payload.social_account_id,
        idempotency_key=payload.idempotency_key,
        trigger=payload.trigger,
        scheduled_for=payload.scheduled_at,
        requires_approval=payload.requires_approval,
    )
    return PublishingJobRead.model_validate(job)


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=PublishingJobRead,
    dependencies=[Depends(require(Permission.CONTENT_CANCEL))],
)
async def cancel_job(job_id: uuid.UUID, session: DBSession, scope: TenantScopeDep) -> PublishingJobRead:
    job = await _service(session, scope).cancel(job_id)
    return PublishingJobRead.model_validate(job)


@router.get(
    "/jobs",
    response_model=list[PublishingJobRead],
    dependencies=[Depends(require(Permission.CONTENT_READ))],
)
async def list_jobs(
    session: DBSession,
    scope: TenantScopeDep,
    content_id: uuid.UUID | None = None,
    state: str | None = None,
) -> list[PublishingJobRead]:
    jobs = await _service(session, scope).list_jobs(content_id=content_id, state=state)
    return [PublishingJobRead.model_validate(job) for job in jobs]


@router.get(
    "/jobs/{job_id}",
    response_model=PublishingJobRead,
    dependencies=[Depends(require(Permission.CONTENT_READ))],
)
async def get_job(job_id: uuid.UUID, scope: TenantScopeDep) -> PublishingJobRead:
    job = await scope.get_one(PublishingJob, job_id)
    return PublishingJobRead.model_validate(job)


@router.get(
    "/jobs/{job_id}/attempts",
    response_model=list[PublishingAttemptRead],
    dependencies=[Depends(require(Permission.CONTENT_READ))],
)
async def list_attempts(
    job_id: uuid.UUID, session: DBSession, scope: TenantScopeDep
) -> list[PublishingAttemptRead]:
    await scope.get_one(PublishingJob, job_id)
    attempts = await _service(session, scope).list_attempts(job_id)
    return [PublishingAttemptRead.model_validate(attempt) for attempt in attempts]
