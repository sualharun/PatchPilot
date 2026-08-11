from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from agent.application.ports.outbound import (
    AccountRepository,
    AuditLog,
    GitHubConnectionRepository,
    GitHubRepositoryGateway,
    Mailer,
    ProviderKeyRepository,
    RepositoryCatalog,
)
from agent.application.services.passwords import MIN_PASSWORD_LENGTH, hash_password, verify_password


@dataclass(frozen=True, slots=True)
class RecordAuditCommand:
    actor: str
    event: str
    target: str
    result: str = "success"
    metadata: dict[str, Any] | None = None


class RecordAuditHandler:
    def __init__(self, audit_log: AuditLog) -> None:
        self._audit_log = audit_log

    def execute(self, command: RecordAuditCommand) -> int:
        return self._audit_log.record_event(
            actor=command.actor,
            event=command.event,
            target=command.target,
            result=command.result,
            metadata=command.metadata,
        )


@dataclass(frozen=True, slots=True)
class ConnectGitHubAccountCommand:
    login: str
    email: str
    scopes: str | None
    token_hint: str | None
    installation_id: str | None = None
    github_user_id: str | None = None
    name: str | None = None
    avatar_url: str | None = None


class ConnectGitHubAccountHandler:
    def __init__(
        self,
        accounts: AccountRepository,
        connections: GitHubConnectionRepository,
        audit_log: AuditLog,
    ) -> None:
        self._accounts = accounts
        self._connections = connections
        self._audit_log = audit_log

    def execute(self, command: ConnectGitHubAccountCommand) -> None:
        if command.github_user_id:
            user = self._accounts.upsert_github_user(
                github_user_id=command.github_user_id,
                login=command.login,
                name=command.name or command.login,
                email=command.email,
                avatar_url=command.avatar_url,
            )
        else:
            user = self._accounts.get_or_create_user(email=command.email, name=command.login)
        self._connections.upsert_github_connection(
            user_id=int(user["id"]) if user.get("id") else None,
            login=command.login,
            scopes=command.scopes or "",
            token_hint=command.token_hint or "not stored",
            installation_id=command.installation_id,
        )
        self._audit_log.record_event(actor=command.login, event="auth.github_login", target="dashboard")


class SyncGitHubRepositoriesHandler:
    def __init__(
        self,
        github: GitHubRepositoryGateway,
        accounts: AccountRepository,
        repositories: RepositoryCatalog,
    ) -> None:
        self._github = github
        self._accounts = accounts
        self._repositories = repositories

    def execute(self, *, limit: int = 100) -> list[dict[str, Any]]:
        repositories = self._github.list_accessible_repositories(limit)
        workspace_id = _workspace_id(self._accounts.get_account_context())
        for repository in repositories:
            if repository.get("full_name"):
                self._repositories.upsert_repository(
                    workspace_id=workspace_id,
                    full_name=str(repository["full_name"]),
                    default_branch=repository.get("default_branch"),
                    config_status="github-verified",
                )
        return repositories


class VerifyGitHubRepositoryHandler:
    def __init__(
        self,
        github: GitHubRepositoryGateway,
        accounts: AccountRepository,
        repositories: RepositoryCatalog,
    ) -> None:
        self._github = github
        self._accounts = accounts
        self._repositories = repositories

    def execute(self, full_name: str) -> dict[str, Any]:
        result = self._github.verify_repository_access(full_name)
        self._repositories.upsert_repository(
            workspace_id=_workspace_id(self._accounts.get_account_context()),
            full_name=str(result["full_name"]),
            default_branch=str(result["default_branch"]),
            config_status="github-verified",
        )
        return result


class SeedRuntimeStateHandler:
    def __init__(
        self,
        accounts: AccountRepository,
        provider_keys: ProviderKeyRepository,
    ) -> None:
        self._accounts = accounts
        self._provider_keys = provider_keys

    def execute(
        self,
        *,
        username: str,
        openai_key_hint: str | None,
        anthropic_key_hint: str | None,
    ) -> None:
        self._accounts.seed_defaults(username=username)
        workspace_id = _workspace_id(self._accounts.get_account_context())
        if openai_key_hint:
            self._provider_keys.upsert_provider_key(
                workspace_id=workspace_id,
                provider="openai",
                key_hint=openai_key_hint,
            )
        if anthropic_key_hint:
            self._provider_keys.upsert_provider_key(
                workspace_id=workspace_id,
                provider="anthropic",
                key_hint=anthropic_key_hint,
            )


class SignupError(Exception):
    """Signup rejected for a user-facing reason; ``code`` keys the message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CreateAccountCommand:
    email: str
    password: str
    password_confirm: str
    name: str | None = None


class CreateAccountHandler:
    def __init__(self, accounts: AccountRepository, audit_log: AuditLog) -> None:
        self._accounts = accounts
        self._audit_log = audit_log

    def execute(self, command: CreateAccountCommand) -> dict[str, Any]:
        email = command.email.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise SignupError("invalid_email")
        if command.password != command.password_confirm:
            raise SignupError("password_mismatch")
        if len(command.password) < MIN_PASSWORD_LENGTH:
            raise SignupError("weak_password")
        if self._accounts.get_user_by_email(email):
            raise SignupError("email_exists")
        try:
            user = self._accounts.create_password_user(
                email=email,
                name=(command.name or "").strip() or email.split("@", 1)[0],
                password_hash=hash_password(command.password),
            )
        except ValueError:
            # Concurrent signup with the same email won the race; the UNIQUE
            # constraint on users.email backstops the store's re-check.
            raise SignupError("email_exists") from None
        self._audit_log.record_event(actor=email, event="auth.signup", target="dashboard")
        return user


class AccountError(Exception):
    """Account change rejected for a user-facing reason; ``code`` keys the message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChangePasswordCommand:
    login: str
    current_password: str
    new_password: str
    new_password_confirm: str


