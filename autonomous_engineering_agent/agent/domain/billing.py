"""Domain rules for plans, run caps, and subscription state."""

from __future__ import annotations

from dataclasses import dataclass

SUBSCRIPTION_STATUSES = frozenset({"trialing", "active", "past_due", "canceled"})
ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"trialing", "active"})


@dataclass(frozen=True, slots=True)
class PlanLimits:
    name: str
    monthly_run_cap: int
    monthly_spend_cap_usd: float | None = None


PLANS: dict[str, PlanLimits] = {
    "free": PlanLimits("free", monthly_run_cap=5),
    "starter": PlanLimits("starter", monthly_run_cap=50),
    "pro": PlanLimits("pro", monthly_run_cap=250, monthly_spend_cap_usd=100.0),
}


@dataclass(frozen=True, slots=True)
class LimitDecision:
    allowed: bool
    reason: str
    plan: str
    runs_used: int
    run_cap: int


def normalize_subscription_status(raw: str) -> str:
    status = raw.strip().lower()
    if status in SUBSCRIPTION_STATUSES:
        return status
    if status in {"incomplete", "incomplete_expired", "unpaid"}:
        return "past_due"
    return "canceled"


def effective_plan(subscription_status: str | None, plan_name: str | None) -> PlanLimits:
    """Paid plans only count while the subscription is trialing or active."""
    if plan_name and subscription_status in ACTIVE_SUBSCRIPTION_STATUSES:
        return PLANS.get(plan_name, PLANS["free"])
    return PLANS["free"]


def check_run_allowed(plan: PlanLimits, *, runs_used: int, spend_used_usd: float = 0.0) -> LimitDecision:
    if runs_used >= plan.monthly_run_cap:
        return LimitDecision(
            allowed=False,
            reason=f"monthly run cap reached ({runs_used}/{plan.monthly_run_cap} on the {plan.name} plan)",
            plan=plan.name,
            runs_used=runs_used,
            run_cap=plan.monthly_run_cap,
        )
    if plan.monthly_spend_cap_usd is not None and spend_used_usd >= plan.monthly_spend_cap_usd:
        return LimitDecision(
            allowed=False,
            reason=f"monthly spend cap reached (${spend_used_usd:.2f} of ${plan.monthly_spend_cap_usd:.2f})",
            plan=plan.name,
            runs_used=runs_used,
            run_cap=plan.monthly_run_cap,
        )
    return LimitDecision(
        allowed=True, reason="ok", plan=plan.name, runs_used=runs_used, run_cap=plan.monthly_run_cap
    )
