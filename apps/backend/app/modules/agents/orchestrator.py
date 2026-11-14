"""Pipeline orchestrator — drives one :class:`AgentRun` through its stages.

The orchestrator owns the state machine, the retries, the dead letter and
the audit trail. The agents own the work. Invariants:

* A run only moves along :data:`ALLOWED_TRANSITIONS`; anything else raises.
* ``PUBLISHING`` is reachable only from ``AWAITING_APPROVAL`` — a human
  must approve first. The orchestrator never approves on its own.
* A stage that raises is retried on the next beat tick (the orchestrator
  records the attempt and returns the run unfinished — it deliberately does
  NOT re-raise, because the Celery task wrapper rolls the session back on
  exception, which would lose the attempt count). After
  ``MAX_STAGE_ATTEMPTS`` the run is dead-lettered: status FAILED,
  ``result["dead_letter"] = True``, the last error attached, and an
  ``agents.pipeline_dead_letter`` audit row. Nothing is ever silently lost.
* A failed quality gate routes back to ``DESIGNING`` with the reasons, at
  most ``MAX_QUALITY_REWORK`` times; the third failure dead-letters.
* Every AI spend is added to ``AgentRun.spent_cents``; a run with a
  ``budget_cents`` cap fails loudly when it would exceed it.
* All queries go through the caller's :class:`TenantScope`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import IllegalStateTransition, ValidationError
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
    DEFAULT_HARNESS,
    DEFAULT_SCORING_SET,
    HarnessConfig,
    ScoringCase,
    ScoringSet,
    case_result_from_gate,
    run_revision_round,
)
from app.modules.agents.state import (
    MAX_QUALITY_REWORK,
    MAX_STAGE_ATTEMPTS,
    PipelineStage,
    assert_transition,
    is_terminal,
    run_status_for,
)
from app.modules.ai.service import AIContentService
from app.modules.audit.service import AuditService
from app.modules.content.models import AgentRun, AITask, Content, ContentApproval, MediaAsset
from app.modules.instagram.service import InstagramConnectService
from app.modules.publishing.service import PublishingService
from app.modules.storage.backend import StorageBackend, get_storage
from app.modules.tenants.scope import TenantScope


class PipelineOrchestrator:
    def __init__(
        self,
        session: AsyncSession,
        scope: TenantScope,
        *,
        ai_provider,
        meta_client_factory=None,
        storage: StorageBackend | None = None,
        workdir: Path | None = None,
    ) -> None:
        self.session = session
        self.scope = scope
        self.audit = AuditService(session)
        self.ai = AIContentService(session, scope, provider=ai_provider, audit=self.audit)
        self.storage = storage or get_storage()
        self.workdir = workdir or Path("/tmp/instamind-agent-runs")
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.connect = InstagramConnectService(session, scope)
        self.planner = PlannerAgent(self.ai)
        self.writer = WriterAgent(self.ai)
        self.designer = DesignerAgent(self.storage, workdir=self.workdir)
        self.gate = QualityGateAgent()
        self.publisher = PublisherAgent(
            PublishingService(
                session,
                scope,
                connect_service=self.connect,
                client_factory=meta_client_factory,
                audit=self.audit,
            ),
        )
        self.analyst = AnalystAgent(session, scope)
        self.meta_client_factory = meta_client_factory

    # ------------------------------------------------------------------ start
    async def start_run(
        self,
        *,
        topic: str,
        content_type: str = "REELS",
        account_id: uuid.UUID | None = None,
        brand_profile_id: uuid.UUID | None = None,
        base_video_path: Path | None = None,
        budget_cents: int = 0,
        scheduled_for: datetime | None = None,
    ) -> AgentRun:
        if content_type not in {"REELS", "IMAGE"}:
            raise ValidationError(
                f"Unsupported content type for the pipeline: {content_type}. "
                "CAROUSEL rendering is not implemented yet — start REELS or IMAGE runs only.",
                context={"code": "unsupported_content_type"},
            )
        run = AgentRun(
            workspace_id=self.scope.workspace_id,
            instruction=topic,
            planner="PLANNER",
            status=run_status_for(PipelineStage.QUEUED),
            plan={
                "stage": str(PipelineStage.QUEUED),
                "topic": topic,
                "content_type": content_type,
                "account_id": str(account_id) if account_id else None,
                "brand_profile_id": str(brand_profile_id) if brand_profile_id else None,
                "base_video_path": str(base_video_path) if base_video_path else None,
                "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
                "attempts": {},
                "quality_rework": 0,
                "gate_failures": [],
                # Self-revising harness: the active config (versioned) and
                # any pending candidate proposal. A proposal never becomes
                # active on its own — see approve_harness_revision.
                "harness": DEFAULT_HARNESS.to_dict(),
                "harness_proposal": None,
            },
            result={},
            budget_cents=budget_cents,
            spent_cents=0,
            requested_by=self.scope.context.user_id,
        )
        self.session.add(run)
        await self.session.flush()
        await self.audit.record(
            action="agents.run_started",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="agent_run",
            resource_id=str(run.id),
            metadata={"topic": topic, "content_type": content_type},
        )
        return run

    # --------------------------------------------------------------- process
    async def process_run(self, run_id: uuid.UUID) -> AgentRun:
        """Drive a run forward until it waits for a human, publishes, or
        fails. Safe to call repeatedly: a run already at AWAITING_APPROVAL
        or in a terminal stage is returned untouched."""
        run = await self.scope.get_one(AgentRun, run_id)
        stage = PipelineStage(run.plan["stage"])
        if stage in {PipelineStage.AWAITING_APPROVAL, PipelineStage.PUBLISHED, PipelineStage.FAILED, PipelineStage.DEAD, PipelineStage.CANCELLED}:
            return run
        while not is_terminal(stage) and stage is not PipelineStage.AWAITING_APPROVAL:
            stage = await self._run_stage(run, stage)
            await self.session.flush()
        return run

    async def _run_stage(self, run: AgentRun, stage: PipelineStage) -> PipelineStage:
        attempts = run.plan.get("attempts", {})
        try:
            if stage is PipelineStage.QUEUED:
                return await self._transition(run, PipelineStage.PLANNING)
            if stage is PipelineStage.PLANNING:
                await self._do_planning(run)
                return await self._transition(run, PipelineStage.WRITING)
            if stage is PipelineStage.WRITING:
                await self._do_writing(run)
                return await self._transition(run, PipelineStage.DESIGNING)
            if stage is PipelineStage.DESIGNING:
                await self._do_designing(run)
                return await self._transition(run, PipelineStage.QUALITY_GATE)
            if stage is PipelineStage.QUALITY_GATE:
                return await self._do_quality_gate(run)
            if stage is PipelineStage.PUBLISHING:
                await self._do_publishing(run)
                return await self._transition(run, PipelineStage.PUBLISHED)
        except Exception as exc:  # noqa: BLE001 - counted, then dead-lettered or left for the next tick
            attempts[str(stage)] = int(attempts.get(str(stage), 0)) + 1
            # Reassign the whole JSON column: nested in-place mutation is not
            # tracked by SQLAlchemy on a plain JSON column.
            run.plan = {**run.plan, "attempts": attempts}
            run.result = {**run.result, "last_error": str(exc)[:500]}
            await self.session.flush()
            if attempts[str(stage)] >= MAX_STAGE_ATTEMPTS:
                await self._dead_letter(run, stage, exc)
                return PipelineStage.DEAD
            # Do NOT re-raise: the Celery task wrapper rolls the session back
            # on exception, which would lose the attempt count. The run stays
            # unfinished and the next beat tick retries it; the attempt bound
            # above is what dead-letters it.
            return stage
        raise IllegalStateTransition(f"No executor for pipeline stage {stage}.")

    async def _transition(self, run: AgentRun, to_stage: PipelineStage) -> PipelineStage:
        from_stage = PipelineStage(run.plan["stage"])
        assert_transition(from_stage, to_stage)
        run.plan = {**run.plan, "stage": str(to_stage)}
        run.status = run_status_for(to_stage)
        self.scope.touch(run)
        await self.session.flush()
        await self.audit.record(
            action="agents.stage_transition",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="agent_run",
            resource_id=str(run.id),
            metadata={"from": str(from_stage), "to": str(to_stage)},
        )
        return to_stage

    async def _dead_letter(self, run: AgentRun, stage: PipelineStage, exc: Exception) -> None:
        run.plan = {**run.plan, "stage": str(PipelineStage.DEAD)}
        run.status = run_status_for(PipelineStage.DEAD)
        run.result = {
            **run.result,
            "dead_letter": True,
            "failed_stage": str(stage),
            "error": str(exc)[:500],
        }
        self.scope.touch(run)
        await self.session.flush()
        await self.audit.record(
            action="agents.pipeline_dead_letter",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="agent_run",
            resource_id=str(run.id),
            metadata={"failed_stage": str(stage), "error": str(exc)[:500]},
        )

    async def _meta_client_for_account(self, account_id: uuid.UUID):
        """A Meta client authenticated as this account: refresh the token if
        it is inside the safety margin, then decrypt the active token. The
        factory is called with the token, exactly like the publishing worker."""
        from app.modules.instagram.models import SocialAccount

        if self.meta_client_factory is None:
            raise ValidationError(
                "No Meta client factory is configured; the Publisher cannot verify a live post.",
                context={"code": "meta_client_missing"},
            )
        account = await self.scope.get_one(SocialAccount, account_id)
        await self.connect.refresh_if_needed(account)
        token = await self.connect.get_active_token(account)
        return self.meta_client_factory(access_token=token)

    # ------------------------------------------------------------ stage work
    def _context(self, run: AgentRun) -> AgentContext:
        plan = run.plan
        return AgentContext(
            run_id=run.id,
            workspace_id=self.scope.workspace_id,
            topic=str(plan.get("topic") or run.instruction),
            content_type=str(plan.get("content_type") or "REELS"),
            account_id=uuid.UUID(plan["account_id"]) if plan.get("account_id") else None,
            brand_profile_id=uuid.UUID(plan["brand_profile_id"]) if plan.get("brand_profile_id") else None,
            base_video_path=Path(plan["base_video_path"]) if plan.get("base_video_path") else None,
            workdir=self.workdir,
            banned_words=list(plan.get("banned_words") or []),
            analyst_recommendations=list((run.result or {}).get("analyst", {}).get("recommendations") or []),
        )

    async def _record_task(self, run: AgentRun, *, kind: str, status: str, output: dict | None = None, error: str | None = None) -> None:
        task = AITask(
            workspace_id=self.scope.workspace_id,
            agent_run_id=run.id,
            kind=kind,
            status=status,
            input_payload={"stage": run.plan["stage"]},
            output_payload=output or {},
            error=error,
        )
        self.session.add(task)
        await self.session.flush()

    def _check_budget(self, run: AgentRun) -> None:
        if run.budget_cents and run.spent_cents >= run.budget_cents:
            raise ValidationError(
                f"Run budget exhausted: spent {run.spent_cents}c of {run.budget_cents}c.",
                context={"code": "run_budget_exceeded"},
            )

    async def _do_planning(self, run: AgentRun) -> None:
        self._check_budget(run)
        ctx = self._context(run)
        plan = await self.planner.plan(ctx)
        run.plan = {**run.plan, "plan": plan}
        run.spent_cents += int(plan.get("cost_cents") or 0)
        await self._record_task(run, kind="planning", status="SUCCEEDED", output=plan)

    @staticmethod
    def _active_harness(run: AgentRun) -> HarnessConfig:
        """The run's active harness config (defaults to the shipped
        behaviour when a run predates the harness field)."""
        return HarnessConfig.from_dict((run.plan or {}).get("harness") or {})

    async def _do_writing(self, run: AgentRun) -> None:
        self._check_budget(run)
        ctx = self._context(run)
        idea = str((run.plan.get("plan") or {}).get("idea") or ctx.topic)
        harness = self._active_harness(run)
        script = await self.writer.write(
            ctx, idea=idea, instruction_addendum=harness.writer_instruction_addendum or None
        )
        run.plan = {**run.plan, "script": script}
        run.spent_cents += int(script.get("cost_cents") or 0)
        await self._record_task(run, kind="writing", status="SUCCEEDED", output=script)

    async def _do_designing(self, run: AgentRun) -> None:
        ctx = self._context(run)
        script = run.plan.get("script") or {}
        gate_failures = list(run.plan.get("gate_failures") or [])
        designed = await self.designer.design(
            ctx,
            script=script,
            gate_failures=gate_failures,
            store_media=self._store_media,
            harness_config=self._active_harness(run),
        )
        caption = str(script.get("caption") or "")
        hashtags = list(script.get("hashtags") or [])
        # Only the publishable asset goes into media_asset_ids: for a reel
        # that is the video — the cover travels separately as cover_asset_id
        # (the publishing service uses assets[0] as the reel video and
        # cover_asset_id for the cover_url; putting the cover image in the
        # media list would fail validation and publish the wrong asset).
        if ctx.content_type == "REELS":
            video_ids = [asset_id for asset_id in designed.media_asset_ids if asset_id != designed.cover_asset_id]
            publish_asset_ids = video_ids
        else:
            publish_asset_ids = designed.media_asset_ids
        content = Content(
            workspace_id=self.scope.workspace_id,
            brand_profile_id=ctx.brand_profile_id,
            title=ctx.topic[:200],
            caption=caption,
            hashtags=hashtags,
            content_type=ctx.content_type,
            media_asset_ids=publish_asset_ids,
            media_order=publish_asset_ids,
            cover_asset_id=uuid.UUID(designed.cover_asset_id) if designed.cover_asset_id and ctx.content_type == "REELS" else None,
            status="DRAFT",
            scheduled_at=datetime.fromisoformat(run.plan["scheduled_for"]) if run.plan.get("scheduled_for") else None,
            ai_generated=True,
        )
        self.session.add(content)
        await self.session.flush()
        run.plan = {
            **run.plan,
            "content_id": str(content.id),
            "local_paths": {
                "cover": str(designed.cover_path),
                "video": str(designed.video_path) if designed.video_path else None,
                "overlays": [str(path) for path in designed.overlay_paths],
            },
            "title_font_px": designed.title_font_px,
            "motion_source": designed.motion_source,
        }
        await self._record_task(
            run, kind="designing", status="SUCCEEDED",
            output={"content_id": str(content.id), "media_asset_ids": designed.media_asset_ids},
        )

    async def _store_media(self, **fields) -> MediaAsset:
        asset = MediaAsset(
            workspace_id=self.scope.workspace_id,
            filename=Path(fields["storage_key"]).name,
            content_type=fields["detected_mime"],
            **{key: fields[key] for key in (
                "kind", "storage_backend", "storage_key", "bucket", "detected_mime",
                "size_bytes", "checksum_sha256", "width", "height", "duration_ms",
            ) if key in fields},
        )
        asset.public_url = await self.storage.public_url(fields["storage_key"])
        self.session.add(asset)
        await self.session.flush()
        return asset

    async def _do_quality_gate(self, run: AgentRun) -> PipelineStage:
        script = run.plan.get("script") or {}
        paths = run.plan.get("local_paths") or {}
        cover = Path(paths["cover"]) if paths.get("cover") else None
        overlays = [Path(path) for path in (paths.get("overlays") or [])]
        video = Path(paths["video"]) if paths.get("video") else None
        report = self.gate.check(
            script=script,
            cover_path=cover,
            overlay_paths=overlays,
            video_path=video,
            caption=str(script.get("caption") or ""),
            banned_words=list(run.plan.get("banned_words") or []),
            title_font_px=int(run.plan.get("title_font_px") or 118),
        )
        if report.passed:
            await self._record_task(run, kind="quality_gate", status="SUCCEEDED", output={"passed": True})
            return await self._transition(run, PipelineStage.AWAITING_APPROVAL)
        rework = int(run.plan.get("quality_rework") or 0) + 1
        run.plan = {
            **run.plan,
            "quality_rework": rework,
            "gate_failures": report.failure_reasons(),
        }
        await self._record_task(
            run, kind="quality_gate", status="FAILED",
            output={"failures": report.failure_reasons(), "rework": rework},
        )
        await self.audit.record(
            action="agents.quality_gate_failed",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="agent_run",
            resource_id=str(run.id),
            metadata={"failures": report.failure_reasons(), "rework": rework},
        )
        if rework > MAX_QUALITY_REWORK:
            await self._dead_letter(
                run, PipelineStage.QUALITY_GATE,
                ValidationError(f"Quality gate failed {rework} times: {'; '.join(report.failure_reasons())}"),
            )
            return PipelineStage.DEAD
        return await self._transition(run, PipelineStage.DESIGNING)

    async def _do_publishing(self, run: AgentRun) -> None:
        ctx = self._context(run)
        if ctx.account_id is None:
            raise ValidationError("No social account was given for this run.", context={"code": "account_required"})
        content_id = run.plan.get("content_id")
        if not content_id:
            raise ValidationError("This run has no content to publish.", context={"code": "content_missing"})
        verify_client = await self._meta_client_for_account(ctx.account_id)
        result = await self.publisher.publish(
            content_id=uuid.UUID(content_id),
            account_id=ctx.account_id,
            run_id=run.id,
            verify_client=verify_client,
        )
        run.result = {**run.result, "publish": result}
        await self._record_task(run, kind="publishing", status="SUCCEEDED", output=result)

    # -------------------------------------------------------------- approval
    async def approve(self, run_id: uuid.UUID, *, reviewer_id: uuid.UUID | None = None, comment: str = "") -> AgentRun:
        """Human approval. Records a ContentApproval row for the run's content
        and moves the run to PUBLISHING. Only legal from AWAITING_APPROVAL."""
        run = await self.scope.get_one(AgentRun, run_id)
        if PipelineStage(run.plan["stage"]) is not PipelineStage.AWAITING_APPROVAL:
            raise IllegalStateTransition(
                f"Only runs awaiting approval can be approved (current stage: {run.plan['stage']})."
            )
        content_id = run.plan.get("content_id")
        if content_id:
            approval = ContentApproval(
                workspace_id=self.scope.workspace_id,
                content_id=uuid.UUID(content_id),
                requested_by=run.requested_by,
                reviewer_id=reviewer_id,
                status="APPROVED",
                comment=comment,
                decided_at=datetime.now(UTC),
            )
            self.session.add(approval)
            content = await self.scope.get_one(Content, uuid.UUID(content_id))
            content.status = "APPROVED"
            self.scope.touch(content)
        await self._transition(run, PipelineStage.PUBLISHING)
        await self.audit.record(
            action="agents.run_approved",
            workspace_id=self.scope.workspace_id,
            actor_id=reviewer_id,
            resource_type="agent_run",
            resource_id=str(run.id),
            metadata={"comment": comment},
        )
        return run

    async def reject(self, run_id: uuid.UUID, *, reviewer_id: uuid.UUID | None = None, comment: str = "") -> AgentRun:
        """Human rejection. The run fails with the reason attached; the
        content is marked FAILED, never published."""
        run = await self.scope.get_one(AgentRun, run_id)
        if PipelineStage(run.plan["stage"]) is not PipelineStage.AWAITING_APPROVAL:
            raise IllegalStateTransition(
                f"Only runs awaiting approval can be rejected (current stage: {run.plan['stage']})."
            )
        content_id = run.plan.get("content_id")
        if content_id:
            approval = ContentApproval(
                workspace_id=self.scope.workspace_id,
                content_id=uuid.UUID(content_id),
                requested_by=run.requested_by,
                reviewer_id=reviewer_id,
                status="REJECTED",
                comment=comment,
                decided_at=datetime.now(UTC),
            )
            self.session.add(approval)
            content = await self.scope.get_one(Content, uuid.UUID(content_id))
            content.status = "FAILED"
            content.failure_reason = comment or "Rejected by reviewer."
            self.scope.touch(content)
        await self._transition(run, PipelineStage.FAILED)
        run.result = {**run.result, "error": comment or "Rejected by reviewer."}
        await self.session.flush()
        await self.audit.record(
            action="agents.run_rejected",
            workspace_id=self.scope.workspace_id,
            actor_id=reviewer_id,
            resource_type="agent_run",
            resource_id=str(run.id),
            metadata={"comment": comment},
        )
        return run

    async def cancel(self, run_id: uuid.UUID) -> AgentRun:
        run = await self.scope.get_one(AgentRun, run_id)
        stage = PipelineStage(run.plan["stage"])
        if is_terminal(stage):
            raise IllegalStateTransition(f"Run is already {stage}; it cannot be cancelled.")
        await self._transition(run, PipelineStage.CANCELLED)
        await self.audit.record(
            action="agents.run_cancelled",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="agent_run",
            resource_id=str(run.id),
        )
        return run

    # ------------------------------------------------- self-revising harness
    async def _evaluate_harness_case(
        self, run: AgentRun, case: ScoringCase, config: HarnessConfig
    ):
        """Grade one exam case under one harness config: Writer (frozen
        prompts — the same AI service the pipeline always uses) → Designer
        with the candidate config → the existing quality gate on the real
        rendered media. The case topic is used here, inside the evaluator,
        and never reaches the proposer: only the returned CaseResult
        (case id + passed/failed check names) is reported upstream."""
        ctx = self._context(run)
        ctx = AgentContext(
            run_id=ctx.run_id,
            workspace_id=ctx.workspace_id,
            topic=case.topic,
            content_type=case.content_type,
            account_id=ctx.account_id,
            brand_profile_id=ctx.brand_profile_id,
            workdir=ctx.workdir,
            banned_words=[],
            analyst_recommendations=[],
        )
        script = await self.writer.write(
            ctx, idea=case.topic, instruction_addendum=config.writer_instruction_addendum or None
        )
        designed = await self.designer.design(
            ctx, script=script, gate_failures=[], store_media=self._store_media, harness_config=config
        )
        report = self.gate.check(
            script=script,
            cover_path=designed.cover_path,
            overlay_paths=designed.overlay_paths,
            video_path=designed.video_path,
            caption=str(script.get("caption") or ""),
            banned_words=[],
            title_font_px=designed.title_font_px,
        )
        return case_result_from_gate(
            case.id,
            failed_checks=[failure.check for failure in report.failures],
            has_cover=True,
            has_video=designed.video_path is not None,
            has_overlays=bool(designed.overlay_paths),
        )

    async def run_harness_revision_round(
        self,
        run_id: uuid.UUID,
        *,
        scoring_set: ScoringSet | None = None,
        max_rounds: int = 3,
        base_config: HarnessConfig | None = None,
    ) -> AgentRun:
        """Run the bounded self-revision loop over the held-out scoring
        set and record it on the run. The active harness is NOT changed
        here: a kept candidate is stored as ``plan["harness_proposal"]``
        and needs explicit human approval (``approve_harness_revision``).
        Every round is recorded in ``result["harness_revision"]`` and as
        an ``AITask`` + audit row. This never touches publishing or the
        content-approval gate."""
        run = await self.scope.get_one(AgentRun, run_id)
        scoring_set = scoring_set or DEFAULT_SCORING_SET
        base = base_config or self._active_harness(run)

        async def evaluate_case(case: ScoringCase, config: HarnessConfig):
            return await self._evaluate_harness_case(run, case, config)

        result = await run_revision_round(base, scoring_set, evaluate_case, max_rounds=max_rounds)
        if result.proposal is not None:
            run.plan = {**run.plan, "harness_proposal": result.proposal.to_dict()}
        run.result = {**run.result, "harness_revision": result.to_dict()}
        await self.session.flush()
        await self._record_task(
            run, kind="harness_revision", status="SUCCEEDED", output=result.to_dict()
        )
        await self.audit.record(
            action="agents.harness_revision_round",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="agent_run",
            resource_id=str(run.id),
            metadata={
                "scoring_set": scoring_set.name,
                "improved": result.improved,
                "proposal_version": result.proposal.version if result.proposal else None,
            },
        )
        return run

    async def approve_harness_revision(
        self, run_id: uuid.UUID, *, reviewer_id: uuid.UUID | None = None
    ) -> AgentRun:
        """Human approval of a pending harness proposal: the proposal
        becomes the run's active harness and the pending slot clears.
        Raises :class:`IllegalStateTransition` when no proposal is
        pending. This approves a *harness config only* — it does not
        approve any content for publishing."""
        run = await self.scope.get_one(AgentRun, run_id)
        proposal = (run.plan or {}).get("harness_proposal")
        if not proposal:
            raise IllegalStateTransition(
                "No harness revision proposal is pending for this run.",
                context={"code": "no_harness_proposal"},
            )
        run.plan = {**run.plan, "harness": proposal, "harness_proposal": None}
        revision = dict((run.result or {}).get("harness_revision") or {})
        revision["approved"] = {
            "version": proposal.get("version"),
            "reviewer_id": str(reviewer_id) if reviewer_id else None,
        }
        run.result = {**run.result, "harness_revision": revision}
        await self.session.flush()
        await self._record_task(
            run,
            kind="harness_revision",
            status="SUCCEEDED",
            output={"approved_version": proposal.get("version")},
        )
        await self.audit.record(
            action="agents.harness_revision_approved",
            workspace_id=self.scope.workspace_id,
            actor_id=reviewer_id,
            resource_type="agent_run",
            resource_id=str(run.id),
            metadata={"version": proposal.get("version")},
        )
        return run

    async def reject_harness_revision(
        self, run_id: uuid.UUID, *, reviewer_id: uuid.UUID | None = None
    ) -> AgentRun:
        """Human rejection of a pending harness proposal: the proposal is
        discarded and the active harness stays exactly as it was. Raises
        :class:`IllegalStateTransition` when no proposal is pending."""
        run = await self.scope.get_one(AgentRun, run_id)
        proposal = (run.plan or {}).get("harness_proposal")
        if not proposal:
            raise IllegalStateTransition(
                "No harness revision proposal is pending for this run.",
                context={"code": "no_harness_proposal"},
            )
        run.plan = {**run.plan, "harness_proposal": None}
        revision = dict((run.result or {}).get("harness_revision") or {})
        revision["rejected"] = {
            "version": proposal.get("version"),
            "reviewer_id": str(reviewer_id) if reviewer_id else None,
        }
        run.result = {**run.result, "harness_revision": revision}
        await self.session.flush()
        await self._record_task(
            run,
            kind="harness_revision",
            status="SUCCEEDED",
            output={"rejected_version": proposal.get("version")},
        )
        await self.audit.record(
            action="agents.harness_revision_rejected",
            workspace_id=self.scope.workspace_id,
            actor_id=reviewer_id,
            resource_type="agent_run",
            resource_id=str(run.id),
            metadata={"version": proposal.get("version")},
        )
        return run

    # --------------------------------------------------------------- analyst
    async def analyse_run(self, run_id: uuid.UUID) -> AgentRun:
        """Analyst pass for a published run: pull Insights, store snapshots
        (NULL when Meta returns nothing), compute recommendations, and store
        them on the run for the Planner's next cycle."""
        run = await self.scope.get_one(AgentRun, run_id)
        if PipelineStage(run.plan["stage"]) is not PipelineStage.PUBLISHED:
            raise IllegalStateTransition(
                f"Only published runs can be analysed (current stage: {run.plan['stage']})."
            )
        publish = (run.result or {}).get("publish") or {}
        media_id = publish.get("media_id")
        ctx = self._context(run)
        if not media_id or ctx.account_id is None:
            raise ValidationError(
                "Analyst needs a published media id and an account.",
                context={"code": "analyst_prerequisites_missing"},
            )
        client = await self._meta_client_for_account(ctx.account_id)
        until = datetime.now(UTC)
        since = until - timedelta(days=1)
        report = await self.analyst.analyse(
            client=client,
            media_id=str(media_id),
            account_id=ctx.account_id,
            content_type=ctx.content_type,
            since=since,
            until=until,
        )
        run.result = {
            **run.result,
            "analyst": {
                "snapshots": report.snapshots,
                "recommendations": report.recommendations,
                "missing_metrics": report.missing_metrics,
                "analysed_at": until.isoformat(),
            },
        }
        self.scope.touch(run)
        await self.session.flush()
        await self.audit.record(
            action="agents.run_analysed",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="agent_run",
            resource_id=str(run.id),
            metadata={"recommendations": len(report.recommendations), "missing_metrics": report.missing_metrics},
        )
        return run


__all__ = ["PipelineOrchestrator"]
