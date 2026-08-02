import sentry_sdk

from agent.infrastructure.observability import capture_exception, init_sentry
from agent.infrastructure.observability.sentry import _redact_value


def test_init_sentry_is_noop_without_dsn():
    assert init_sentry(None) is False
    assert init_sentry("") is False
    assert init_sentry("   ") is False


def test_init_sentry_activates_with_dsn():
    try:
        initialized = init_sentry("https://public@o0.ingest.sentry.io/0", environment="test")
        assert initialized is True
        assert sentry_sdk.is_initialized() is True
    finally:
        sentry_sdk.get_global_scope().set_client(None)


def test_capture_exception_is_safe_without_active_client():
    sentry_sdk.get_global_scope().set_client(None)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        capture_exception(exc)  # should not raise


def test_redact_value_scrubs_nested_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_abcdefghijklmnopqrstuvwx")
    event = {
        "exception": {"values": [{"value": "failed for token github_pat_abcdefghijklmnopqrstuvwx"}]},
        "extra": {"nested": ["ok", "github_pat_abcdefghijklmnopqrstuvwx"]},
    }

    redacted = _redact_value(event)

    assert "github_pat_abcdefghijklmnopqrstuvwx" not in str(redacted)
