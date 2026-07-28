"""Pure domain policies and calculations."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .entities import PRAnalysisJob
from .errors import InvalidIssueReference, InvalidPullRequestEvent
from .value_objects import IssueRef, RepositoryRef

GITHUB_ISSUE_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)")
SHORT_ISSUE_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^#\s]+)#(?P<number>\d+)$")
SEPARATE_ISSUE_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)\s+(?P<number>\d+)$")
PR_ACTIONS_TO_ANALYZE = frozenset({"opened", "reopened", "ready_for_review", "synchronize"})


def parse_issue_ref(value: str) -> IssueRef:
    normalized = value.strip()
    for pattern in (GITHUB_ISSUE_RE, SHORT_ISSUE_RE, SEPARATE_ISSUE_RE):
        match = pattern.search(normalized)
        if match:
            repository = RepositoryRef(match.group("owner"), match.group("repo"))
            return IssueRef(repository=repository, number=int(match.group("number")))
    raise InvalidIssueReference("Issue must be a GitHub issue URL, 'owner/repo#123', or 'owner/repo 123'")


def branch_name_for_issue(issue_number: int, timestamp: int) -> str:
    if issue_number <= 0:
        raise InvalidIssueReference("issue number must be positive")
    if timestamp < 0:
        raise ValueError("timestamp must not be negative")
    return f"agent/fix-issue-{issue_number}-{timestamp}"


def normalize_max_iterations(value: int) -> int:
    return max(1, value)


def pr_job_from_payload(payload: Mapping[str, Any], *, delivery_id: str, enqueued_at: float) -> PRAnalysisJob | None:
    action = str(payload.get("action") or "")
    if action not in PR_ACTIONS_TO_ANALYZE:
        return None
    pull_request = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    owner = (repository.get("owner") or {}).get("login")
    repo = repository.get("name")
    number = pull_request.get("number") or payload.get("number")
    commit_sha = (pull_request.get("head") or {}).get("sha")
    if not owner or not repo or not number or not commit_sha:
        raise InvalidPullRequestEvent("pull_request webhook is missing owner, repo, number, or head sha")
    installation = payload.get("installation") or {}
    sender = payload.get("sender") or {}
    return PRAnalysisJob(
        repository=RepositoryRef(str(owner), str(repo)),
        pr_number=int(number),
        commit_sha=str(commit_sha),
        action=action,
        delivery_id=delivery_id,
        installation_id=str(installation["id"]) if installation.get("id") is not None else None,
        sender_login=str(sender["login"]) if sender.get("login") is not None else None,
        enqueued_at=enqueued_at,
    )


def compute_run_metrics(
    commands: Sequence[Mapping[str, Any]],
    patches: Sequence[str],
    iterations: int,
    usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    command_runtime = sum(float(command.get("runtime_seconds") or 0) for command in commands)
    tests = [command for command in commands if command.get("phase") == "test"]
    metrics: dict[str, Any] = {
        "iterations": iterations,
        "command_count": len(commands),
        "test_command_count": len(tests),
        "total_command_runtime_seconds": round(command_runtime, 3),
        "patch_count": len(patches),
        "patch_lines": sum(len(patch.splitlines()) for patch in patches),
        "timed_out_command_count": sum(1 for command in commands if command.get("timed_out")),
    }
    if usage:
        metrics.update(
            llm_total_tokens=usage.get("total_tokens", 0),
            estimated_cost_usd=usage.get("estimated_cost_usd"),
            cost_source=usage.get("cost_source"),
        )
    return metrics
