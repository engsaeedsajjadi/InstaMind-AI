"""Self-revising harness — "The End of Harness Engineering", adapted.

The paper's claim, in one line: design the test before you write the code.
Applied honestly to this pipeline, that means:

* **The exam** (:class:`ScoringSet`) is a small held-out set of topics,
  written by a human, graded with the *existing* quality gate
  (:func:`app.modules.agents.quality.run_quality_gate`) on real rendered
  media. The proposer never sees the topics — an
  :class:`ExecutionReport` carries only case ids and failure check names —
  so the harness cannot be tuned to the exam's wording, only to the
  checks themselves.
* **Frozen solver**: during one revision round the Writer/Planner prompts
  (``REEL_SCRIPT_SYSTEM_PROMPT`` etc. in ``app/modules/ai/service.py``)
  are never mutated. Only the *harness* changes, and the harness is a
  versioned :class:`HarnessConfig` — render parameters plus a writer
  instruction addendum that is passed per-run, never written into the
  live default prompts.
* **Proposer**: deterministic and rule-based in v1 — no new LLM call, no
  API key. It maps execution-report failure patterns to concrete harness
  edits (cover-title failure → larger title font, motion failure →
  stronger zoom, hook/caption failure → a tighter writer instruction).
  Every proposal is an auditable :class:`HarnessEdit`.
* **Bounded loop**: at most ``max_rounds`` rounds (default 3), at most one
  candidate per round. A candidate is kept *only* if its total score on
  the same held-out set strictly improves; otherwise it is discarded and
  the loop stops.
* **Human approval**: a kept candidate becomes a *proposal*, never the
  active harness. :meth:`PipelineOrchestrator.approve_harness_revision`
  (or ``reject_harness_revision``) must be called explicitly before the
  config becomes active. Content publishing still needs its own separate
  human approval (``AWAITING_APPROVAL → PUBLISHING``) and is untouched
  by this module.

What this module does **not** do: no live-LLM proposer, no live Meta
run, no CAROUSEL rendering, no change to the quality-gate thresholds,
and no fix for the known S3 temp-download limitation (scoring runs use
local storage / a tmp workdir, like the existing tests).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field

# Checks the exam grades, with the weight each carries in a case score.
# These mirror the existing quality gate (quality.py): hook 5-140 chars,
# bold readable cover title, rendered overlay text, real motion, and the
# caption length/hashtag/banned-word rules. No invented metrics.
CHECK_WEIGHTS: dict[str, float] = {
    "hook": 1.0,
    "cover_title": 2.0,
    "overlay_text": 1.0,
    "motion": 2.0,
    "caption": 1.0,
}

# Hard bounds for proposer edits, so a revision can never propose an
# absurd render parameter.
_MAX_TITLE_FONT_PX = 180
_MAX_ZOOM_STEP = 0.01
_MAX_OVERLAY_FONT_PX = 80

_HOOK_INSTRUCTION = (
    "Keep the hook to one short sentence of 5-140 characters that states "
    "the problem or the promise."
)
_CAPTION_INSTRUCTION = (
    "Keep the caption within 2200 characters and at most 10 hashtags, "
    "and never use a banned word."
)


# --------------------------------------------------------------------------- #
# The exam
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScoringCase:
    """One held-out exam case. Topics are deliberately distinct from every
    topic used in development or in the existing tests (e.g. the coffee
    topics in ``test_agentic_pipeline.py``)."""

    id: str
    topic: str
    content_type: str = "REELS"  # REELS | IMAGE (CAROUSEL is not rendered)


@dataclass(frozen=True)
class ScoringSet:
    """The exam: held-out cases plus the per-check weights."""

    name: str
    cases: list[ScoringCase] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=lambda: dict(CHECK_WEIGHTS))

    def __post_init__(self) -> None:
        if not self.cases:
            raise ValueError("A scoring set needs at least one case.")
        ids = [case.id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Scoring case ids must be unique: {ids}.")
        for case in self.cases:
            if case.content_type not in {"REELS", "IMAGE"}:
                raise ValueError(
                    f"Scoring case {case.id} has unsupported content type "
                    f"{case.content_type!r} (CAROUSEL rendering is not implemented)."
                )


DEFAULT_SCORING_SET = ScoringSet(
    name="default-fa-v1",
    cases=[
        ScoringCase(id="plants", topic="آموزش نگهداری گیاهان آپارتمانی", content_type="REELS"),
        ScoringCase(id="bread", topic="طرز تهیه نان خانگی", content_type="REELS"),
        ScoringCase(id="water", topic="نکات صرفه‌جویی در مصرف آب", content_type="IMAGE"),
        ScoringCase(id="pottery", topic="معرفی کارگاه سفالگری", content_type="REELS"),
        ScoringCase(id="shoes", topic="راهنمای انتخاب کفش دویدن", content_type="IMAGE"),
        ScoringCase(id="library", topic="معرفی کتابخانه محله", content_type="REELS"),
    ],
)


# --------------------------------------------------------------------------- #
# The harness (versioned) and its edits
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HarnessConfig:
    """Everything around the model that a revision is allowed to change.

    Defaults match the behaviour the pipeline already ships
    (``DesignerAgent``: title 118 px, rework title 140 px, zoom 0.0016,
    rework zoom 0.0032; scene frames 52 px, overlays 46 px), so passing no
    config changes nothing. ``version``/``parent_version`` make the
    lineage auditable: every candidate is ``version + 1`` of its parent.
    """

    version: int = 1
    parent_version: int | None = None
    title_font_px: int = 118
    rework_title_font_px: int = 140
    zoom_step: float = 0.0016
    rework_zoom_step: float = 0.0032
    scene_font_px: int = 52
    overlay_font_px: int = 46
    writer_instruction_addendum: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> HarnessConfig:
        """Tolerant read: unknown keys are dropped, missing keys default,
        so a plan written by an older version still loads."""
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


DEFAULT_HARNESS = HarnessConfig()


@dataclass(frozen=True)
class HarnessEdit:
    """One auditable harness change proposed from an execution report."""

    target: str
    old_value: object
    new_value: object
    reason: str
    failure_evidence: str

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Execution report — the only thing the proposer ever sees
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CaseResult:
    """One case's grade. Deliberately contains no topic text: the proposer
    receives this, never the :class:`ScoringCase`, so the exam stays
    held out."""

    case_id: str
    passed_checks: list[str] = field(default_factory=list)
    failed_checks: list[str] = field(default_factory=list)
    score: float = 0.0
    max_score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionReport:
    """The grade of one harness config over the whole scoring set."""

    config_version: int
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def total_score(self) -> float:
        return sum(case.score for case in self.cases)

    @property
    def max_score(self) -> float:
        return sum(case.max_score for case in self.cases)

    def failure_counts(self) -> dict[str, int]:
        """How many cases failed each check, e.g. ``{"motion": 3}``."""
        counts: dict[str, int] = {}
        for case in self.cases:
            for check in case.failed_checks:
                counts[check] = counts.get(check, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "config_version": self.config_version,
            "total_score": self.total_score,
            "max_score": self.max_score,
            "failure_counts": self.failure_counts(),
            "cases": [case.to_dict() for case in self.cases],
        }


def case_result_from_gate(
    case_id: str,
    *,
    failed_checks: list[str],
    has_cover: bool,
    has_video: bool,
    has_overlays: bool,
    weights: dict[str, float] | None = None,
) -> CaseResult:
    """Turn a quality-gate failure list into a weighted :class:`CaseResult`.

    Applicable checks: hook + caption always; cover_title when a cover was
    rendered; motion for reels (a video exists); overlay_text only when
    overlays were rendered (base-video reels — slideshow reels carry their
    scene text in the frames themselves).
    """
    weights = weights or CHECK_WEIGHTS
    applicable = ["hook", "caption"]
    if has_cover:
        applicable.append("cover_title")
    if has_video:
        applicable.append("motion")
    if has_overlays:
        applicable.append("overlay_text")
    failed = [check for check in failed_checks if check in applicable]
    passed = [check for check in applicable if check not in failed]
    return CaseResult(
        case_id=case_id,
        passed_checks=passed,
        failed_checks=failed,
        score=sum(weights.get(check, 1.0) for check in passed),
        max_score=sum(weights.get(check, 1.0) for check in applicable),
    )


def score_execution_report(report: ExecutionReport) -> tuple[float, float]:
    """(total score, max score) — the single number the loop optimises."""
    return report.total_score, report.max_score


# --------------------------------------------------------------------------- #
# Proposer — deterministic, rule-based (v1: no LLM call)
# --------------------------------------------------------------------------- #


def propose_edits(
    report: ExecutionReport,
    current_config: HarnessConfig,
    *,
    max_edits: int = 3,
) -> list[HarnessEdit]:
    """Map an execution report's failure patterns to concrete harness edits.

    One edit per distinct failure type, strongest failures first, capped
    at ``max_edits``. A target already at its bound, or an instruction
    already present in the addendum, produces no edit — the proposer never
    proposes a no-op. The report carries no topics, so no edit can depend
    on an exam case's wording.
    """
    counts = report.failure_counts()
    if not counts:
        return []

    # (failure count, fixed tie-break order, edit) — built, then capped.
    candidates: list[tuple[int, int, HarnessEdit]] = []

    cover_failures = counts.get("cover_title", 0)
    if cover_failures:
        new_font = 140 if current_config.title_font_px < 140 else min(current_config.title_font_px + 22, _MAX_TITLE_FONT_PX)
        if new_font != current_config.title_font_px:
            candidates.append(
                (
                    cover_failures,
                    0,
                    HarnessEdit(
                        target="title_font_px",
                        old_value=current_config.title_font_px,
                        new_value=new_font,
                        reason="Cover title failed the quality gate; a larger, bolder title is needed.",
                        failure_evidence=f"cover_title failed in {cover_failures}/{len(report.cases)} cases",
                    ),
                )
            )

    motion_failures = counts.get("motion", 0)
    if motion_failures:
        new_zoom = 0.0032 if current_config.zoom_step < 0.0032 else min(round(current_config.zoom_step * 2, 4), _MAX_ZOOM_STEP)
        if new_zoom != current_config.zoom_step:
            candidates.append(
                (
                    motion_failures,
                    1,
                    HarnessEdit(
                        target="zoom_step",
                        old_value=current_config.zoom_step,
                        new_value=new_zoom,
                        reason="Reel has no real motion; a stronger slideshow zoom is needed.",
                        failure_evidence=f"motion failed in {motion_failures}/{len(report.cases)} cases",
                    ),
                )
            )

    overlay_failures = counts.get("overlay_text", 0)
    if overlay_failures:
        new_font = min(current_config.overlay_font_px + 12, _MAX_OVERLAY_FONT_PX)
        if new_font != current_config.overlay_font_px:
            candidates.append(
                (
                    overlay_failures,
                    2,
                    HarnessEdit(
                        target="overlay_font_px",
                        old_value=current_config.overlay_font_px,
                        new_value=new_font,
                        reason="Scene overlay text failed the quality gate; larger overlay text is needed.",
                        failure_evidence=f"overlay_text failed in {overlay_failures}/{len(report.cases)} cases",
                    ),
                )
            )

    # Hook / caption failures share one addendum edit (one target, one edit).
    addendum_sentences: list[str] = []
    hook_failures = counts.get("hook", 0)
    caption_failures = counts.get("caption", 0)
    if hook_failures and _HOOK_INSTRUCTION not in current_config.writer_instruction_addendum:
        addendum_sentences.append(_HOOK_INSTRUCTION)
    if caption_failures and _CAPTION_INSTRUCTION not in current_config.writer_instruction_addendum:
        addendum_sentences.append(_CAPTION_INSTRUCTION)
    if addendum_sentences:
        existing = current_config.writer_instruction_addendum.strip()
        combined = " ".join([existing, *addendum_sentences]).strip() if existing else " ".join(addendum_sentences)
        candidates.append(
            (
                hook_failures + caption_failures,
                3,
                HarnessEdit(
                    target="writer_instruction_addendum",
                    old_value=current_config.writer_instruction_addendum,
                    new_value=combined,
                    reason="Hook/caption failed the quality gate; the Writer needs a tighter instruction.",
                    failure_evidence=(
                        f"hook failed in {hook_failures}/{len(report.cases)} cases; "
                        f"caption failed in {caption_failures}/{len(report.cases)} cases"
                    ),
                ),
            )
        )

    # Strongest failures first; cap the number of edits per round.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [edit for _, _, edit in candidates[:max_edits]]


_ALLOWED_EDIT_TARGETS = {
    "title_font_px",
    "rework_title_font_px",
    "zoom_step",
    "rework_zoom_step",
    "scene_font_px",
    "overlay_font_px",
    "writer_instruction_addendum",
}


def apply_edits(config: HarnessConfig, edits: list[HarnessEdit]) -> HarnessConfig:
    """Apply edits to a *copy*: the parent config is frozen and untouched,
    the child is ``version + 1`` with ``parent_version`` set. Unknown
    targets are rejected loudly instead of silently dropped."""
    if not edits:
        raise ValueError("Cannot apply an empty edit list.")
    values = config.to_dict()
    for edit in edits:
        if edit.target not in _ALLOWED_EDIT_TARGETS:
            raise ValueError(f"Unknown harness edit target: {edit.target!r}.")
        values[edit.target] = edit.new_value
    values["version"] = config.version + 1
    values["parent_version"] = config.version
    return HarnessConfig(**values)


# --------------------------------------------------------------------------- #
# The bounded revision loop (pure apart from the caller-supplied evaluator)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RevisionRound:
    """One round of the loop, recorded for the audit trail."""

    round_index: int
    baseline_version: int
    baseline_score: float
    candidate_version: int | None
    candidate_score: float | None
    edits: list[HarnessEdit] = field(default_factory=list)
    kept: bool = False
    max_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "round_index": self.round_index,
            "baseline_version": self.baseline_version,
            "baseline_score": self.baseline_score,
            "candidate_version": self.candidate_version,
            "candidate_score": self.candidate_score,
            "edits": [edit.to_dict() for edit in self.edits],
            "kept": self.kept,
            "max_score": self.max_score,
        }


@dataclass(frozen=True)
class RevisionResult:
    """Outcome of a revision run. ``proposal`` is the last kept candidate
    (or ``None``); it is a *proposal* — it does not become active until a
    human approves it."""

    baseline_config: HarnessConfig
    final_config: HarnessConfig
    proposal: HarnessConfig | None
    rounds: list[RevisionRound] = field(default_factory=list)

    @property
    def improved(self) -> bool:
        return self.proposal is not None

    def to_dict(self) -> dict:
        return {
            "baseline_version": self.baseline_config.version,
            "final_version": self.final_config.version,
            "proposal_version": self.proposal.version if self.proposal else None,
            "improved": self.improved,
            "rounds": [round_.to_dict() for round_ in self.rounds],
        }


async def run_revision_round(
    baseline_config: HarnessConfig,
    scoring_set: ScoringSet,
    evaluate_case: Callable[[ScoringCase, HarnessConfig], Awaitable[CaseResult]],
    *,
    max_rounds: int = 3,
    max_edits: int = 3,
) -> RevisionResult:
    """Evaluate → propose → evaluate candidate → keep only on strict
    improvement. Bounded: at most ``max_rounds`` rounds, one candidate per
    round; a discarded candidate stops the loop. The evaluator is supplied
    by the caller (the orchestrator's Writer + Designer + quality gate in
    production; a fake in tests) — this loop itself touches no DB, no
    network and no LLM beyond what the evaluator does."""
    if max_rounds < 1:
        raise ValueError("max_rounds must be at least 1.")

    async def evaluate_all(config: HarnessConfig) -> ExecutionReport:
        cases = [await evaluate_case(case, config) for case in scoring_set.cases]
        return ExecutionReport(config_version=config.version, cases=cases)

    current = baseline_config
    proposal: HarnessConfig | None = None
    rounds: list[RevisionRound] = []

    for index in range(max_rounds):
        baseline_report = await evaluate_all(current)
        edits = propose_edits(baseline_report, current, max_edits=max_edits)
        if not edits:
            rounds.append(
                RevisionRound(
                    round_index=index,
                    baseline_version=current.version,
                    baseline_score=baseline_report.total_score,
                    candidate_version=None,
                    candidate_score=None,
                    edits=[],
                    kept=False,
                    max_score=baseline_report.max_score,
                )
            )
            break
        candidate = apply_edits(current, edits)
        candidate_report = await evaluate_all(candidate)
        kept = candidate_report.total_score > baseline_report.total_score
        rounds.append(
            RevisionRound(
                round_index=index,
                baseline_version=current.version,
                baseline_score=baseline_report.total_score,
                candidate_version=candidate.version,
                candidate_score=candidate_report.total_score,
                edits=edits,
                kept=kept,
                max_score=baseline_report.max_score,
            )
        )
        if not kept:
            break  # Candidate discarded; do not chase a worse harness.
        proposal = candidate
        current = candidate

    return RevisionResult(
        baseline_config=baseline_config,
        final_config=current,
        proposal=proposal,
        rounds=rounds,
    )


__all__ = [
    "CHECK_WEIGHTS",
    "DEFAULT_HARNESS",
    "DEFAULT_SCORING_SET",
    "CaseResult",
    "ExecutionReport",
    "HarnessConfig",
    "HarnessEdit",
    "RevisionResult",
    "RevisionRound",
    "ScoringCase",
    "ScoringSet",
    "apply_edits",
    "case_result_from_gate",
    "propose_edits",
    "run_revision_round",
    "score_execution_report",
]
