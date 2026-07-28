from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_SAFE_COMMANDS = {
    "python",
    "python3",
    "pip",
    "pytest",
    "coverage",
    "tox",
    "nox",
    "poetry",
    "pipenv",
    "unittest",
    "make",
}


@dataclass(slots=True)
class SandboxConfig:
    image: str = "python:3.11-slim"
    cpu_limit: str = "2"
    memory_limit: str = "2g"
    network: str = "bridge"
    install_timeout_seconds: int = 600
    test_timeout_seconds: int = 600
    command_timeout_seconds: int = 300
    allowed_commands: set[str] = field(default_factory=lambda: set(DEFAULT_SAFE_COMMANDS))


@dataclass(slots=True)
class AgentConfig:
    install_commands: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    install_commands_configured: bool = False
    test_commands_configured: bool = False
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    database_url: str = "sqlite:///agent_runs.sqlite3"
    logs_dir: Path = Path(".agent-logs")
    github_token: str | None = None
    github_webhook_secret: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    dashboard_auth_enabled: bool = False
    dashboard_username: str = "admin"
    dashboard_password: str | None = None
    dashboard_session_secret: str | None = None
    dashboard_secure_cookies: bool = False
    dashboard_demo_data_enabled: bool = True
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    github_oauth_callback_url: str | None = None
    github_oauth_mock_enabled: bool = False
    github_app_install_url: str | None = None
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    github_app_private_key_path: Path | None = None
    github_app_installation_id: str | None = None
    github_app_webhook_secret: str | None = None
    artifact_storage_dir: Path = Path(".agent-artifacts")
    worker_id: str = "local-worker"
    worker_lease_seconds: int = 900
    worker_max_attempts: int = 3
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_pr_analysis_topic: str = "pr-analysis-jobs"
    kafka_consumer_group: str = "patchpilot-pr-workers"
    pr_analysis_status_context: str = "patchpilot/pr-analysis"
    production: bool = False


def validate_config(config: AgentConfig) -> list[str]:
    errors: list[str] = []
    if config.sandbox.install_timeout_seconds <= 0:
        errors.append("sandbox.install_timeout_seconds must be positive")
    if config.sandbox.test_timeout_seconds <= 0:
        errors.append("sandbox.test_timeout_seconds must be positive")
    if config.sandbox.command_timeout_seconds <= 0:
        errors.append("sandbox.command_timeout_seconds must be positive")
    if config.worker_lease_seconds <= 0:
        errors.append("worker_lease_seconds must be positive")
    if config.worker_max_attempts <= 0:
        errors.append("worker_max_attempts must be positive")
    if not config.kafka_bootstrap_servers.strip():
        errors.append("kafka_bootstrap_servers must not be empty")
    if not config.kafka_pr_analysis_topic.strip():
        errors.append("kafka_pr_analysis_topic must not be empty")
    if not config.kafka_consumer_group.strip():
        errors.append("kafka_consumer_group must not be empty")
    if not config.sandbox.image.strip():
        errors.append("sandbox.image must not be empty")
    if not config.sandbox.allowed_commands:
        errors.append("sandbox.allowed_commands must contain at least one command")
    for field_name, commands in (("install_commands", config.install_commands), ("test_commands", config.test_commands)):
        for command in commands:
            if not isinstance(command, str) or not command.strip():
                errors.append(f"{field_name} must contain non-empty strings")
    return errors


