from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.application.ports.outbound import PRJobConsumer, PullRequestGateway
from agent.domain.entities import PRAnalysisJob


@dataclass(frozen=True, slots=True)
class PRAnalysisResult:
    comment_url: str
    files_reviewed: int


@dataclass(frozen=True, slots=True)
class PRWorkerResult:
    processed: int
    succeeded: int
    failed: int


class AnalyzePullRequestHandler:
    def __init__(self, github: PullRequestGateway, *, status_context: str) -> None:
        self._github = github
        self._status_context = status_context

    def execute(self, job: PRAnalysisJob) -> PRAnalysisResult:
        self._github.create_commit_status(
            job.owner,
            job.repo,
            job.commit_sha,
            state="pending",
            context=self._status_context,
            description="PatchPilot is analyzing this pull request",
        )
        pull_request = self._github.fetch_pull_request(job.owner, job.repo, job.pr_number)
        files = self._github.fetch_pull_request_files(job.owner, job.repo, job.pr_number)
        summary = _review_summary(pull_request, files)
        comment_url = self._github.post_pull_request_comment(job.owner, job.repo, job.pr_number, summary)
        self._github.create_commit_status(
            job.owner,
            job.repo,
            job.commit_sha,
            state="success",
            context=self._status_context,
            description=f"Reviewed {len(files)} changed file(s)",
            target_url=comment_url or None,
        )
        return PRAnalysisResult(comment_url=comment_url, files_reviewed=len(files))


class ProcessPullRequestJobsHandler:
    def __init__(self, consumer: PRJobConsumer, analyzer: AnalyzePullRequestHandler) -> None:
        self._consumer = consumer
        self._analyzer = analyzer

    def execute(self, *, limit: int | None = None) -> PRWorkerResult:
        processed = succeeded = failed = 0
        for job in self._consumer.jobs():
            if limit is not None and processed >= limit:
                break
            processed += 1
            try:
                self._analyzer.execute(job)
                self._consumer.commit()
                succeeded += 1
            except Exception:
                failed += 1
        return PRWorkerResult(processed=processed, succeeded=succeeded, failed=failed)


def _review_summary(pull_request: dict[str, Any], files: list[dict[str, Any]]) -> str:
    additions = sum(int(file.get("additions") or 0) for file in files)
    deletions = sum(int(file.get("deletions") or 0) for file in files)
    filenames = [str(file.get("filename") or "unknown") for file in files[:10]]
    body = [
        "## PatchPilot PR analysis",
        "",
        f"**PR:** {pull_request.get('title') or 'Pull request'}",
        f"**Files changed:** {len(files)}",
        f"**Diff size:** +{additions} / -{deletions}",
        "",
        "**Changed files sampled:**",
        *[f"- `{filename}`" for filename in filenames],
        "",
        "This async review job was queued from a GitHub webhook and processed by the PatchPilot Kafka worker.",
    ]
    if len(files) > len(filenames):
        body.insert(-2, f"- ...and {len(files) - len(filenames)} more")
    return "\n".join(body)
