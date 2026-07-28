"""Compatibility imports for the Kafka PR worker adapter."""

from .application.services.pr_analysis import PRWorkerResult
from .interfaces.workers.pr_worker import PullRequestAnalyzer, process_pr_analysis_jobs

__all__ = ["PRWorkerResult", "PullRequestAnalyzer", "process_pr_analysis_jobs"]
