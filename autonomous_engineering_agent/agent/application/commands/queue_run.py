from __future__ import annotations

from dataclasses import dataclass

from agent.application.ports.outbound import AuditLog, Clock, RunRepository
from agent.domain.entities import QueuedRun
from agent.domain.services import branch_name_for_issue, normalize_max_iterations
from agent.domain.value_objects import IssueRef


@dataclass(frozen=True, slots=True)
class QueueRunCommand:
    issue: IssueRef
    model: str
    max_iterations: int
    open_pr: bool
    requested_by: str
    installation_id: str | None = None
    workspace_id: int | None = None


@dataclass(frozen=True, slots=True)
class QueueRunResult:
    run_id: int
    branch: str


class QueueRunHandler:
    """Inbound command handler for scheduling an autonomous issue run."""

    def __init__(self, runs: RunRepository, audit_log: AuditLog, clock: Clock) -> None:
        self._runs = runs
        self._audit_log = audit_log
        self._clock = clock

    def execute(self, command: QueueRunCommand) -> QueueRunResult:
        max_iterations = normalize_max_iterations(command.max_iterations)
        branch = branch_name_for_issue(command.issue.number, self._clock.timestamp())
        queued_run = QueuedRun(
            issue=command.issue,
            model=command.model,
            branch=branch,
            max_iterations=max_iterations,
            open_pr=command.open_pr,
            requested_by=command.requested_by,
            installation_id=command.installation_id,
            workspace_id=command.workspace_id,
        )
        run_id = self._runs.queue_run(queued_run)
        self._audit_log.record_event(
            actor=command.requested_by,
            event="run.queued",
            target=command.issue.url,
            metadata={
                "run_id": run_id,
                "model": command.model,
                "max_iterations": max_iterations,
                "open_pr": command.open_pr,
            },
        )
        return QueueRunResult(run_id=run_id, branch=branch)
