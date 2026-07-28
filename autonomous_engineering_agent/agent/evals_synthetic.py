from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import AgentConfig
from .executor import EngineeringAgent
from .github_client import IssueContext, IssueRef
from .llm import AgentDecision
from .persistence import RunStore
from .tool_calls import ToolCall


@dataclass(slots=True)
class SyntheticTask:
    name: str
    issue_number: int
    repo_name: str
    title: str
    body: str
    files: dict[str, str]
    patch: str
    expected_status: str = "success"


class SyntheticGitHub:
    def __init__(self, repo_path: Path, task: SyntheticTask) -> None:
        self.repo_path = repo_path
        self.task = task

    def fetch_issue(self, issue: IssueRef) -> IssueContext:
        return IssueContext(
            ref=issue,
            title=self.task.title,
            body=self.task.body,
            comments=("Synthetic benchmark issue.",),
            labels=("synthetic", "bug"),
            linked_prs=(),
        )

    def clone_url(self, issue: IssueRef) -> str:
        return str(self.repo_path)

    def create_draft_pr(self, *args: Any, **kwargs: Any) -> str:
        return "https://github.com/synthetic/repo/pull/1"


class SyntheticPatchLLM:
    def __init__(self, patch: str) -> None:
        self.patch = patch

    def propose_fix(self, *, model: str, prompt: str) -> AgentDecision:
        return AgentDecision(
            summary="Apply the benchmark patch.",
            plan=["Apply deterministic benchmark patch", "Run tests"],
            patches=[],
            tool_calls=[
                ToolCall(
                    name="apply_patch",
                    args={"patch": self.patch},
                    rationale="Synthetic eval uses a deterministic expected patch.",
                )
            ],
            usage={
                "provider": "synthetic",
                "model": model,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "cost_source": "synthetic",
            },
        )


def load_synthetic_manifest(path: Path) -> list[SyntheticTask]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tasks = []
    for item in raw.get("tasks", []):
        tasks.append(
            SyntheticTask(
                name=str(item["name"]),
                issue_number=int(item["issue_number"]),
                repo_name=str(item.get("repo_name", item["name"])).replace("/", "-"),
                title=str(item["title"]),
                body=str(item["body"]),
                files={str(key): str(value) for key, value in dict(item["files"]).items()},
                patch=str(item["patch"]),
                expected_status=str(item.get("expected_status", "success")),
            )
        )
    return tasks


def run_synthetic_eval(manifest_path: Path, output_path: Path) -> dict[str, Any]:
    tasks = load_synthetic_manifest(manifest_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="patchpilot-synth-") as tmp:
        root = Path(tmp)
        for task in tasks:
            task_started = time.monotonic()
            source_repo = _create_repo(root, task)
            database_url = f"sqlite:///{root / (task.name + '.sqlite3')}"
            store = RunStore(database_url)
            agent = EngineeringAgent(
                config=AgentConfig(database_url=database_url, logs_dir=output_path.parent / "synthetic-logs"),
                github=SyntheticGitHub(source_repo, task),
                llm=SyntheticPatchLLM(task.patch),
                store=store,
            )
            result = agent.run(
                issue=IssueRef(owner="synthetic", repo=task.repo_name, number=task.issue_number),
                model="synthetic-patch",
                max_iterations=1,
                open_pr=False,
            )
            results.append(
                {
                    "name": task.name,
                    "status": result.status,
                    "expected_status": task.expected_status,
                    "passed": result.status == task.expected_status,
                    "runtime_seconds": round(time.monotonic() - task_started, 3),
                    "logs_path": str(result.logs_path),
                }
            )
    passed = sum(1 for result in results if result["passed"])
    report = {
        "schema_version": 1,
        "task_count": len(results),
        "passed_count": passed,
        "pass_rate": round(passed / len(results), 4) if results else 0,
        "runtime_seconds": round(time.monotonic() - started, 3),
        "results": results,
    }
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def _create_repo(root: Path, task: SyntheticTask) -> Path:
    repo = root / task.repo_name
    repo.mkdir(parents=True)
    for relative_path, content in task.files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (repo / "agent.yaml").write_text(
        "install_commands: []\ntest_commands:\n  - python -m unittest discover\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Synthetic Eval",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo
