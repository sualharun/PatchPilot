from __future__ import annotations

import os
import re
from collections.abc import Iterable

SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "AUTH")


def collect_secret_values(extra_values: Iterable[str] | None = None) -> list[str]:
    values: list[str] = []
    for name, value in os.environ.items():
        if value and len(value) >= 8 and any(hint in name.upper() for hint in SECRET_ENV_HINTS):
            values.append(value)
    if extra_values:
        values.extend(value for value in extra_values if value and len(value) >= 8)
    return sorted(set(values), key=len, reverse=True)


TOKEN_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"),  # Stripe secret/restricted/publishable keys
    re.compile(r"whsec_[A-Za-z0-9]{10,}"),  # Stripe webhook signing secrets
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]+"),
]

PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"
)


def redact_text(text: str | None, extra_values: Iterable[str] | None = None) -> str:
    if not text:
        return ""
    redacted = str(text)
    for secret in collect_secret_values(extra_values):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = PRIVATE_KEY_BLOCK_RE.sub("[REDACTED PRIVATE KEY]", redacted)
    for pattern in TOKEN_PATTERNS:
        redacted = pattern.sub(lambda match: _redact_assignment(match.group(0)), redacted)
    return redacted


def _redact_assignment(value: str) -> str:
    if re.search(r"[:=]", value):
        key = re.split(r"[:=]", value, maxsplit=1)[0]
        sep = ":" if ":" in value else "="
        return f"{key}{sep}[REDACTED]"
    return "[REDACTED]"
