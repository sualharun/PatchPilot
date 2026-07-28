"""Use cases for billing: plan resolution, run limits, and Stripe webhook state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agent.domain.billing import (
    PLANS,
    LimitDecision,
    check_run_allowed,
    effective_plan,
    normalize_subscription_status,
)


class BillingRepository(Protocol):
    def upsert_stripe_customer(self, *, workspace_id: int, stripe_customer_id: str, email: str | None) -> int: ...

    def stripe_customer_for_workspace(self, workspace_id: int) -> dict[str, Any] | None: ...

    def workspace_for_stripe_customer(self, stripe_customer_id: str) -> int | None: ...

    def upsert_subscription(
        self,
        *,
        workspace_id: int,
        stripe_subscription_id: str,
        stripe_price_id: str | None,
        plan: str,
        status: str,
        current_period_end: str | None = None,
    ) -> int: ...

    def subscription_for_workspace(self, workspace_id: int) -> dict[str, Any] | None: ...

    def set_workspace_limits(
        self,
        *,
        workspace_id: int,
        plan: str,
        monthly_run_cap: int | None = None,
        monthly_spend_cap_usd: float | None = None,
    ) -> None: ...

    def get_workspace_limits(self, workspace_id: int) -> dict[str, Any] | None: ...

    def add_usage(
        self,
        *,
        workspace_id: int | None,
        run_id: int | None,
        kind: str = "run",
        amount: int = 1,
        cost_usd: float | None = None,
    ) -> int: ...

    def usage_this_month(self, workspace_id: int | None) -> dict[str, Any]: ...


class BillingService:
    def __init__(self, billing: BillingRepository) -> None:
        self._billing = billing

    def check_run_allowed(self, workspace_id: int | None) -> LimitDecision:
        usage = self._billing.usage_this_month(workspace_id)
        plan = self.plan_for_workspace(workspace_id)
        return check_run_allowed(
            plan, runs_used=int(usage["runs"]), spend_used_usd=float(usage["cost_usd"])
        )

    def record_run(self, workspace_id: int | None, run_id: int | None) -> None:
        self._billing.add_usage(workspace_id=workspace_id, run_id=run_id, kind="run")

    def plan_for_workspace(self, workspace_id: int | None):
        if workspace_id is None:
            return PLANS["free"]
        limits = self._billing.get_workspace_limits(workspace_id)
        subscription = self._billing.subscription_for_workspace(workspace_id)
        plan = effective_plan(
            (subscription or {}).get("status"),
            (limits or {}).get("plan") or (subscription or {}).get("plan"),
        )
        return plan

    def stripe_customer(self, workspace_id: int | None) -> dict[str, Any] | None:
        if workspace_id is None:
            return None
        return self._billing.stripe_customer_for_workspace(workspace_id)

    def overview(self, workspace_id: int | None) -> dict[str, Any]:
        plan = self.plan_for_workspace(workspace_id)
        usage = self._billing.usage_this_month(workspace_id)
        subscription = (
            self._billing.subscription_for_workspace(workspace_id) if workspace_id is not None else None
        )
        return {
            "plan": plan.name,
            "monthly_run_cap": plan.monthly_run_cap,
            "monthly_spend_cap_usd": plan.monthly_spend_cap_usd,
            "runs_this_month": usage["runs"],
            "spend_this_month_usd": usage["cost_usd"],
            "subscription_status": (subscription or {}).get("status") or "none",
        }


@dataclass(frozen=True, slots=True)
class StripeWebhookSettings:
    price_id_starter: str | None = None
    price_id_pro: str | None = None


class HandleStripeWebhookHandler:
    """Track subscription state from Stripe events and keep workspace limits in sync."""

    def __init__(self, billing: BillingRepository, settings: StripeWebhookSettings) -> None:
        self._billing = billing
        self._settings = settings

    def execute(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("type") or "")
        data = (event.get("data") or {}).get("object") or {}
        if event_type == "checkout.session.completed":
            return self._checkout_completed(data)
        if event_type.startswith("customer.subscription."):
            return self._subscription_changed(data, deleted=event_type.endswith(".deleted"))
        return {"status": "ignored", "type": event_type}

    def _checkout_completed(self, session: dict[str, Any]) -> dict[str, Any]:
        workspace_id = _workspace_id_from_metadata(session)
        customer_id = str(session.get("customer") or "")
        if workspace_id is None or not customer_id:
            return {"status": "ignored", "reason": "missing workspace metadata or customer"}
        self._billing.upsert_stripe_customer(
            workspace_id=workspace_id,
            stripe_customer_id=customer_id,
            email=(session.get("customer_details") or {}).get("email"),
        )
        return {"status": "processed", "workspace_id": workspace_id}

    def _subscription_changed(self, subscription: dict[str, Any], *, deleted: bool) -> dict[str, Any]:
        subscription_id = str(subscription.get("id") or "")
        if not subscription_id:
            return {"status": "ignored", "reason": "missing subscription id"}
        workspace_id = _workspace_id_from_metadata(subscription)
        if workspace_id is None:
            customer_id = str(subscription.get("customer") or "")
            workspace_id = (
                self._billing.workspace_for_stripe_customer(customer_id) if customer_id else None
            )
        if workspace_id is None:
            return {"status": "ignored", "reason": "unknown workspace for subscription"}
        price_id = _price_id(subscription)
        plan = self._plan_for_price(price_id)
        status = "canceled" if deleted else normalize_subscription_status(str(subscription.get("status") or ""))
        self._billing.upsert_subscription(
            workspace_id=workspace_id,
            stripe_subscription_id=subscription_id,
            stripe_price_id=price_id,
            plan=plan,
            status=status,
            current_period_end=str(subscription.get("current_period_end") or "") or None,
        )
        effective = effective_plan(status, plan)
        self._billing.set_workspace_limits(
            workspace_id=workspace_id,
            plan=effective.name,
            monthly_run_cap=effective.monthly_run_cap,
            monthly_spend_cap_usd=effective.monthly_spend_cap_usd,
        )
        return {"status": "processed", "workspace_id": workspace_id, "plan": effective.name, "state": status}

    def _plan_for_price(self, price_id: str | None) -> str:
        if price_id and price_id == self._settings.price_id_pro:
            return "pro"
        if price_id and price_id == self._settings.price_id_starter:
            return "starter"
        return "starter" if price_id else "free"


def _workspace_id_from_metadata(obj: dict[str, Any]) -> int | None:
    raw = (obj.get("metadata") or {}).get("workspace_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _price_id(subscription: dict[str, Any]) -> str | None:
    items = ((subscription.get("items") or {}).get("data")) or []
    if items:
        price = (items[0] or {}).get("price") or {}
        if price.get("id"):
            return str(price["id"])
    return None
