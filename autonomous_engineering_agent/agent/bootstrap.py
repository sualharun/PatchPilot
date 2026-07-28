"""Application composition root.

Only this outer module knows which concrete adapters satisfy the application
ports. HTTP routes, CLI commands, and workers receive the assembled handlers.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.application.commands.dashboard import (
    ConnectGitHubAccountHandler,
    RecordAuditHandler,
    SeedRuntimeStateHandler,
    SyncGitHubRepositoriesHandler,
    VerifyGitHubRepositoryHandler,
)
from agent.application.commands.handle_pr_webhook import EnqueuePullRequestAnalysisHandler
from agent.application.commands.queue_run import QueueRunHandler
from agent.application.queries.dashboard import DashboardQueryService
from agent.config import AgentConfig, load_config
from agent.github_client import GitHubClient
from agent.infrastructure.artifacts import FilesystemArtifactCatalog
from agent.infrastructure.clock import SystemClock
from agent.infrastructure.db.repositories import (
    SqlAccountRepository,
    SqlAuditLog,
    SqlEvalReportRepository,
    SqlGitHubConnectionRepository,
    SqlProviderKeyRepository,
    SqlRepositoryCatalog,
    SqlRunRepository,
)
from agent.infrastructure.kafka import KafkaPRJobProducer
from agent.persistence import RunStore


@dataclass(slots=True)
class ApplicationContainer:
    config: AgentConfig
    store: RunStore
    runs: SqlRunRepository
    audit_log: SqlAuditLog
    accounts: SqlAccountRepository
    repositories: SqlRepositoryCatalog
    provider_keys: SqlProviderKeyRepository
    github_connections: SqlGitHubConnectionRepository
    eval_reports: SqlEvalReportRepository
    queue_run: QueueRunHandler
    enqueue_pr_analysis: EnqueuePullRequestAnalysisHandler
    record_audit: RecordAuditHandler
    connect_github_account: ConnectGitHubAccountHandler
    sync_github_repositories: SyncGitHubRepositoriesHandler
    verify_github_repository: VerifyGitHubRepositoryHandler
    seed_runtime_state: SeedRuntimeStateHandler
    queries: DashboardQueryService

    @property
    def database_kind(self) -> str:
        return self.store.kind

    def close(self) -> None:
        self.store.close()


def build_application(
    *,
    database_url: str | None = None,
    config: AgentConfig | None = None,
) -> ApplicationContainer:
    settings = config or load_config()
    store = RunStore(database_url or settings.database_url, allow_sqlite_fallback=not settings.production)
    runs = SqlRunRepository(store)
    audit_log = SqlAuditLog(store)
    accounts = SqlAccountRepository(store)
    repositories = SqlRepositoryCatalog(store)
    provider_keys = SqlProviderKeyRepository(store)
    github_connections = SqlGitHubConnectionRepository(store)
    eval_reports = SqlEvalReportRepository(store)
    github = GitHubClient(settings.github_token)
    clock = SystemClock()
    queries = DashboardQueryService(
        runs=runs,
        audit_log=audit_log,
        accounts=accounts,
        repositories=repositories,
        provider_keys=provider_keys,
        github_connections=github_connections,
        eval_reports=eval_reports,
        artifacts=FilesystemArtifactCatalog(),
    )
    return ApplicationContainer(
        config=settings,
        store=store,
        runs=runs,
        audit_log=audit_log,
        accounts=accounts,
        repositories=repositories,
        provider_keys=provider_keys,
        github_connections=github_connections,
        eval_reports=eval_reports,
        queue_run=QueueRunHandler(runs, audit_log, clock),
        enqueue_pr_analysis=EnqueuePullRequestAnalysisHandler(KafkaPRJobProducer(settings), audit_log, clock),
        record_audit=RecordAuditHandler(audit_log),
        connect_github_account=ConnectGitHubAccountHandler(accounts, github_connections, audit_log),
        sync_github_repositories=SyncGitHubRepositoriesHandler(github, accounts, repositories),
        verify_github_repository=VerifyGitHubRepositoryHandler(github, accounts, repositories),
        seed_runtime_state=SeedRuntimeStateHandler(accounts, provider_keys),
        queries=queries,
    )
