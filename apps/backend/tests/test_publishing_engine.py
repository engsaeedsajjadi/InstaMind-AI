"""Publishing engine: idempotency, state machine, retries, Meta failures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.exceptions import (
    ConflictError,
    IllegalStateTransition,
    MetaAPIError,
    QuotaExceeded,
    ValidationError,
)
from app.modules.audit.models import AuditLog
from app.modules.content.models import Content, ContentApproval
from app.modules.instagram.service import InstagramConnectService
from app.modules.publishing.models import PublishingAttempt, PublishingJob
from app.modules.publishing.service import PublishingService, compute_backoff
from app.modules.tenants.scope import TenantScope
from app.modules.tenants.service import TenantService


async def no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def service_for(session):
    def _build(scope, client_factory) -> PublishingService:
        return PublishingService(
            session,
            scope,
            connect_service=InstagramConnectService(session, scope),
            client_factory=client_factory,
            sleeper=no_sleep,
        )

    return _build


@pytest.fixture
async def scenario(session, make_user, make_workspace, make_account, make_media, make_content):
    """A ready-to-publish workspace: user, account, one JPEG, one approved post."""
    user = await make_user()
    workspace = await make_workspace(user.id)
    account = await make_account(workspace.id)
    asset = await make_media(workspace.id)
    content = await make_content(workspace.id, media_ids=[asset.id], status="APPROVED")
    session.add(
        ContentApproval(
            workspace_id=workspace.id,
            content_id=content.id,
            status="APPROVED",
            reviewer_id=user.id,
            decided_at=datetime.now(UTC),
        )
    )
    await session.flush()
    ctx = await TenantService(session).build_context(workspace_id=workspace.id, user_id=user.id)
    return {
        "user": user,
        "workspace": workspace,
        "account": account,
        "asset": asset,
        "content": content,
        "scope": TenantScope(session, ctx),
    }


@pytest.mark.asyncio
async def test_enqueue_is_idempotent(session, scenario, service_for, fake_transport, meta_client_factory):
    service = service_for(scenario["scope"], meta_client_factory)
    first = await service.enqueue(
        content_id=scenario["content"].id,
        social_account_id=scenario["account"].id,
        idempotency_key="same-key",
    )
    second = await service.enqueue(
        content_id=scenario["content"].id,
        social_account_id=scenario["account"].id,
        idempotency_key="same-key",
    )
    assert first.id == second.id
    count = (
        await session.execute(
            select(PublishingJob).where(PublishingJob.workspace_id == scenario["workspace"].id)
        )
    ).scalars().all()
    assert len(count) == 1


@pytest.mark.asyncio
async def test_enqueue_for_a_disconnected_account_is_refused(
    session, scenario, service_for, meta_client_factory
):
    account = scenario["account"]
    account.status = "DISCONNECTED"
    await session.flush()
    service = service_for(scenario["scope"], meta_client_factory)
    with pytest.raises(ConflictError):
        await service.enqueue(content_id=scenario["content"].id, social_account_id=account.id)


@pytest.mark.asyncio
async def test_image_publish_end_to_end(session, scenario, service_for, fake_transport, meta_client_factory):
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(
        content_id=scenario["content"].id, social_account_id=scenario["account"].id
    )
    claimed = await service.claim_next(worker_id="worker-1")
    assert [item.id for item in claimed] == [job.id]

    fake_transport.enqueue({"id": "container-1"})          # create container
    fake_transport.enqueue({"id": "media-1", "permalink": "https://instagram.com/p/AAA/"})

    result = await service.process_job(job, worker_id="worker-1")
    assert result.state == "SUCCEEDED"
    assert result.permalink == "https://instagram.com/p/AAA/"

    content = await scenario["scope"].get_one(Content, scenario["content"].id)
    assert content.status == "PUBLISHED"
    assert content.published_at is not None

    # Two-step container flow, in order, against the official endpoints.
    urls = [call["url"] for call in fake_transport.calls]
    assert any(url.endswith("/media") for url in urls)
    assert any(url.endswith("/media_publish") for url in urls)
    assert urls.index(next(u for u in urls if u.endswith("/media"))) < urls.index(
        next(u for u in urls if u.endswith("/media_publish"))
    )

    audit = (
        await session.execute(
            select(AuditLog).where(AuditLog.action == "publishing.succeeded")
        )
    ).scalars().all()
    assert audit, "a successful publish must be audited"


@pytest.mark.asyncio
async def test_reel_publish_polls_the_container_until_finished(
    session, scenario, service_for, fake_transport, meta_client_factory, make_media
):
    reel_asset = await make_media(
        scenario["workspace"].id,
        kind="VIDEO",
        mime="video/mp4",
        public_url="https://cdn.example.com/reel.mp4",
        duration_ms=20_000,
    )
    content = scenario["content"]
    content.content_type = "REELS"
    content.media_asset_ids = [str(reel_asset.id)]
    content.media_order = [str(reel_asset.id)]
    await session.flush()

    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(content_id=content.id, social_account_id=scenario["account"].id)
    await service.claim_next(worker_id="w")
    fake_transport.enqueue({"id": "reel-container"})
    fake_transport.enqueue({"status_code": "IN_PROGRESS", "status": "processing"})
    fake_transport.enqueue({"status_code": "FINISHED", "status": "ready"})
    fake_transport.enqueue({"id": "media-2", "permalink": "https://instagram.com/reel/BBB/"})

    result = await service.process_job(job, worker_id="w")
    assert result.state == "SUCCEEDED"
    statuses = [call["url"] for call in fake_transport.calls]
    assert sum("reel-container" in url for url in statuses) == 2


@pytest.mark.asyncio
async def test_carousel_creates_children_then_a_parent(
    session, scenario, service_for, fake_transport, meta_client_factory, make_media
):
    second = await make_media(scenario["workspace"].id, public_url="https://cdn.example.com/b.jpg")
    content = scenario["content"]
    content.content_type = "CAROUSEL"
    content.media_asset_ids = [str(scenario["asset"].id), str(second.id)]
    content.media_order = [str(scenario["asset"].id), str(second.id)]
    await session.flush()

    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(content_id=content.id, social_account_id=scenario["account"].id)
    await service.claim_next(worker_id="w")
    fake_transport.enqueue({"id": "child-1"})
    fake_transport.enqueue({"id": "child-2"})
    fake_transport.enqueue({"id": "parent"})
    fake_transport.enqueue({"id": "media-3", "permalink": "https://instagram.com/p/CCC/"})

    result = await service.process_job(job, worker_id="w")
    assert result.state == "SUCCEEDED"
    bodies = [call["json"] for call in fake_transport.calls if call["json"]]
    assert sum(1 for body in bodies if body.get("is_carousel_item")) == 2
    parent = next(body for body in bodies if body.get("media_type") == "CAROUSEL")
    assert parent["children"] == "child-1,child-2"


@pytest.mark.asyncio
async def test_story_is_published_without_a_caption(
    session, scenario, service_for, fake_transport, meta_client_factory
):
    content = scenario["content"]
    content.content_type = "STORIES"
    content.caption = ""
    await session.flush()

    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(content_id=content.id, social_account_id=scenario["account"].id)
    await service.claim_next(worker_id="w")
    fake_transport.enqueue({"id": "story-container"})
    fake_transport.enqueue({"id": "media-4", "permalink": "https://instagram.com/stories/x/"})
    result = await service.process_job(job, worker_id="w")
    assert result.state == "SUCCEEDED"
    container_body = next(call["json"] for call in fake_transport.calls if call["json"])
    assert container_body["media_type"] == "STORIES"
    assert "caption" not in container_body


@pytest.mark.asyncio
async def test_unapproved_content_is_not_published(
    session, scenario, service_for, meta_client_factory, make_media, make_content
):
    asset = await make_media(scenario["workspace"].id)
    draft = await make_content(scenario["workspace"].id, media_ids=[asset.id], status="APPROVED")
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(
        content_id=draft.id, social_account_id=scenario["account"].id, requires_approval=True
    )
    await service.claim_next(worker_id="w")
    with pytest.raises(IllegalStateTransition):
        await service.process_job(job, worker_id="w")
    assert job.state == "QUEUED", "the job must stay queued until approved"


@pytest.mark.asyncio
async def test_transient_meta_error_schedules_a_retry(
    session, scenario, service_for, fake_transport, meta_client_factory
):
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(content_id=scenario["content"].id, social_account_id=scenario["account"].id)
    await service.claim_next(worker_id="w")
    fake_transport.enqueue_error("(#1) An unknown error occurred", code=1, status_code=500)

    with pytest.raises(MetaAPIError):
        await service.process_job(job, worker_id="w")
    assert job.state == "QUEUED"
    assert job.attempts == 1
    assert job.next_retry_at is not None and job.next_retry_at > datetime.now(UTC)
    attempt = (
        await session.execute(select(PublishingAttempt).where(PublishingAttempt.job_id == job.id))
    ).scalar_one()
    assert attempt.outcome == "RETRYABLE_ERROR"


@pytest.mark.asyncio
async def test_rate_limit_error_is_classified(
    session, scenario, service_for, fake_transport, meta_client_factory
):
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(content_id=scenario["content"].id, social_account_id=scenario["account"].id)
    await service.claim_next(worker_id="w")
    fake_transport.enqueue_error("(#4) Application request limit reached", code=4, status_code=400)
    with pytest.raises(MetaAPIError):
        await service.process_job(job, worker_id="w")
    attempt = (
        await session.execute(select(PublishingAttempt).where(PublishingAttempt.job_id == job.id))
    ).scalar_one()
    assert attempt.outcome == "RATE_LIMITED"
    assert attempt.meta_error_code == 4


@pytest.mark.asyncio
async def test_permanent_error_marks_the_content_failed(
    session, scenario, service_for, fake_transport, meta_client_factory
):
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(content_id=scenario["content"].id, social_account_id=scenario["account"].id)
    await service.claim_next(worker_id="w")
    fake_transport.enqueue_error(
        "Only photo and video files are supported", code=36003, status_code=400
    )
    with pytest.raises(MetaAPIError):
        await service.process_job(job, worker_id="w")
    assert job.state == "FAILED"
    assert job.is_retryable is False
    content = await scenario["scope"].get_one(Content, scenario["content"].id)
    assert content.status == "FAILED"
    assert content.failure_reason


@pytest.mark.asyncio
async def test_exhausted_attempts_become_dead(
    session, scenario, service_for, fake_transport, meta_client_factory
):
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(content_id=scenario["content"].id, social_account_id=scenario["account"].id)
    job.max_attempts = 2
    await session.flush()
    await service.claim_next(worker_id="w")
    fake_transport.enqueue_error("transient", code=1, status_code=500)
    with pytest.raises(MetaAPIError):
        await service.process_job(job, worker_id="w")
    assert job.state == "QUEUED" and job.attempts == 1
    await service.claim_next(worker_id="w")
    fake_transport.enqueue_error("transient again", code=1, status_code=500)
    with pytest.raises(MetaAPIError):
        await service.process_job(job, worker_id="w")
    assert job.state == "DEAD"
    assert job.is_retryable is False


@pytest.mark.asyncio
async def test_cancel_is_only_possible_before_execution(
    session, scenario, service_for, meta_client_factory
):
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(
        content_id=scenario["content"].id,
        social_account_id=scenario["account"].id,
        scheduled_for=datetime.now(UTC) + timedelta(hours=1),
    )
    cancelled = await service.cancel(job.id)
    assert cancelled.state == "CANCELLED"
    with pytest.raises(IllegalStateTransition):
        await service.cancel(job.id)


@pytest.mark.asyncio
async def test_claim_next_returns_nothing_when_nothing_is_due(
    session, scenario, service_for, meta_client_factory
):
    service = service_for(scenario["scope"], meta_client_factory)
    await service.enqueue(
        content_id=scenario["content"].id,
        social_account_id=scenario["account"].id,
        scheduled_for=datetime.now(UTC) + timedelta(days=1),
    )
    assert await service.claim_next(worker_id="w") == []


@pytest.mark.asyncio
async def test_claiming_marks_the_job_running_and_leases_it(
    session, scenario, service_for, meta_client_factory
):
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(
        content_id=scenario["content"].id, social_account_id=scenario["account"].id
    )
    await service.claim_next(worker_id="w-1")
    assert job.state == "RUNNING"
    assert job.lease_owner == "w-1"
    # A second worker cannot claim the same job.
    assert await service.claim_next(worker_id="w-2") == []


@pytest.mark.asyncio
async def test_validation_failure_before_publish(
    session, scenario, service_for, fake_transport, meta_client_factory
):
    """A caption that is too long must never reach Meta."""
    content = scenario["content"]
    content.caption = "a" * 2300
    await session.flush()
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(content_id=content.id, social_account_id=scenario["account"].id)
    await service.claim_next(worker_id="w")
    with pytest.raises(ValidationError):
        await service.process_job(job, worker_id="w")
    assert fake_transport.calls == [], "no Meta call may be made for invalid content"


@pytest.mark.asyncio
async def test_missing_public_url_blocks_publishing(session, scenario, service_for, meta_client_factory):
    asset = scenario["asset"]
    asset.public_url = None
    await session.flush()
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(content_id=scenario["content"].id, social_account_id=scenario["account"].id)
    await service.claim_next(worker_id="w")
    with pytest.raises(ValidationError) as exc:
        await service.process_job(job, worker_id="w")
    assert "publicly reachable" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_daily_quota_guard(session, scenario, service_for, meta_client_factory):
    from app.modules.publishing.models import PublishingQuota

    scenario["scope"].session.add(
        PublishingQuota(
            workspace_id=scenario["workspace"].id,
            social_account_id=scenario["account"].id,
            window_start=datetime.now(UTC).replace(minute=0, second=0, microsecond=0),
            used=100,
            limit=100,
        )
    )
    await session.flush()
    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(
        content_id=scenario["content"].id, social_account_id=scenario["account"].id
    )
    await service.claim_next(worker_id="w")
    with pytest.raises(QuotaExceeded):
        await service.process_job(job, worker_id="w")


@pytest.mark.asyncio
async def test_quota_increments_after_a_successful_publish(
    session, scenario, service_for, fake_transport, meta_client_factory
):
    from app.modules.publishing.models import PublishingQuota

    service = service_for(scenario["scope"], meta_client_factory)
    job = await service.enqueue(content_id=scenario["content"].id, social_account_id=scenario["account"].id)
    await service.claim_next(worker_id="w")
    fake_transport.enqueue({"id": "c"})
    fake_transport.enqueue({"id": "m", "permalink": "https://instagram.com/p/D/"})
    await service.process_job(job, worker_id="w")

    rows = (
        await session.execute(
            select(PublishingQuota).where(
                PublishingQuota.social_account_id == scenario["account"].id
            )
        )
    ).scalars().all()
    assert rows and rows[0].used == 1


class TestBackoffPolicy:
    def test_backoff_grows_exponentially(self):
        delays = [compute_backoff(n, base=2.0, cap=900.0) for n in range(1, 6)]
        assert delays == sorted(delays)
        assert delays[0] == 2.0
        assert delays[3] == 16.0

    def test_backoff_is_capped(self):
        assert compute_backoff(30, base=2.0, cap=900.0) == 900.0