def load_config(repo_path: Path | None = None, env_file: Path | None = None) -> AgentConfig:
    if env_file:
        load_dotenv()
        load_dotenv(env_file, override=True)
    else:
        load_dotenv()

    raw: dict[str, Any] = {}
    if repo_path:
        config_path = repo_path / "agent.yaml"
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
                if not isinstance(loaded, dict):
                    raise ValueError("agent.yaml must contain a mapping at the top level")
                raw = loaded

    sandbox_raw = raw.get("sandbox", {}) or {}
    sandbox = SandboxConfig(
        image=str(sandbox_raw.get("image", os.getenv("AGENT_DOCKER_IMAGE", "python:3.11-slim"))),
        cpu_limit=str(sandbox_raw.get("cpu_limit", os.getenv("AGENT_CPU_LIMIT", "2"))),
        memory_limit=str(sandbox_raw.get("memory_limit", os.getenv("AGENT_MEMORY_LIMIT", "2g"))),
        network=str(sandbox_raw.get("network", os.getenv("AGENT_DOCKER_NETWORK", "bridge"))),
        install_timeout_seconds=int(
            sandbox_raw.get("install_timeout_seconds", os.getenv("AGENT_INSTALL_TIMEOUT", 600))
        ),
        test_timeout_seconds=int(sandbox_raw.get("test_timeout_seconds", os.getenv("AGENT_TEST_TIMEOUT", 600))),
        command_timeout_seconds=int(
            sandbox_raw.get("command_timeout_seconds", os.getenv("AGENT_COMMAND_TIMEOUT", 300))
        ),
        allowed_commands=set(sandbox_raw.get("allowed_commands", DEFAULT_SAFE_COMMANDS)),
    )

    config = AgentConfig(
        install_commands=list(raw.get("install_commands", []) or []),
        test_commands=list(raw.get("test_commands", []) or []),
        install_commands_configured="install_commands" in raw,
        test_commands_configured="test_commands" in raw,
        sandbox=sandbox,
        database_url=str(raw.get("database_url", os.getenv("DATABASE_URL", "sqlite:///agent_runs.sqlite3"))),
        logs_dir=Path(raw.get("logs_dir", os.getenv("AGENT_LOGS_DIR", ".agent-logs"))),
        github_token=os.getenv("GITHUB_TOKEN"),
        github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        dashboard_auth_enabled=_env_bool("DASHBOARD_AUTH_ENABLED", default=False),
        dashboard_username=os.getenv("DASHBOARD_USERNAME", "admin"),
        dashboard_password=os.getenv("DASHBOARD_PASSWORD"),
        dashboard_session_secret=os.getenv("DASHBOARD_SESSION_SECRET"),
        dashboard_secure_cookies=_env_bool("DASHBOARD_SECURE_COOKIES", default=False),
        dashboard_demo_data_enabled=_env_bool("DASHBOARD_DEMO_DATA_ENABLED", default=True),
        github_oauth_client_id=os.getenv("GITHUB_OAUTH_CLIENT_ID"),
        github_oauth_client_secret=os.getenv("GITHUB_OAUTH_CLIENT_SECRET"),
        github_oauth_callback_url=os.getenv("GITHUB_OAUTH_CALLBACK_URL"),
        github_oauth_mock_enabled=_env_bool("GITHUB_OAUTH_MOCK_ENABLED", default=False),
        github_app_install_url=os.getenv("GITHUB_APP_INSTALL_URL"),
        github_app_id=os.getenv("GITHUB_APP_ID"),
        github_app_private_key=os.getenv("GITHUB_APP_PRIVATE_KEY"),
        github_app_private_key_path=_optional_path(os.getenv("GITHUB_APP_PRIVATE_KEY_PATH")),
        github_app_installation_id=os.getenv("GITHUB_APP_INSTALLATION_ID"),
        github_app_webhook_secret=os.getenv("GITHUB_APP_WEBHOOK_SECRET"),
        artifact_storage_dir=Path(os.getenv("ARTIFACT_STORAGE_DIR", ".agent-artifacts")),
        worker_id=os.getenv("PATCHPILOT_WORKER_ID", "local-worker"),
        worker_lease_seconds=int(os.getenv("PATCHPILOT_WORKER_LEASE_SECONDS", 900)),
        worker_max_attempts=int(os.getenv("PATCHPILOT_WORKER_MAX_ATTEMPTS", 3)),
        kafka_bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        kafka_pr_analysis_topic=os.getenv("KAFKA_PR_ANALYSIS_TOPIC", "pr-analysis-jobs"),
        kafka_consumer_group=os.getenv("KAFKA_CONSUMER_GROUP", "patchpilot-pr-workers"),
        pr_analysis_status_context=os.getenv("PR_ANALYSIS_STATUS_CONTEXT", "patchpilot/pr-analysis"),
        production=_env_bool("PATCHPILOT_PRODUCTION", default=False),
    )
    errors = validate_config(config)
    if errors:
        raise ValueError("Invalid configuration: " + "; ".join(errors))
    return config


def load_repo_config(repo_path: Path, base_config: AgentConfig) -> AgentConfig:
    repo_config = load_config(repo_path=repo_path)
    repo_config.github_token = base_config.github_token
    repo_config.github_webhook_secret = base_config.github_webhook_secret
    repo_config.openai_api_key = base_config.openai_api_key
    repo_config.anthropic_api_key = base_config.anthropic_api_key
    if base_config.database_url != "sqlite:///agent_runs.sqlite3":
        repo_config.database_url = base_config.database_url
    if str(base_config.logs_dir) != ".agent-logs":
        repo_config.logs_dir = base_config.logs_dir
    return repo_config


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _optional_path(raw: str | None) -> Path | None:
    return Path(raw).expanduser() if raw else None
