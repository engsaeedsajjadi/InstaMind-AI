"""Agentic content pipeline: five agents over a Celery job queue.

One content item is one :class:`AgentRun` moving through
``QUEUED → PLANNING → WRITING → DESIGNING → QUALITY_GATE
→ AWAITING_APPROVAL → PUBLISHING → PUBLISHED`` (see ``state.py``).
Publishing requires explicit human approval; nothing auto-publishes.
"""

from __future__ import annotations

from app.modules.agents.agents import (
    AgentContext,
    AnalystAgent,
    DesignerAgent,
    PlannerAgent,
    PublisherAgent,
    QualityGateAgent,
    WriterAgent,
)
from app.modules.agents.harness_revision import (
    CHECK_WEIGHTS,
    DEFAULT_HARNESS,
    DEFAULT_SCORING_SET,
    CaseResult,
    ExecutionReport,
    HarnessConfig,
    HarnessEdit,
    RevisionResult,
    RevisionRound,
    ScoringCase,
    ScoringSet,
    apply_edits,
    case_result_from_gate,
    propose_edits,
    run_revision_round,
)
from app.modules.agents.orchestrator import PipelineOrchestrator
from app.modules.agents.quality import QualityReport, run_quality_gate
from app.modules.agents.render import (
    composite_reel,
    extract_frames,
    probe_duration,
    render_cover,
    render_overlay,
    render_scene_frame,
    slideshow_reel,
)
from app.modules.agents.state import MAX_QUALITY_REWORK, MAX_STAGE_ATTEMPTS, PipelineStage

__all__ = [
    "CHECK_WEIGHTS",
    "DEFAULT_HARNESS",
    "DEFAULT_SCORING_SET",
    "MAX_QUALITY_REWORK",
    "MAX_STAGE_ATTEMPTS",
    "AgentContext",
    "AnalystAgent",
    "CaseResult",
    "DesignerAgent",
    "ExecutionReport",
    "HarnessConfig",
    "HarnessEdit",
    "PipelineOrchestrator",
    "PipelineStage",
    "PlannerAgent",
    "PublisherAgent",
    "QualityGateAgent",
    "QualityReport",
    "RevisionResult",
    "RevisionRound",
    "ScoringCase",
    "ScoringSet",
    "WriterAgent",
    "apply_edits",
    "case_result_from_gate",
    "composite_reel",
    "extract_frames",
    "probe_duration",
    "propose_edits",
    "render_cover",
    "render_overlay",
    "render_scene_frame",
    "run_quality_gate",
    "run_revision_round",
    "slideshow_reel",
]
