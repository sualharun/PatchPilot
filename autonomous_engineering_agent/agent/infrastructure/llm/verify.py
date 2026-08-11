"""Liveness checks for provider API keys.

Uses each provider's model-listing endpoint: the cheapest authenticated call
that distinguishes "key works" from "key rejected" without spending tokens.
Returns a fixed status code rather than the provider's message so callers can
render it safely and consistently.
"""

from __future__ import annotations

import requests

# ok | invalid | forbidden | rate_limited | unreachable | not_configured | unsupported
VerifyStatus = str

_TIMEOUT_SECONDS = 15


def verify_provider_key(provider: str, api_key: str | None) -> VerifyStatus:
    if not api_key:
        return "not_configured"
    if provider == "openai":
        url = "https://api.openai.com/v1/models"
        headers = {"Authorization": f"Bearer {api_key}"}
    elif provider == "anthropic":
        url = "https://api.anthropic.com/v1/models"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        return "unsupported"
    try:
        response = requests.get(url, headers=headers, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException:
        return "unreachable"
    return _status_for(response.status_code)


def _status_for(status_code: int) -> VerifyStatus:
    if status_code == 200:
        return "ok"
    if status_code == 401:
        return "invalid"
    if status_code == 403:
        return "forbidden"
    if status_code == 429:
        # The key authenticated; the account is just being throttled.
        return "rate_limited"
    return "unreachable"
