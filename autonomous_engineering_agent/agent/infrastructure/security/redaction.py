from agent.infrastructure.security.secrets import redact_text


class EnvironmentSecretRedactor:
    def redact(self, text: str | None) -> str:
        return redact_text(text)
