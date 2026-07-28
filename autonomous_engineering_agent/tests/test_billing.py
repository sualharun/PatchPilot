import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from agent.application.services.billing import (
    BillingService,
    HandleStripeWebhookHandler,
    StripeWebhookSettings,
)
from agent.dashboard import create_app
from agent.domain.billing import PLANS, check_run_allowed, effective_plan
from agent.infrastructure.db.repositories import SqlBillingRepository
from agent.infrastructure.db.store import RunStore
from agent.infrastructure.stripe import verify_stripe_signature


def _sign(secret: str, payload: bytes, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signature = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={signature}"


def test_verify_stripe_signature_accepts_valid_and_rejects_invalid():
    payload = b'{"type": "checkout.session.completed"}'
    header = _sign("whsec_test", payload)

    assert verify_stripe_signature(secret="whsec_test", payload=payload, signature_header=header)
    assert not verify_stripe_signature(secret="whsec_other", payload=payload, signature_header=header)
    assert not verify_stripe_signature(secret="whsec_test", payload=b"tampered", signature_header=header)
    stale = _sign("whsec_test", payload, timestamp=int(time.time()) - 4000)
    assert not verify_stripe_signature(secret="whsec_test", payload=payload, signature_header=stale)


def _subscription_event(event_type="customer.subscription.created", status="active", price="price_starter"):
    return {
        "type": event_type,
        "data": {
            "object": {
                "id": "sub_123",
                "customer": "cus_123",
                "status": status,
                "metadata": {"workspace_id": "1"},
                "items": {"data": [{"price": {"id": price}}]},
                "current_period_end": 1893456000,
            }
        },
    }


def test_stripe_webhook_updates_subscription_and_limits(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    store.get_or_create_workspace(name="Test", slug="test")
    handler = HandleStripeWebhookHandler(
        SqlBillingRepository(store),
        StripeWebhookSettings(price_id_starter="price_starter", price_id_pro="price_pro"),
    )

    result = handler.execute(_subscription_event())
    assert result["status"] == "processed"
    assert result["plan"] == "starter"

    subscription = store.subscription_for_workspace(1)
    assert subscription["status"] == "active"
    assert subscription["plan"] == "starter"
    limits = store.get_workspace_limits(1)
    assert limits["plan"] == "starter"
    assert int(limits["monthly_run_cap"]) == PLANS["starter"].monthly_run_cap

    canceled = handler.execute(_subscription_event(event_type="customer.subscription.deleted"))
    assert canceled["state"] == "canceled"
    assert store.get_workspace_limits(1)["plan"] == "free"


def test_checkout_completed_links_customer_to_workspace(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    store.get_or_create_workspace(name="Test", slug="test")
    handler = HandleStripeWebhookHandler(SqlBillingRepository(store), StripeWebhookSettings())

    result = handler.execute(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "customer": "cus_777",
                    "metadata": {"workspace_id": "1"},
                    "customer_details": {"email": "buyer@example.com"},
                }
            },
        }
    )

    assert result["status"] == "processed"
    assert store.workspace_for_stripe_customer("cus_777") == 1

    # A later subscription event without workspace metadata resolves via the customer.
    event = _subscription_event()
    del event["data"]["object"]["metadata"]
    event["data"]["object"]["customer"] = "cus_777"
    handler_with_prices = HandleStripeWebhookHandler(
        SqlBillingRepository(store), StripeWebhookSettings(price_id_starter="price_starter")
    )
    outcome = handler_with_prices.execute(event)
    assert outcome["workspace_id"] == 1


def test_run_limit_enforced_for_free_plan(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    store.get_or_create_workspace(name="Test", slug="test")
    billing = BillingService(SqlBillingRepository(store))

    for index in range(PLANS["free"].monthly_run_cap):
        decision = billing.check_run_allowed(1)
        assert decision.allowed, f"run {index} should be allowed"
        billing.record_run(1, run_id=index + 1)

    blocked = billing.check_run_allowed(1)
    assert not blocked.allowed
    assert "run cap" in blocked.reason


def test_effective_plan_falls_back_to_free_when_not_active():
    assert effective_plan("past_due", "pro").name == "free"
    assert effective_plan("canceled", "starter").name == "free"
    assert effective_plan("active", "pro").name == "pro"
    assert effective_plan("trialing", "starter").name == "starter"


def test_spend_cap_enforced():
    decision = check_run_allowed(PLANS["pro"], runs_used=1, spend_used_usd=150.0)
    assert not decision.allowed
    assert "spend cap" in decision.reason


def test_stripe_webhook_endpoint_verifies_signature(monkeypatch, tmp_path):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    db_url = f"sqlite:///{tmp_path / 'runs.sqlite3'}"
    client = TestClient(create_app(db_url))

    body = json.dumps(_subscription_event()).encode()

    rejected = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=bad"})
    assert rejected.status_code == 401

    accepted = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": _sign("whsec_test", body)},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "processed"

    store = RunStore(db_url)
    assert store.subscription_for_workspace(1) is not None


def test_billing_page_shows_plan_and_usage(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'runs.sqlite3'}"
    client = TestClient(create_app(db_url))

    page = client.get("/billing")
    assert page.status_code == 200
    assert "Runs this month" in page.text
    assert "free" in page.text
