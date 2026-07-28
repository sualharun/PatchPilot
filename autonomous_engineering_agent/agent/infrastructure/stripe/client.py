"""Minimal Stripe REST adapter: checkout sessions, portal sessions, webhook signatures.

Uses the plain HTTPS API so the project does not need the stripe SDK.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import requests

STRIPE_API_BASE = "https://api.stripe.com/v1"


class StripeClient:
    def __init__(self, secret_key: str, api_base: str = STRIPE_API_BASE) -> None:
        self._secret_key = secret_key
        self._api_base = api_base.rstrip("/")

    def create_checkout_session(
        self,
        *,
        price_id: str,
        workspace_id: int,
        customer_email: str | None,
        success_url: str,
        cancel_url: str,
    ) -> dict[str, Any]:
        data = {
            "mode": "subscription",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata[workspace_id]": str(workspace_id),
            "subscription_data[metadata][workspace_id]": str(workspace_id),
        }
        if customer_email:
            data["customer_email"] = customer_email
        return self._post("/checkout/sessions", data)

    def create_portal_session(self, *, customer_id: str, return_url: str) -> dict[str, Any]:
        return self._post(
            "/billing_portal/sessions",
            {"customer": customer_id, "return_url": return_url},
        )

    def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        response = requests.post(
            f"{self._api_base}{path}",
            data=data,
            auth=(self._secret_key, ""),
            timeout=20,
        )
        response.raise_for_status()
        return response.json()


def verify_stripe_signature(
    *,
    secret: str,
    payload: bytes,
    signature_header: str | None,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> bool:
    """Verify a Stripe-Signature header (t=...,v1=...) against the raw payload."""
    if not secret or not signature_header:
        return False
    timestamp = None
    candidates: list[str] = []
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)
    if not timestamp or not candidates:
        return False
    try:
        issued_at = int(timestamp)
    except ValueError:
        return False
    current = now if now is not None else int(time.time())
    if abs(current - issued_at) > tolerance_seconds:
        return False
    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in candidates)
