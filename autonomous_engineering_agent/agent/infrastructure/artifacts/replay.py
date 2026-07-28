from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.domain.services import compute_run_metrics


@dataclass(slots=True)
class ReplayArtifact:
    issue_url: str
    repo: str
    branch: str
    model: str
    repo_path: str
    commit_sha: str | None
    status: str
    summary: str
    iterations: int
    commands: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    patches: list[str]
    test_results: list[dict[str, Any]]
    usage: dict[str, Any]
    metrics: dict[str, Any]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "issue_url": self.issue_url,
            "repo": self.repo,
            "branch": self.branch,
            "model": self.model,
            "repo_path": self.repo_path,
            "commit_sha": self.commit_sha,
            "status": self.status,
            "summary": self.summary,
            "iterations": self.iterations,
            "commands": self.commands,
            "tool_calls": self.tool_calls,
            "patches": self.patches,
            "test_results": self.test_results,
            "usage": self.usage,
            "metrics": self.metrics,
        }


def compute_metrics(
    commands: list[dict[str, Any]],
    patches: list[str],
    iterations: int,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return compute_run_metrics(commands, patches, iterations, usage)
