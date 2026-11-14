"""Agentic content-pipeline endpoints.

A run is started here and then driven by the Celery worker (the beat task
``instamind.agents.process_due_runs`` picks up unfinished runs every 60 s).
Publishing never happens from these endpoints directly: a run only reaches
``PUBLISHING`` after a human approves it here, and the worker publishes it
through the official Meta API publishing service.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import DBSession, TenantScopeDep, require
from app.core.permissions import Permission
from app.modules.agents.orchestrator import PipelineOrchestrator
from app.modules.content.models import AgentRun
from app.providers.ai.openai_provider import get_ai_provider
from app.providers.meta.client import MetaGraphClient

router = APIRouter(prefix="/agents", tags=["agents"])


def _orchestrator(session: DBSession, scope: TenantScopeDep) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        session,
        scope,
        ai_provider=get_ai_provider(),
        meta_client_factory=lambda access_token: MetaGraphClient(access_token=access_token),
        workdir=Path("/tmp/instamind-agent-runs"),
    )


class StartRunRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=500)
    content_type: str = "REELS"
    account_id: uuid.UUID | None = None
    brand_profile_id: uuid.UUID | None = None
    base_video_path: str | None = None
    budget_cents: int = Field(default=0, ge=0)
    scheduled_for: datetime | None = None


class RunResponse(BaseModel):
    id: uuid.UUID
    stage: str
    status: str
    topic: str
    content_type: str
    spent_cents: int
    result: dict


class ApproveRequest(BaseModel):
    comment: str = ""


def _to_response(run: AgentRun) -> RunResponse:
    plan = run.plan or {}
    return RunResponse(
        id=run.id,
        stage=str(plan.get("stage") or ""),
        status=run.status,
        topic=str(plan.get("topic") or run.instruction),
        content_type=str(plan.get("content_type") or ""),
        spent_cents=run.spent_cents,
        result=run.result or {},
    )


@router.post(
    "/runs",
    response_model=RunResponse,
    status_code=201,
    dependencies=[Depends(require(Permission.AI_GENERATE))],
)
async def start_run(payload: StartRunRequest, session: DBSession, scope: TenantScopeDep) -> RunResponse:
    orchestrator = _orchestrator(session, scope)
    run = await orchestrator.start_run(
        topic=payload.topic,
        content_type=payload.content_type,
        account_id=payload.account_id,
        brand_profile_id=payload.brand_profile_id,
        base_video_path=Path(payload.base_video_path) if payload.base_video_path else None,
        budget_cents=payload.budget_cents,
        scheduled_for=payload.scheduled_for,
    )
    await session.commit()
    return _to_response(run)


@router.get(
    "/runs/{run_id}",
    response_model=RunResponse,
    dependencies=[Depends(require(Permission.CONTENT_READ))],
)
async def get_run(run_id: uuid.UUID, session: DBSession, scope: TenantScopeDep) -> RunResponse:
    run = await scope.get_one(AgentRun, run_id)
    return _to_response(run)


@router.post(
    "/runs/{run_id}/approve",
    response_model=RunResponse,
    dependencies=[Depends(require(Permission.CONTENT_APPROVE))],
)
async def approve_run(
    run_id: uuid.UUID, payload: ApproveRequest, session: DBSession, scope: TenantScopeDep
) -> RunResponse:
    orchestrator = _orchestrator(session, scope)
    run = await orchestrator.approve(run_id, reviewer_id=scope.context.user_id, comment=payload.comment)
    await session.commit()
    return _to_response(run)


@router.post(
    "/runs/{run_id}/reject",
    response_model=RunResponse,
    dependencies=[Depends(require(Permission.CONTENT_APPROVE))],
)
async def reject_run(
    run_id: uuid.UUID, payload: ApproveRequest, session: DBSession, scope: TenantScopeDep
) -> RunResponse:
    orchestrator = _orchestrator(session, scope)
    run = await orchestrator.reject(run_id, reviewer_id=scope.context.user_id, comment=payload.comment)
    await session.commit()
    return _to_response(run)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunResponse,
    dependencies=[Depends(require(Permission.CONTENT_CANCEL))],
)
async def cancel_run(run_id: uuid.UUID, session: DBSession, scope: TenantScopeDep) -> RunResponse:
    orchestrator = _orchestrator(session, scope)
    run = await orchestrator.cancel(run_id)
    await session.commit()
    return _to_response(run)


@router.get(
    "/runs/{run_id}/stage",
    dependencies=[Depends(require(Permission.CONTENT_READ))],
)
async def run_stage(run_id: uuid.UUID, session: DBSession, scope: TenantScopeDep) -> dict:
    """Current stage + the plan the Planner/Writer produced so far (read-only)."""
    run = await scope.get_one(AgentRun, run_id)
    plan = run.plan or {}
    return {
        "run_id": str(run.id),
        "stage": str(plan.get("stage") or ""),
        "status": run.status,
        "plan": {key: plan[key] for key in ("topic", "content_type", "plan", "script") if key in plan},
        "gate_failures": plan.get("gate_failures") or [],
        "quality_rework": plan.get("quality_rework") or 0,
        "attempts": plan.get("attempts") or {},
    }


__all__ = ["router"]
