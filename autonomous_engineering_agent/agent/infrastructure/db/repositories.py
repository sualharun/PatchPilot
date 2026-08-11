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
                installation_id=run.installation_id,
                workspace_id=run.workspace_id,
                max_attempts=run.max_attempts,
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

    def list_runs(self, limit: int = 50, workspace_id: int | None = None) -> list[dict[str, Any]]:
        return self._store.list_runs(limit, workspace_id=workspace_id)

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

    def requeue_for_retry(self, run_id: int, *, backoff_seconds: int, error: str) -> None:
        self._store.requeue_for_retry(run_id, backoff_seconds=backoff_seconds, error=error)

    def mark_dead_letter(self, run_id: int, *, error: str) -> None:
        self._store.mark_dead_letter(run_id, error=error)


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

    def get_account_context(self, login: str | None = None) -> dict[str, Any]:
        return self._store.get_account_context(login)

    def seed_defaults(self, *, username: str, workspace_name: str = "PatchPilot") -> None:
        self._store.seed_defaults(username=username, workspace_name=workspace_name)

    def get_or_create_user(self, *, email: str, name: str) -> dict[str, Any]:
        return self._store.get_or_create_user(email=email, name=name)

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return self._store.get_user_by_email(email)

    def get_user_by_login(self, login: str) -> dict[str, Any] | None:
        return self._store.get_user_by_login(login)

    def create_password_user(self, *, email: str, name: str, password_hash: str) -> dict[str, Any]:
        return self._store.create_password_user(email=email, name=name, password_hash=password_hash)

    def set_user_password(self, *, user_id: int, password_hash: str) -> None:
        self._store.set_user_password(user_id=user_id, password_hash=password_hash)

    def set_onboarding_completed(self, *, user_id: int) -> None:
        self._store.set_onboarding_completed(user_id=user_id)

    def add_email_verification_token(self, *, user_id: int, token_hash: str, expires_at: str) -> int:
        return self._store.add_email_verification_token(
            user_id=user_id, token_hash=token_hash, expires_at=expires_at
        )

    def consume_email_verification_token(self, token_hash: str) -> dict[str, Any] | None:
        return self._store.consume_email_verification_token(token_hash)

    def update_user_email(self, *, user_id: int, email: str, update_login: bool) -> None:
        self._store.update_user_email(user_id=user_id, email=email, update_login=update_login)

    def upsert_github_user(
        self,
        *,
        github_user_id: str,
        login: str,
        name: str,
        email: str,
        avatar_url: str | None = None,
    ) -> dict[str, Any]:
        return self._store.upsert_user_from_github(
            github_user_id=github_user_id,
            login=login,
            name=name,
            email=email,
            avatar_url=avatar_url,
        )

    def workspace_for_login(self, login: str | None) -> dict[str, Any] | None:
        return self._store.workspace_for_login(login)


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


class SqlWebhookDeliveryRepository:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    def record_delivery(self, *, delivery_id: str, event: str, action: str | None) -> bool:
        return self._store.record_webhook_delivery(delivery_id=delivery_id, event=event, action=action)

    def finish_delivery(self, delivery_id: str, *, status: str, error: str | None = None) -> None:
        self._store.finish_webhook_delivery(delivery_id, status=status, error=error)

    def list_deliveries(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._store.list_webhook_deliveries(limit)


class SqlGitHubAppRepository:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    def upsert_installation(
        self,
        *,
        installation_id: str,
        account_login: str,
        account_type: str = "User",
        status: str = "active",
    ) -> int:
        return self._store.upsert_github_app_installation(
            installation_id=installation_id,
            account_login=account_login,
            account_type=account_type,
            status=status,
        )

    def set_installation_status(self, installation_id: str, status: str) -> None:
        self._store.set_github_app_installation_status(installation_id, status)

    def get_installation(self, installation_id: str) -> dict[str, Any] | None:
        return self._store.get_github_app_installation(installation_id)

    def list_installations(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._store.list_github_app_installations(limit)

    def add_repository(
        self,
        *,
        installation_id: str,
        full_name: str,
        github_repo_id: int | None = None,
        private: bool = False,
    ) -> int:
        return self._store.add_github_app_repository(
            installation_id=installation_id,
            full_name=full_name,
            github_repo_id=github_repo_id,
            private=private,
        )

    def remove_repository(self, *, installation_id: str, full_name: str) -> None:
        self._store.remove_github_app_repository(installation_id=installation_id, full_name=full_name)

    def installation_for_repository(self, full_name: str) -> str | None:
        return self._store.installation_for_repository(full_name)

    def list_repositories(self, installation_id: str | None = None) -> list[dict[str, Any]]:
        return self._store.list_github_app_repositories(installation_id)


class SqlBillingRepository:
    """RunStore already implements the billing methods; this keeps the port boundary explicit."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def __getattr__(self, name: str):
        if name in {
            "upsert_stripe_customer",
            "stripe_customer_for_workspace",
            "workspace_for_stripe_customer",
            "upsert_subscription",
            "subscription_for_workspace",
            "set_workspace_limits",
            "get_workspace_limits",
            "add_usage",
            "usage_this_month",
        }:
            return getattr(self._store, name)
        raise AttributeError(name)


class SqlEvalReportRepository:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    def list_eval_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._store.list_eval_reports(limit)
