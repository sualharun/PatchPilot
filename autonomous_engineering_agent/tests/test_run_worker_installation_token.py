from agent.infrastructure.config.settings import AgentConfig
from agent.interfaces.workers.run_worker import _github_client_from_config


def _config(**overrides):
    return AgentConfig(github_token="broad-pat-token", **overrides)


def test_uses_installation_token_when_run_has_installation_id(monkeypatch):
    captured = {}

    def fake_from_installation(*, app_id, private_key, installation_id):
        captured["app_id"] = app_id
        captured["private_key"] = private_key
        captured["installation_id"] = installation_id

        class _Client:
            token = "installation-scoped-token"

        return _Client()

    monkeypatch.setattr(
        "agent.interfaces.workers.run_worker.GitHubClient.from_github_app_installation",
        staticmethod(fake_from_installation),
    )
    config = _config(github_app_id="12345", github_app_private_key="fake-pem")

    client = _github_client_from_config(config, installation_id="777")

    assert client.token == "installation-scoped-token"
    assert captured["installation_id"] == "777"


def test_falls_back_to_broad_pat_without_app_credentials():
    config = _config()

    client = _github_client_from_config(config, installation_id=None)

    assert client.token == "broad-pat-token"


def test_falls_back_to_broad_pat_when_no_installation_id_for_the_run():
    config = _config(github_app_id="12345", github_app_private_key="fake-pem")

    client = _github_client_from_config(config, installation_id=None)

    assert client.token == "broad-pat-token"
