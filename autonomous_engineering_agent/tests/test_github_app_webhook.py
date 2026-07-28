import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from agent.application.commands.handle_github_app_webhook import (
    GitHubAppWebhookCommand,
    GitHubAppWebhookSettings,
    HandleGitHubAppWebhookHandler,
)
from agent.application.commands.queue_run import QueueRunHandler
from agent.dashboard import create_app
from agent.infrastructure.clock import SystemClock
from agent.infrastructure.db.repositories import (
    SqlAuditLog,
    SqlGitHubAppRepository,
    SqlRunRepository,
    SqlWebhookDeliveryRepository,
)
from agent.infrastructure.db.store import RunStore


def _handler(store, *, trigger_label="patchpilot", pr_analysis=None):
    audit = SqlAuditLog(store)
    return HandleGitHubAppWebhookHandler(
        deliveries=SqlWebhookDeliveryRepository(store),
        installations=SqlGitHubAppRepository(store),
        queue_run=QueueRunHandler(SqlRunRepository(store), audit, SystemClock()),
        pr_analysis=pr_analysis,
        audit_log=audit,
        settings=GitHubAppWebhookSettings(default_model="gpt-4o-mini", trigger_label=trigger_label),
    )


def _installation_payload(action="created", installation_id=42):
    return {
        "action": action,
        "installation": {
            "id": installation_id,
            "account": {"login": "octocat", "type": "User"},
        },
        "repositories": [
            {"id": 1, "full_name": "octocat/hello", "private": False},
        ],
    }


def _issue_payload(action="opened", repo="octocat/hello", number=7, label=None, installation_id=None):
    payload = {
        "action": action,
        "issue": {"number": number, "title": "Bug"},
        "repository": {"id": 1, "full_name": repo, "owner": {"login": repo.split("/")[0], "type": "User"}},
        "sender": {"login": "alice"},
    }
    if label:
        payload["label"] = {"name": label}
    if installation_id is not None:
        payload["installation"] = {"id": installation_id}
    return payload


