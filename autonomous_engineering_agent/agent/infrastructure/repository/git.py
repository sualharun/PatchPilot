from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from agent.domain.services import branch_name_for_issue
from agent.domain.value_objects import IssueRef
from agent.infrastructure.security.secrets import redact_text

# Setup steps routinely create a virtualenv and caches inside the checkout. The
# agent stages with `git add .`, so without these the artifacts land in the pull
# request and bury the actual fix under thousands of files. Written to
# .git/info/exclude rather than .gitignore: it is local to our clone, never
# pushed, and leaves the target repository's own ignore rules alone. Git ignore
# rules only apply to untracked paths, so a repository that genuinely tracks one
# of these still has its files staged.
_ARTIFACT_EXCLUDES = (
    ".venv/",
    "venv/",
    "ENV/",
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    "node_modules/",
    "*.egg-info/",
    ".coverage",
    "htmlcov/",
)


@dataclass(slots=True)
class RepoWorkspace:
    path: Path
    branch: str
    push_url: str | None = None

    def status(self) -> str:
        return _git(self.path, ["status", "--short"])

    def diff(self) -> str:
        return _git(self.path, ["diff", "--", "."])

    def commit_all(self, message: str) -> None:
        _git(self.path, ["add", "."])
        if not self.status().strip():
            return
        _git(
            self.path,
            [
                "-c",
                "user.name=Autonomous Engineering Agent",
                "-c",
                "user.email=agent@example.invalid",
                "commit",
                "-m",
                message,
            ],
        )

    def push(self, remote: str = "origin") -> None:
        target = self.push_url or remote
        _git(self.path, ["push", "-u", target, self.branch])

    def head_sha(self) -> str | None:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.path,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None


def clone_repository(clone_url: str, issue_ref: IssueRef, base_dir: Path | None = None) -> RepoWorkspace:
    if base_dir is not None:
        root = Path(base_dir).resolve()
    elif workspace_root := os.getenv("PATCHPILOT_WORKSPACE_ROOT"):
        root = Path(tempfile.mkdtemp(prefix="agent-run-", dir=workspace_root)).resolve()
    else:
        root = Path(tempfile.mkdtemp(prefix="agent-run-")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    repo_path = root / issue_ref.repo
    _run(["git", "clone", clone_url, str(repo_path)], cwd=root)
    _git(repo_path, ["remote", "set-url", "origin", f"https://github.com/{issue_ref.owner}/{issue_ref.repo}.git"])
    branch = branch_name_for_issue(issue_ref.number, int(time.time()))
    _git(repo_path, ["checkout", "-b", branch])
    exclude_build_artifacts(repo_path)
    push_url = clone_url if "x-access-token:" in clone_url else None
    return RepoWorkspace(path=repo_path, branch=branch, push_url=push_url)


def exclude_build_artifacts(repo_path: Path) -> None:
    """Keep environment and cache directories out of the agent's commits."""
    exclude_file = Path(repo_path) / ".git" / "info" / "exclude"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    missing = [pattern for pattern in _ARTIFACT_EXCLUDES if pattern not in existing.split()]
    if not missing:
        return
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    exclude_file.write_text(
        f"{existing}{prefix}# added by PatchPilot\n" + "\n".join(missing) + "\n",
        encoding="utf-8",
    )


def cleanup_workspace(workspace: RepoWorkspace) -> None:
    parent = workspace.path.parent
    if parent.name.startswith("agent-run-") and parent.exists():
        shutil.rmtree(parent, ignore_errors=True)


def _git(repo_path: Path, args: list[str]) -> str:
    return _run(["git", *args], cwd=repo_path)


def _run(args: list[str], cwd: Path) -> str:
    env = os.environ.copy()
    completed = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env, timeout=120)
    if completed.returncode != 0:
        raise RuntimeError(redact_text(completed.stderr or completed.stdout))
    return redact_text(completed.stdout)
