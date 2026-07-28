"""Compatibility exports for the domain model.

New code should import from ``entities``, ``enums``, and ``value_objects``.
Keeping this module avoids breaking integrations that used the first MVP API.
"""

from .entities import IssueContext, PRAnalysisJob, QueuedRun, RunState, Workspace, WorkspaceUser
from .enums import JobStatus, RunStatus
from .value_objects import AgentDecision, CommandResult, IssueRef, RepositoryRef, ToolCall

User = WorkspaceUser

__all__ = [
    "AgentDecision",
    "CommandResult",
    "IssueContext",
    "IssueRef",
    "JobStatus",
    "PRAnalysisJob",
    "QueuedRun",
    "RepositoryRef",
    "RunState",
    "RunStatus",
    "ToolCall",
    "User",
    "Workspace",
    "WorkspaceUser",
]
