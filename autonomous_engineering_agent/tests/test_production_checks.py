import pytest
from fastapi.testclient import TestClient

from agent.config import load_config, validate_production_config
from agent.dashboard import create_app
from agent.infrastructure.security import RateLimiter
from agent.infrastructure.security.secrets import redact_text


def _set_full_production_env(monkeypatch):
    monkeypatch.setenv("PATCHPILOT_PRODUCTION", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.internal:5432/patchpilot")
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "a" * 32)
    monkeypatch.setenv("DASHBOARD_SECURE_COOKIES", "true")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CALLBACK_URL", "https://patchpilot.example.com/auth/github/callback")
    monkeypatch.setenv("GITHUB_OAUTH_MOCK_ENABLED", "false")
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nZmFrZQ==\n-----END RSA PRIVATE KEY-----")
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "app-webhook-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890abcdef1234")


def test_load_config_succeeds_with_all_production_settings(monkeypatch):
    _set_full_production_env(monkeypatch)

    config = load_config()

    assert config.production is True
    assert validate_production_config(config) == []


@pytest.mark.parametrize(
    "missing_env,override_value,expected_fragment",
    [
        ("DATABASE_URL", "sqlite:///agent_runs.sqlite3", "PostgreSQL"),
        ("DASHBOARD_SESSION_SECRET", "", "DASHBOARD_SESSION_SECRET"),
        ("DASHBOARD_SECURE_COOKIES", "false", "DASHBOARD_SECURE_COOKIES"),
        ("GITHUB_OAUTH_CLIENT_ID", "", "GitHub OAuth"),
        ("GITHUB_APP_ID", "", "GitHub App"),
        ("GITHUB_APP_WEBHOOK_SECRET", "", "GITHUB_APP_WEBHOOK_SECRET"),
        ("OPENAI_API_KEY", "", "OpenAI or Anthropic"),
    ],
)
def test_load_config_fails_boot_when_a_required_secret_is_missing(
    monkeypatch, missing_env, override_value, expected_fragment
):
    # A developer .env file may already export these names; explicitly overriding
    # (rather than delenv, which python-dotenv would just refill) keeps this
    # deterministic regardless of the machine it runs on.
    _set_full_production_env(monkeypatch)
    monkeypatch.setenv(missing_env, override_value)
    if missing_env == "OPENAI_API_KEY":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    with pytest.raises(ValueError, match=expected_fragment):
        load_config()


def test_load_config_fails_when_dashboard_auth_disabled(monkeypatch):
    _set_full_production_env(monkeypatch)
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "false")

    with pytest.raises(ValueError, match="DASHBOARD_AUTH_ENABLED"):
        load_config()


def test_load_config_fails_when_oauth_mock_enabled_in_production(monkeypatch):
    _set_full_production_env(monkeypatch)
    monkeypatch.setenv("GITHUB_OAUTH_MOCK_ENABLED", "true")

    with pytest.raises(ValueError, match="GITHUB_OAUTH_MOCK_ENABLED"):
        load_config()


def test_rate_limiter_blocks_after_threshold():
    limiter = RateLimiter(max_requests=3, window_seconds=60)
    now = 1_000.0

    assert limiter.allow("1.2.3.4", now=now)
    assert limiter.allow("1.2.3.4", now=now + 1)
    assert limiter.allow("1.2.3.4", now=now + 2)
    assert not limiter.allow("1.2.3.4", now=now + 3)
    # a different key has its own budget
    assert limiter.allow("5.6.7.8", now=now + 3)


def test_rate_limiter_window_expires():
    limiter = RateLimiter(max_requests=1, window_seconds=10)
    now = 1_000.0

    assert limiter.allow("1.2.3.4", now=now)
    assert not limiter.allow("1.2.3.4", now=now + 5)
    assert limiter.allow("1.2.3.4", now=now + 11)


def test_login_endpoint_rate_limited(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))
    login_page = client.get("/login")
    csrf = login_page.text.split('name="csrf_token" value="')[1].split('"')[0]

    responses = [
        client.post(
            "/login",
            data={"username": "alex", "password": "wrong", "next_path": "/runs", "csrf_token": csrf},
            follow_redirects=False,
        )
        for _ in range(11)
    ]

    assert responses[-1].status_code == 429


def test_redact_text_masks_stripe_and_private_keys():
    text = (
        "stripe secret sk_live_ABCDEFGHIJ1234567890 "
        "webhook whsec_ABCDEFGHIJ1234567890 "
        "-----BEGIN RSA PRIVATE KEY-----\nZmFrZWtleWRhdGE=\n-----END RSA PRIVATE KEY-----"
    )

    redacted = redact_text(text)

    assert "sk_live_" not in redacted
    assert "whsec_" not in redacted
    assert "ZmFrZWtleWRhdGE=" not in redacted
    assert "[REDACTED" in redacted
