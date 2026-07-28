from __future__ import annotations

from agent.infrastructure.db.store import RunRecord, RunStore


def seed_database(store: RunStore, *, profile: str = "local-dev") -> dict[str, int | str]:
    if profile not in {"local-dev", "demo"}:
        raise ValueError("profile must be one of: local-dev, demo")

    store.seed_defaults(username="alex", workspace_name="PatchPilot")
    account = store.get_account_context()
    workspace_id = int(account["workspace"]["id"]) if account["workspace"].get("id") else None
    user_id = int(account["user"]["id"]) if account["user"].get("id") else None

    repositories = [
        ("acme/api", "main", "pyproject.toml", "pytest -q", "seeded"),
        ("acme/worker", "main", "requirements.txt", "pytest tests", "seeded"),
        ("acme/webhooks", "main", "pyproject.toml", "pytest -q tests/webhooks", "seeded"),
    ]
    for full_name, default_branch, python_setup, test_command, status in repositories:
        store.upsert_repository(
            workspace_id=workspace_id,
            full_name=full_name,
            default_branch=default_branch,
            python_setup=python_setup,
            test_command=test_command,
            config_status=status,
        )

    store.upsert_provider_key(workspace_id=workspace_id, provider="openai", key_hint="sk-...demo")
    store.upsert_provider_key(workspace_id=workspace_id, provider="anthropic", key_hint="sk-ant-...demo")
    store.upsert_github_connection(
        user_id=user_id,
        login="alex-morgan",
        token_hint="github_pat_...demo",
        scopes="read:user user:email repo",
        installation_id="demo-installation",
    )

    created_runs = 0
    if not store.list_runs(limit=1):
        demo_runs = [
            RunRecord(
                issue_url="https://github.com/acme/api/issues/4821",
                repo="acme/api",
                branch="agent/fix-issue-4821",
                model="claude-3-5-sonnet-latest",
                status="pr_opened",
                iterations=2,
                commands=[{"command": "pytest -q", "exit_code": 0, "runtime_seconds": 252}],
                tool_calls=[{"name": "read_file", "ok": True}, {"name": "apply_patch", "ok": True}],
                patches=["diff --git a/tests/test_user.py b/tests/test_user.py"],
                test_results=[{"command": "pytest -q", "exit_code": 0, "passed": 42, "failed": 0}],
                token_usage={"input_tokens": 12000, "output_tokens": 2200, "total_tokens": 14200},
                estimated_cost_usd=0.68,
                pr_url="https://github.com/acme/api/pull/4824",
                summary="Fixed failing user creation validation.",
            ),
            RunRecord(
                issue_url="https://github.com/acme/worker/issues/367",
                repo="acme/worker",
                branch="agent/fix-issue-367",
                model="gpt-4.1",
                status="success",
                iterations=1,
                commands=[{"command": "pytest tests", "exit_code": 0, "runtime_seconds": 438}],
                test_results=[{"command": "pytest tests", "exit_code": 0, "passed": 55, "failed": 0}],
                token_usage={"input_tokens": 9000, "output_tokens": 1800, "total_tokens": 10800},
                estimated_cost_usd=0.91,
                summary="Improved retry logic for job processing.",
            ),
            RunRecord(
                issue_url="https://github.com/acme/api/issues/4810",
                repo="acme/api",
                branch="agent/fix-issue-4810",
                model="claude-3-5-sonnet-latest",
                status="failed_tests",
                iterations=3,
                commands=[{"command": "pytest -q", "exit_code": 1, "runtime_seconds": 363}],
                test_results=[{"command": "pytest -q", "exit_code": 1, "passed": 29, "failed": 8}],
                token_usage={"input_tokens": 15000, "output_tokens": 3100, "total_tokens": 18100},
                estimated_cost_usd=0.66,
                summary="Auth refresh flow still has failing tests after retries.",
            ),
        ]
        for run in demo_runs:
            store.start_run(run)
            created_runs += 1

    store.add_audit_event(
        actor="seed",
        event="database.seeded",
        target=profile,
        metadata={"repositories": len(repositories), "runs_created": created_runs},
    )
    return {"profile": profile, "repositories": len(repositories), "runs_created": created_runs}
