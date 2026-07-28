"""Compatibility facade for the application-layer engineering agent.

The orchestration now lives in ``agent.application.services``. This wrapper
keeps the original public constructor stable for CLI users and integrations.
"""

from __future__ import annotations

from pathlib import Path

from .application.dto import AgentRunResult, ExecuteRunCommand, ExecutionSettings
from .application.services.engineering_agent import ExecuteEngineeringRunHandler
from .config import AgentConfig
from .domain.entities import RunState
from .infrastructure.artifacts import JsonArtifactWriter
from .infrastructure.db.repositories import SqlRunRepository
from .infrastructure.repository import GitWorkspaceManager, PythonProjectEnvironmentDetector
from .infrastructure.repository.adapters import execution_settings_from_config, sandbox_config_from_settings
from .infrastructure.security import EnvironmentSecretRedactor
from .persistence import RunStore
from .sandbox import DockerSandbox
from .tools import RepoTools


class _CompatibilityToolFactory:
    """Uses the module-level DockerSandbox so existing test injection still works."""

    def create(self, repo_path: Path, settings: ExecutionSettings) -> RepoTools:
        sandbox = DockerSandbox(sandbox_config_from_settings(settings.sandbox))
        return RepoTools(repo_path, sandbox)


class EngineeringAgent:
    def __init__(self, *, config: AgentConfig, github, llm, store: RunStore) -> None:
        self._handler = ExecuteEngineeringRunHandler(
            settings=execution_settings_from_config(config),
            github=github,
            llm=llm,
            runs=SqlRunRepository(store),
            workspaces=GitWorkspaceManager(),
            tool_factory=_CompatibilityToolFactory(),
            environments=PythonProjectEnvironmentDetector(config),
            artifacts=JsonArtifactWriter(),
            redactor=EnvironmentSecretRedactor(),
        )

    def run(
        self,
        *,
        issue,
        model: str,
        max_iterations: int,
        open_pr: bool,
        run_id: int | None = None,
    ) -> AgentRunResult:
        return self._handler.execute(
            ExecuteRunCommand(
                issue=issue,
                model=model,
                max_iterations=max_iterations,
                open_pr=open_pr,
                run_id=run_id,
            )
        )


__all__ = ["AgentRunResult", "EngineeringAgent", "RunState"]
