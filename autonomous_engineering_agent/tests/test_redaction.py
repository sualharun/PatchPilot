from agent.redaction import redact_text


def test_redacts_explicit_secret_values():
    text = "token is abcdefgh12345678"

    assert redact_text(text, ["abcdefgh12345678"]) == "token is [REDACTED]"


def test_redacts_common_token_patterns():
    text = "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz"

    assert "[REDACTED]" in redact_text(text)
    assert "abcdefghijklmnopqrstuvwxyz" not in redact_text(text)
