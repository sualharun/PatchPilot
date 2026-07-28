"""Worker adapter that composes the application PR-analysis handlers."""

from __future__ import annotations

from agent.application.services.pr_analysis import (
    AnalyzePullRequestHandler,
    ProcessPullRequestJobsHandler,
    PRWorkerResult,
)
from agent.infrastructure.config.settings import AgentConfig
from agent.infrastructure.github.client import GitHubClient
from agent.infrastructure.kafka import KafkaPRJobConsumer


class PullRequestAnalyzer(AnalyzePullRequestHandler):
    def analyze(self, job):
        result = self.execute(job)
        return {"comment_url": result.comment_url, "files": result.files_reviewed}


def process_pr_analysis_jobs(config: AgentConfig, *, limit: int | None = None) -> PRWorkerResult:
    handler = ProcessPullRequestJobsHandler(
        KafkaPRJobConsumer(config),
        AnalyzePullRequestHandler(_github_client_from_config(config), status_context=config.pr_analysis_status_context),
    )
    return handler.execute(limit=limit)


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


__all__ = ["PRWorkerResult", "PullRequestAnalyzer", "process_pr_analysis_jobs"]
