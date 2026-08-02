from pathlib import Path

from agent.config import (
    COMMAND_POLICY_VERSION,
    DEFAULT_SAFE_COMMANDS,
    AgentConfig,
    SandboxConfig,
    load_command_policy,
    load_config,
    load_repo_config,
    validate_config,
)


def test_config_loads_agent_yaml_and_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.yaml").write_text(
        """
install_commands:
  - python -m pip install -e .
test_commands:
  - python -m pytest tests/unit
sandbox:
  image: python:3.12-slim
  cpu_limit: "1"
  memory_limit: 1g
  network: none
database_url: sqlite:///custom.sqlite3
logs_dir: logs
""",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("GITHUB_TOKEN=github_pat_testtoken123456789\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    config = load_config(repo_path=repo, env_file=env_file)

    assert config.install_commands == ["python -m pip install -e ."]
    assert config.test_commands == ["python -m pytest tests/unit"]
    assert config.install_commands_configured is True
    assert config.test_commands_configured is True
    assert config.sandbox.image == "python:3.12-slim"
    assert config.sandbox.network == "none"
    assert config.database_url == "sqlite:///custom.sqlite3"
    assert config.logs_dir == Path("logs")
    assert config.github_token == "github_pat_testtoken123456789"


def test_validate_config_rejects_non_positive_timeout():
    config = AgentConfig(sandbox=SandboxConfig(test_timeout_seconds=0))

    assert "sandbox.test_timeout_seconds must be positive" in validate_config(config)


def test_sandbox_defaults_have_no_network_and_versioned_allowlist():
    config = SandboxConfig()

    assert config.network == "none"
    assert config.install_network == "patchpilot-sandbox-egress"
    assert config.allowed_commands == DEFAULT_SAFE_COMMANDS
    assert COMMAND_POLICY_VERSION >= 1


def test_load_command_policy_rejects_missing_version(tmp_path):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text('{"allowed_commands": ["python"]}', encoding="utf-8")

    try:
        load_command_policy(policy_path)
    except ValueError as exc:
        assert "version" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_load_command_policy_rejects_empty_allowlist(tmp_path):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text('{"version": 1, "allowed_commands": []}', encoding="utf-8")

    try:
        load_command_policy(policy_path)
    except ValueError as exc:
        assert "allowed_commands" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_repo_agent_yaml_cannot_loosen_sandbox_policy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.yaml").write_text(
        """
sandbox:
  network: bridge
  allowed_commands:
    - bash
""",
        encoding="utf-8",
    )
    base_config = AgentConfig()

    repo_config = load_repo_config(repo, base_config)

    assert repo_config.sandbox == base_config.sandbox
    assert repo_config.sandbox.network == "none"
    assert "bash" not in repo_config.sandbox.allowed_commands
