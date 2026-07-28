from fastapi.testclient import TestClient

from agent.dashboard import create_app
from agent.infrastructure.db.store import RunRecord, RunStore


def test_upsert_user_from_github_creates_user_workspace_membership(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")

    user = store.upsert_user_from_github(
        github_user_id="99",
        login="octocat",
        name="The Octocat",
        email="octocat@example.com",
        avatar_url="https://avatars.githubusercontent.com/u/99",
    )

    assert user["login"] == "octocat"
    assert user["github_user_id"] == "99"
    assert user["avatar_url"].endswith("/u/99")
    workspace = store.workspace_for_login("octocat")
    assert workspace is not None
    assert workspace["slug"] == "octocat"


def test_upsert_user_from_github_updates_existing_user(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    store.upsert_user_from_github(
        github_user_id="99", login="octocat", name="Old Name", email="octocat@example.com"
    )

    user = store.upsert_user_from_github(
        github_user_id="99",
        login="octocat",
        name="New Name",
        email="octocat@example.com",
        avatar_url="https://avatars.githubusercontent.com/u/99",
    )

    assert user["name"] == "New Name"
    users = store._execute("SELECT COUNT(*) FROM users", []).fetchone()
    assert int(users[0]) == 1


def test_account_context_prefers_session_login(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    store.seed_defaults(username="admin")
    store.upsert_user_from_github(
        github_user_id="99", login="octocat", name="The Octocat", email="octocat@example.com"
    )

    context = store.get_account_context("octocat")

    assert context["user"]["login"] == "octocat"
    assert context["workspace"]["slug"] == "octocat"


def test_list_runs_is_workspace_aware(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")

    def record(workspace_id):
        return RunRecord(
            issue_url="https://github.com/a/b/issues/1",
            repo="a/b",
            branch="agent/fix-issue-1-1",
            model="gpt-4o-mini",
            status="queued",
            workspace_id=workspace_id,
        )

    store.start_run(record(1))
    store.start_run(record(2))
    store.start_run(record(None))

    workspace_one = store.list_runs(workspace_id=1)
    assert {run.get("workspace_id") for run in workspace_one} == {1, None}
    assert len(store.list_runs()) == 3


def test_mock_oauth_creates_real_user_and_settings_shows_it(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CALLBACK_URL", "http://testserver/auth/github/callback")
    monkeypatch.setenv("GITHUB_OAUTH_MOCK_ENABLED", "true")
    db_url = f"sqlite:///{tmp_path / 'runs.sqlite3'}"
    client = TestClient(create_app(db_url))

    response = client.get("/auth/github/callback?code=mock", follow_redirects=False)
    assert response.status_code == 303

    store = RunStore(db_url)
    user = store.get_user_by_login("mock-github-user")
    assert user is not None
    assert user["github_user_id"] == "424242"
    assert user["avatar_url"].startswith("https://avatars.githubusercontent.com")

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert "mock-github-user" in settings_page.text

    account_page = client.get("/account")
    assert account_page.status_code == 200
    assert "Mock GitHub User" in account_page.text
    assert "Sign out" in account_page.text


def test_logout_clears_session(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "alex")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-session-secret")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'runs.sqlite3'}"))

    login_page = client.get("/login")
    csrf = login_page.text.split('name="csrf_token" value="')[1].split('"')[0]
    client.post(
        "/login",
        data={"username": "alex", "password": "secret-pass", "next_path": "/runs", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert client.get("/runs").status_code == 200

    settings_page = client.get("/settings")
    csrf = settings_page.text.split('name="csrf_token" value="')[1].split('"')[0]
    response = client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 303

    redirected = client.get("/runs", follow_redirects=False)
    assert redirected.status_code == 303
    assert "/login" in redirected.headers["location"]
