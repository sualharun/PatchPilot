"""Password hashing for self-service accounts.

Stdlib PBKDF2 so the application layer stays dependency-free. The stored
format is self-describing (algorithm:iterations:salt:digest) so iteration
counts can be raised later without invalidating existing hashes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_ITERATIONS = 240_000

MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return ":".join(
        [
            "pbkdf2-sha256",
            str(_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode(),
            base64.urlsafe_b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split(":")
        if algorithm != "pbkdf2-sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False
