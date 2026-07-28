import hashlib
import hmac
import json
import re
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from agent.dashboard import create_app


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
    assert callback.headers["location"] == "/runs"
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
