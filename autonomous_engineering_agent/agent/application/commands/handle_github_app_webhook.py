"""Use case for GitHub App webhook events: installations, issues, pull requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.application.commands.handle_pr_webhook import (
    EnqueuePullRequestAnalysisCommand,
    EnqueuePullRequestAnalysisHandler,
)
from agent.application.commands.queue_run import QueueRunCommand, QueueRunHandler
from agent.application.ports.outbound import (
    AuditLog,
    GitHubAppInstallationRepository,
    WebhookDeliveryRepository,
)
from agent.domain.services import issue_run_candidate


@dataclass(frozen=True, slots=True)
class GitHubAppWebhookSettings:
    default_model: str = "gpt-4o-mini"
    max_iterations: int = 5
    open_pr: bool = True
    trigger_label: str = "patchpilot"
    worker_max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class GitHubAppWebhookCommand:
    event: str
    delivery_id: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GitHubAppWebhookResult:
    status: str  # duplicate | processed | ignored | queued
    detail: dict[str, Any] = field(default_factory=dict)


class HandleGitHubAppWebhookHandler:
    def __init__(
        self,
        *,
        deliveries: WebhookDeliveryRepository,
        installations: GitHubAppInstallationRepository,
        queue_run: QueueRunHandler,
        pr_analysis: EnqueuePullRequestAnalysisHandler | None,
        audit_log: AuditLog,
        settings: GitHubAppWebhookSettings,
        accounts: Any | None = None,
        billing: Any | None = None,
    ) -> None:
        self._deliveries = deliveries
        self._installations = installations
        self._queue_run = queue_run
        self._pr_analysis = pr_analysis
        self._audit_log = audit_log
        self._settings = settings
        self._accounts = accounts
        self._billing = billing

    def execute(self, command: GitHubAppWebhookCommand) -> GitHubAppWebhookResult:
        action = str(command.payload.get("action") or "")
        if command.delivery_id and not self._deliveries.record_delivery(
            delivery_id=command.delivery_id, event=command.event, action=action or None
        ):
            return GitHubAppWebhookResult(status="duplicate", detail={"delivery_id": command.delivery_id})
        try:
            result = self._dispatch(command, action)
        except Exception as exc:
            if command.delivery_id:
                self._deliveries.finish_delivery(command.delivery_id, status="error", error=str(exc)[:500])
            raise
        if command.delivery_id:
            self._deliveries.finish_delivery(command.delivery_id, status=result.status)
        return result

    def _dispatch(self, command: GitHubAppWebhookCommand, action: str) -> GitHubAppWebhookResult:
        if command.event == "installation":
            return self._handle_installation(command.payload, action)
        if command.event == "installation_repositories":
            return self._handle_installation_repositories(command.payload)
        if command.event == "issues":
            return self._handle_issue(command.payload)
        if command.event == "pull_request":
            return self._handle_pull_request(command)
        return GitHubAppWebhookResult(status="ignored", detail={"event": command.event})

    def _handle_installation(self, payload: dict[str, Any], action: str) -> GitHubAppWebhookResult:
        installation = payload.get("installation") or {}
        installation_id = str(installation.get("id") or "")
        if not installation_id:
            return GitHubAppWebhookResult(status="ignored", detail={"reason": "missing installation id"})
        account = installation.get("account") or {}
        login = str(account.get("login") or "unknown")
        if action in {"created", "new_permissions_accepted", "unsuspend"}:
            self._installations.upsert_installation(
                installation_id=installation_id,
                account_login=login,
                account_type=str(account.get("type") or "User"),
                status="active",
            )
            for repo in payload.get("repositories") or []:
                if repo.get("full_name"):
                    self._installations.add_repository(
                        installation_id=installation_id,
                        full_name=str(repo["full_name"]),
                        github_repo_id=repo.get("id"),
                        private=bool(repo.get("private")),
                    )
        elif action == "deleted":
            self._installations.set_installation_status(installation_id, "deleted")
        elif action == "suspend":
            self._installations.set_installation_status(installation_id, "suspended")
        else:
            return GitHubAppWebhookResult(status="ignored", detail={"action": action})
        self._audit_log.record_event(
            actor=login,
            event=f"github_app.installation.{action}",
            target=f"installation:{installation_id}",
            metadata={"installation_id": installation_id},
        )
        return GitHubAppWebhookResult(status="processed", detail={"installation_id": installation_id})

    def _handle_installation_repositories(self, payload: dict[str, Any]) -> GitHubAppWebhookResult:
        installation = payload.get("installation") or {}
        installation_id = str(installation.get("id") or "")
        if not installation_id:
            return GitHubAppWebhookResult(status="ignored", detail={"reason": "missing installation id"})
        added = [repo for repo in payload.get("repositories_added") or [] if repo.get("full_name")]
        removed = [repo for repo in payload.get("repositories_removed") or [] if repo.get("full_name")]
        for repo in added:
            self._installations.add_repository(
                installation_id=installation_id,
                full_name=str(repo["full_name"]),
                github_repo_id=repo.get("id"),
                private=bool(repo.get("private")),
            )
        for repo in removed:
            self._installations.remove_repository(
                installation_id=installation_id, full_name=str(repo["full_name"])
            )
        self._audit_log.record_event(
            actor=str((installation.get("account") or {}).get("login") or "github"),
            event="github_app.repositories_updated",
            target=f"installation:{installation_id}",
            metadata={"added": len(added), "removed": len(removed)},
        )
        return GitHubAppWebhookResult(
            status="processed",
            detail={"installation_id": installation_id, "added": len(added), "removed": len(removed)},
        )

    def _handle_issue(self, payload: dict[str, Any]) -> GitHubAppWebhookResult:
        issue = issue_run_candidate(payload, trigger_label=self._settings.trigger_label)
        if issue is None:
            return GitHubAppWebhookResult(status="ignored", detail={"reason": "issue event not eligible"})
        installation_id = self._installations.installation_for_repository(issue.repository.full_name)
        payload_installation = str((payload.get("installation") or {}).get("id") or "") or None
        if installation_id is None and payload_installation:
            # Self-heal: the repo was not synced yet but the event proves the installation.
            account = (payload.get("repository") or {}).get("owner") or {}
            self._installations.upsert_installation(
                installation_id=payload_installation,
                account_login=str(account.get("login") or issue.repository.owner),
                account_type=str(account.get("type") or "User"),
                status="active",
            )
            self._installations.add_repository(
                installation_id=payload_installation,
                full_name=issue.repository.full_name,
                github_repo_id=(payload.get("repository") or {}).get("id"),
                private=bool((payload.get("repository") or {}).get("private")),
            )
            installation_id = payload_installation
        if installation_id is None:
            return GitHubAppWebhookResult(
                status="ignored", detail={"reason": "repository is not installed", "repo": issue.repository.full_name}
            )
        workspace_id = self._workspace_for_installation(installation_id)
        if self._billing is not None:
            decision = self._billing.check_run_allowed(workspace_id)
            if not decision.allowed:
                self._audit_log.record_event(
                    actor="github-app",
                    event="run.limited",
                    target=issue.url,
                    result="limited",
                    metadata={"reason": decision.reason, "workspace_id": workspace_id},
                )
                return GitHubAppWebhookResult(status="limited", detail={"reason": decision.reason})
        sender = str((payload.get("sender") or {}).get("login") or "github")
        result = self._queue_run.execute(
            QueueRunCommand(
                issue=issue,
                model=self._settings.default_model,
                max_iterations=self._settings.max_iterations,
                open_pr=self._settings.open_pr,
                requested_by=f"github-app:{sender}",
                installation_id=installation_id,
                workspace_id=workspace_id,
                max_attempts=self._settings.worker_max_attempts,
            )
        )
        if self._billing is not None:
            self._billing.record_run(workspace_id, result.run_id)
        return GitHubAppWebhookResult(
            status="queued",
            detail={"run_id": result.run_id, "branch": result.branch, "installation_id": installation_id},
        )

    def _workspace_for_installation(self, installation_id: str) -> int | None:
        if self._accounts is None:
            return None
        installation = self._installations.get_installation(installation_id) or {}
        workspace = self._accounts.workspace_for_login(installation.get("account_login"))
        workspace_id = (workspace or {}).get("id")
        return int(workspace_id) if workspace_id else None

    def _handle_pull_request(self, command: GitHubAppWebhookCommand) -> GitHubAppWebhookResult:
        if self._pr_analysis is None:
            return GitHubAppWebhookResult(status="ignored", detail={"reason": "pr analysis not configured"})
        job = self._pr_analysis.execute(
            EnqueuePullRequestAnalysisCommand(payload=command.payload, delivery_id=command.delivery_id)
        )
        if job is None:
            return GitHubAppWebhookResult(status="ignored", detail={"reason": "pr action not analyzed"})
        return GitHubAppWebhookResult(
            status="queued", detail={"repo": job.full_name, "pr_number": job.pr_number}
        )
