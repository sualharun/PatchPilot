"""SQL-backed outbound adapters.

These classes are the only place application ports are translated to the
legacy ``RunStore`` persistence API. They can be replaced by SQLAlchemy or a
different database without changing commands, queries, or domain objects.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.domain.entities import QueuedRun
from agent.infrastructure.db.store import RunRecord, RunStore


class SqlRunRepository:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    def queue_run(self, run: QueuedRun) -> int:
        return self._store.start_run(
            RunRecord(
                issue_url=run.issue.url,
                repo=run.issue.repository.full_name,
                branch=run.branch,
                model=run.model,
                status=run.status.value,
                iterations=0,
                max_iterations=run.max_iterations,
                open_pr=run.open_pr,
                commands=[],
                tool_calls=[],
                patches=[],
                test_results=[],
            )
        )

    def start_run(self, run: Mapping[str, Any]) -> int:
        return self._store.start_run(RunRecord(**dict(run)))

    def update_run(self, run_id: int, **fields: Any) -> None:
        self._store.update_run(run_id, **fields)

    def finish_run(self, run_id: int, status: str, **fields: Any) -> None:
        self._store.finish_run(run_id, status, **fields)

    def get_run(self, run_id: int) -> dict[str, Any]:
        return self._store.get_run(run_id)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._store.list_runs(limit)

    def list_runs_by_status(self, status: str, limit: int = 10) -> list[dict[str, Any]]:
        return self._store.list_runs_by_status(status, limit)

    def claim_next_queued_run(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> dict[str, Any] | None:
        return self._store.claim_next_queued_run(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )


class SqlAuditLog:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    def record_event(
        self,
        *,
        actor: str,
        event: str,
        target: str,
        result: str = "success",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        return self._store.add_audit_event(
            actor=actor,
            event=event,
            target=target,
            result=result,
            metadata=metadata,
        )

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._store.list_audit_events(limit)


class SqlAccountRepository:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    def get_account_context(self) -> dict[str, Any]:
        return self._store.get_account_context()

    def seed_defaults(self, *, username: str, workspace_name: str = "PatchPilot") -> None:
        self._store.seed_defaults(username=username, workspace_name=workspace_name)

    def get_or_create_user(self, *, email: str, name: str) -> dict[str, Any]:
        return self._store.get_or_create_user(email=email, name=name)


class SqlRepositoryCatalog:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    def list_repositories(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._store.list_repositories(limit)

    def upsert_repository(self, *, full_name: str, workspace_id: int | None = None, **fields: Any) -> int:
        return self._store.upsert_repository(full_name=full_name, workspace_id=workspace_id, **fields)


class SqlProviderKeyRepository:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    def list_provider_keys(self) -> list[dict[str, Any]]:
        return self._store.list_provider_keys()

    def upsert_provider_key(self, *, workspace_id: int | None, provider: str, key_hint: str) -> int:
        return self._store.upsert_provider_key(workspace_id=workspace_id, provider=provider, key_hint=key_hint)


class SqlGitHubConnectionRepository:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    def get_github_connection(self) -> dict[str, Any] | None:
        return self._store.get_github_connection()

    def upsert_github_connection(
        self,
        *,
        user_id: int | None,
        login: str,
        scopes: str,
        token_hint: str,
        installation_id: str | None = None,
    ) -> int:
        return self._store.upsert_github_connection(
            user_id=user_id,
            login=login,
            scopes=scopes,
            token_hint=token_hint,
            installation_id=installation_id,
        )


class SqlEvalReportRepository:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    def list_eval_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._store.list_eval_reports(limit)
