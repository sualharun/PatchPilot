"""Regression tests for a cross-tenant workspace isolation bug.

`workspace_for_login` used to fall back to `SELECT * FROM workspaces ORDER BY
id ASC LIMIT 1` whenever a *truthy* login had no matching membership. Since a
GitHub App installation's account_login is never a dashboard user, every
webhook-triggered run silently resolved to workspace #1 -- an unrelated
tenant's real workspace -- consuming its billing quota and appearing in its
run history. See the security review for full details.
"""

from agent.application.commands.handle_github_app_webhook import (
    GitHubAppWebhookCommand,
    GitHubAppWebhookSettings,
    HandleGitHubAppWebhookHandler,
)
from agent.application.commands.queue_run import QueueRunHandler
from agent.application.services.billing import BillingService
from agent.domain.billing import PLANS
from agent.infrastructure.clock import SystemClock
from agent.infrastructure.db.repositories import (
    SqlAccountRepository,
    SqlAuditLog,
    SqlBillingRepository,
    SqlGitHubAppRepository,
    SqlRunRepository,
    SqlWebhookDeliveryRepository,
)
from agent.infrastructure.db.store import RunStore


def _wired_handler(store):
    audit = SqlAuditLog(store)
    accounts = SqlAccountRepository(store)
    billing = BillingService(SqlBillingRepository(store))
    return HandleGitHubAppWebhookHandler(
        deliveries=SqlWebhookDeliveryRepository(store),
        installations=SqlGitHubAppRepository(store),
        queue_run=QueueRunHandler(SqlRunRepository(store), audit, SystemClock()),
        pr_analysis=None,
        audit_log=audit,
        settings=GitHubAppWebhookSettings(default_model="gpt-4o-mini"),
        accounts=accounts,
        billing=billing,
    )


def test_unmatched_installation_account_gets_its_own_workspace_not_the_operators(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    # Simulate an already-running deployment: the operator's own workspace
    # (seeded at boot) is workspace #1 and already has a paid, near-exhausted plan.
    store.seed_defaults(username="admin")
    operator_workspace = store.workspace_for_login(None)
    store.set_workspace_limits(workspace_id=int(operator_workspace["id"]), plan="pro", monthly_run_cap=1)
    store.add_usage(workspace_id=int(operator_workspace["id"]), run_id=None, kind="run")  # cap already used

    handler = _wired_handler(store)
    handler.execute(
        GitHubAppWebhookCommand(
            event="installation",
            delivery_id="d-1",
            payload={
                "action": "created",
                "installation": {"id": 42, "account": {"login": "some-external-org", "type": "Organization"}},
                "repositories": [{"id": 1, "full_name": "some-external-org/hello", "private": False}],
            },
        )
    )

    result = handler.execute(
        GitHubAppWebhookCommand(
            event="issues",
            delivery_id="d-2",
            payload={
                "action": "opened",
                "issue": {"number": 7, "title": "Bug"},
                "repository": {
                    "id": 1,
                    "full_name": "some-external-org/hello",
                    "owner": {"login": "some-external-org", "type": "Organization"},
                },
                "sender": {"login": "external-user"},
            },
        )
    )

    assert result.status == "queued", result.detail
    run_workspace_id = result.detail["installation_id"] and store.get_run(int(result.detail["run_id"]))[
        "workspace_id"
    ]
    assert run_workspace_id != int(operator_workspace["id"])

    installer_workspace = store.workspace_for_login("some-external-org")
    assert installer_workspace is not None
    assert int(installer_workspace["id"]) == run_workspace_id
    assert installer_workspace["slug"] == "some-external-org"

    # The operator's own workspace usage must be untouched by the external run.
    assert store.usage_this_month(int(operator_workspace["id"]))["runs"] == 1


def test_installer_workspace_defaults_to_free_plan_and_enforces_its_own_cap(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    store.seed_defaults(username="admin")
    handler = _wired_handler(store)
    handler.execute(
        GitHubAppWebhookCommand(
            event="installation",
            delivery_id="d-1",
            payload={
                "action": "created",
                "installation": {"id": 42, "account": {"login": "free-tier-org", "type": "Organization"}},
                "repositories": [{"id": 1, "full_name": "free-tier-org/hello", "private": False}],
            },
        )
    )

    def open_issue(number, delivery_id):
        return handler.execute(
            GitHubAppWebhookCommand(
                event="issues",
                delivery_id=delivery_id,
                payload={
                    "action": "opened",
                    "issue": {"number": number, "title": "Bug"},
                    "repository": {
                        "id": 1,
                        "full_name": "free-tier-org/hello",
                        "owner": {"login": "free-tier-org", "type": "Organization"},
                    },
                    "sender": {"login": "external-user"},
                },
            )
        )

    results = [open_issue(number, f"delivery-{number}") for number in range(1, PLANS["free"].monthly_run_cap + 2)]

    assert all(r.status == "queued" for r in results[: PLANS["free"].monthly_run_cap])
    assert results[PLANS["free"].monthly_run_cap].status == "limited"


def test_same_login_reuses_workspace_across_installation_and_later_oauth_signup(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    store.seed_defaults(username="admin")
    handler = _wired_handler(store)
    handler.execute(
        GitHubAppWebhookCommand(
            event="installation",
            delivery_id="d-1",
            payload={
                "action": "created",
                "installation": {"id": 42, "account": {"login": "octocat", "type": "User"}},
                "repositories": [{"id": 1, "full_name": "octocat/hello", "private": False}],
            },
        )
    )
    installer_workspace = store.workspace_for_login("octocat")

    user = store.upsert_user_from_github(
        github_user_id="99", login="octocat", name="The Octocat", email="octocat@example.com"
    )

    assert store.workspace_for_login("octocat")["id"] == installer_workspace["id"]
    membership_workspace = store.get_account_context("octocat")["workspace"]
    assert int(membership_workspace["id"]) == int(installer_workspace["id"])
    assert user["login"] == "octocat"
