"""Inbound ports called by HTTP, CLI, and worker adapters."""

from __future__ import annotations

from typing import Any, Protocol

from agent.application.commands.handle_pr_webhook import (
    EnqueuePullRequestAnalysisCommand,
)
from agent.application.commands.queue_run import QueueRunCommand, QueueRunResult
from agent.application.dto import AgentRunResult, ExecuteRunCommand
from agent.application.queries.dashboard import RunStats
from agent.application.services.pr_analysis import PRWorkerResult
from agent.application.services.run_worker import WorkerResult
from agent.domain.entities import PRAnalysisJob


class QueueRun(Protocol):
    def execute(self, command: QueueRunCommand) -> QueueRunResult: ...


class EnqueuePullRequestAnalysis(Protocol):
    def execute(self, command: EnqueuePullRequestAnalysisCommand) -> PRAnalysisJob | None: ...


class ExecuteEngineeringRun(Protocol):
    def execute(self, command: ExecuteRunCommand) -> AgentRunResult: ...


class DashboardQueries(Protocol):
    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]: ...

    def get_run(self, run_id: int) -> dict[str, Any]: ...

    def stats(self, limit: int = 250) -> RunStats: ...

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def list_artifacts(self, location: str) -> list[str]: ...

    def account(self) -> dict[str, Any]: ...

    def repositories(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def provider_keys(self) -> list[dict[str, Any]]: ...

    def github_connection(self) -> dict[str, Any] | None: ...

    def eval_reports(self, limit: int = 20) -> list[dict[str, Any]]: ...

    def test_overview(self, limit: int = 100) -> dict[str, Any]: ...

    def billing_overview(self, limit: int = 500) -> dict[str, Any]: ...


class ProcessQueuedRuns(Protocol):
    def execute(self, *, limit: int = 1) -> WorkerResult: ...


class ProcessPullRequestJobs(Protocol):
    def execute(self, *, limit: int | None = None) -> PRWorkerResult: ...
