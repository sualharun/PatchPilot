from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .config import AgentConfig, validate_config
from .persistence import RunStore
from .redaction import redact_text


@dataclass(slots=True)
class DoctorCheck:
    name: str
    status: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "message": self.message}


def run_doctor(
    config: AgentConfig,
    *,
    check_docker_run: bool = False,
    check_database: bool = True,
) -> list[DoctorCheck]:
    checks = [
        _check_executable("git"),
        _check_executable("docker"),
        _check_executable("python3"),
        _check_config(config),
        _check_llm_key(config),
        _check_github_key(config),
        _check_production_config(config),
        _check_docker_daemon(),
    ]
    if check_database:
        checks.insert(6, _check_database(config))
    else:
        checks.insert(6, DoctorCheck(name="database", status="warn", message="database reachability check skipped"))
    if check_docker_run:
        checks.append(_check_docker_python_image(config))
    return checks


def has_errors(checks: list[DoctorCheck]) -> bool:
    return any(check.status == "error" for check in checks)


def format_doctor(checks: list[DoctorCheck]) -> str:
    lines = []
    for check in checks:
        marker = {"ok": "OK", "warn": "WARN", "error": "ERROR"}[check.status]
        lines.append(f"[{marker}] {check.name}: {check.message}")
    return "\n".join(lines)


def _check_executable(name: str) -> DoctorCheck:
    path = shutil.which(name)
    if path:
        return DoctorCheck(name=f"executable:{name}", status="ok", message=path)
    severity = "error" if name in {"git", "docker"} else "warn"
    return DoctorCheck(name=f"executable:{name}", status=severity, message=f"{name} not found on PATH")


def _check_config(config: AgentConfig) -> DoctorCheck:
    errors = validate_config(config)
    if errors:
        return DoctorCheck(name="config", status="error", message="; ".join(errors))
    return DoctorCheck(name="config", status="ok", message="configuration is valid")


def _check_llm_key(config: AgentConfig) -> DoctorCheck:
    if config.openai_api_key or config.anthropic_api_key:
        return DoctorCheck(name="llm_api_key", status="ok", message="LLM API key detected")
    return DoctorCheck(name="llm_api_key", status="warn", message="no LLM API key detected; stub mode will not edit code")


def _check_github_key(config: AgentConfig) -> DoctorCheck:
    if config.github_token:
        return DoctorCheck(name="github_token", status="ok", message="GitHub token detected")
    return DoctorCheck(
        name="github_token",
        status="warn",
        message="no GitHub token detected; public issue fetching may work, PR creation will not",
    )


def _check_database(config: AgentConfig) -> DoctorCheck:
    try:
        store = RunStore(config.database_url, allow_sqlite_fallback=not config.production)
        store.close()
    except Exception as exc:
        return DoctorCheck(name="database", status="error", message=redact_text(str(exc)))
    return DoctorCheck(name="database", status="ok", message=_redact_database_url(config.database_url))


def _check_production_config(config: AgentConfig) -> DoctorCheck:
    if not config.production:
        return DoctorCheck(name="production_config", status="ok", message="production checks disabled")
    errors = []
    if not config.database_url.startswith(("postgresql://", "postgres://")):
        errors.append("DATABASE_URL must be PostgreSQL in production")
    if not config.dashboard_auth_enabled:
        errors.append("DASHBOARD_AUTH_ENABLED must be true in production")
    if not config.dashboard_password:
        errors.append("DASHBOARD_PASSWORD is required in production")
    if not config.dashboard_session_secret:
        errors.append("DASHBOARD_SESSION_SECRET is required in production")
    if not config.dashboard_secure_cookies:
        errors.append("DASHBOARD_SECURE_COOKIES must be true in production")
    if config.dashboard_demo_data_enabled:
        errors.append("DASHBOARD_DEMO_DATA_ENABLED must be false in production")
    if not (config.github_oauth_client_id and config.github_oauth_client_secret and config.github_oauth_callback_url):
        errors.append("GitHub OAuth client id, secret, and callback URL are required in production")
    if not config.github_webhook_secret:
        errors.append("GITHUB_WEBHOOK_SECRET is required for production PR webhooks")
    if config.github_oauth_mock_enabled:
        errors.append("GITHUB_OAUTH_MOCK_ENABLED must be false in production")
    if not config.github_app_install_url:
        errors.append("GITHUB_APP_INSTALL_URL should be configured for production onboarding")
    if not config.github_app_id:
        errors.append("GITHUB_APP_ID should be configured for installation-token auth")
    if not config.github_app_installation_id:
        errors.append("GITHUB_APP_INSTALLATION_ID should be configured for installation-token auth")
    if not (config.github_app_private_key or config.github_app_private_key_path):
        errors.append("GitHub App private key or private key path should be configured")
    if str(config.artifact_storage_dir) == ".agent-artifacts":
        errors.append("ARTIFACT_STORAGE_DIR should point at persistent storage in production")
    if config.kafka_bootstrap_servers == "localhost:9092":
        errors.append("KAFKA_BOOTSTRAP_SERVERS should point at the production Kafka/Redpanda service")
    if errors:
        return DoctorCheck(name="production_config", status="error", message="; ".join(errors))
    return DoctorCheck(name="production_config", status="ok", message="production configuration is deployable")


def _check_docker_daemon() -> DoctorCheck:
    if not shutil.which("docker"):
        return DoctorCheck(name="docker_daemon", status="error", message="docker executable is unavailable")
    try:
        completed = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return DoctorCheck(name="docker_daemon", status="error", message=redact_text(str(exc)))
    if completed.returncode != 0:
        return DoctorCheck(name="docker_daemon", status="error", message=redact_text(completed.stderr or completed.stdout))
    return DoctorCheck(name="docker_daemon", status="ok", message="Docker daemon is reachable")


def _check_docker_python_image(config: AgentConfig) -> DoctorCheck:
    try:
        completed = subprocess.run(
            ["docker", "run", "--rm", config.sandbox.image, "python", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        return DoctorCheck(name="docker_python_image", status="error", message=redact_text(str(exc)))
    if completed.returncode != 0:
        return DoctorCheck(
            name="docker_python_image",
            status="error",
            message=redact_text(completed.stderr or completed.stdout),
        )
    return DoctorCheck(name="docker_python_image", status="ok", message=redact_text(completed.stdout or completed.stderr).strip())


def _redact_database_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    if not parsed.password:
        return database_url
    username = parsed.username or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{username}:***@" if username else "***@"
    return urlunsplit((parsed.scheme, f"{auth}{host}{port}", parsed.path, parsed.query, parsed.fragment))
