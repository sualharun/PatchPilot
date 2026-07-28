from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from agent.domain.value_objects import CommandResult
from agent.infrastructure.config.settings import SandboxConfig
from agent.infrastructure.security.secrets import redact_text


class DockerSandbox:
    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    def run(self, repo_path: Path, command: str, timeout_seconds: int | None = None) -> CommandResult:
        repo_path = repo_path.resolve()
        if not repo_path.exists() or not repo_path.is_dir():
            raise ValueError(f"Repository path does not exist: {repo_path}")
        self._ensure_allowed(command)
        docker_mount_path = _host_visible_path(repo_path)

        timeout = timeout_seconds or self.config.command_timeout_seconds
        container_name = f"agent-{uuid.uuid4().hex[:12]}"
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--cpus",
            self.config.cpu_limit,
            "--memory",
            self.config.memory_limit,
            "--network",
            self.config.network,
            "-v",
            f"{docker_mount_path}:/workspace",
            "-w",
            "/workspace",
            self.config.image,
            "/bin/sh",
            "-lc",
            command,
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout)
            runtime = time.monotonic() - started
            return CommandResult(
                command=command,
                stdout=redact_text(completed.stdout),
                stderr=redact_text(completed.stderr),
                exit_code=completed.returncode,
                runtime_seconds=runtime,
            )
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True, timeout=30)
            runtime = time.monotonic() - started
            return CommandResult(
                command=command,
                stdout=redact_text(_decode_timeout_output(exc.stdout)),
                stderr=redact_text(_decode_timeout_output(exc.stderr) + f"\nCommand timed out after {timeout}s"),
                exit_code=124,
                runtime_seconds=runtime,
                timed_out=True,
            )

    def _ensure_allowed(self, command: str) -> None:
        if any(marker in command for marker in ("&&", "||", ";", "|", "`", "$(", ">", "<")):
            raise ValueError("Shell control operators are not allowed in sandbox commands")
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"Invalid command: {exc}") from exc
        if not parts:
            raise ValueError("Empty commands are not allowed")
        executable = Path(parts[0]).name
        if executable not in self.config.allowed_commands:
            raise ValueError(f"Command '{executable}' is not in the sandbox allowlist")
        if any(token == ".." or token.startswith("/") for token in parts):
            raise ValueError("Commands may not target paths outside the mounted repository")


def _host_visible_path(repo_path: Path) -> Path:
    container_root_raw = os.getenv("PATCHPILOT_WORKSPACE_ROOT")
    host_root_raw = os.getenv("PATCHPILOT_HOST_WORKSPACE_ROOT")
    if not container_root_raw or not host_root_raw:
        return repo_path

    container_root = Path(container_root_raw).resolve()
    try:
        relative_path = repo_path.relative_to(container_root)
    except ValueError:
        return repo_path
    return Path(host_root_raw).resolve() / relative_path


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
