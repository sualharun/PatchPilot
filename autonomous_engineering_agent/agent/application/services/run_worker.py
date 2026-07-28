from __future__ import annotations

from dataclasses import dataclass

from agent.application.dto import ExecuteRunCommand
from agent.application.ports.outbound import AuditLog, EngineeringRunExecutorFactory, RunRepository, SecretRedactor
from agent.domain.enums import RunStatus
from agent.domain.services import parse_issue_ref


@dataclass(frozen=True, slots=True)
class RunWorkerSettings:
    worker_id: str
    lease_seconds: int
    max_attempts: int
    retry_backoff_seconds: int = 30


@dataclass(frozen=True, slots=True)
class WorkerResult:
    processed: int
    succeeded: int
    failed: int


class ProcessQueuedRunsHandler:
    def __init__(
        self,
        *,
        runs: RunRepository,
        audit_log: AuditLog,
        executors: EngineeringRunExecutorFactory,
        redactor: SecretRedactor,
        settings: RunWorkerSettings,
    ) -> None:
        self._runs = runs
        self._audit_log = audit_log
        self._executors = executors
        self._redactor = redactor
        self._settings = settings

    def execute(self, *, limit: int = 1) -> WorkerResult:
        processed = succeeded = failed = 0
        for _ in range(max(0, limit)):
            queued = self._runs.claim_next_queued_run(
                worker_id=self._settings.worker_id,
                lease_seconds=self._settings.lease_seconds,
                max_attempts=self._settings.max_attempts,
            )
            if queued is None:
                break
            processed += 1
            run_id = int(queued["id"])
            try:
                model = str(queued["model"])
                installation_id = str(queued["installation_id"]) if queued.get("installation_id") else None
                result = self._executors.for_model(model, installation_id).execute(
                    ExecuteRunCommand(
                        issue=parse_issue_ref(str(queued["issue_url"])),
                        model=model,
                        max_iterations=int(queued.get("max_iterations") or queued.get("iterations") or 5),
                        open_pr=bool(queued.get("open_pr")),
                        run_id=run_id,
                    )
                )
                self._audit_log.record_event(
                    actor="worker",
                    event="run.completed",
                    target=str(queued["issue_url"]),
                    result=result.status,
                    metadata={"run_id": run_id, "logs_path": str(result.logs_path)},
                )
                if result.status in {RunStatus.SUCCESS.value, RunStatus.PR_OPENED.value}:
                    succeeded += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                attempts = int(queued.get("attempts") or 1)
                row_max_attempts = int(queued.get("max_attempts") or self._settings.max_attempts)
                safe_error = self._redactor.redact(str(exc))
                exhausted = attempts >= row_max_attempts
                if exhausted:
                    status = RunStatus.DEAD_LETTER.value
                    self._runs.mark_dead_letter(run_id, error=safe_error)
                else:
                    status = RunStatus.QUEUED.value
                    backoff = self._settings.retry_backoff_seconds * attempts
                    self._runs.requeue_for_retry(run_id, backoff_seconds=backoff, error=safe_error)
                self._audit_log.record_event(
                    actor="worker",
                    event="run.failed" if not exhausted else "run.dead_letter",
                    target=str(queued.get("issue_url") or run_id),
                    result=status,
                    metadata={
                        "run_id": run_id,
                        "attempts": attempts,
                        "max_attempts": row_max_attempts,
                        "error": safe_error,
                        "retrying": not exhausted,
                    },
                )
        return WorkerResult(processed=processed, succeeded=succeeded, failed=failed)
