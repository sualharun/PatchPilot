"""Compatibility imports for secret redaction."""

from .infrastructure.security.secrets import collect_secret_values, redact_text

__all__ = ["collect_secret_values", "redact_text"]
