import subprocess
import time
from pathlib import Path

from agent.config import AgentConfig
from agent.executor import EngineeringAgent
from agent.github_client import IssueContext, IssueRef
from agent.llm import AgentDecision
from agent.persistence import RunStore
from agent.sandbox import CommandResult
from agent.tool_calls import ToolCall


class FakeGitHub:
    def __init__(self, source_repo: Path) -> None:
        self.source_repo = source_repo

    def fetch_issue(self, issue):
        return IssueContext(
            ref=issue,
            title="Fix add()",
            body="`calc.py` subtracts instead of adding.",
            comments=["The failing test is in tests/test_calc.py."],
            labels=["bug"],
            linked_prs=[],
        )

    def clone_url(self, issue):
        return str(self.source_repo)

    def create_draft_pr(self, *args, **kwargs):
        raise AssertionError("PR creation should not run when open_pr is false")


class FakeLLM:
    def propose_fix(self, *, model: str, prompt: str):
        assert "calc.py" in prompt
        return AgentDecision(
            summary="Change add() to return the sum.",
            plan=["Patch calc.py", "Run tests"],
            patches=[],
            tool_calls=[
                ToolCall(
                    name="apply_patch",
                    args={
                        "patch": """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""
                    },
                    rationale="Fix the arithmetic bug described in the issue.",
                )
            ],
        )


class LocalSandbox:
    def __init__(self, config) -> None:
        self.config = config

    def run(self, repo_path: Path, command: str, timeout_seconds: int | None = None):
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=repo_path,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return CommandResult(
            command=command,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            runtime_seconds=time.monotonic() - started,
        )


def test_agent_smoke_clone_patch_test_commit_persist(tmp_path, monkeypatch):
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    (source_repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    tests_dir = source_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (source_repo / "agent.yaml").write_text(
        """
install_commands:
  - python3 -m pip --version
test_commands:
  - python3 -m pytest
logs_dir: logs
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=source_repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=source_repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        cwd=source_repo,
        check=True,
        capture_output=True,
    )

    monkeypatch.setattr("agent.executor.DockerSandbox", LocalSandbox)
    store = RunStore("sqlite:///:memory:")
    agent = EngineeringAgent(
        config=AgentConfig(database_url="sqlite:///:memory:", logs_dir=tmp_path / "logs"),
        github=FakeGitHub(source_repo),
        llm=FakeLLM(),
        store=store,
    )

    result = agent.run(
        issue=IssueRef(owner="local", repo="source", number=7),
        model="fake-model",
        max_iterations=2,
        open_pr=False,
    )

    assert result.status == "success"
    assert result.tests_passed is True
    assert result.pr_url is None
    assert result.branch.startswith("agent/fix-issue-7-")
    assert result.logs_path.exists()
    artifact = result.logs_path.read_text(encoding="utf-8")
    assert '"schema_version": 1' in artifact
    assert '"tool_calls"' in artifact
    assert "return a + b" in (result.repo_path / "calc.py").read_text(encoding="utf-8")
    assert subprocess.run(["git", "log", "--oneline", "-1"], cwd=result.repo_path, capture_output=True, text=True).stdout

    saved = store.get_run(1)
    assert saved["status"] == "success"
    assert saved["iterations"] == 1
    assert saved["test_results"][0]["exit_code"] == 0
