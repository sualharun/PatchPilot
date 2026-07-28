"""Compatibility imports for the GitHub outbound adapter."""

from .domain.entities import IssueContext
from .domain.services import parse_issue_ref
from .domain.value_objects import IssueRef
from .infrastructure.github.client import GitHubClient, create_github_app_jwt, jwt

__all__ = [
    "GitHubClient",
    "IssueContext",
    "IssueRef",
    "create_github_app_jwt",
    "jwt",
    "parse_issue_ref",
]
