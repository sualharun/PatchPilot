from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent.domain.value_objects import IssueRef


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    image: str = "python:3.11-slim"
    cpu_limit: str = "2"
    memory_limit: str = "2g"
    network: str = "bridge"
    install_timeout_seconds: int = 600
    test_timeout_seconds: int = 600
    command_timeout_seconds: int = 300
    allowed_commands: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    install_commands: tuple[str, ...] = ()
    test_commands: tuple[str, ...] = ()
    install_commands_configured: bool = False
    test_commands_configured: bool = False
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    logs_dir: Path = Path(".agent-logs")


@dataclass(frozen=True, slots=True)
class ExecuteRunCommand:
    issue: IssueRef
    model: str
    max_iterations: int
    open_pr: bool
    run_id: int | None = None


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: str
    summary: str
    tests_run: list[str]
    tests_passed: bool
    branch: str
    pr_url: str | None
    logs_path: Path
    repo_path: Path
