"""Agentic content pipeline: state machine, quality gate, orchestrator."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.exceptions import IllegalStateTransition, NotFoundError
from app.modules.agents.orchestrator import PipelineOrchestrator
from app.modules.agents.quality import (
    check_caption,
    check_cover_title,
    check_hook,
    check_motion,
    check_overlay_text,
    run_quality_gate,
)
from app.modules.agents.render import (
    render_cover,
    render_overlay,
    render_scene_frame,
    slideshow_reel,
)
from app.modules.agents.state import (
    PipelineStage,
    assert_transition,
    is_terminal,
    run_status_for,
)
from app.modules.audit.models import AuditLog
from app.modules.content.models import AITask, Content
from app.modules.publishing.models import PublishingJob
from app.modules.storage.backend import LocalStorage
from app.modules.tenants.scope import TenantScope
from app.modules.tenants.service import TenantService
from app.providers.ai.base import AICompletion, AIUsage

# --------------------------------------------------------------------------- #
# Fake AI provider that answers each agent with the right JSON shape.
# --------------------------------------------------------------------------- #

PLAN_JSON = {
    "items": [
        {
            "day_index": 1,
            "content_type": "REELS",
            "idea": "معرفی قهوه اتیوپی تازه‌برشت",
            "caption_draft": "قهوه اتیوپی تازه رسید",
            "hashtags": ["قهوه"],
        }
    ]
}

REEL_SCRIPT_JSON = {
    "hook": "قهوه‌ات تموم شده؟",
    "scenes": [
        {"text": "قهوه‌ات تموم شده؟", "start_s": 0, "end_s": 3},
        {"text": "اتیوپی، تازه‌برشت", "start_s": 3, "end_s": 6},
        {"text": "سفارش بده", "start_s": 6, "end_s": 9},
    ],
    "cover_title": "قهوه اتیوپی تازه",
    "caption": "قهوه اتیوپی تازه‌برشت رسید. سفارش بدهید.",
    "hashtags": ["قهوه"],
}

CAPTION_JSON = {"caption": "قهوه اتیوپی تازه رسید", "hashtags": ["قهوه"], "cta": "سفارش بدهید"}


class RoutingFakeProvider:
    """Routes by system prompt so each agent gets a usable JSON answer."""

    name = "fake-routing"

    def __init__(self, *, cost_cents: int = 3, fail: bool = False) -> None:
        self.cost_cents = cost_cents
        self.fail = fail

    async def complete(self, messages, **kwargs):
        if self.fail:
            raise RuntimeError("provider unavailable")
        system = next((m.content for m in messages if m.role == "system"), "")
        if "content calendar" in system:
            text = json.dumps(PLAN_JSON)
        elif "reel script" in system:
            text = json.dumps(REEL_SCRIPT_JSON)
        else:
            text = json.dumps(CAPTION_JSON)
        return AICompletion(
            text=text,
            model="fake",
            usage=AIUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_cents=self.cost_cents),
        )


@pytest.fixture
async def workspace_scope(session, make_user, make_workspace):
    user = await make_user()
    workspace = await make_workspace(user.id)
    ctx = await TenantService(session).build_context(workspace_id=workspace.id, user_id=user.id)
    return user, workspace, TenantScope(session, ctx)


@pytest.fixture
def orchestrator_for(session, tmp_path):
    def _build(scope, provider, *, meta_client_factory=None):
        return PipelineOrchestrator(
            session,
            scope,
            ai_provider=provider,
            meta_client_factory=meta_client_factory,
            storage=LocalStorage(root=str(tmp_path / "storage"), base_url="http://localhost:8000/media"),
            workdir=tmp_path / "runs",
        )

    return _build


async def _run_to_approval(session, orchestrator, *, account_id=None):
    run = await orchestrator.start_run(topic="قهوه اتیوپی", content_type="REELS", account_id=account_id)
    await session.flush()
    run = await orchestrator.process_run(run.id)
    await session.flush()
    return run


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #


def test_state_machine_legal_path():
    assert run_status_for(PipelineStage.QUEUED) == "PENDING"
    assert run_status_for(PipelineStage.AWAITING_APPROVAL) == "AWAITING_APPROVAL"
    assert run_status_for(PipelineStage.PUBLISHED) == "SUCCEEDED"
    assert is_terminal(PipelineStage.PUBLISHED)
    assert is_terminal(PipelineStage.DEAD)
    assert not is_terminal(PipelineStage.DESIGNING)
    assert_transition(PipelineStage.QUEUED, PipelineStage.PLANNING)
    assert_transition(PipelineStage.QUALITY_GATE, PipelineStage.DESIGNING)  # rework loop
    assert_transition(PipelineStage.AWAITING_APPROVAL, PipelineStage.PUBLISHING)


def test_state_machine_illegal_transitions_raise():
    with pytest.raises(IllegalStateTransition):
        assert_transition(PipelineStage.QUEUED, PipelineStage.PUBLISHING)  # skips the human gate
    with pytest.raises(IllegalStateTransition):
        assert_transition(PipelineStage.DESIGNING, PipelineStage.AWAITING_APPROVAL)  # skips the gate
    with pytest.raises(IllegalStateTransition):
        assert_transition(PipelineStage.PUBLISHED, PipelineStage.QUEUED)


# --------------------------------------------------------------------------- #
# Quality gate — pure checks on real media
# --------------------------------------------------------------------------- #


def test_hook_checks():
    assert check_hook(None) is not None
    assert check_hook("  ") is not None
    assert check_hook("x" * 141) is not None
    assert check_hook("قهوه‌ات تموم شده؟") is None


def test_cover_title_passes_on_rendered_cover(tmp_path):
    cover = tmp_path / "cover.jpg"
    render_cover(cover, title_lines=["قهوه اتیوپی", "تازه‌برشت"])
    assert check_cover_title(cover) is None


def test_cover_title_fails_on_blank_image(tmp_path):
    blank = tmp_path / "blank.jpg"
    Image.new("RGB", (1080, 1920), (10, 32, 54)).save(blank)
    failure = check_cover_title(blank)
    assert failure is not None
    assert "No rendered title text" in failure.reason


def test_overlay_text_passes_and_fails(tmp_path):
    overlay = tmp_path / "overlay.png"
    render_overlay(overlay, lines=["اتیوپی، تازه‌برشت"])
    assert check_overlay_text(overlay) is None
    blank = tmp_path / "blank.png"
    Image.new("RGBA", (720, 1280), (0, 0, 0, 0)).save(blank)
    assert check_overlay_text(blank) is not None


def test_caption_checks():
    assert check_caption("x" * 2201) is not None
    assert check_caption("ok " + " ".join(f"#tag{i}" for i in range(31))) is not None
    assert check_caption("کاپوچینو تازه", banned_words=["ارزان‌ترین"]) is None
    assert check_caption("ارزان‌ترین قهوه", banned_words=["ارزان‌ترین"]) is not None


def _static_video(image: Path, out: Path) -> Path:
    """A single still image looped into a video — no motion at all."""
    subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-i", str(image), "-t", "4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, check=True,
    )
    return out


def test_motion_passes_for_slideshow_and_fails_for_static(tmp_path):
    frame_a = tmp_path / "a.jpg"
    frame_b = tmp_path / "b.jpg"
    render_scene_frame(frame_a, lines=["قهوه‌ات تموم شده؟"])
    render_scene_frame(frame_b, lines=["اتیوپی، تازه‌برشت"])
    reel = slideshow_reel([frame_a, frame_b], tmp_path / "reel.mp4", seconds_per_image=2.0)
    assert check_motion(reel) is None

    static = _static_video(frame_a, tmp_path / "static.mp4")
    failure = check_motion(static)
    assert failure is not None
    assert "no real motion" in failure.reason.lower()


def test_run_quality_gate_combines_failures(tmp_path):
    cover = tmp_path / "cover.jpg"
    render_cover(cover, title_lines=["قهوه اتیوپی"])
    script = {"hook": "قهوه‌ات تموم شده؟"}
    report = run_quality_gate(
        script=script, cover_path=cover, overlay_paths=[], video_path=None,
        caption="کاپوچینو تازه",
    )
    assert report.passed
    bad = run_quality_gate(
        script={"hook": ""}, cover_path=cover, overlay_paths=[], video_path=None,
        caption="x" * 2201,
    )
    assert not bad.passed
    assert len(bad.failures) == 2


# --------------------------------------------------------------------------- #
# Orchestrator end to end
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_full_run_reaches_awaiting_approval(session, workspace_scope, orchestrator_for):
    user, workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider())
    run = await _run_to_approval(session, orchestrator)
    assert run.plan["stage"] == "AWAITING_APPROVAL"
    assert run.status == "AWAITING_APPROVAL"
    assert run.spent_cents == 6  # plan (3c) + reel script (3c)
    assert run.plan["script"]["hook"] == "قهوه‌ات تموم شده؟"
    assert run.plan["script"]["scenes"][0]["text"] == run.plan["script"]["hook"]  # hook in first 3s

    content = await session.get(Content, uuid.UUID(run.plan["content_id"]))
    assert content is not None
    assert content.status == "DRAFT"
    # Only the reel video is in the publish list; the cover travels as cover_asset_id.
    assert len(content.media_asset_ids) == 1
    assert content.cover_asset_id is not None
    tasks = (await session.execute(select(AITask).where(AITask.agent_run_id == run.id))).scalars().all()
    assert {task.kind for task in tasks} == {"planning", "writing", "designing", "quality_gate"}
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.workspace_id == workspace.id,
                AuditLog.resource_type == "agent_run",
                AuditLog.resource_id == str(run.id),
            )
        )
    ).scalars().all()
    assert any(row.action == "agents.run_started" for row in audit)
    assert any(row.action == "agents.stage_transition" for row in audit)


@pytest.mark.asyncio
async def test_process_run_stops_at_approval_and_never_publishes_unapproved(
    session, workspace_scope, orchestrator_for
):
    _user, _workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider())
    run = await _run_to_approval(session, orchestrator)
    # A second drive must not move it forward or publish anything.
    again = await orchestrator.process_run(run.id)
    assert again.plan["stage"] == "AWAITING_APPROVAL"
    jobs = (await session.execute(select(PublishingJob))).scalars().all()
    assert jobs == []


@pytest.mark.asyncio
async def test_approve_then_publish_with_live_verification(
    session, workspace_scope, orchestrator_for, make_account, fake_transport, meta_client_factory
):
    user, workspace, scope = workspace_scope
    account = await make_account(workspace.id)
    orchestrator = orchestrator_for(scope, RoutingFakeProvider(), meta_client_factory=meta_client_factory)
    run = await _run_to_approval(session, orchestrator, account_id=account.id)

    # Meta script: container -> FINISHED -> publish -> live verify.
    fake_transport.enqueue({"id": "reel-container"})
    fake_transport.enqueue({"status_code": "FINISHED", "status": "ready"})
    fake_transport.enqueue({"id": "media-1", "permalink": "https://instagram.com/reel/BBB/"})
    fake_transport.enqueue({"id": "media-1", "permalink": "https://instagram.com/reel/BBB/", "media_type": "REELS"})

    approved = await orchestrator.approve(run.id, reviewer_id=user.id, comment="خوبه")
    assert approved.plan["stage"] == "PUBLISHING"
    await session.flush()
    published = await orchestrator.process_run(run.id)
    assert published.plan["stage"] == "PUBLISHED"
    assert published.status == "SUCCEEDED"
    assert published.result["publish"]["media_id"] == "media-1"
    assert published.result["publish"]["reused"] is False

    jobs = (await session.execute(select(PublishingJob))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].state == "SUCCEEDED"
    assert jobs[0].idempotency_key == f"agent-run:{run.id}"
    content = await session.get(Content, uuid.UUID(run.plan["content_id"]))
    assert content.status == "PUBLISHED"

    # Driving the run again must not start a second publish.
    again = await orchestrator.process_run(run.id)
    assert again.plan["stage"] == "PUBLISHED"
    jobs = (await session.execute(select(PublishingJob))).scalars().all()
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_reject_fails_the_run_without_publishing(session, workspace_scope, orchestrator_for):
    user, _workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider())
    run = await _run_to_approval(session, orchestrator)
    rejected = await orchestrator.reject(run.id, reviewer_id=user.id, comment="متن درست نیست")
    assert rejected.plan["stage"] == "FAILED"
    assert "متن درست نیست" in rejected.result["error"]
    jobs = (await session.execute(select(PublishingJob))).scalars().all()
    assert jobs == []
    content = await session.get(Content, uuid.UUID(run.plan["content_id"]))
    assert content.status == "FAILED"


@pytest.mark.asyncio
async def test_stage_failure_dead_letters_after_three_attempts(session, workspace_scope, orchestrator_for):
    _user, workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider(fail=True))
    run = await orchestrator.start_run(topic="قهوه اتیوپی", content_type="REELS")
    await session.flush()
    for _ in range(3):
        run = await orchestrator.process_run(run.id)
        await session.flush()
    assert run.plan["stage"] == "DEAD"
    assert run.status == "FAILED"
    assert run.result["dead_letter"] is True
    assert run.result["failed_stage"] == "PLANNING"
    assert run.plan["attempts"]["PLANNING"] == 3
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.workspace_id == workspace.id,
                AuditLog.action == "agents.pipeline_dead_letter",
            )
        )
    ).scalars().all()
    assert len(audit) == 1


@pytest.mark.asyncio
async def test_quality_gate_rework_is_bounded(session, workspace_scope, orchestrator_for, tmp_path):
    _user, _workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider())
    run = await orchestrator.start_run(topic="قهوه اتیوپی", content_type="REELS")
    await session.flush()
    blank = tmp_path / "blank.jpg"
    Image.new("RGB", (1080, 1920), (10, 32, 54)).save(blank)
    run.plan = {
        **run.plan,
        "stage": "QUALITY_GATE",
        "script": {"hook": ""},
        "local_paths": {"cover": str(blank), "video": None, "overlays": []},
        "title_font_px": 118,
    }
    run.status = "RUNNING"
    await session.flush()
    stage = await orchestrator._do_quality_gate(run)
    assert stage == PipelineStage.DESIGNING
    assert run.plan["quality_rework"] == 1
    assert run.plan["gate_failures"]
    # The pipeline re-designs and the gate runs again from QUALITY_GATE.
    run.plan = {**run.plan, "stage": "QUALITY_GATE"}
    await session.flush()
    stage = await orchestrator._do_quality_gate(run)
    assert stage == PipelineStage.DESIGNING
    assert run.plan["quality_rework"] == 2
    run.plan = {**run.plan, "stage": "QUALITY_GATE"}
    await session.flush()
    stage = await orchestrator._do_quality_gate(run)
    assert stage == PipelineStage.DEAD
    assert run.result["dead_letter"] is True


@pytest.mark.asyncio
async def test_run_is_invisible_from_another_workspace(
    session, workspace_scope, orchestrator_for, make_user, make_workspace
):
    _user, _workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider())
    run = await orchestrator.start_run(topic="قهوه اتیوپی", content_type="REELS")
    await session.flush()
    other_user = await make_user()
    other_workspace = await make_workspace(other_user.id)
    other_ctx = await TenantService(session).build_context(
        workspace_id=other_workspace.id, user_id=other_user.id
    )
    other_scope = TenantScope(session, other_ctx)
    other_orchestrator = orchestrator_for(other_scope, RoutingFakeProvider())
    with pytest.raises(NotFoundError):
        await other_orchestrator.process_run(run.id)
    with pytest.raises(NotFoundError):
        await other_orchestrator.approve(run.id)


@pytest.mark.asyncio
async def test_analyst_stores_null_for_missing_metrics(
    session, workspace_scope, orchestrator_for, make_account, fake_transport, meta_client_factory
):
    _user, workspace, scope = workspace_scope
    account = await make_account(workspace.id)
    orchestrator = orchestrator_for(scope, RoutingFakeProvider(), meta_client_factory=meta_client_factory)
    run = await orchestrator.start_run(topic="قهوه اتیوپی", content_type="REELS", account_id=account.id)
    await session.flush()
    run.plan = {**run.plan, "stage": "PUBLISHED"}
    run.status = "SUCCEEDED"
    run.result = {"publish": {"media_id": "media-1", "permalink": "https://instagram.com/reel/BBB/"}}
    await session.flush()

    fake_transport.enqueue(
        {"data": [
            {"name": "views", "values": [{"value": 120}]},
            {"name": "likes", "values": []},
        ]}
    )
    analysed = await orchestrator.analyse_run(run.id)
    analyst = analysed.result["analyst"]
    by_metric = {snap["metric"]: snap for snap in analyst["snapshots"]}
    assert by_metric["views"]["value"] == 120
    assert by_metric["views"]["is_available"] is True
    assert by_metric["likes"]["value"] is None
    assert by_metric["likes"]["is_available"] is False
    assert "comments" in analyst["missing_metrics"]
    assert analyst["recommendations"]
    from app.modules.inbox.models import AnalyticsSnapshot

    rows = (
        await session.execute(
            select(AnalyticsSnapshot).where(AnalyticsSnapshot.media_id == "media-1")
        )
    ).scalars().all()
    assert any(row.metric == "likes" and row.value is None and row.is_available is False for row in rows)


@pytest.mark.asyncio
async def test_reel_script_failure_raises_and_records(session, workspace_scope):
    """A provider failure in the Writer is raised (not swallowed) and the
    failed generation is still recorded with its error."""
    from app.core.exceptions import ServiceUnavailable
    from app.modules.ai.service import AIContentService
    from app.modules.content.models import AIGeneration

    _user, _workspace, scope = workspace_scope
    service = AIContentService(session, scope, provider=RoutingFakeProvider(fail=True))
    with pytest.raises(ServiceUnavailable):
        await service.generate_reel_script(topic="قهوه اتیوپی")
    generation = (
        await session.execute(select(AIGeneration).where(AIGeneration.kind == "reel_script"))
    ).scalars().first()
    assert generation is not None
    assert generation.status == "FAILED"
    assert generation.error is not None


@pytest.mark.asyncio
async def test_cancel_stops_an_unfinished_run(session, workspace_scope, orchestrator_for):
    _user, _workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider())
    run = await orchestrator.start_run(topic="قهوه اتیوپی", content_type="REELS")
    await session.flush()
    cancelled = await orchestrator.cancel(run.id)
    assert cancelled.plan["stage"] == "CANCELLED"
    with pytest.raises(IllegalStateTransition):
        await orchestrator.cancel(run.id)
