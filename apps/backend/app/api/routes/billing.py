"""Billing endpoints (M1: plans + usage; payments land with the billing milestone)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import DBSession, TenantScopeDep, require
from app.api.schemas import PlanRead, SubscriptionRead, UsageRead
from app.core.permissions import Permission
from app.modules.billing.models import Plan, Subscription, UsageRecord

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans", response_model=list[PlanRead])
async def list_plans(session: DBSession) -> list[PlanRead]:
    rows = (
        await session.execute(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order.asc())
        )
    ).scalars().all()
    return [PlanRead.model_validate(row) for row in rows]


@router.get(
    "/subscription",
    response_model=SubscriptionRead | None,
    dependencies=[Depends(require(Permission.BILLING_READ))],
)
async def current_subscription(scope: TenantScopeDep) -> SubscriptionRead | None:
    rows = await scope.list(Subscription, order_by=Subscription.created_at.desc(), limit=1)
    return SubscriptionRead.model_validate(rows[0]) if rows else None


@router.get(
    "/usage",
    response_model=list[UsageRead],
    dependencies=[Depends(require(Permission.BILLING_READ))],
)
async def usage(scope: TenantScopeDep) -> list[UsageRead]:
    rows = await scope.list(UsageRecord, order_by=UsageRecord.period_start.desc(), limit=100)
    return [
        UsageRead(
            metric=row.metric,
            used=int(row.quantity or 0),
            limit=row.limit_value,
            period_start=row.period_start,
            period_end=row.period_end,
        )
        for row in rows
    ]
