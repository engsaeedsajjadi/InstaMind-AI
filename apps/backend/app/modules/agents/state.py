"""Agentic content-pipeline state machine.

One content item is one :class:`AgentRun`. The pipeline moves through
ordered stages; the coarse ``AgentRun.status`` mirrors the stage, and the
fine-grained stage lives in ``AgentRun.plan["stage"]`` so the existing
tables (``agent_runs`` / ``ai_tasks``) carry the whole pipeline without a
new migration.

States
------
``QUEUED → PLANNING → WRITING → DESIGNING → QUALITY_GATE
→ AWAITING_APPROVAL → PUBLISHING → PUBLISHED``

Failure paths (never silent — every one writes an audit row and a result):

* ``FAILED`` — a permanent error (bad AI output, unrecoverable Meta error,
  human rejection of the content). The run row *is* the record.
* ``DEAD`` — the dead letter: a stage exhausted its retries, or the quality
  gate failed its bounded rework loops. The run stays queryable with
  ``result["dead_letter"] = True`` and the last error attached.
* ``CANCELLED`` — a human cancelled the run while it was still unfinished.

Rules enforced here, not in callers:

* Only the listed transitions exist; anything else raises
  :class:`IllegalStateTransition`.
* ``PUBLISHING`` is reachable only from ``AWAITING_APPROVAL`` — the human
  gate cannot be skipped by jumping stages.
* A quality-gate failure routes back to ``DESIGNING`` (bounded rework), never
  forward to approval.
"""

from __future__ import annotations

from enum import StrEnum

from app.core.exceptions import IllegalStateTransition

# Maps pipeline stage -> AgentRun.status (the coarse, queryable column).
STAGE_TO_RUN_STATUS: dict[str, str] = {
    "QUEUED": "PENDING",
    "PLANNING": "PLANNING",
    "WRITING": "RUNNING",
    "DESIGNING": "RUNNING",
    "QUALITY_GATE": "RUNNING",
    "AWAITING_APPROVAL": "AWAITING_APPROVAL",
    "PUBLISHING": "RUNNING",
    "PUBLISHED": "SUCCEEDED",
    "FAILED": "FAILED",
    "DEAD": "FAILED",
    "CANCELLED": "CANCELLED",
}

TERMINAL_STAGES = frozenset({"PUBLISHED", "FAILED", "DEAD", "CANCELLED"})

# Maximum attempts *per stage* before the run is dead-lettered.
MAX_STAGE_ATTEMPTS = 3
# Maximum times the quality gate may send work back to the Designer.
MAX_QUALITY_REWORK = 2


class PipelineStage(StrEnum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    WRITING = "WRITING"
    DESIGNING = "DESIGNING"
    QUALITY_GATE = "QUALITY_GATE"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    DEAD = "DEAD"
    CANCELLED = "CANCELLED"


# The only legal edges. "QUALITY_GATE -> DESIGNING" is the rework loop;
# "AWAITING_APPROVAL -> PUBLISHING" is the human gate.
ALLOWED_TRANSITIONS: dict[PipelineStage, frozenset[PipelineStage]] = {
    PipelineStage.QUEUED: frozenset(
        {PipelineStage.PLANNING, PipelineStage.CANCELLED, PipelineStage.FAILED}
    ),
    PipelineStage.PLANNING: frozenset(
        {PipelineStage.WRITING, PipelineStage.FAILED, PipelineStage.DEAD, PipelineStage.CANCELLED}
    ),
    PipelineStage.WRITING: frozenset(
        {PipelineStage.DESIGNING, PipelineStage.FAILED, PipelineStage.DEAD, PipelineStage.CANCELLED}
    ),
    PipelineStage.DESIGNING: frozenset(
        {PipelineStage.QUALITY_GATE, PipelineStage.FAILED, PipelineStage.DEAD, PipelineStage.CANCELLED}
    ),
    PipelineStage.QUALITY_GATE: frozenset(
        {
            PipelineStage.AWAITING_APPROVAL,
            PipelineStage.DESIGNING,  # rework, bounded by MAX_QUALITY_REWORK
            PipelineStage.FAILED,
            PipelineStage.DEAD,
            PipelineStage.CANCELLED,
        }
    ),
    PipelineStage.AWAITING_APPROVAL: frozenset(
        {PipelineStage.PUBLISHING, PipelineStage.FAILED, PipelineStage.CANCELLED}
    ),
    PipelineStage.PUBLISHING: frozenset(
        {PipelineStage.PUBLISHED, PipelineStage.FAILED, PipelineStage.DEAD, PipelineStage.CANCELLED}
    ),
    PipelineStage.PUBLISHED: frozenset(),
    PipelineStage.FAILED: frozenset(),
    PipelineStage.DEAD: frozenset(),
    PipelineStage.CANCELLED: frozenset(),
}


def assert_transition(from_stage: PipelineStage, to_stage: PipelineStage) -> None:
    """Raise :class:`IllegalStateTransition` for any edge that is not listed."""
    if to_stage not in ALLOWED_TRANSITIONS[from_stage]:
        raise IllegalStateTransition(
            f"Pipeline cannot move from {from_stage} to {to_stage}.",
            context={"from_stage": str(from_stage), "to_stage": str(to_stage)},
        )


def is_terminal(stage: PipelineStage) -> bool:
    return stage in TERMINAL_STAGES


def run_status_for(stage: PipelineStage) -> str:
    return STAGE_TO_RUN_STATUS[str(stage)]


__all__ = [
    "ALLOWED_TRANSITIONS",
    "MAX_QUALITY_REWORK",
    "MAX_STAGE_ATTEMPTS",
    "TERMINAL_STAGES",
    "PipelineStage",
    "assert_transition",
    "is_terminal",
    "run_status_for",
]
