from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent.domain.value_objects import CommandResult
from agent.infrastructure.sandbox.docker import DockerSandbox


@dataclass(slots=True)
class RepoTools:
    repo_path: Path
    sandbox: DockerSandbox

    def list_files(self) -> list[str]:
        completed = subprocess.run(["git", "ls-files"], cwd=self.repo_path, capture_output=True, text=True, timeout=30)
        if completed.returncode == 0:
            return [line for line in completed.stdout.splitlines() if line]
        return [str(path.relative_to(self.repo_path)) for path in self.repo_path.rglob("*") if path.is_file()]

    def read_file(self, relative_path: str, max_bytes: int = 80_000) -> str:
        path = self._safe_path(relative_path)
        data = path.read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")

    def write_file(self, relative_path: str, content: str) -> None:
        path = self._safe_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def search_text(self, pattern: str) -> str:
        completed = subprocess.run(
            ["rg", "-n", pattern, "."],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode not in (0, 1):
            raise RuntimeError(completed.stderr)
        return completed.stdout

    def apply_patch(self, patch: str) -> None:
        completed = subprocess.run(
            ["git", "apply", "--whitespace=fix"],
            input=patch,
            cwd=self.repo_path,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or "Patch did not apply")

    def run_command_in_sandbox(
        self, command: str, timeout_seconds: int | None = None, *, needs_network: bool = False
    ) -> CommandResult:
        return self.sandbox.run(self.repo_path, command, timeout_seconds=timeout_seconds, needs_network=needs_network)

    def git_diff(self) -> str:
        completed = subprocess.run(["git", "diff", "--", "."], cwd=self.repo_path, capture_output=True, text=True)
        return completed.stdout

    def git_status(self) -> str:
        completed = subprocess.run(["git", "status", "--short"], cwd=self.repo_path, capture_output=True, text=True)
        return completed.stdout

    def _safe_path(self, relative_path: str) -> Path:
        path = (self.repo_path / relative_path).resolve()
        if self.repo_path.resolve() not in path.parents and path != self.repo_path.resolve():
            raise ValueError("Path escapes repository sandbox")
        return path
