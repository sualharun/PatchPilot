from pathlib import Path

from agent.config import AgentConfig, SandboxConfig, load_config, validate_config


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