class ChangePasswordHandler:
    def __init__(self, accounts: AccountRepository, audit_log: AuditLog) -> None:
        self._accounts = accounts
        self._audit_log = audit_log

    def execute(self, command: ChangePasswordCommand) -> None:
        user = self._accounts.get_user_by_login(command.login) if command.login else None
        if not user:
            raise AccountError("not_managed")
        # OAuth-only accounts have no password yet; session auth suffices to set one.
        if user.get("password_hash") and not verify_password(command.current_password, str(user["password_hash"])):
            raise AccountError("wrong_password")
        if command.new_password != command.new_password_confirm:
            raise AccountError("password_mismatch")
        if len(command.new_password) < MIN_PASSWORD_LENGTH:
            raise AccountError("weak_password")
        self._accounts.set_user_password(user_id=int(user["id"]), password_hash=hash_password(command.new_password))
        self._audit_log.record_event(actor=command.login, event="account.password_changed", target="dashboard")


@dataclass(frozen=True, slots=True)
class UpdateEmailCommand:
    login: str
    new_email: str


class UpdateEmailHandler:
    def __init__(self, accounts: AccountRepository, audit_log: AuditLog) -> None:
        self._accounts = accounts
        self._audit_log = audit_log

    def execute(self, command: UpdateEmailCommand) -> str:
        """Update the email; returns the (possibly new) session login."""
        email = command.new_email.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise AccountError("invalid_email")
        user = self._accounts.get_user_by_login(command.login) if command.login else None
        if not user:
            raise AccountError("not_managed")
        other = self._accounts.get_user_by_email(email)
        if other and int(other["id"]) != int(user["id"]):
            raise AccountError("email_exists")
        # Password accounts use their email as session login; keep them in sync.
        update_login = user.get("login") == user.get("email")
        self._accounts.update_user_email(user_id=int(user["id"]), email=email, update_login=update_login)
        self._audit_log.record_event(
            actor=command.login,
            event="account.email_changed",
            target="dashboard",
            metadata={"old_email": str(user.get("email")), "new_email": email},
        )
        return email if update_login else command.login


EMAIL_VERIFICATION_TTL_HOURS = 24


def _hash_token(token: str) -> str:
    """Tokens are stored hashed: a leaked database row must not be redeemable."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SendVerificationEmailCommand:
    login: str
    base_url: str


class SendVerificationEmailHandler:
    def __init__(self, accounts: AccountRepository, audit_log: AuditLog, mailer: Mailer) -> None:
        self._accounts = accounts
        self._audit_log = audit_log
        self._mailer = mailer

    def execute(self, command: SendVerificationEmailCommand) -> bool:
        """Issue a fresh verification link. Returns True when SMTP accepted it."""
        user = self._accounts.get_user_by_login(command.login) if command.login else None
        if not user or user.get("email_verified_at"):
            return False
        token = secrets.token_urlsafe(32)
        expires_at = (datetime.now(UTC) + timedelta(hours=EMAIL_VERIFICATION_TTL_HOURS)).isoformat()
        self._accounts.add_email_verification_token(
            user_id=int(user["id"]), token_hash=_hash_token(token), expires_at=expires_at
        )
        link = f"{command.base_url.rstrip('/')}/verify-email?token={token}"
        sent = self._mailer.send(
            to_address=str(user["email"]),
            subject="Verify your PatchPilot email",
            body=(
                "Welcome to PatchPilot.\n\n"
                f"Confirm this email address by opening the link below within {EMAIL_VERIFICATION_TTL_HOURS} hours:\n\n"
                f"{link}\n\n"
                "If you did not create a PatchPilot account, you can ignore this message.\n"
            ),
        )
        self._audit_log.record_event(
            actor=str(user["email"]),
            event="account.verification_sent",
            target="dashboard",
            result="success" if sent else "failure",
        )
        return sent


@dataclass(frozen=True, slots=True)
class VerifyEmailCommand:
    token: str


class VerifyEmailHandler:
    def __init__(self, accounts: AccountRepository, audit_log: AuditLog) -> None:
        self._accounts = accounts
        self._audit_log = audit_log

    def execute(self, command: VerifyEmailCommand) -> bool:
        if not command.token:
            return False
        user = self._accounts.consume_email_verification_token(_hash_token(command.token))
        if not user:
            return False
        self._audit_log.record_event(
            actor=str(user.get("email") or "unknown"), event="account.email_verified", target="dashboard"
        )
        return True


@dataclass(frozen=True, slots=True)
class CompleteOnboardingCommand:
    login: str
    reason: str  # "skipped" | "completed"


class CompleteOnboardingHandler:
    def __init__(self, accounts: AccountRepository, audit_log: AuditLog) -> None:
        self._accounts = accounts
        self._audit_log = audit_log

    def execute(self, command: CompleteOnboardingCommand) -> None:
        user = self._accounts.get_user_by_login(command.login) if command.login else None
        if not user:
            return  # env-admin / local dev sessions have no user row to persist on
        if not user.get("onboarding_completed_at"):
            self._accounts.set_onboarding_completed(user_id=int(user["id"]))
            self._audit_log.record_event(
                actor=command.login, event=f"onboarding.{command.reason}", target="dashboard"
            )


def _workspace_id(account: dict[str, Any]) -> int | None:
    value = account.get("workspace", {}).get("id")
    return int(value) if value else None
