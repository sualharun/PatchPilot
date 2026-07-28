from pathlib import Path

from agent.application.dto import AgentRunResult
from agent.application.services.run_worker import ProcessQueuedRunsHandler, RunWorkerSettings
from agent.domain.enums import RunStatus
from agent.infrastructure.db.repositories import SqlAuditLog, SqlRunRepository
from agent.infrastructure.db.store import RunRecord, RunStore
from agent.infrastructure.security import EnvironmentSecretRedactor


class _FailingExecutor:
    def execute(self, command):
        raise RuntimeError("boom: sk-superSecretApiKey1234567890")


class _FailingExecutorFactory:
    def for_model(self, model, installation_id=None):
        return _FailingExecutor()


class _SucceedingExecutor:
    def execute(self, command):
        return AgentRunResult(
            status=RunStatus.SUCCESS.value,
            summary="patched successfully",
            tests_run=["pytest -q"],
            tests_passed=True,
            branch="agent/fix-issue-1-1",
            pr_url=None,
            logs_path=Path("logs/1"),
            repo_path=Path("/tmp/repo"),
        )


class _SucceedingExecutorFactory:
    def for_model(self, model, installation_id=None):
        return _SucceedingExecutor()


def _queue(store, *, max_attempts=3):
    return store.start_run(
        RunRecord(
            issue_url="https://github.com/octo/example/issues/1",
            repo="octo/example",
            branch="agent/fix-issue-1-1",
            model="gpt-4o-mini",
            status="queued",
            max_attempts=max_attempts,
        )
    )


def test_claim_next_queued_run_is_atomic_across_replicas(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    run_id = _queue(store)

    first = store.claim_next_queued_run(worker_id="replica-a", lease_seconds=60, max_attempts=3)
    second = store.claim_next_queued_run(worker_id="replica-b", lease_seconds=60, max_attempts=3)

    assert first is not None
    assert first["id"] == run_id
    assert first["worker_id"] == "replica-a"
    assert second is None  # replica-b must not double-claim the same row


def test_worker_retries_transient_failures_with_backoff(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    _queue(store, max_attempts=3)
    handler = ProcessQueuedRunsHandler(
        runs=SqlRunRepository(store),
        audit_log=SqlAuditLog(store),
        executors=_FailingExecutorFactory(),
        redactor=EnvironmentSecretRedactor(),
        settings=RunWorkerSettings(worker_id="w1", lease_seconds=60, max_attempts=3, retry_backoff_seconds=10),
    )

    result = handler.execute(limit=1)

    assert result.processed == 1
    assert result.failed == 1
    run = store.list_runs()[0]
    assert run["status"] == "queued"
    assert run["attempts"] == 1
    assert "[REDACTED]" in (run["last_error"] or "")
    assert "sk-superSecretApiKey1234567890" not in (run["last_error"] or "")
    # Backoff must push the lease into the future so it is not reclaimed immediately.
    assert run["leased_until"] is not None


def test_worker_moves_to_dead_letter_after_max_attempts(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    _queue(store, max_attempts=2)
    handler = ProcessQueuedRunsHandler(
        runs=SqlRunRepository(store),
        audit_log=SqlAuditLog(store),
        executors=_FailingExecutorFactory(),
        redactor=EnvironmentSecretRedactor(),
        settings=RunWorkerSettings(worker_id="w1", lease_seconds=60, max_attempts=2, retry_backoff_seconds=1),
    )

    handler.execute(limit=1)  # attempt 1: requeued
    # Force the run immediately claimable again for the second attempt.
    store.update_run(1, leased_until=None)
    result = handler.execute(limit=1)  # attempt 2: exhausted -> dead_letter

    assert result.failed == 1
    run = store.get_run(1)
    assert run["status"] == "dead_letter"
    assert run["attempts"] == 2
    assert run["ended_at"] is not None


def test_worker_succeeds_and_records_completion(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    _queue(store)
    handler = ProcessQueuedRunsHandler(
        runs=SqlRunRepository(store),
        audit_log=SqlAuditLog(store),
        executors=_SucceedingExecutorFactory(),
        redactor=EnvironmentSecretRedactor(),
        settings=RunWorkerSettings(worker_id="w1", lease_seconds=60, max_attempts=3),
    )

    result = handler.execute(limit=1)

    assert result.succeeded == 1
    events = store.list_audit_events()
    assert any(event["event"] == "run.completed" for event in events)


def test_dead_letter_is_a_valid_run_status(tmp_path):
    store = RunStore(f"sqlite:///{tmp_path / 'runs.sqlite3'}")
    run_id = _queue(store)

    store.mark_dead_letter(run_id, error="exhausted retries")

    run = store.get_run(run_id)
    assert run["status"] == "dead_letter"
    assert run["last_error"] == "exhausted retries"
