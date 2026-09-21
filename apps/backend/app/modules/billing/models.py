"""Billing & subscription models (M1 scope: plans, subscriptions, usage).

Invoices/payments providers (Stripe, Zarinpal) are wired in a later milestone;
the tables below are already shaped for them so no migration rewrite is needed.
No card data is ever stored — only provider references.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, Model, TenantModel


class Plan(Model):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="IRR")
    price_monthly: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_yearly: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    interval: Mapped[str] = mapped_column(String(16), nullable=False, default="MONTH")
    limits: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # {"social_accounts": 1, "members": 1, "scheduled_posts_month": 30,
    #  "ai_credits_month": 100, "ai_budget_cents_month": 500}
    features: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_trial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class Subscription(TenantModel):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_ws_status", "workspace_id", "status"),)

    plan_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    plan_code: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    provider_subscription_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="TRIALING")
    # TRIALING | ACTIVE | PAST_DUE | CANCELLED | EXPIRED
    interval: Mapped[str] = mapped_column(String(16), nullable=False, default="MONTH")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coupon_code: Mapped[str | None] = mapped_column(String(64))


class Coupon(Model):
    __tablename__ = "coupons"

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="PERCENT")
    # PERCENT | AMOUNT
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="IRR")
    max_redemptions: Mapped[int | None] = mapped_column(Integer)
    redeemed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applies_to_plans: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UsageRecord(TenantModel):
    """Metered consumption used to enforce plan limits.

    One row per (metric, period) so counters are a single ``UPDATE`` and never
    drift from concurrent inserts.
    """

    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "metric", "period_start", name="uq_usage_records_ws_metric_period"
        ),
    )

    metric: Mapped[str] = mapped_column(String(48), nullable=False)
    # published_posts | ai_generations | ai_cost_cents | members | social_accounts
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limit_value: Mapped[int | None] = mapped_column(Integer)


class Invoice(TenantModel):
    __tablename__ = "invoices"

    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    number: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="IRR")
    subtotal: Mapped[int] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    discount: Mapped[int] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    total: Mapped[int] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="DRAFT")
    # DRAFT | OPEN | PAID | VOID | UNCOLLECTIBLE
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    line_items: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Payment(TenantModel):
    """Provider reference only. No card numbers, no CVV, no PAN — ever."""

    __tablename__ = "payments"

    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_payment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="IRR")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    # PENDING | SUCCEEDED | FAILED | REFUNDED
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
