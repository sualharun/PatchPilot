from __future__ import annotations

from pathlib import Path

from agent.application.dto import ExecutionSettings, SandboxSettings
from agent.domain.value_objects import IssueRef
from agent.infrastructure.config.settings import AgentConfig, SandboxConfig, load_repo_config
from agent.infrastructure.repository.git import RepoWorkspace, clone_repository
from agent.infrastructure.repository.python_project import (
    detect_install_commands,
    detect_test_commands,
    summarize_project,
)
from agent.infrastructure.repository.tools import RepoTools
from agent.infrastructure.sandbox.docker import DockerSandbox


def execution_settings_from_config(config: AgentConfig) -> ExecutionSettings:
    return ExecutionSettings(
        install_commands=tuple(config.install_commands),
        test_commands=tuple(config.test_commands),
        install_commands_configured=config.install_commands_configured,
        test_commands_configured=config.test_commands_configured,
        sandbox=SandboxSettings(
            image=config.sandbox.image,
            cpu_limit=config.sandbox.cpu_limit,
            memory_limit=config.sandbox.memory_limit,
            network=config.sandbox.network,
            install_network=config.sandbox.install_network,
            install_timeout_seconds=config.sandbox.install_timeout_seconds,
            test_timeout_seconds=config.sandbox.test_timeout_seconds,
            command_timeout_seconds=config.sandbox.command_timeout_seconds,
            allowed_commands=frozenset(config.sandbox.allowed_commands),
        ),
        logs_dir=config.logs_dir,
    )


def sandbox_config_from_settings(settings: SandboxSettings) -> SandboxConfig:
    return SandboxConfig(
        image=settings.image,
        cpu_limit=settings.cpu_limit,
        memory_limit=settings.memory_limit,
        network=settings.network,
        install_network=settings.install_network,
        install_timeout_seconds=settings.install_timeout_seconds,
        test_timeout_seconds=settings.test_timeout_seconds,
        command_timeout_seconds=settings.command_timeout_seconds,
        allowed_commands=set(settings.allowed_commands),
    )


class GitWorkspaceManager:
    def clone(self, clone_url: str, issue: IssueRef) -> RepoWorkspace:
        return clone_repository(clone_url, issue)


class RepositoryToolFactory:
    def create(self, repo_path: Path, settings: ExecutionSettings) -> RepoTools:
        return RepoTools(repo_path, DockerSandbox(sandbox_config_from_settings(settings.sandbox)))


class PythonProjectEnvironmentDetector:
    def __init__(self, base_config: AgentConfig) -> None:
        self._base_config = base_config

    def resolve(self, repo_path: Path, base_settings: ExecutionSettings) -> ExecutionSettings:
        repo_config = load_repo_config(repo_path, self._base_config)
        settings = execution_settings_from_config(repo_config)
        install_commands = (
            settings.install_commands if settings.install_commands_configured else tuple(detect_install_commands(repo_path))
        )
        test_commands = settings.test_commands if settings.test_commands_configured else tuple(detect_test_commands(repo_path))
        return ExecutionSettings(
            install_commands=install_commands,
            test_commands=test_commands,
            install_commands_configured=True,
            test_commands_configured=True,
            sandbox=settings.sandbox,
            logs_dir=settings.logs_dir,
        )

    def summarize(self, repo_path: Path) -> str:
        return summarize_project(repo_path)
