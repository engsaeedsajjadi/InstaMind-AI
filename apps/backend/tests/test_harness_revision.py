"""Self-revising harness: exam, proposer, bounded loop, approval gate."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.exceptions import IllegalStateTransition
from app.modules.agents.agents import AgentContext, DesignerAgent
from app.modules.agents.harness_revision import (
    DEFAULT_HARNESS,
    DEFAULT_SCORING_SET,
    CaseResult,
    ExecutionReport,
    HarnessConfig,
    HarnessEdit,
    ScoringCase,
    ScoringSet,
    apply_edits,
    case_result_from_gate,
    propose_edits,
    run_revision_round,
    score_execution_report,
)
from app.modules.agents.orchestrator import PipelineOrchestrator
from app.modules.content.models import AITask
from app.modules.storage.backend import LocalStorage
from app.modules.tenants.scope import TenantScope
from app.modules.tenants.service import TenantService
from app.providers.ai.base import AICompletion, AIUsage

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
    """Same routing fake as test_agentic_pipeline (kept local so this
    file does not import another test module)."""

    name = "fake-routing"

    def __init__(self, *, cost_cents: int = 3) -> None:
        self.cost_cents = cost_cents

    async def complete(self, messages, **kwargs):
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

# --------------------------------------------------------------------------- #
# Defaults and the exam
# --------------------------------------------------------------------------- #


def test_default_harness_matches_current_behaviour():
    assert DEFAULT_HARNESS.version == 1
    assert DEFAULT_HARNESS.title_font_px == 118
    assert DEFAULT_HARNESS.rework_title_font_px == 140
    assert DEFAULT_HARNESS.zoom_step == 0.0016
    assert DEFAULT_HARNESS.rework_zoom_step == 0.0032
    assert DEFAULT_HARNESS.scene_font_px == 52
    assert DEFAULT_HARNESS.overlay_font_px == 46
    assert DEFAULT_HARNESS.writer_instruction_addendum == ""
    # Round-trip through the JSON plan column.
    assert HarnessConfig.from_dict(DEFAULT_HARNESS.to_dict()) == DEFAULT_HARNESS
    assert HarnessConfig.from_dict(None) == DEFAULT_HARNESS
    assert HarnessConfig.from_dict({"title_font_px": 140, "unknown_key": 1}).title_font_px == 140


def test_scoring_set_is_held_out_from_dev_topics():
    topics = [case.topic for case in DEFAULT_SCORING_SET.cases]
    assert len(DEFAULT_SCORING_SET.cases) == 6
    assert len({case.id for case in DEFAULT_SCORING_SET.cases}) == 6
    # Nothing in the exam reuses a development/test topic (coffee).
    assert not any("قهوه" in topic for topic in topics)
    assert {"REELS", "IMAGE"} == {case.content_type for case in DEFAULT_SCORING_SET.cases}
    assert set(DEFAULT_SCORING_SET.weights) == {"hook", "cover_title", "overlay_text", "motion", "caption"}


def test_scoring_set_rejects_carousel_and_duplicate_ids():
    with pytest.raises(ValueError, match="CAROUSEL"):
        ScoringSet(name="bad", cases=[ScoringCase(id="x", topic="t", content_type="CAROUSEL")])
    with pytest.raises(ValueError, match="unique"):
        ScoringSet(
            name="bad",
            cases=[
                ScoringCase(id="x", topic="t1"),
                ScoringCase(id="x", topic="t2"),
            ],
        )


def test_case_result_from_gate_weights_checks():
    result = case_result_from_gate(
        "a",
        failed_checks=["cover_title"],
        has_cover=True,
        has_video=True,
        has_overlays=False,
    )
    assert result.passed_checks == ["hook", "caption", "motion"]
    assert result.failed_checks == ["cover_title"]
    assert result.score == 4.0  # hook 1 + caption 1 + motion 2
    assert result.max_score == 6.0
    assert score_execution_report(
        ExecutionReport(config_version=1, cases=[result])
    ) == (4.0, 6.0)


# --------------------------------------------------------------------------- #
# Proposer — deterministic, held out, bounded
# --------------------------------------------------------------------------- #


def _all_failures_report() -> ExecutionReport:
    return ExecutionReport(
        config_version=1,
        cases=[
            CaseResult(
                case_id="a",
                passed_checks=["caption"],
                failed_checks=["hook", "cover_title", "motion"],
                score=1.0,
                max_score=6.0,
            ),
            CaseResult(
                case_id="b",
                passed_checks=["hook"],
                failed_checks=["overlay_text", "caption"],
                score=1.0,
                max_score=5.0,
            ),
        ],
    )


def test_proposer_never_receives_topics():
    report = _all_failures_report()
    dumped = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "topic" not in asdict(report)
    assert "topic" not in report.to_dict()
    # No exam wording leaks into the report the proposer sees.
    for case in DEFAULT_SCORING_SET.cases:
        assert case.topic not in dumped
    # The proposer works on the report alone — no topic argument exists.
    edits = propose_edits(report, DEFAULT_HARNESS, max_edits=10)
    assert edits


def test_proposer_maps_each_failure_type_to_the_right_edit():
    edits = propose_edits(_all_failures_report(), DEFAULT_HARNESS, max_edits=10)
    by_target = {edit.target: edit for edit in edits}
    assert set(by_target) == {
        "title_font_px",
        "zoom_step",
        "overlay_font_px",
        "writer_instruction_addendum",
    }
    assert by_target["title_font_px"].old_value == 118
    assert by_target["title_font_px"].new_value == 140
    assert by_target["zoom_step"].old_value == 0.0016
    assert by_target["zoom_step"].new_value == 0.0032
    assert by_target["overlay_font_px"].old_value == 46
    assert by_target["overlay_font_px"].new_value == 58
    addendum = str(by_target["writer_instruction_addendum"].new_value)
    assert "5-140" in addendum  # hook instruction
    assert "2200" in addendum  # caption instruction
    assert "cover_title failed in 1/2 cases" in by_target["title_font_px"].failure_evidence


def test_proposer_is_bounded_and_never_proposes_a_no_op():
    # max_edits caps the round.
    edits = propose_edits(_all_failures_report(), DEFAULT_HARNESS, max_edits=2)
    assert len(edits) == 2
    # A config already at every bound proposes nothing.
    saturated = HarnessConfig(
        title_font_px=180,
        zoom_step=0.01,
        overlay_font_px=80,
        writer_instruction_addendum=(
            "Keep the hook to one short sentence of 5-140 characters that states "
            "the problem or the promise. Keep the caption within 2200 characters "
            "and at most 10 hashtags, and never use a banned word."
        ),
    )
    assert propose_edits(_all_failures_report(), saturated) == []
    # No failures at all proposes nothing.
    clean = ExecutionReport(
        config_version=1,
        cases=[CaseResult(case_id="a", passed_checks=["hook"], failed_checks=[], score=1.0, max_score=1.0)],
    )
    assert propose_edits(clean, DEFAULT_HARNESS) == []


def test_apply_edits_versions_config_and_leaves_parent_untouched():
    config = HarnessConfig(version=3, title_font_px=118)
    edits = [
        HarnessEdit(
            target="title_font_px",
            old_value=118,
            new_value=140,
            reason="r",
            failure_evidence="e",
        )
    ]
    child = apply_edits(config, edits)
    assert child.version == 4
    assert child.parent_version == 3
    assert child.title_font_px == 140
    assert config.version == 3 and config.title_font_px == 118  # frozen parent
    with pytest.raises(ValueError, match="empty"):
        apply_edits(config, [])
    with pytest.raises(ValueError, match="Unknown harness edit target"):
        apply_edits(
            config,
            [HarnessEdit(target="nope", old_value=1, new_value=2, reason="r", failure_evidence="e")],
        )


# --------------------------------------------------------------------------- #
# Pure loop — fake evaluator, no DB
# --------------------------------------------------------------------------- #


def _one_case_set() -> ScoringSet:
    return ScoringSet(
        name="one",
        cases=[ScoringCase(id="a", topic="موضوع آزمایشی", content_type="IMAGE")],
    )


async def test_revision_loop_keeps_better_candidate():
    async def evaluate(case, config):
        if config.title_font_px >= 140:
            return CaseResult(case_id="a", passed_checks=["hook", "cover_title"], failed_checks=[], score=3.0, max_score=3.0)
        return CaseResult(case_id="a", passed_checks=["hook"], failed_checks=["cover_title"], score=1.0, max_score=3.0)

    result = await run_revision_round(
        HarnessConfig(version=0, title_font_px=60), _one_case_set(), evaluate, max_rounds=3
    )
    assert result.improved
    assert result.proposal is not None
    assert result.proposal.title_font_px == 140
    assert result.proposal.version == 1
    assert result.proposal.parent_version == 0
    assert result.rounds[0].kept is True
    assert result.rounds[0].baseline_score == 1.0
    assert result.rounds[0].candidate_score == 3.0


async def test_revision_loop_discards_worse_candidate():
    async def evaluate(case, config):
        # The baseline fails cover_title, so the proposer edits the title
        # font — but in this simulated world a bigger title scores worse
        # (e.g. it breaks the layout). The candidate must be discarded.
        if config.title_font_px >= 140:
            return CaseResult(
                case_id="a",
                passed_checks=[],
                failed_checks=["cover_title", "motion"],
                score=0.0,
                max_score=3.0,
            )
        return CaseResult(
            case_id="a", passed_checks=["hook"], failed_checks=["cover_title"], score=2.0, max_score=3.0
        )

    result = await run_revision_round(
        HarnessConfig(version=2, title_font_px=60), _one_case_set(), evaluate, max_rounds=3
    )
    assert not result.improved
    assert result.proposal is None
    assert result.final_config.version == 2  # baseline kept, candidate discarded
    assert len(result.rounds) == 1
    assert result.rounds[0].kept is False
    assert result.rounds[0].baseline_score == 2.0
    assert result.rounds[0].candidate_score == 0.0


async def test_revision_loop_stops_when_nothing_to_fix():
    async def evaluate(case, config):
        return CaseResult(case_id="a", passed_checks=["hook"], failed_checks=[], score=1.0, max_score=1.0)

    result = await run_revision_round(DEFAULT_HARNESS, _one_case_set(), evaluate)
    assert result.proposal is None
    assert result.rounds[0].edits == []
    assert result.rounds[0].candidate_version is None


# --------------------------------------------------------------------------- #
# Designer honours a harness config (no DB)
# --------------------------------------------------------------------------- #


async def test_designer_accepts_harness_config(tmp_path):
    storage = LocalStorage(root=str(tmp_path / "storage"), base_url="http://localhost:8000/media")
    designer = DesignerAgent(storage, workdir=tmp_path / "runs")

    async def store_media(**fields):
        return SimpleNamespace(id=uuid.uuid4())

    ctx = AgentContext(
        run_id=uuid.uuid4(), workspace_id=uuid.uuid4(), topic="قهوه", content_type="IMAGE"
    )
    script = {"hook": "قهوه تازه", "cover_title": "قهوه تازه", "caption": "تازه", "scenes": []}
    default = await designer.design(ctx, script=script, gate_failures=[], store_media=store_media)
    assert default.title_font_px == 118
    assert default.harness_version == DEFAULT_HARNESS.version
    rework = await designer.design(
        ctx, script=script, gate_failures=["cover_title: no text"], store_media=store_media
    )
    assert rework.title_font_px == 140  # existing rework behaviour, unchanged
    custom = await designer.design(
        ctx,
        script=script,
        gate_failures=[],
        store_media=store_media,
        harness_config=HarnessConfig(version=5, title_font_px=150, zoom_step=0.005),
    )
    assert custom.title_font_px == 150
    assert custom.harness_version == 5


# --------------------------------------------------------------------------- #
# Orchestrator end to end — fake provider, real render + quality gate
# --------------------------------------------------------------------------- #


@pytest.fixture
async def workspace_scope(session, make_user, make_workspace):
    user = await make_user()
    workspace = await make_workspace(user.id)
    ctx = await TenantService(session).build_context(workspace_id=workspace.id, user_id=user.id)
    return user, workspace, TenantScope(session, ctx)


@pytest.fixture
def orchestrator_for(session, tmp_path):
    def _build(scope, provider):
        return PipelineOrchestrator(
            session,
            scope,
            ai_provider=provider,
            storage=LocalStorage(root=str(tmp_path / "storage"), base_url="http://localhost:8000/media"),
            workdir=tmp_path / "runs",
        )

    return _build


def _two_case_set() -> ScoringSet:
    """Held-out exam for the e2e test: topics distinct from the default
    set and from every dev/test topic."""
    return ScoringSet(
        name="e2e-fa-v1",
        cases=[
            ScoringCase(id="swim", topic="معرفی باشگاه شنا", content_type="REELS"),
            ScoringCase(id="skin", topic="نکات مراقبت از پوست", content_type="IMAGE"),
        ],
    )


def _weak_config() -> HarnessConfig:
    # Deliberately bad harness: title below the gate minimum (72 px) and
    # zero zoom (a still slideshow) — both must fail at baseline. Version
    # 10 keeps it distinct from the run's default active harness (v1), so
    # the test can tell "active unchanged" apart from "proposal kept".
    return HarnessConfig(version=10, title_font_px=60, zoom_step=0.0)


async def test_revision_round_end_to_end_proposal_needs_approval(
    session, workspace_scope, orchestrator_for
):
    _user, _workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider())
    run = await orchestrator.start_run(topic="قهوه اتیوپی", content_type="REELS")
    await session.flush()
    assert run.plan["harness"]["title_font_px"] == 118  # new runs ship the default

    revised = await orchestrator.run_harness_revision_round(
        run.id, scoring_set=_two_case_set(), base_config=_weak_config()
    )
    revision = revised.result["harness_revision"]
    assert revision["improved"] is True
    assert revision["proposal_version"] == 11
    assert revision["rounds"][0]["kept"] is True
    assert revision["rounds"][0]["baseline_score"] < revision["rounds"][0]["candidate_score"]
    # The candidate is a PROPOSAL only: the active harness is untouched.
    assert revised.plan["harness"]["version"] == 1
    assert revised.plan["harness"]["title_font_px"] == 118
    proposal = revised.plan["harness_proposal"]
    assert proposal["version"] == 11
    assert proposal["parent_version"] == 10
    assert proposal["title_font_px"] == 140
    assert proposal["zoom_step"] == 0.0032
    tasks = (await session.execute(select(AITask).where(AITask.agent_run_id == run.id))).scalars().all()
    assert any(task.kind == "harness_revision" for task in tasks)

    # Approving activates the proposal and clears the pending slot.
    approved = await orchestrator.approve_harness_revision(run.id, reviewer_id=_user.id)
    assert approved.plan["harness"]["version"] == 11
    assert approved.plan["harness"]["title_font_px"] == 140
    assert approved.plan["harness_proposal"] is None
    assert approved.result["harness_revision"]["approved"]["version"] == 11
    # A second approval with nothing pending is illegal.
    with pytest.raises(IllegalStateTransition):
        await orchestrator.approve_harness_revision(run.id)


async def test_reject_harness_revision_discards_proposal(
    session, workspace_scope, orchestrator_for
):
    user, _workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider())
    run = await orchestrator.start_run(topic="قهوه اتیوپی", content_type="REELS")
    await session.flush()
    revised = await orchestrator.run_harness_revision_round(
        run.id, scoring_set=_two_case_set(), base_config=_weak_config()
    )
    assert revised.plan["harness_proposal"] is not None
    rejected = await orchestrator.reject_harness_revision(run.id, reviewer_id=user.id)
    assert rejected.plan["harness_proposal"] is None
    assert rejected.plan["harness"]["version"] == 1  # active harness unchanged (default)
    assert rejected.plan["harness"]["title_font_px"] == 118
    assert rejected.result["harness_revision"]["rejected"]["version"] == 11
    with pytest.raises(IllegalStateTransition):
        await orchestrator.reject_harness_revision(run.id)


async def test_approve_or_reject_without_proposal_raises(
    session, workspace_scope, orchestrator_for
):
    _user, _workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider())
    run = await orchestrator.start_run(topic="قهوه اتیوپی", content_type="REELS")
    await session.flush()
    with pytest.raises(IllegalStateTransition):
        await orchestrator.approve_harness_revision(run.id)
    with pytest.raises(IllegalStateTransition):
        await orchestrator.reject_harness_revision(run.id)


async def test_revision_round_with_no_improvement_keeps_active_harness(
    session, workspace_scope, orchestrator_for
):
    """Baseline is already the default (passing) harness: the round finds
    nothing to fix, proposes nothing, and changes nothing."""
    _user, _workspace, scope = workspace_scope
    orchestrator = orchestrator_for(scope, RoutingFakeProvider())
    run = await orchestrator.start_run(topic="قهوه اتیوپی", content_type="REELS")
    await session.flush()
    revised = await orchestrator.run_harness_revision_round(
        run.id, scoring_set=_two_case_set(), base_config=DEFAULT_HARNESS
    )
    assert revised.result["harness_revision"]["improved"] is False
    assert revised.plan["harness_proposal"] is None
    assert revised.plan["harness"]["version"] == DEFAULT_HARNESS.version
