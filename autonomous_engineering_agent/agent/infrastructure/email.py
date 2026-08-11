"""SMTP mailer for transactional dashboard email.

Stdlib ``smtplib`` so no vendor is baked in: point SMTP_HOST at Postmark, SES,
Mailgun, or a local relay. When SMTP is unconfigured the mailer logs the message
instead of sending it, so signup keeps working in local dev and any deployment
that has not set credentials yet.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


class SmtpMailer:
    def __init__(
        self,
        *,
        host: str | None,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        from_address: str = "no-reply@patchpilot.local",
        use_tls: bool = True,
        timeout_seconds: int = 15,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_address = from_address
        self._use_tls = use_tls
        self._timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self._host)

    def send(self, *, to_address: str, subject: str, body: str) -> bool:
        """Send one plain-text message. Returns True when SMTP accepted it."""
        if not self._host:
            # warning, not info: with no logging config the stdlib last-resort
            # handler only emits at WARNING, and this line is the only way to
            # retrieve a verification link when SMTP is unset.
            logger.warning("SMTP not configured; would send %r to %s:\n%s", subject, to_address, body)
            return False
        message = EmailMessage()
        message["From"] = self._from_address
        message["To"] = to_address
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout_seconds) as client:
                if self._use_tls:
                    client.starttls()
                if self._username and self._password:
                    client.login(self._username, self._password)
                client.send_message(message)
        except (smtplib.SMTPException, OSError):
            # Never fail the caller's request because email is down.
            logger.exception("Failed to send %r to %s", subject, to_address)
            return False
        return True
