"""Domain entities with identity and lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .enums import RunStatus
from .errors import DomainError, InvalidRunTransition
from .value_objects import IssueRef, RepositoryRef


@dataclass(frozen=True, slots=True)
class Workspace:
    id: int | None
    name: str
    slug: str


@dataclass(frozen=True, slots=True)
class WorkspaceUser:
    id: int | None
    email: str
    name: str


@dataclass(frozen=True, slots=True)
class IssueContext:
    ref: IssueRef
    title: str
    body: str
    comments: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    linked_prs: tuple[str, ...] = ()


@dataclass(slots=True)
class QueuedRun:
    issue: IssueRef
    model: str
    branch: str
    max_iterations: int
    open_pr: bool
    requested_by: str
    installation_id: str | None = None
    status: RunStatus = RunStatus.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise DomainError("max_iterations must be at least 1")
        if not self.model.strip():
            raise DomainError("model must not be empty")
        if not self.branch.strip():
            raise DomainError("branch must not be empty")

    def mark_running(self) -> None:
        if self.status is not RunStatus.QUEUED:
            raise InvalidRunTransition(f"cannot start run from {self.status}")
        self.status = RunStatus.RUNNING

    def finish(self, status: RunStatus) -> None:
        if not status.is_terminal:
            raise InvalidRunTransition(f"{status} is not a terminal run status")
        if self.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
            raise InvalidRunTransition(f"cannot finish run from {self.status}")
        self.status = status


@dataclass(frozen=True, slots=True, init=False)
class PRAnalysisJob:
    repository: RepositoryRef
    pr_number: int
    commit_sha: str
    action: str
    delivery_id: str
    installation_id: str | None = None
    sender_login: str | None = None
    enqueued_at: float = 0.0

    def __init__(
        self,
        repository: RepositoryRef | None = None,
        pr_number: int = 0,
        commit_sha: str = "",
        action: str = "",
        delivery_id: str = "",
        installation_id: str | None = None,
        sender_login: str | None = None,
        enqueued_at: float = 0.0,
        *,
        owner: str | None = None,
        repo: str | None = None,
    ) -> None:
        if repository is None:
            if owner is None or repo is None:
                raise DomainError("a pull request job requires a repository or owner and repo")
            repository = RepositoryRef(owner=owner, name=repo)
        if pr_number <= 0:
            raise DomainError("pull request number must be positive")
        if not commit_sha.strip():
            raise DomainError("commit SHA must not be empty")
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "pr_number", pr_number)
        object.__setattr__(self, "commit_sha", commit_sha)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "delivery_id", delivery_id)
        object.__setattr__(self, "installation_id", installation_id)
        object.__setattr__(self, "sender_login", sender_login)
        object.__setattr__(self, "enqueued_at", enqueued_at)

    @property
    def owner(self) -> str:
        return self.repository.owner

    @property
    def repo(self) -> str:
        return self.repository.name

    @property
    def full_name(self) -> str:
        return self.repository.full_name


@dataclass(slots=True)
class RunState:
    commands: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)
    test_results: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    iterations: int = 0
    summary: str = ""
