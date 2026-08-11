"""Application composition root.

Only this outer module knows which concrete adapters satisfy the application
ports. HTTP routes, CLI commands, and workers receive the assembled handlers.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.application.commands.dashboard import (
    ChangePasswordHandler,
    CompleteOnboardingHandler,
    ConnectGitHubAccountHandler,
    CreateAccountHandler,
    RecordAuditHandler,
    SeedRuntimeStateHandler,
    SendVerificationEmailHandler,
    SyncGitHubRepositoriesHandler,
    UpdateEmailHandler,
    VerifyEmailHandler,
    VerifyGitHubRepositoryHandler,
)
from agent.application.commands.handle_pr_webhook import EnqueuePullRequestAnalysisHandler
from agent.application.commands.queue_run import QueueRunHandler
from agent.application.queries.dashboard import DashboardQueryService
from agent.application.services.billing import (
    BillingService,
    HandleStripeWebhookHandler,
    StripeWebhookSettings,
)
from agent.config import AgentConfig, load_config
from agent.github_client import GitHubClient
from agent.infrastructure.artifacts import FilesystemArtifactCatalog
from agent.infrastructure.clock import SystemClock
from agent.infrastructure.db.repositories import (
    SqlAccountRepository,
    SqlAuditLog,
    SqlBillingRepository,
    SqlEvalReportRepository,
    SqlGitHubAppRepository,
    SqlGitHubConnectionRepository,
    SqlProviderKeyRepository,
    SqlRepositoryCatalog,
    SqlRunRepository,
    SqlWebhookDeliveryRepository,
)
from agent.infrastructure.email import SmtpMailer
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
    github_app: SqlGitHubAppRepository
    webhook_deliveries: SqlWebhookDeliveryRepository
    billing: BillingService
    handle_stripe_webhook: HandleStripeWebhookHandler
    queue_run: QueueRunHandler
    enqueue_pr_analysis: EnqueuePullRequestAnalysisHandler
    record_audit: RecordAuditHandler
    create_account: CreateAccountHandler
    change_password: ChangePasswordHandler
    update_email: UpdateEmailHandler
    complete_onboarding: CompleteOnboardingHandler
    send_verification_email: SendVerificationEmailHandler
    verify_email: VerifyEmailHandler
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
    github_app = SqlGitHubAppRepository(store)
    webhook_deliveries = SqlWebhookDeliveryRepository(store)
    billing_repository = SqlBillingRepository(store)
    github = GitHubClient(settings.github_token)
    clock = SystemClock()
    mailer = SmtpMailer(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        from_address=settings.smtp_from_address,
        use_tls=settings.smtp_use_tls,
    )
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
        github_app=github_app,
        webhook_deliveries=webhook_deliveries,
        billing=BillingService(billing_repository),
        handle_stripe_webhook=HandleStripeWebhookHandler(
            billing_repository,
            StripeWebhookSettings(
                price_id_starter=settings.stripe_price_id_starter,
                price_id_pro=settings.stripe_price_id_pro,
            ),
        ),
        queue_run=QueueRunHandler(runs, audit_log, clock),
        enqueue_pr_analysis=EnqueuePullRequestAnalysisHandler(KafkaPRJobProducer(settings), audit_log, clock),
        record_audit=RecordAuditHandler(audit_log),
        create_account=CreateAccountHandler(accounts, audit_log),
        change_password=ChangePasswordHandler(accounts, audit_log),
        update_email=UpdateEmailHandler(accounts, audit_log),
        complete_onboarding=CompleteOnboardingHandler(accounts, audit_log),
        send_verification_email=SendVerificationEmailHandler(accounts, audit_log, mailer),
        verify_email=VerifyEmailHandler(accounts, audit_log),
        connect_github_account=ConnectGitHubAccountHandler(accounts, github_connections, audit_log),
        sync_github_repositories=SyncGitHubRepositoriesHandler(github, accounts, repositories),
        verify_github_repository=VerifyGitHubRepositoryHandler(github, accounts, repositories),
        seed_runtime_state=SeedRuntimeStateHandler(accounts, provider_keys),
        queries=queries,
    )
