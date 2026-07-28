"""Read-side query handlers for the developer dashboard.

The HTTP adapter renders these projections but never queries persistence
directly. This keeps SQL and aggregation rules out of routes and templates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from agent.application.ports.outbound import (
    AccountRepository,
    ArtifactCatalog,
    AuditLog,
    EvalReportRepository,
    GitHubConnectionRepository,
    ProviderKeyRepository,
    RepositoryCatalog,
    RunRepository,
)


@dataclass(frozen=True, slots=True)
class RunStats:
    total_runs: int
    success_rate: float
    median_runtime_seconds: float
    total_cost_usd: float
    status_distribution: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DashboardQueryService:
    def __init__(
        self,
        *,
        runs: RunRepository,
        audit_log: AuditLog,
        accounts: AccountRepository,
        repositories: RepositoryCatalog,
        provider_keys: ProviderKeyRepository,
        github_connections: GitHubConnectionRepository,
        eval_reports: EvalReportRepository,
        artifacts: ArtifactCatalog,
    ) -> None:
        self._runs = runs
        self._audit_log = audit_log
        self._accounts = accounts
        self._repositories = repositories
        self._provider_keys = provider_keys
        self._github_connections = github_connections
        self._eval_reports = eval_reports
        self._artifacts = artifacts

    def list_runs(self, limit: int = 50, workspace_id: int | None = None) -> list[dict[str, Any]]:
        return self._runs.list_runs(limit=max(1, min(limit, 500)), workspace_id=workspace_id)

    def get_run(self, run_id: int) -> dict[str, Any]:
        return self._runs.get_run(run_id)

    def stats(self, limit: int = 250) -> RunStats:
        runs = self.list_runs(limit)
        return calculate_run_stats(runs)

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._audit_log.list_events(limit=max(1, min(limit, 500)))

    def list_artifacts(self, location: str) -> list[str]:
        return self._artifacts.list_artifacts(location)

    def account(self, login: str | None = None) -> dict[str, Any]:
        return self._accounts.get_account_context(login)

    def workspace_for_login(self, login: str | None) -> dict[str, Any] | None:
        return self._accounts.workspace_for_login(login)

    def repositories(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._repositories.list_repositories(limit=max(1, min(limit, 500)))

    def provider_keys(self) -> list[dict[str, Any]]:
        return self._provider_keys.list_provider_keys()

    def github_connection(self) -> dict[str, Any] | None:
        return self._github_connections.get_github_connection()

    def eval_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._eval_reports.list_eval_reports(limit=max(1, min(limit, 100)))

    def test_overview(self, limit: int = 100, workspace_id: int | None = None) -> dict[str, Any]:
        runs = self.list_runs(limit, workspace_id=workspace_id)
        return {
            "runs": runs,
            "passing": sum(1 for run in runs if run.get("status") in {"success", "pr_opened"}),
            "running": sum(1 for run in runs if run.get("status") in {"queued", "running"}),
            "failed": sum(1 for run in runs if run.get("status") == "failed_tests"),
            "setup_failed": sum(1 for run in runs if run.get("status") == "setup_failed"),
        }

    def billing_overview(self, limit: int = 500, workspace_id: int | None = None) -> dict[str, Any]:
        runs = self.list_runs(limit, workspace_id=workspace_id)
        total_cost = sum(float(run.get("estimated_cost_usd") or 0) for run in runs)
        by_model: dict[str, dict[str, float]] = {}
        for run in runs:
            model = str(run.get("model") or "unknown")
            usage = run.get("token_usage") or {}
            bucket = by_model.setdefault(model, {"runs": 0, "tokens": 0, "cost": 0.0})
            bucket["runs"] += 1
            bucket["tokens"] += float(usage.get("total_tokens") or 0)
            bucket["cost"] += float(run.get("estimated_cost_usd") or 0)
        return {
            "runs": runs,
            "total_cost": total_cost,
            "average_cost": total_cost / len(runs) if runs else 0.0,
            "by_model": by_model,
        }


def _runtime_seconds(run: dict[str, Any]) -> float:
    return sum(float(command.get("runtime_seconds") or 0) for command in (run.get("commands") or []))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    middle = len(values) // 2
    if len(values) % 2:
        return round(values[middle], 2)
    return round((values[middle - 1] + values[middle]) / 2, 2)


def calculate_run_stats(runs: list[dict[str, Any]]) -> RunStats:
    runtimes = sorted(_runtime_seconds(run) for run in runs)
    successful = sum(1 for run in runs if run.get("status") in {"success", "pr_opened"})
    distribution: dict[str, int] = {}
    for run in runs:
        status = str(run.get("status") or "unknown")
        distribution[status] = distribution.get(status, 0) + 1
    return RunStats(
        total_runs=len(runs),
        success_rate=round(successful / len(runs) * 100, 1) if runs else 0.0,
        median_runtime_seconds=_median(runtimes),
        total_cost_usd=round(sum(float(run.get("estimated_cost_usd") or 0) for run in runs), 4),
        status_distribution=distribution,
    )
