import pytest

from agent.github_client import GitHubClient, create_github_app_jwt, parse_issue_ref


@pytest.mark.parametrize(
    ("value", "owner", "repo", "number"),
    [
        ("https://github.com/octo/example/issues/42", "octo", "example", 42),
        ("octo/example#42", "octo", "example", 42),
        ("octo/example 42", "octo", "example", 42),
    ],
)
def test_parse_issue_ref(value, owner, repo, number):
    parsed = parse_issue_ref(value)

    assert parsed.owner == owner
    assert parsed.repo == repo
    assert parsed.number == number
    assert parsed.url == f"https://github.com/{owner}/{repo}/issues/{number}"


def test_parse_issue_ref_rejects_unknown_format():
    with pytest.raises(ValueError):
        parse_issue_ref("not-an-issue")


def test_create_github_app_jwt(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1_700_000_000)
    captured = {}

    def fake_encode(payload, private_key, algorithm):
        captured["payload"] = payload
        captured["private_key"] = private_key
        captured["algorithm"] = algorithm
        return "jwt-token"

    monkeypatch.setattr("agent.github_client.jwt.encode", fake_encode)

    token = create_github_app_jwt(app_id="12345", private_key="private-key")

    assert token == "jwt-token"
    assert captured["payload"] == {"iat": 1_699_999_940, "exp": 1_700_000_540, "iss": "12345"}
    assert captured["private_key"] == "private-key"
    assert captured["algorithm"] == "RS256"


def test_verify_repository_access_uses_repo_endpoint(monkeypatch):
    client = GitHubClient(token="token")

    def fake_get(path):
        assert path == "/repos/octo/example"
        return {
            "full_name": "octo/example",
            "default_branch": "main",
            "private": True,
            "permissions": {"pull": True, "push": False},
        }

    monkeypatch.setattr(client, "_get", fake_get)

    result = client.verify_repository_access("octo/example")

    assert result["full_name"] == "octo/example"
    assert result["can_pull"] is True
    assert result["can_push"] is False
