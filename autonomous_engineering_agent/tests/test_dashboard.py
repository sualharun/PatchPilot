import hashlib
import hmac
import json
import re
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from agent.dashboard import create_app
from agent.infrastructure.security import RateLimiter


def _csrf_token(client: TestClient) -> str:
    html = client.get("/login").text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_dashboard_pages_and_api_load(tmp_path):
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    routes = [
        "/",
        "/login",
        "/demo",
        "/overview",
        "/runs",
        "/repositories",
        "/issues",
        "/pull-requests",
        "/agents",
        "/tests",
        "/settings",
        "/billing",
        "/audit-log",
        "/github",
        "/security",
        "/feedback",
        "/docs",
        "/faq",
        "/privacy",
        "/terms",
    ]
    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, route
        assert "PatchPilot" in response.text
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["status"] == "ready"
    assert client.get("/api/runs").json()["runs"]
    assert client.get("/api/stats").json()["total_runs"] > 0
    assert "Start Agent Run" in client.get("/overview").text


def test_dashboard_auth_redirects_and_login(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    response = client.get("/runs", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/runs"
    assert client.get("/api/runs").status_code == 401

    csrf_token = _csrf_token(client)
    bad_login = client.post(
        "/login",
        data={"username": "alex", "password": "wrong", "next_path": "/runs", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert bad_login.headers["location"] == "/login?error=invalid"

    good_login = client.post(
        "/login",
        data={"username": "alex", "password": "secret-pass", "next_path": "/runs", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert good_login.status_code == 303
    assert good_login.headers["location"] == "/runs"
    assert "patchpilot_session" in good_login.headers["set-cookie"]
    assert client.get("/runs").status_code == 200


def test_privacy_and_terms_are_public_even_with_auth_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    privacy = client.get("/privacy")
    terms = client.get("/terms")

    assert privacy.status_code == 200
    assert "not been reviewed by a lawyer" in privacy.text
    assert terms.status_code == 200
    assert "not been reviewed by a lawyer" in terms.text


def test_dashboard_can_queue_run(tmp_path):
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))
    response = client.post(
        "/api/runs",
        data={
            "issue": "octo/example#12",
            "model": "gpt-4.1",
            "max_iterations": "4",
            "open_pr": "false",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["status"] == "queued"
    assert runs[0]["repo"] == "octo/example"
    assert client.get("/api/audit-events").json()["events"][0]["event"] == "run.queued"


def test_dashboard_real_data_mode_uses_empty_states(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_DEMO_DATA_ENABLED", "false")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    assert client.get("/api/runs").json()["runs"] == []
    assert client.get("/api/audit-events").json()["events"] == []
    assert "No runs yet" in client.get("/runs").text
    assert "No audit events yet" in client.get("/audit-log").text
    assert "No persisted run" in client.get("/runs/101").text
    assert client.get("/api/runs/101").status_code == 404


def test_dashboard_tabs_target_real_sections_and_runs_filters(tmp_path):
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))
    routes = [
        "/overview",
        "/runs",
        "/agents",
        "/repositories",
        "/issues",
        "/pull-requests",
        "/tests",
        "/settings",
        "/billing",
        "/audit-log",
        "/github",
        "/security",
        "/docs",
    ]

    for route in routes:
        html = client.get(route).text
        for href, selector in re.findall(
            r'<a[^>]+href="([^"]+)"[^>]+data-section="(#[^"]+)"', html
        ):
            destination = urlparse(href)
            if destination.path == route:
                assert f'id="{selector[1:]}"' in html, (route, href)

    runs_html = client.get("/runs").text
    assert 'id="runs-search"' in runs_html
    assert 'id="runs-search-field"' in runs_html
    assert 'id="runs-search-suggestions"' in runs_html
    assert 'id="runs-status"' in runs_html
    assert 'id="runs-table-body"' in runs_html
    assert 'data-run-id="' in runs_html
    assert 'class="metric-range"' in runs_html
    assert "Average / run" in runs_html
    assert 'class="mini-bars"' not in runs_html
    assert 'data-run-status="failed"' in runs_html
    assert "Runs API" not in runs_html
    assert "Spend categories" not in client.get("/billing").text


def test_github_oauth_mock_callback_creates_session(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CALLBACK_URL", "http://testserver/auth/github/callback")
    monkeypatch.setenv("GITHUB_OAUTH_MOCK_ENABLED", "true")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    start = client.get("/auth/github/start", follow_redirects=False)
    assert start.status_code == 303
    state = re.search(r"state=([^&]+)", start.headers["location"])
    assert state is not None

    callback = client.get(f"/auth/github/callback?code=mock&state={state.group(1)}", follow_redirects=False)

    assert callback.status_code == 303
    assert callback.headers["location"] == "/onboarding"
    assert "patchpilot_session" in callback.headers["set-cookie"]
    status = client.get("/api/github/status").json()
    assert status["connected"] is True
    assert status["connection"]["login"] == "mock-github-user"


def test_github_pull_request_webhook_enqueues_kafka_job(monkeypatch, tmp_path):
    published = []

    class FakeProducer:
        def __init__(self, config):
            self.config = config

        def publish(self, job):
            published.append(job)

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setattr("agent.dashboard.KafkaPRJobProducer", FakeProducer)
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))
    payload = {
        "action": "opened",
        "repository": {"name": "example", "owner": {"login": "octo"}},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        "installation": {"id": 99},
        "sender": {"login": "mona"},
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-1",
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert published[0].full_name == "octo/example"
    assert published[0].pr_number == 42
    assert client.get("/api/audit-events").json()["events"][0]["event"] == "pr_analysis.enqueued"


def test_signup_creates_account_and_allows_login(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._SIGNUP_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    assert "Create your PatchPilot account" in client.get("/signup").text

    csrf_token = _csrf_token(client)
    created = client.post(
        "/signup",
        data={
            "email": "New.User@Example.com",
            "password": "s3cure-pass",
            "password_confirm": "s3cure-pass",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/login?created=1"

    login = client.post(
        "/login",
        data={
            "username": "new.user@example.com",
            "password": "s3cure-pass",
            "next_path": "/runs",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    # A first-time account lands on the onboarding checklist, not /runs.
    assert login.headers["location"] == "/onboarding"
    assert "patchpilot_session" in login.headers["set-cookie"]
    assert client.get("/runs").status_code == 200
    events = [event["event"] for event in client.get("/api/audit-events").json()["events"]]
    assert "auth.signup" in events

    bad_login = client.post(
        "/login",
        data={
            "username": "new.user@example.com",
            "password": "wrong-pass",
            "next_path": "/runs",
            # The CSRF token is bound to the session cookie, which changed on login.
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    assert bad_login.headers["location"] == "/login?error=invalid"


def test_signup_rejects_duplicate_email_weak_password_and_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._SIGNUP_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    def attempt(email, password, confirm):
        response = client.post(
            "/signup",
            data={
                "email": email,
                "password": password,
                "password_confirm": confirm,
                "csrf_token": _csrf_token(client),
            },
            follow_redirects=False,
        )
        return response.headers["location"]

    assert attempt("dup@example.com", "s3cure-pass", "s3cure-pass") == "/login?created=1"
    assert attempt("dup@example.com", "s3cure-pass", "s3cure-pass") == "/signup?error=email_exists"
    assert attempt("weak@example.com", "short", "short") == "/signup?error=weak_password"
    assert attempt("mm@example.com", "s3cure-pass", "different-pass") == "/signup?error=password_mismatch"
    assert attempt("not-an-email", "s3cure-pass", "s3cure-pass") == "/signup?error=invalid_email"
    assert "The passwords do not match." in client.get("/signup?error=password_mismatch").text


def test_auth_mode_password_hides_and_blocks_github_oauth(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_AUTH_MODE", "password")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CALLBACK_URL", "http://testserver/auth/github/callback")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    login_page = client.get("/login").text
    assert "Continue with GitHub" not in login_page
    assert 'name="password"' in login_page

    start = client.get("/auth/github/start", follow_redirects=False)
    assert start.headers["location"] == "/login?error=github_oauth_disabled"

    login = client.post(
        "/login",
        data={"username": "alex", "password": "secret-pass", "next_path": "/runs", "csrf_token": _csrf_token(client)},
        follow_redirects=False,
    )
    assert login.headers["location"] == "/runs"


def test_auth_mode_github_oauth_hides_and_blocks_password_login(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_AUTH_MODE", "github-oauth")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CALLBACK_URL", "http://testserver/auth/github/callback")
    monkeypatch.setenv("GITHUB_OAUTH_MOCK_ENABLED", "true")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    login_page = client.get("/login").text
    assert "Continue with GitHub" in login_page
    assert 'name="password"' not in login_page
    assert "Create an account" not in login_page

    login = client.post(
        "/login",
        data={"username": "alex", "password": "secret-pass", "next_path": "/runs", "csrf_token": _csrf_token(client)},
        follow_redirects=False,
    )
    assert login.headers["location"] == "/login?error=invalid"
    assert client.get("/signup", follow_redirects=False).headers["location"] == "/login"
    assert (
        client.post("/signup", data={"email": "a@b.co", "password": "x", "password_confirm": "x"}, follow_redirects=False)
        .headers["location"]
        == "/login"
    )

    start = client.get("/auth/github/start", follow_redirects=False)
    state = re.search(r"state=([^&]+)", start.headers["location"])
    assert state is not None
    callback = client.get(f"/auth/github/callback?code=mock&state={state.group(1)}", follow_redirects=False)
    assert callback.headers["location"] == "/onboarding"
    assert "patchpilot_session" in callback.headers["set-cookie"]


def test_account_password_and_email_management(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_ONBOARDING_ENABLED", "false")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._SIGNUP_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._LOGIN_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    def signup(email):
        client.post(
            "/signup",
            data={
                "email": email,
                "password": "s3cure-pass",
                "password_confirm": "s3cure-pass",
                "csrf_token": _csrf_token(client),
            },
            follow_redirects=False,
        )

    def login(username, password):
        return client.post(
            "/login",
            data={"username": username, "password": password, "next_path": "/runs", "csrf_token": _csrf_token(client)},
            follow_redirects=False,
        )

    signup("taken@example.com")
    signup("owner@example.com")
    assert login("owner@example.com", "s3cure-pass").headers["location"] == "/runs"

    account_page = client.get("/account").text
    assert 'action="/account/password"' in account_page
    assert 'action="/account/email"' in account_page

    wrong = client.post(
        "/account/password",
        data={
            "current_password": "not-the-password",
            "new_password": "n3w-secret-pass",
            "new_password_confirm": "n3w-secret-pass",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    assert wrong.headers["location"] == "/account?error=wrong_password"

    changed = client.post(
        "/account/password",
        data={
            "current_password": "s3cure-pass",
            "new_password": "n3w-secret-pass",
            "new_password_confirm": "n3w-secret-pass",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    assert changed.headers["location"] == "/account?ok=password"
    assert "Password updated." in client.get("/account?ok=password").text

    duplicate = client.post(
        "/account/email",
        data={"email": "taken@example.com", "csrf_token": _csrf_token(client)},
        follow_redirects=False,
    )
    assert duplicate.headers["location"] == "/account?error=email_exists"

    renamed = client.post(
        "/account/email",
        data={"email": "renamed@example.com", "csrf_token": _csrf_token(client)},
        follow_redirects=False,
    )
    assert renamed.headers["location"] == "/account?ok=email"
    assert "patchpilot_session" in renamed.headers["set-cookie"]
    assert client.get("/account").status_code == 200

    events = [event["event"] for event in client.get("/api/audit-events").json()["events"]]
    assert "account.password_changed" in events
    assert "account.email_changed" in events

    client.post("/logout", data={"csrf_token": _csrf_token(client)}, follow_redirects=False)
    assert login("renamed@example.com", "s3cure-pass").headers["location"] == "/login?error=invalid"
    assert login("renamed@example.com", "n3w-secret-pass").headers["location"] == "/runs"


def test_account_changes_blocked_for_env_managed_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._LOGIN_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))
    client.post(
        "/login",
        data={"username": "alex", "password": "secret-pass", "next_path": "/runs", "csrf_token": _csrf_token(client)},
        follow_redirects=False,
    )

    response = client.post(
        "/account/password",
        data={
            "current_password": "secret-pass",
            "new_password": "n3w-secret-pass",
            "new_password_confirm": "n3w-secret-pass",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/account?error=not_managed"
    assert "DASHBOARD_USERNAME" in client.get("/account?error=not_managed").text


def test_onboarding_first_login_redirect_and_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._SIGNUP_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._LOGIN_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    def login(username, password, next_path="/runs"):
        return client.post(
            "/login",
            data={
                "username": username,
                "password": password,
                "next_path": next_path,
                "csrf_token": _csrf_token(client),
            },
            follow_redirects=False,
        )

    # The env-admin never gets redirected to onboarding.
    assert login("alex", "secret-pass").headers["location"] == "/runs"
    client.post("/logout", data={"csrf_token": _csrf_token(client)}, follow_redirects=False)

    client.post(
        "/signup",
        data={
            "email": "fresh@example.com",
            "password": "s3cure-pass",
            "password_confirm": "s3cure-pass",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    assert login("fresh@example.com", "s3cure-pass").headers["location"] == "/onboarding"

    page = client.get("/onboarding").text
    assert "Welcome, fresh" in page
    assert "GitHub connection" in page
    assert "API key" in page
    assert "First run" in page
    assert 'action="/onboarding/skip"' in page

    skipped = client.post(
        "/onboarding/skip", data={"csrf_token": _csrf_token(client)}, follow_redirects=False
    )
    assert skipped.headers["location"] == "/overview"

    # An explicitly requested deep link is honored, and once skipped the
    # default destination is /runs again.
    client.post("/logout", data={"csrf_token": _csrf_token(client)}, follow_redirects=False)
    assert login("fresh@example.com", "s3cure-pass", next_path="/settings").headers["location"] == "/settings"
    client.post("/logout", data={"csrf_token": _csrf_token(client)}, follow_redirects=False)
    assert login("fresh@example.com", "s3cure-pass").headers["location"] == "/runs"

    events = [event["event"] for event in client.get("/api/audit-events").json()["events"]]
    assert "onboarding.skipped" in events


def test_onboarding_redirect_disabled_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_ONBOARDING_ENABLED", "false")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._SIGNUP_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._LOGIN_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    client.post(
        "/signup",
        data={
            "email": "fresh@example.com",
            "password": "s3cure-pass",
            "password_confirm": "s3cure-pass",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    login = client.post(
        "/login",
        data={
            "username": "fresh@example.com",
            "password": "s3cure-pass",
            "next_path": "/runs",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    assert login.headers["location"] == "/runs"


def test_github_app_setup_page_and_callback(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_APP_INSTALL_URL", "https://github.com/apps/patchpilot/installations/new")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    page = client.get("/github-app-setup").text
    assert "Why install the GitHub App?" in page
    assert "Install on GitHub" in page
    assert "Contents: read &amp; write" in page

    start = client.get("/github-app-setup/start", follow_redirects=False)
    assert start.status_code == 303
    assert start.headers["location"].startswith("https://github.com/apps/patchpilot/installations/new?state=")

    missing = client.get("/github-app-setup/callback", follow_redirects=False)
    assert missing.headers["location"] == "/github-app-setup?error=missing_installation"

    callback = client.get(
        "/github-app-setup/callback?installation_id=42&setup_action=install", follow_redirects=False
    )
    assert callback.headers["location"] == "/github-app-setup?installed=1"

    installed_page = client.get("/github-app-setup?installed=1").text
    assert "GitHub App installed." in installed_page
    assert "Installation ID" in installed_page
    assert "Manage or uninstall on GitHub" in installed_page

    settings_page = client.get("/settings").text
    assert 'id="github-app"' in settings_page
    assert "installed" in settings_page

    events = [event["event"] for event in client.get("/api/audit-events").json()["events"]]
    assert "github_app.setup_completed" in events


def test_github_app_setup_without_install_url_shows_error(monkeypatch, tmp_path):
    # setenv("") rather than delenv: load_dotenv would re-supply a developer's
    # .env value for a deleted variable, but never overrides an existing one.
    monkeypatch.setenv("GITHUB_APP_INSTALL_URL", "")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    start = client.get("/github-app-setup/start", follow_redirects=False)
    assert start.headers["location"] == "/github-app-setup?error=not_configured"
    assert "GITHUB_APP_INSTALL_URL is not configured" in client.get("/github-app-setup?error=not_configured").text

    settings_page = client.get("/settings").text
    assert "Set up the GitHub App" in settings_page


def test_faq_is_public_and_linked_from_login(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    faq = client.get("/faq")
    assert faq.status_code == 200
    assert "Is the GitHub App required?" in faq.text
    assert "What does a run cost?" in faq.text
    assert 'href="/faq"' in client.get("/login").text


def test_settings_provider_key_test_button(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    calls = []

    def fake_verify(provider, api_key):
        calls.append((provider, api_key))
        return "ok" if api_key else "not_configured"

    monkeypatch.setattr("agent.interfaces.http.dashboard.verify_provider_key", fake_verify)
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    settings_page = client.get("/settings").text
    assert "Test OpenAI key" in settings_page
    assert "https://platform.openai.com/api-keys" in settings_page
    assert "What a run costs" in settings_page

    ok = client.post("/settings/test-provider-key", data={"provider": "openai"}, follow_redirects=False)
    assert ok.headers["location"] == "/settings?tested=openai&status=ok#providers"
    assert calls == [("openai", "sk-test-key")]
    assert "key is valid" in client.get("/settings?tested=openai&status=ok").text

    missing = client.post("/settings/test-provider-key", data={"provider": "anthropic"}, follow_redirects=False)
    assert missing.headers["location"] == "/settings?tested=anthropic&status=not_configured#providers"
    assert "no key is set" in client.get("/settings?tested=anthropic&status=not_configured").text

    unsupported = client.post("/settings/test-provider-key", data={"provider": "cohere"}, follow_redirects=False)
    assert unsupported.headers["location"] == "/settings?tested=unknown&status=unsupported#providers"

    events = [event["event"] for event in client.get("/api/audit-events").json()["events"]]
    assert "settings.provider_key_tested" in events


def test_demo_data_banner_marks_sample_runs(monkeypatch, tmp_path):
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    # Demo mode with no real runs: the samples are labelled as such.
    runs_page = client.get("/runs").text
    assert "illustrative samples" in runs_page

    client.post(
        "/api/runs",
        data={"issue": "octo/example#12", "model": "gpt-4.1", "max_iterations": "4", "open_pr": "false"},
        follow_redirects=False,
    )

    # A real run replaces the samples, so the banner goes away.
    assert "illustrative samples" not in client.get("/runs").text


def test_demo_data_banner_absent_when_demo_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_DEMO_DATA_ENABLED", "false")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    assert "illustrative samples" not in client.get("/runs").text


def _verification_client(monkeypatch, tmp_path, sent):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_ONBOARDING_ENABLED", "false")
    monkeypatch.setenv("DASHBOARD_REQUIRE_EMAIL_VERIFICATION", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("PATCHPILOT_PUBLIC_BASE_URL", "https://patchpilot.test")
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._SIGNUP_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._LOGIN_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    monkeypatch.setattr(
        "agent.infrastructure.email.SmtpMailer.send",
        lambda self, *, to_address, subject, body: (sent.append((to_address, subject, body)), True)[1],
    )
    return TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))


def test_email_verification_soft_flow(monkeypatch, tmp_path):
    sent = []
    client = _verification_client(monkeypatch, tmp_path, sent)

    client.post(
        "/signup",
        data={
            "email": "verify@example.com",
            "password": "s3cure-pass",
            "password_confirm": "s3cure-pass",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    assert len(sent) == 1
    to_address, subject, body = sent[0]
    assert to_address == "verify@example.com"
    assert subject == "Verify your PatchPilot email"
    token = re.search(r"https://patchpilot\.test/verify-email\?token=([A-Za-z0-9_-]+)", body)
    assert token is not None

    # Soft verification: an unverified account can still sign in and work.
    login = client.post(
        "/login",
        data={
            "username": "verify@example.com",
            "password": "s3cure-pass",
            "next_path": "/runs",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    assert login.headers["location"] == "/runs"
    assert "email address is not verified" in client.get("/account").text

    verified = client.get(f"/verify-email?token={token.group(1)}", follow_redirects=False)
    assert verified.headers["location"] == "/account?ok=verified"
    account_page = client.get("/account?ok=verified").text
    assert "Email address verified." in account_page
    assert "email address is not verified" not in account_page

    # A consumed token cannot be replayed.
    replay = client.get(f"/verify-email?token={token.group(1)}", follow_redirects=False)
    assert replay.headers["location"] == "/account?error=verification_invalid"

    events = [event["event"] for event in client.get("/api/audit-events").json()["events"]]
    assert "account.verification_sent" in events
    assert "account.email_verified" in events


def test_email_verification_resend_and_bad_token(monkeypatch, tmp_path):
    sent = []
    client = _verification_client(monkeypatch, tmp_path, sent)

    client.post(
        "/signup",
        data={
            "email": "resend@example.com",
            "password": "s3cure-pass",
            "password_confirm": "s3cure-pass",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    client.post(
        "/login",
        data={
            "username": "resend@example.com",
            "password": "s3cure-pass",
            "next_path": "/runs",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )

    bad = client.get("/verify-email?token=not-a-real-token", follow_redirects=False)
    assert bad.headers["location"] == "/account?error=verification_invalid"

    resend = client.post(
        "/account/resend-verification", data={"csrf_token": _csrf_token(client)}, follow_redirects=False
    )
    assert resend.headers["location"] == "/account?ok=verification_sent"
    assert len(sent) == 2

    # The newest link still verifies the account.
    token = re.search(r"/verify-email\?token=([A-Za-z0-9_-]+)", sent[-1][2])
    assert token is not None
    assert client.get(f"/verify-email?token={token.group(1)}", follow_redirects=False).headers[
        "location"
    ] == "/account?ok=verified"

    # Already verified: nothing more to send.
    already = client.post(
        "/account/resend-verification", data={"csrf_token": _csrf_token(client)}, follow_redirects=False
    )
    assert already.headers["location"] == "/account?error=verification_unavailable"


def test_unconfigured_smtp_logs_the_link_instead_of_sending(caplog, tmp_path):
    from agent.infrastructure.email import SmtpMailer

    mailer = SmtpMailer(host=None)

    with caplog.at_level("WARNING", logger="agent.infrastructure.email"):
        sent = mailer.send(to_address="dev@example.com", subject="Verify", body="http://localhost/verify-email?token=t")

    assert sent is False
    # Emitted at WARNING so it survives with no logging configuration: this line
    # is the only way to get a verification link when SMTP is unset.
    assert any(record.levelname == "WARNING" for record in caplog.records)
    assert "http://localhost/verify-email?token=t" in caplog.text


def test_signup_sends_no_email_when_verification_disabled(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(
        "agent.infrastructure.email.SmtpMailer.send",
        lambda self, *, to_address, subject, body: (sent.append(to_address), True)[1],
    )
    monkeypatch.setattr(
        "agent.interfaces.http.dashboard._SIGNUP_RATE_LIMITER", RateLimiter(max_requests=100, window_seconds=60)
    )
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    client.post(
        "/signup",
        data={
            "email": "quiet@example.com",
            "password": "s3cure-pass",
            "password_confirm": "s3cure-pass",
            "csrf_token": _csrf_token(client),
        },
        follow_redirects=False,
    )
    assert sent == []
