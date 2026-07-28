"""Immutable domain values with no I/O or framework dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import DomainError

ToolName = Literal[
    "list_files",
    "read_file",
    "search_text",
    "apply_patch",
    "write_file",
    "run_command_in_sandbox",
    "git_diff",
    "git_status",
]


def _required(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise DomainError(f"{field_name} must not be empty")
    return normalized


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    owner: str
    name: str
    default_branch: str = "main"

    def __post_init__(self) -> None:
        owner = _required(self.owner, "repository owner")
        name = _required(self.name.removesuffix(".git"), "repository name")
        if "/" in owner or "/" in name:
            raise DomainError("repository owner and name must be individual path segments")
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "default_branch", _required(self.default_branch, "default branch"))

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True, init=False)
class IssueRef:
    repository: RepositoryRef
    number: int
    url: str = ""
    title: str = ""

    def __init__(
        self,
        repository: RepositoryRef | None = None,
        number: int = 0,
        url: str = "",
        title: str = "",
        *,
        owner: str | None = None,
        repo: str | None = None,
    ) -> None:
        if repository is None:
            if owner is None or repo is None:
                raise DomainError("an issue requires a repository or owner and repo")
            repository = RepositoryRef(owner=owner, name=repo)
        if number <= 0:
            raise DomainError("issue number must be positive")
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "number", number)
        object.__setattr__(self, "url", url or f"https://github.com/{repository.full_name}/issues/{number}")
        object.__setattr__(self, "title", title)

    @property
    def owner(self) -> str:
        return self.repository.owner

    @property
    def repo(self) -> str:
        return self.repository.name

    @property
    def full_name(self) -> str:
        return self.repository.full_name


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: ToolName
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    stdout: str
    stderr: str
    exit_code: int
    runtime_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(slots=True)
class AgentDecision:
    summary: str
    plan: list[str]
    patches: list[str]
    tool_calls: list[ToolCall] | None = None
    tool_results: list[dict[str, Any]] | None = None
    usage: dict[str, Any] | None = None
    done: bool = False
