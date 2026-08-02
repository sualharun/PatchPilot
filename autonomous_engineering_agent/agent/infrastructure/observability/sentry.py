"""Optional Sentry error reporting.

Everything here is a no-op unless SENTRY_DSN is set, so nothing breaks locally or in CI
without one configured.
"""

from __future__ import annotations

from typing import Any

import sentry_sdk

from agent.infrastructure.security.secrets import redact_text


def init_sentry(dsn: str | None, *, environment: str = "development") -> bool:
    """Initialize Sentry if a DSN is configured. Returns whether it was initialized."""
    if not dsn or not dsn.strip():
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=0.0,
        send_default_pii=False,
        before_send=_redact_event,
    )
    return True


def capture_exception(exc: BaseException | None = None) -> None:
    """Report an exception to Sentry. Safe to call whether or not Sentry was initialized --
    the SDK is a no-op without an active client."""
    sentry_sdk.capture_exception(exc)


def _redact_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    """Run our own secret redaction over the event before it leaves the process, since it may
    quote exception messages that embed tokens (e.g. an HTTP error including a URL with a key)."""
    return _redact_value(event)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value
