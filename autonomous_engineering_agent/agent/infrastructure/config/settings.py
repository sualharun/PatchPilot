from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

COMMAND_POLICY_PATH = Path(__file__).resolve().parents[1] / "sandbox" / "command_policy.json"


def load_command_policy(path: Path = COMMAND_POLICY_PATH) -> tuple[int, set[str]]:
    """Load the checked-in, versioned sandbox command allowlist.

    Keeping this in a reviewed file (instead of a Python literal) means changing what
    a sandboxed run may execute always shows up as a diff in code review/git history.
    """
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    version = data.get("version")
    commands = data.get("allowed_commands")
    if not isinstance(version, int):
        raise ValueError(f"Sandbox command policy {path} must set an integer 'version'")
    if not isinstance(commands, list) or not commands or not all(isinstance(c, str) and c.strip() for c in commands):
        raise ValueError(f"Sandbox command policy {path} must set a non-empty list of strings for 'allowed_commands'")
    return version, set(commands)


COMMAND_POLICY_VERSION, DEFAULT_SAFE_COMMANDS = load_command_policy()


@dataclass(slots=True)
class SandboxConfig:
    image: str = "python:3.11-slim"
    cpu_limit: str = "2"
    memory_limit: str = "2g"
    # Default network for commands that don't need internet access (test runs, tool calls).
    network: str = "none"
    # Dedicated, isolated network used only for dependency installation. Must not be able to
    # reach other project containers (postgres, redpanda, the dashboard) or the cloud metadata
    # endpoint (169.254.169.254) -- see scripts/setup-sandbox-network.sh.
    install_network: str = "patchpilot-sandbox-egress"
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
    github_app_trigger_label: str = "patchpilot"
    github_app_auto_open_pr: bool = True
    default_model: str = "gpt-4o-mini"
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_id_starter: str | None = None
    stripe_price_id_pro: str | None = None
    public_base_url: str = "http://localhost:8000"
    artifact_storage_dir: Path = Path(".agent-artifacts")
    worker_id: str = "local-worker"
    worker_lease_seconds: int = 900
    worker_max_attempts: int = 3
    worker_retry_backoff_seconds: int = 30
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_pr_analysis_topic: str = "pr-analysis-jobs"
    kafka_consumer_group: str = "patchpilot-pr-workers"
    pr_analysis_status_context: str = "patchpilot/pr-analysis"
    production: bool = False
    sentry_dsn: str | None = None


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
    if config.production:
        errors.extend(validate_production_config(config))
    return errors


def validate_production_config(config: AgentConfig) -> list[str]:
    """Boot-time checks for a real user launch. Never includes secret values, only names."""
    errors: list[str] = []
    if not config.database_url.startswith(("postgresql://", "postgres://")):
        errors.append("DATABASE_URL must be a PostgreSQL URL in production")
    if not config.dashboard_auth_enabled:
        errors.append("DASHBOARD_AUTH_ENABLED must be true in production")
    if not config.dashboard_session_secret:
        errors.append("DASHBOARD_SESSION_SECRET is required in production")
    if not config.dashboard_secure_cookies:
        errors.append("DASHBOARD_SECURE_COOKIES must be true in production")
    if not (config.github_oauth_client_id and config.github_oauth_client_secret and config.github_oauth_callback_url):
        errors.append(
            "GitHub OAuth credentials are required in production "
            "(GITHUB_OAUTH_CLIENT_ID, GITHUB_OAUTH_CLIENT_SECRET, GITHUB_OAUTH_CALLBACK_URL)"
        )
    if config.github_oauth_mock_enabled:
        errors.append("GITHUB_OAUTH_MOCK_ENABLED must be false in production")
    has_app_key = bool(config.github_app_private_key or config.github_app_private_key_path)
    if not (config.github_app_id and has_app_key):
        errors.append(
            "GitHub App credentials are required in production (GITHUB_APP_ID and "
            "GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH)"
        )
    if not config.github_app_webhook_secret:
        errors.append("GITHUB_APP_WEBHOOK_SECRET is required in production")
    if not (config.openai_api_key or config.anthropic_api_key):
        errors.append("An OpenAI or Anthropic API key is required in production")
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
        network=str(sandbox_raw.get("network", os.getenv("AGENT_DOCKER_NETWORK", "none"))),
        install_network=str(
            sandbox_raw.get("install_network", os.getenv("AGENT_DOCKER_INSTALL_NETWORK", "patchpilot-sandbox-egress"))
        ),
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
        github_app_trigger_label=os.getenv("GITHUB_APP_TRIGGER_LABEL", "patchpilot"),
        github_app_auto_open_pr=_env_bool("GITHUB_APP_AUTO_OPEN_PR", default=True),
        default_model=os.getenv("PATCHPILOT_DEFAULT_MODEL", "gpt-4o-mini"),
        stripe_secret_key=os.getenv("STRIPE_SECRET_KEY"),
        stripe_webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET"),
        stripe_price_id_starter=os.getenv("STRIPE_PRICE_ID_STARTER"),
        stripe_price_id_pro=os.getenv("STRIPE_PRICE_ID_PRO"),
        public_base_url=os.getenv("PATCHPILOT_PUBLIC_BASE_URL", "http://localhost:8000"),
        artifact_storage_dir=Path(os.getenv("ARTIFACT_STORAGE_DIR", ".agent-artifacts")),
        worker_id=os.getenv("PATCHPILOT_WORKER_ID", "local-worker"),
        worker_lease_seconds=int(os.getenv("PATCHPILOT_WORKER_LEASE_SECONDS", 900)),
        worker_max_attempts=int(os.getenv("PATCHPILOT_WORKER_MAX_ATTEMPTS", 3)),
        worker_retry_backoff_seconds=int(os.getenv("PATCHPILOT_WORKER_RETRY_BACKOFF_SECONDS", 30)),
        kafka_bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        kafka_pr_analysis_topic=os.getenv("KAFKA_PR_ANALYSIS_TOPIC", "pr-analysis-jobs"),
        kafka_consumer_group=os.getenv("KAFKA_CONSUMER_GROUP", "patchpilot-pr-workers"),
        pr_analysis_status_context=os.getenv("PR_ANALYSIS_STATUS_CONTEXT", "patchpilot/pr-analysis"),
        production=_env_bool("PATCHPILOT_PRODUCTION", default=False),
        sentry_dsn=os.getenv("SENTRY_DSN"),
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
    # The sandbox's network access and command allowlist are a deployment-controlled security
    # boundary. A repository's own agent.yaml is untrusted input and must not be able to loosen
    # it (e.g. requesting the "bridge" network or adding executables to the allowlist).
    repo_config.sandbox = base_config.sandbox
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