def test_installation_created_stores_installation_and_repositories(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    handler = _handler(store)

    result = handler.execute(
        GitHubAppWebhookCommand(event="installation", delivery_id="d-1", payload=_installation_payload())
    )

    assert result.status == "processed"
    installation = store.get_github_app_installation("42")
    assert installation is not None
    assert installation["account_login"] == "octocat"
    assert installation["status"] == "active"
    assert store.installation_for_repository("octocat/hello") == "42"


def test_installation_deleted_marks_repositories_removed(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    handler = _handler(store)
    handler.execute(GitHubAppWebhookCommand(event="installation", delivery_id="d-1", payload=_installation_payload()))

    handler.execute(
        GitHubAppWebhookCommand(
            event="installation", delivery_id="d-2", payload=_installation_payload(action="deleted")
        )
    )

    assert store.get_github_app_installation("42")["status"] == "deleted"
    assert store.installation_for_repository("octocat/hello") is None


def test_installation_repositories_added_and_removed(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    handler = _handler(store)
    handler.execute(GitHubAppWebhookCommand(event="installation", delivery_id="d-1", payload=_installation_payload()))

    handler.execute(
        GitHubAppWebhookCommand(
            event="installation_repositories",
            delivery_id="d-2",
            payload={
                "action": "added",
                "installation": {"id": 42, "account": {"login": "octocat"}},
                "repositories_added": [{"id": 2, "full_name": "octocat/second", "private": True}],
                "repositories_removed": [{"full_name": "octocat/hello"}],
            },
        )
    )

    assert store.installation_for_repository("octocat/second") == "42"
    assert store.installation_for_repository("octocat/hello") is None


def test_duplicate_delivery_is_ignored(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    handler = _handler(store)
    first = handler.execute(
        GitHubAppWebhookCommand(event="installation", delivery_id="dup-1", payload=_installation_payload())
    )
    second = handler.execute(
        GitHubAppWebhookCommand(event="installation", delivery_id="dup-1", payload=_installation_payload())
    )

    assert first.status == "processed"
    assert second.status == "duplicate"
    deliveries = store.list_webhook_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "processed"


def test_issue_opened_enqueues_run_for_installed_repo(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    handler = _handler(store)
    handler.execute(GitHubAppWebhookCommand(event="installation", delivery_id="d-1", payload=_installation_payload()))

    result = handler.execute(
        GitHubAppWebhookCommand(event="issues", delivery_id="d-2", payload=_issue_payload())
    )

    assert result.status == "queued"
    run = store.get_run(int(result.detail["run_id"]))
    assert run["status"] == "queued"
    assert run["repo"] == "octocat/hello"
    assert run["installation_id"] == "42"


def test_issue_on_uninstalled_repo_is_ignored(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    handler = _handler(store)

    result = handler.execute(
        GitHubAppWebhookCommand(event="issues", delivery_id="d-1", payload=_issue_payload(repo="not/installed"))
    )

    assert result.status == "ignored"
    assert store.list_runs() == []


def test_issue_event_self_heals_installation_from_payload(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    handler = _handler(store)

    result = handler.execute(
        GitHubAppWebhookCommand(
            event="issues", delivery_id="d-1", payload=_issue_payload(installation_id=77)
        )
    )

    assert result.status == "queued"
    assert store.installation_for_repository("octocat/hello") == "77"


def test_labeled_issue_requires_trigger_label(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    handler = _handler(store)
    handler.execute(GitHubAppWebhookCommand(event="installation", delivery_id="d-1", payload=_installation_payload()))

    wrong = handler.execute(
        GitHubAppWebhookCommand(
            event="issues", delivery_id="d-2", payload=_issue_payload(action="labeled", label="bug")
        )
    )
    right = handler.execute(
        GitHubAppWebhookCommand(
            event="issues", delivery_id="d-3", payload=_issue_payload(action="labeled", label="patchpilot")
        )
    )

    assert wrong.status == "ignored"
    assert right.status == "queued"


def test_pull_request_issue_payload_never_enqueues(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    handler = _handler(store)
    handler.execute(GitHubAppWebhookCommand(event="installation", delivery_id="d-1", payload=_installation_payload()))
    payload = _issue_payload()
    payload["issue"]["pull_request"] = {"url": "https://api.github.com/repos/octocat/hello/pulls/7"}

    result = handler.execute(GitHubAppWebhookCommand(event="issues", delivery_id="d-2", payload=payload))

    assert result.status == "ignored"
    assert store.list_runs() == []


def test_error_recorded_on_delivery(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")

    class ExplodingInstallations(SqlGitHubAppRepository):
        def upsert_installation(self, **kwargs):
            raise RuntimeError("boom")

    audit = SqlAuditLog(store)
    handler = HandleGitHubAppWebhookHandler(
        deliveries=SqlWebhookDeliveryRepository(store),
        installations=ExplodingInstallations(store),
        queue_run=QueueRunHandler(SqlRunRepository(store), audit, SystemClock()),
        pr_analysis=None,
        audit_log=audit,
        settings=GitHubAppWebhookSettings(),
    )

    try:
        handler.execute(
            GitHubAppWebhookCommand(event="installation", delivery_id="d-err", payload=_installation_payload())
        )
    except RuntimeError:
        pass

    deliveries = store.list_webhook_deliveries()
    assert deliveries[0]["status"] == "error"
    assert "boom" in (deliveries[0]["error"] or "")


def test_github_app_webhook_endpoint_verifies_signature_and_queues(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "app-secret")
    db_url = f"sqlite:///{tmp_path / 'runs.sqlite3'}"
    client = TestClient(create_app(db_url))

    store = RunStore(db_url)
    store.upsert_github_app_installation(installation_id="42", account_login="octocat")
    store.add_github_app_repository(installation_id="42", full_name="octocat/hello")

    body = json.dumps(_issue_payload()).encode()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()

    unauthorized = client.post(
        "/webhooks/github-app",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "issues", "X-GitHub-Delivery": "x"},
    )
    assert unauthorized.status_code == 401

    response = client.post(
        "/webhooks/github-app",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-1",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"

    duplicate = client.post(
        "/webhooks/github-app",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-1",
            "Content-Type": "application/json",
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
