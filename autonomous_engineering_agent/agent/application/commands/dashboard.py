from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.application.ports.outbound import (
    AccountRepository,
    AuditLog,
    GitHubConnectionRepository,
    GitHubRepositoryGateway,
    ProviderKeyRepository,
    RepositoryCatalog,
)


@dataclass(frozen=True, slots=True)
class RecordAuditCommand:
    actor: str
    event: str
    target: str
    result: str = "success"
    metadata: dict[str, Any] | None = None


class RecordAuditHandler:
    def __init__(self, audit_log: AuditLog) -> None:
        self._audit_log = audit_log

    def execute(self, command: RecordAuditCommand) -> int:
        return self._audit_log.record_event(
            actor=command.actor,
            event=command.event,
            target=command.target,
            result=command.result,
            metadata=command.metadata,
        )


@dataclass(frozen=True, slots=True)
class ConnectGitHubAccountCommand:
    login: str
    email: str
    scopes: str | None
    token_hint: str | None
    installation_id: str | None = None
    github_user_id: str | None = None
    name: str | None = None
    avatar_url: str | None = None


class ConnectGitHubAccountHandler:
    def __init__(
        self,
        accounts: AccountRepository,
        connections: GitHubConnectionRepository,
        audit_log: AuditLog,
    ) -> None:
        self._accounts = accounts
        self._connections = connections
        self._audit_log = audit_log

    def execute(self, command: ConnectGitHubAccountCommand) -> None:
        if command.github_user_id:
            user = self._accounts.upsert_github_user(
                github_user_id=command.github_user_id,
                login=command.login,
                name=command.name or command.login,
                email=command.email,
                avatar_url=command.avatar_url,
            )
        else:
            user = self._accounts.get_or_create_user(email=command.email, name=command.login)
        self._connections.upsert_github_connection(
            user_id=int(user["id"]) if user.get("id") else None,
            login=command.login,
            scopes=command.scopes or "",
            token_hint=command.token_hint or "not stored",
            installation_id=command.installation_id,
        )
        self._audit_log.record_event(actor=command.login, event="auth.github_login", target="dashboard")


class SyncGitHubRepositoriesHandler:
    def __init__(
        self,
        github: GitHubRepositoryGateway,
        accounts: AccountRepository,
        repositories: RepositoryCatalog,
    ) -> None:
        self._github = github
        self._accounts = accounts
        self._repositories = repositories

    def execute(self, *, limit: int = 100) -> list[dict[str, Any]]:
        repositories = self._github.list_accessible_repositories(limit)
        workspace_id = _workspace_id(self._accounts.get_account_context())
        for repository in repositories:
            if repository.get("full_name"):
                self._repositories.upsert_repository(
                    workspace_id=workspace_id,
                    full_name=str(repository["full_name"]),
                    default_branch=repository.get("default_branch"),
                    config_status="github-verified",
                )
        return repositories


class VerifyGitHubRepositoryHandler:
    def __init__(
        self,
        github: GitHubRepositoryGateway,
        accounts: AccountRepository,
        repositories: RepositoryCatalog,
    ) -> None:
        self._github = github
        self._accounts = accounts
        self._repositories = repositories

    def execute(self, full_name: str) -> dict[str, Any]:
        result = self._github.verify_repository_access(full_name)
        self._repositories.upsert_repository(
            workspace_id=_workspace_id(self._accounts.get_account_context()),
            full_name=str(result["full_name"]),
            default_branch=str(result["default_branch"]),
            config_status="github-verified",
        )
        return result


class SeedRuntimeStateHandler:
    def __init__(
        self,
        accounts: AccountRepository,
        provider_keys: ProviderKeyRepository,
    ) -> None:
        self._accounts = accounts
        self._provider_keys = provider_keys

    def execute(
        self,
        *,
        username: str,
        openai_key_hint: str | None,
        anthropic_key_hint: str | None,
    ) -> None:
        self._accounts.seed_defaults(username=username)
        workspace_id = _workspace_id(self._accounts.get_account_context())
        if openai_key_hint:
            self._provider_keys.upsert_provider_key(
                workspace_id=workspace_id,
                provider="openai",
                key_hint=openai_key_hint,
            )
        if anthropic_key_hint:
            self._provider_keys.upsert_provider_key(
                workspace_id=workspace_id,
                provider="anthropic",
                key_hint=anthropic_key_hint,
            )


def _workspace_id(account: dict[str, Any]) -> int | None:
    value = account.get("workspace", {}).get("id")
    return int(value) if value else None
