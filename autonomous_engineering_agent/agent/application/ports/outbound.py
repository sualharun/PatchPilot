"""Outbound ports implemented by infrastructure adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from agent.application.dto import AgentRunResult, ExecuteRunCommand, ExecutionSettings
from agent.domain.entities import IssueContext, PRAnalysisJob, QueuedRun
from agent.domain.value_objects import AgentDecision, CommandResult, IssueRef


class Clock(Protocol):
    def timestamp(self) -> int: ...


class RunRepository(Protocol):
    def queue_run(self, run: QueuedRun) -> int: ...

    def start_run(self, run: Mapping[str, Any]) -> int: ...

    def update_run(self, run_id: int, **fields: Any) -> None: ...

    def finish_run(self, run_id: int, status: str, **fields: Any) -> None: ...

    def get_run(self, run_id: int) -> dict[str, Any]: ...

    def list_runs(self, limit: int = 50, workspace_id: int | None = None) -> list[dict[str, Any]]: ...

    def list_runs_by_status(self, status: str, limit: int = 10) -> list[dict[str, Any]]: ...

    def claim_next_queued_run(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> dict[str, Any] | None: ...

    def requeue_for_retry(self, run_id: int, *, backoff_seconds: int, error: str) -> None: ...

    def mark_dead_letter(self, run_id: int, *, error: str) -> None: ...


class AuditLog(Protocol):
    def record_event(
        self,
        *,
        actor: str,
        event: str,
        target: str,
        result: str = "success",
        metadata: dict[str, Any] | None = None,
    ) -> int: ...

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]: ...


class AccountRepository(Protocol):
    def get_account_context(self, login: str | None = None) -> dict[str, Any]: ...

    def seed_defaults(self, *, username: str, workspace_name: str = "PatchPilot") -> None: ...

    def get_or_create_user(self, *, email: str, name: str) -> dict[str, Any]: ...

    def upsert_github_user(
        self,
        *,
        github_user_id: str,
        login: str,
        name: str,
        email: str,
        avatar_url: str | None = None,
    ) -> dict[str, Any]: ...

    def workspace_for_login(self, login: str | None) -> dict[str, Any] | None: ...


class RepositoryCatalog(Protocol):
    def list_repositories(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def upsert_repository(self, *, full_name: str, workspace_id: int | None = None, **fields: Any) -> int: ...


class ProviderKeyRepository(Protocol):
    def list_provider_keys(self) -> list[dict[str, Any]]: ...

    def upsert_provider_key(self, *, workspace_id: int | None, provider: str, key_hint: str) -> int: ...


class GitHubConnectionRepository(Protocol):
    def get_github_connection(self) -> dict[str, Any] | None: ...

    def upsert_github_connection(
        self,
        *,
        user_id: int | None,
        login: str,
        scopes: str,
        token_hint: str,
        installation_id: str | None = None,
    ) -> int: ...


class EvalReportRepository(Protocol):
    def list_eval_reports(self, limit: int = 20) -> list[dict[str, Any]]: ...


class WebhookDeliveryRepository(Protocol):
    def record_delivery(self, *, delivery_id: str, event: str, action: str | None) -> bool: ...

    def finish_delivery(self, delivery_id: str, *, status: str, error: str | None = None) -> None: ...

    def list_deliveries(self, limit: int = 100) -> list[dict[str, Any]]: ...


class GitHubAppInstallationRepository(Protocol):
    def upsert_installation(
        self,
        *,
        installation_id: str,
        account_login: str,
        account_type: str = "User",
        status: str = "active",
    ) -> int: ...

    def set_installation_status(self, installation_id: str, status: str) -> None: ...

    def get_installation(self, installation_id: str) -> dict[str, Any] | None: ...

    def list_installations(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def add_repository(
        self,
        *,
        installation_id: str,
        full_name: str,
        github_repo_id: int | None = None,
        private: bool = False,
    ) -> int: ...

    def remove_repository(self, *, installation_id: str, full_name: str) -> None: ...

    def installation_for_repository(self, full_name: str) -> str | None: ...

    def list_repositories(self, installation_id: str | None = None) -> list[dict[str, Any]]: ...


class ArtifactCatalog(Protocol):
    def list_artifacts(self, location: str) -> list[str]: ...


class IssueTrackerGateway(Protocol):
    def fetch_issue(self, ref: IssueRef) -> IssueContext: ...

    def clone_url(self, ref: IssueRef) -> str: ...

    def create_draft_pr(self, ref: IssueRef, branch: str, title: str, body: str) -> str: ...


class GitHubRepositoryGateway(Protocol):

    def current_user(self) -> dict[str, Any]: ...

    def list_accessible_repositories(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def verify_repository_access(self, full_name: str) -> dict[str, Any]: ...


class PullRequestGateway(Protocol):

    def fetch_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]: ...

    def fetch_pull_request_files(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]: ...

    def post_pull_request_comment(self, owner: str, repo: str, number: int, body: str) -> str: ...

    def create_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        *,
        state: str,
        context: str,
        description: str,
        target_url: str | None = None,
    ) -> str: ...


class PRJobPublisher(Protocol):
    topic: str

    def publish(self, job: PRAnalysisJob) -> None: ...


class PRJobConsumer(Protocol):
    def jobs(self) -> Iterable[PRAnalysisJob]: ...

    def commit(self) -> None: ...


class LLMGateway(Protocol):
    def propose_fix(self, *, model: str, prompt: str) -> AgentDecision: ...


class RepositoryWorkspace(Protocol):
    path: Path
    branch: str

    def status(self) -> str: ...

    def diff(self) -> str: ...

    def commit_all(self, message: str) -> None: ...

    def push(self, remote: str = "origin") -> None: ...

    def head_sha(self) -> str | None: ...


class WorkspaceManager(Protocol):
    def clone(self, clone_url: str, issue: IssueRef) -> RepositoryWorkspace: ...


class RepositoryTools(Protocol):
    def list_files(self) -> list[str]: ...

    def read_file(self, relative_path: str, max_bytes: int = 80_000) -> str: ...

    def write_file(self, relative_path: str, content: str) -> None: ...

    def search_text(self, pattern: str) -> str: ...

    def apply_patch(self, patch: str) -> None: ...

    def run_command_in_sandbox(
        self, command: str, timeout_seconds: int | None = None, *, needs_network: bool = False
    ) -> CommandResult: ...

    def git_diff(self) -> str: ...

    def git_status(self) -> str: ...


class RepositoryToolFactory(Protocol):
    def create(self, repo_path: Path, settings: ExecutionSettings) -> RepositoryTools: ...


class ProjectEnvironmentDetector(Protocol):
    def resolve(self, repo_path: Path, base_settings: ExecutionSettings) -> ExecutionSettings: ...

    def summarize(self, repo_path: Path) -> str: ...


class ArtifactWriter(Protocol):
    def exists(self, path: Path) -> bool: ...

    def write(self, path: Path, payload: Mapping[str, Any]) -> None: ...


class SecretRedactor(Protocol):
    def redact(self, text: str | None) -> str: ...


class EngineeringRunExecutor(Protocol):
    def execute(self, command: ExecuteRunCommand) -> AgentRunResult: ...


class EngineeringRunExecutorFactory(Protocol):
    def for_model(self, model: str, installation_id: str | None = None) -> EngineeringRunExecutor: ...
