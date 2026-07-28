import pytest


@pytest.fixture(autouse=True)
def stable_test_environment(monkeypatch):
    """Keep a developer .env from changing unit-test behavior.

    Individual tests can still override these values with monkeypatch.setenv.
    """

    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "false")
    monkeypatch.setenv("DASHBOARD_DEMO_DATA_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_SECURE_COOKIES", "false")
    monkeypatch.setenv("GITHUB_OAUTH_MOCK_ENABLED", "false")
    monkeypatch.setenv("PATCHPILOT_PRODUCTION", "false")
    monkeypatch.setenv("AGENT_MODEL_PRICES_JSON", "")

