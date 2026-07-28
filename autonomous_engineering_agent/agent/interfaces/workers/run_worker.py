"""Polling worker adapter for queued autonomous engineering runs."""

from __future__ import annotations

from agent.application.dto import ExecuteRunCommand
from agent.application.services.run_worker import ProcessQueuedRunsHandler, RunWorkerSettings, WorkerResult
from agent.executor import EngineeringAgent
from agent.infrastructure.config.settings import AgentConfig
from agent.infrastructure.db.repositories import SqlAuditLog, SqlRunRepository
from agent.infrastructure.db.store import RunStore
from agent.infrastructure.github.client import GitHubClient
from agent.infrastructure.llm.providers import provider_for_model
from agent.infrastructure.security import EnvironmentSecretRedactor


class _ExecutorAdapter:
    def __init__(self, agent: EngineeringAgent) -> None:
        self._agent = agent

    def execute(self, command: ExecuteRunCommand):
        return self._agent.run(
            issue=command.issue,
            model=command.model,
            max_iterations=command.max_iterations,
            open_pr=command.open_pr,
            run_id=command.run_id,
        )


class _ExecutorFactory:
    def __init__(self, config: AgentConfig, store: RunStore, github: GitHubClient) -> None:
        self._config = config
        self._store = store
        self._github = github

    def for_model(self, model: str) -> _ExecutorAdapter:
        llm = provider_for_model(model, self._config.openai_api_key, self._config.anthropic_api_key)
        return _ExecutorAdapter(
            EngineeringAgent(config=self._config, github=self._github, llm=llm, store=self._store)
        )


def process_queued_runs(config: AgentConfig, *, limit: int = 1) -> WorkerResult:
    store = RunStore(config.database_url, allow_sqlite_fallback=not config.production)
    try:
        handler = ProcessQueuedRunsHandler(
            runs=SqlRunRepository(store),
            audit_log=SqlAuditLog(store),
            executors=_ExecutorFactory(config, store, _github_client_from_config(config)),
            redactor=EnvironmentSecretRedactor(),
            settings=RunWorkerSettings(
                worker_id=config.worker_id,
                lease_seconds=config.worker_lease_seconds,
                max_attempts=config.worker_max_attempts,
            ),
        )
        return handler.execute(limit=limit)
    finally:
        store.close()


def _github_client_from_config(config: AgentConfig) -> GitHubClient:
    if config.github_app_id and config.github_app_installation_id:
        private_key = config.github_app_private_key
        if not private_key and config.github_app_private_key_path:
            private_key = config.github_app_private_key_path.read_text(encoding="utf-8")
        if private_key:
            return GitHubClient.from_github_app_installation(
                app_id=config.github_app_id,
                private_key=private_key,
                installation_id=config.github_app_installation_id,
            )
    return GitHubClient(config.github_token)


__all__ = ["WorkerResult", "process_queued_runs"]
