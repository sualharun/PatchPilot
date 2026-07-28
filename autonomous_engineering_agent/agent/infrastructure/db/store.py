from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent.domain.enums import RunStatus

RUN_STATUSES = {status.value for status in RunStatus}


@dataclass(slots=True)
class RunRecord:
    issue_url: str
    repo: str
    branch: str
    model: str
    status: str = "agent_error"
    iterations: int = 0
    commands: list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    patches: list[str] | None = None
    test_results: list[dict[str, Any]] | None = None
    token_usage: dict[str, Any] | None = None
    estimated_cost_usd: float | None = None
    pr_url: str | None = None
    summary: str | None = None
    logs_path: str | None = None
    max_iterations: int = 5
    open_pr: bool = False
    installation_id: str | None = None
    workspace_id: int | None = None
    max_attempts: int = 3
    queued_at: str | None = None
    leased_until: str | None = None
    worker_id: str | None = None
    attempts: int = 0
    last_error: str | None = None
    started_at: str = ""
    ended_at: str | None = None


class RunStore:
    def __init__(self, database_url: str, *, allow_sqlite_fallback: bool = True) -> None:
        self.database_url = database_url
        self.kind = "postgres" if database_url.startswith(("postgresql://", "postgres://")) else "sqlite"
        try:
            self._conn: Any = self._connect()
        except Exception:
            if self.kind != "postgres" or not allow_sqlite_fallback:
                raise
            self.kind = "sqlite"
            self.database_url = "sqlite:///agent_runs.sqlite3"
            self._conn = self._connect()
        self.ensure_schema()

    def start_run(self, record: RunRecord) -> int:
        record.started_at = record.started_at or _now()
        if record.status == "queued" and not record.queued_at:
            record.queued_at = record.started_at
        payload = _payload(record)
        return self._insert(payload)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if "status" in fields and fields["status"] not in RUN_STATUSES:
            raise ValueError(f"Invalid run status: {fields['status']}")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [_json_value(value) for value in fields.values()]
        values.append(run_id)
        self._execute(f"UPDATE runs SET {assignments} WHERE id = ?", values)

    def finish_run(self, run_id: int, status: str, **fields: Any) -> None:
        self.update_run(run_id, status=status, ended_at=_now(), **fields)

    def get_run(self, run_id: int) -> dict[str, Any]:
        cursor = self._execute("SELECT * FROM runs WHERE id = ?", [run_id])
        row = cursor.fetchone()
        if row is None:
            raise KeyError(run_id)
        columns = [description[0] for description in cursor.description]
        data = dict(zip(columns, row, strict=True))
        return _decode_run(data)

    def list_runs(self, limit: int = 50, workspace_id: int | None = None) -> list[dict[str, Any]]:
        if workspace_id is not None:
            # Legacy runs queued before workspaces have NULL and stay visible.
            cursor = self._execute(
                "SELECT * FROM runs WHERE workspace_id = ? OR workspace_id IS NULL ORDER BY id DESC LIMIT ?",
                [workspace_id, limit],
            )
        else:
            cursor = self._execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", [limit])
        columns = [description[0] for description in cursor.description]
        rows = []
        for row in cursor.fetchall():
            data = dict(zip(columns, row, strict=True))
            rows.append(_decode_run(data))
        return rows

    def list_runs_by_status(self, status: str, limit: int = 10) -> list[dict[str, Any]]:
        cursor = self._execute("SELECT * FROM runs WHERE status = ? ORDER BY id ASC LIMIT ?", [status, limit])
        columns = [description[0] for description in cursor.description]
        rows = []
        for row in cursor.fetchall():
            data = dict(zip(columns, row, strict=True))
            rows.append(_decode_run(data))
        return rows

    def claim_next_queued_run(
        self, *, worker_id: str, lease_seconds: int, max_attempts: int, claim_attempts: int = 5
    ) -> dict[str, Any] | None:
        """Atomically claim one queued run, safe when multiple worker replicas poll concurrently.

        A candidate row is selected, then claimed with a conditional UPDATE guarded by
        ``status = 'queued'``. If a concurrent worker already claimed it, the UPDATE affects
        zero rows and this retries against the next candidate instead of double-processing.
        """
        now = _now()
        lease_until = _now(offset_seconds=lease_seconds)
        for _ in range(max(1, claim_attempts)):
            cursor = self._execute(
                """
                SELECT * FROM runs
                WHERE status = ?
                  AND attempts < COALESCE(max_attempts, ?)
                  AND (leased_until IS NULL OR leased_until < ?)
                ORDER BY id ASC
                LIMIT 1
                """,
                ["queued", max_attempts, now],
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [description[0] for description in cursor.description]
            data = _decode_run(dict(zip(columns, row, strict=True)))
            run_id = int(data["id"])
            attempts = int(data.get("attempts") or 0) + 1
            started_at = data.get("started_at") or now
            claim_cursor = self._execute(
                "UPDATE runs SET status = ?, worker_id = ?, leased_until = ?, attempts = ?, started_at = ? "
                "WHERE id = ? AND status = ?",
                ["running", worker_id, lease_until, attempts, started_at, run_id, "queued"],
            )
            if (claim_cursor.rowcount or 0) < 1:
                continue  # lost the race to another replica; try the next candidate
            data["status"] = "running"
            data["worker_id"] = worker_id
            data["leased_until"] = lease_until
            data["attempts"] = attempts
            data["started_at"] = started_at
            return data
        return None

    def requeue_for_retry(self, run_id: int, *, backoff_seconds: int, error: str) -> None:
        self.update_run(
            run_id,
            status="queued",
            worker_id=None,
            leased_until=_now(offset_seconds=backoff_seconds),
            last_error=error,
            summary=error,
        )

    def mark_dead_letter(self, run_id: int, *, error: str) -> None:
        self.finish_run(run_id, "dead_letter", worker_id=None, leased_until=None, last_error=error, summary=error)

    def add_audit_event(
        self,
        *,
        actor: str,
        event: str,
        target: str,
        result: str = "success",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        payload = {
            "created_at": _now(),
            "actor": actor,
            "event": event,
            "target": target,
            "result": result,
            "metadata": metadata or {},
        }
        fields = list(payload)
        placeholders = ", ".join("?" for _ in fields)
        sql = f"INSERT INTO audit_events ({', '.join(fields)}) VALUES ({placeholders})"
        if self.kind == "postgres":
            sql += " RETURNING id"
        cursor = self._execute(sql, [_json_value(payload[field]) for field in fields])
        if self.kind == "postgres":
            return int(cursor.fetchone()[0])
        return int(cursor.lastrowid)

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        cursor = self._execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", [limit])
        columns = [description[0] for description in cursor.description]
        rows = []
        for row in cursor.fetchall():
            data = dict(zip(columns, row, strict=True))
            if isinstance(data.get("metadata"), str):
                data["metadata"] = json.loads(data["metadata"] or "{}")
            rows.append(data)
        return rows

    def seed_defaults(self, *, username: str, workspace_name: str = "PatchPilot") -> None:
        user = self.get_or_create_user(email=f"{username}@local.patchpilot", name=username)
        workspace = self.get_or_create_workspace(name=workspace_name, slug="default")
        self.ensure_membership(int(user["id"]), int(workspace["id"]), role="owner")

    def get_or_create_user(self, *, email: str, name: str) -> dict[str, Any]:
        existing = self._fetch_one("SELECT * FROM users WHERE email = ?", [email])
        if existing:
            return existing
        user_id = self._insert_generic("users", {"email": email, "name": name, "created_at": _now()})
        return self._fetch_one("SELECT * FROM users WHERE id = ?", [user_id]) or {}

    def upsert_user_from_github(
        self,
        *,
        github_user_id: str,
        login: str,
        name: str,
        email: str,
        avatar_url: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a real user record from a GitHub OAuth profile."""
        existing = self._fetch_one(
            "SELECT * FROM users WHERE github_user_id = ?", [github_user_id]
        ) or self._fetch_one("SELECT * FROM users WHERE email = ?", [email])
        if existing:
            self._execute(
                "UPDATE users SET github_user_id = ?, login = ?, name = ?, email = ?, avatar_url = ? WHERE id = ?",
                [github_user_id, login, name, email, avatar_url, int(existing["id"])],
            )
            user_id = int(existing["id"])
        else:
            user_id = self._insert_generic(
                "users",
                {
                    "email": email,
                    "name": name,
                    "github_user_id": github_user_id,
                    "login": login,
                    "avatar_url": avatar_url,
                    "created_at": _now(),
                },
            )
        workspace = self.get_or_create_workspace(name=name or login, slug=login.lower())
        self.ensure_membership(user_id, int(workspace["id"]), role="owner")
        return self._fetch_one("SELECT * FROM users WHERE id = ?", [user_id]) or {}

    def get_user_by_login(self, login: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM users WHERE login = ?", [login])

    def workspace_for_login(self, login: str | None) -> dict[str, Any] | None:
        """The workspace of the logged-in user; first workspace as fallback."""
        if login:
            row = self._fetch_one(
                """
                SELECT w.* FROM workspaces w
                JOIN memberships m ON m.workspace_id = w.id
                JOIN users u ON u.id = m.user_id
                WHERE u.login = ? ORDER BY m.id ASC LIMIT 1
                """,
                [login],
            )
            if row:
                return row
        return self._fetch_one("SELECT * FROM workspaces ORDER BY id ASC LIMIT 1", [])

    def get_or_create_workspace(self, *, name: str, slug: str) -> dict[str, Any]:
        existing = self._fetch_one("SELECT * FROM workspaces WHERE slug = ?", [slug])
        if existing:
            return existing
        workspace_id = self._insert_generic("workspaces", {"name": name, "slug": slug, "created_at": _now()})
        return self._fetch_one("SELECT * FROM workspaces WHERE id = ?", [workspace_id]) or {}

    def ensure_membership(self, user_id: int, workspace_id: int, *, role: str) -> None:
        existing = self._fetch_one(
            "SELECT * FROM memberships WHERE user_id = ? AND workspace_id = ?",
            [user_id, workspace_id],
        )
        if existing:
            return
        self._insert_generic(
            "memberships",
            {"user_id": user_id, "workspace_id": workspace_id, "role": role, "created_at": _now()},
        )

    def get_account_context(self, login: str | None = None) -> dict[str, Any]:
        user = (self.get_user_by_login(login) if login else None) or self._fetch_one(
            "SELECT * FROM users ORDER BY id ASC LIMIT 1", []
        ) or {
            "id": 0,
            "email": "local@patchpilot.dev",
            "name": "PatchPilot",
        }
        workspace = self.workspace_for_login(login or user.get("login")) or {
            "id": 0,
            "name": "PatchPilot",
            "slug": "default",
        }
        github = None
        if user.get("login"):
            github = self._fetch_one("SELECT * FROM github_connections WHERE login = ?", [user["login"]])
        if github is None:
            github = self._fetch_one("SELECT * FROM github_connections ORDER BY id DESC LIMIT 1", [])
        return {"workspace": workspace, "user": user, "github": github}

    def upsert_repository(self, *, full_name: str, workspace_id: int | None = None, **fields: Any) -> int:
        existing = self._fetch_one("SELECT * FROM repositories WHERE full_name = ?", [full_name])
        payload = {
            "workspace_id": workspace_id,
            "full_name": full_name,
            "default_branch": fields.get("default_branch"),
            "python_setup": fields.get("python_setup"),
            "test_command": fields.get("test_command"),
            "config_status": fields.get("config_status", "detected"),
            "last_run_id": fields.get("last_run_id"),
            "created_at": _now(),
        }
        if existing:
            self._execute(
                """
                UPDATE repositories
                SET workspace_id = ?, default_branch = ?, python_setup = ?, test_command = ?,
                    config_status = ?, last_run_id = ?
                WHERE full_name = ?
                """,
                [
                    payload["workspace_id"],
                    payload["default_branch"],
                    payload["python_setup"],
                    payload["test_command"],
                    payload["config_status"],
                    payload["last_run_id"],
                    full_name,
                ],
            )
            return int(existing["id"])
        return self._insert_generic("repositories", payload)

    def list_repositories(self, limit: int = 100) -> list[dict[str, Any]]:
        cursor = self._execute("SELECT * FROM repositories ORDER BY full_name ASC LIMIT ?", [limit])
        columns = [description[0] for description in cursor.description]
        repositories = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        seen = {repo["full_name"] for repo in repositories}
        for run in self.list_runs(limit=limit):
            full_name = run.get("repo")
            if full_name and full_name not in seen:
                repositories.append(
                    {
                        "id": None,
                        "workspace_id": None,
                        "full_name": full_name,
                        "default_branch": "main",
                        "python_setup": "detected from run",
                        "test_command": _last_command(run),
                        "config_status": "run-derived",
                        "last_run_id": run.get("id"),
                        "created_at": run.get("started_at"),
                    }
                )
                seen.add(full_name)
        return repositories[:limit]

    def upsert_provider_key(self, *, workspace_id: int | None, provider: str, key_hint: str) -> int:
        existing = self._fetch_one(
            "SELECT * FROM provider_keys WHERE provider = ? AND (workspace_id = ? OR workspace_id IS NULL)",
            [provider, workspace_id],
        )
        if existing:
            self._execute(
                "UPDATE provider_keys SET key_hint = ? WHERE id = ?",
                [key_hint, int(existing["id"])],
            )
            return int(existing["id"])
        return self._insert_generic(
            "provider_keys",
            {"workspace_id": workspace_id, "provider": provider, "key_hint": key_hint, "created_at": _now()},
        )

    def list_provider_keys(self) -> list[dict[str, Any]]:
        cursor = self._execute("SELECT * FROM provider_keys ORDER BY provider ASC", [])
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def upsert_github_connection(
        self,
        *,
        user_id: int | None,
        login: str,
        token_hint: str,
        scopes: str,
        installation_id: str | None = None,
    ) -> int:
        existing = self._fetch_one("SELECT * FROM github_connections WHERE login = ?", [login])
        payload = {
            "user_id": user_id,
            "login": login,
            "token_hint": token_hint,
            "scopes": scopes,
            "installation_id": installation_id,
            "connected_at": _now(),
        }
        if existing:
            self._execute(
                """
                UPDATE github_connections
                SET user_id = ?, token_hint = ?, scopes = ?, installation_id = ?, connected_at = ?
                WHERE login = ?
                """,
                [user_id, token_hint, scopes, installation_id, payload["connected_at"], login],
            )
            return int(existing["id"])
        return self._insert_generic("github_connections", payload)

    def get_github_connection(self) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM github_connections ORDER BY id DESC LIMIT 1", [])

    def record_webhook_delivery(self, *, delivery_id: str, event: str, action: str | None) -> bool:
        """Insert a delivery row; returns False when delivery_id was already seen."""
        try:
            self._insert_generic(
                "webhook_deliveries",
                {
                    "delivery_id": delivery_id,
                    "event": event,
                    "action": action,
                    "received_at": _now(),
                    "status": "received",
                },
            )
            return True
        except Exception as exc:
            self._rollback()
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                return False
            raise

    def finish_webhook_delivery(self, delivery_id: str, *, status: str, error: str | None = None) -> None:
        self._execute(
            "UPDATE webhook_deliveries SET status = ?, error = ?, processed_at = ? WHERE delivery_id = ?",
            [status, error, _now(), delivery_id],
        )

    def list_webhook_deliveries(self, limit: int = 100) -> list[dict[str, Any]]:
        cursor = self._execute("SELECT * FROM webhook_deliveries ORDER BY id DESC LIMIT ?", [limit])
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def upsert_github_app_installation(
        self,
        *,
        installation_id: str,
        account_login: str,
        account_type: str = "User",
        status: str = "active",
    ) -> int:
        existing = self._fetch_one(
            "SELECT * FROM github_app_installations WHERE installation_id = ?", [installation_id]
        )
        if existing:
            self._execute(
                "UPDATE github_app_installations SET account_login = ?, account_type = ?, status = ?, updated_at = ? "
                "WHERE installation_id = ?",
                [account_login, account_type, status, _now(), installation_id],
            )
            return int(existing["id"])
        return self._insert_generic(
            "github_app_installations",
            {
                "installation_id": installation_id,
                "account_login": account_login,
                "account_type": account_type,
                "status": status,
                "installed_at": _now(),
            },
        )

    def set_github_app_installation_status(self, installation_id: str, status: str) -> None:
        self._execute(
            "UPDATE github_app_installations SET status = ?, updated_at = ? WHERE installation_id = ?",
            [status, _now(), installation_id],
        )
        if status == "deleted":
            self._execute(
                "UPDATE github_app_repositories SET status = 'removed', removed_at = ? WHERE installation_id = ?",
                [_now(), installation_id],
            )

    def get_github_app_installation(self, installation_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM github_app_installations WHERE installation_id = ?", [installation_id]
        )

    def list_github_app_installations(self, limit: int = 100) -> list[dict[str, Any]]:
        cursor = self._execute("SELECT * FROM github_app_installations ORDER BY id DESC LIMIT ?", [limit])
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def add_github_app_repository(
        self,
        *,
        installation_id: str,
        full_name: str,
        github_repo_id: int | None = None,
        private: bool = False,
    ) -> int:
        existing = self._fetch_one(
            "SELECT * FROM github_app_repositories WHERE installation_id = ? AND full_name = ?",
            [installation_id, full_name],
        )
        if existing:
            self._execute(
                "UPDATE github_app_repositories SET status = 'active', removed_at = NULL, "
                "github_repo_id = ?, private = ? WHERE id = ?",
                [github_repo_id, int(private), int(existing["id"])],
            )
            return int(existing["id"])
        return self._insert_generic(
            "github_app_repositories",
            {
                "installation_id": installation_id,
                "full_name": full_name,
                "github_repo_id": github_repo_id,
                "private": int(private),
                "status": "active",
                "added_at": _now(),
            },
        )

    def remove_github_app_repository(self, *, installation_id: str, full_name: str) -> None:
        self._execute(
            "UPDATE github_app_repositories SET status = 'removed', removed_at = ? "
            "WHERE installation_id = ? AND full_name = ?",
            [_now(), installation_id, full_name],
        )

    def installation_for_repository(self, full_name: str) -> str | None:
        row = self._fetch_one(
            """
            SELECT r.installation_id FROM github_app_repositories r
            JOIN github_app_installations i ON i.installation_id = r.installation_id
            WHERE r.full_name = ? AND r.status = 'active' AND i.status = 'active'
            ORDER BY r.id DESC LIMIT 1
            """,
            [full_name],
        )
        return str(row["installation_id"]) if row else None

    def list_github_app_repositories(self, installation_id: str | None = None) -> list[dict[str, Any]]:
        if installation_id:
            cursor = self._execute(
                "SELECT * FROM github_app_repositories WHERE installation_id = ? ORDER BY full_name ASC",
                [installation_id],
            )
        else:
            cursor = self._execute("SELECT * FROM github_app_repositories ORDER BY full_name ASC", [])
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def upsert_stripe_customer(self, *, workspace_id: int, stripe_customer_id: str, email: str | None) -> int:
        existing = self._fetch_one("SELECT * FROM stripe_customers WHERE workspace_id = ?", [workspace_id])
        if existing:
            self._execute(
                "UPDATE stripe_customers SET stripe_customer_id = ?, email = ? WHERE workspace_id = ?",
                [stripe_customer_id, email, workspace_id],
            )
            return int(existing["id"])
        return self._insert_generic(
            "stripe_customers",
            {
                "workspace_id": workspace_id,
                "stripe_customer_id": stripe_customer_id,
                "email": email,
                "created_at": _now(),
            },
        )

    def stripe_customer_for_workspace(self, workspace_id: int) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM stripe_customers WHERE workspace_id = ?", [workspace_id])

    def workspace_for_stripe_customer(self, stripe_customer_id: str) -> int | None:
        row = self._fetch_one(
            "SELECT workspace_id FROM stripe_customers WHERE stripe_customer_id = ?", [stripe_customer_id]
        )
        return int(row["workspace_id"]) if row else None

    def upsert_subscription(
        self,
        *,
        workspace_id: int,
        stripe_subscription_id: str,
        stripe_price_id: str | None,
        plan: str,
        status: str,
        current_period_end: str | None = None,
    ) -> int:
        existing = self._fetch_one(
            "SELECT * FROM subscriptions WHERE stripe_subscription_id = ?", [stripe_subscription_id]
        )
        if existing:
            self._execute(
                "UPDATE subscriptions SET workspace_id = ?, stripe_price_id = ?, plan = ?, status = ?, "
                "current_period_end = ?, updated_at = ? WHERE stripe_subscription_id = ?",
                [workspace_id, stripe_price_id, plan, status, current_period_end, _now(), stripe_subscription_id],
            )
            return int(existing["id"])
        return self._insert_generic(
            "subscriptions",
            {
                "workspace_id": workspace_id,
                "stripe_subscription_id": stripe_subscription_id,
                "stripe_price_id": stripe_price_id,
                "plan": plan,
                "status": status,
                "current_period_end": current_period_end,
                "created_at": _now(),
            },
        )

    def subscription_for_workspace(self, workspace_id: int) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM subscriptions WHERE workspace_id = ? ORDER BY id DESC LIMIT 1", [workspace_id]
        )

    def set_workspace_limits(
        self,
        *,
        workspace_id: int,
        plan: str,
        monthly_run_cap: int | None = None,
        monthly_spend_cap_usd: float | None = None,
    ) -> None:
        existing = self._fetch_one("SELECT * FROM workspace_limits WHERE workspace_id = ?", [workspace_id])
        if existing:
            self._execute(
                "UPDATE workspace_limits SET plan = ?, monthly_run_cap = ?, monthly_spend_cap_usd = ?, "
                "updated_at = ? WHERE workspace_id = ?",
                [plan, monthly_run_cap, monthly_spend_cap_usd, _now(), workspace_id],
            )
        else:
            self._insert_generic(
                "workspace_limits",
                {
                    "workspace_id": workspace_id,
                    "plan": plan,
                    "monthly_run_cap": monthly_run_cap,
                    "monthly_spend_cap_usd": monthly_spend_cap_usd,
                    "updated_at": _now(),
                },
            )

    def get_workspace_limits(self, workspace_id: int) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM workspace_limits WHERE workspace_id = ?", [workspace_id])

    def add_usage(
        self,
        *,
        workspace_id: int | None,
        run_id: int | None,
        kind: str = "run",
        amount: int = 1,
        cost_usd: float | None = None,
    ) -> int:
        return self._insert_generic(
            "usage_ledger",
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "kind": kind,
                "amount": amount,
                "cost_usd": cost_usd,
                "created_at": _now(),
            },
        )

    def usage_this_month(self, workspace_id: int | None) -> dict[str, Any]:
        month_start = datetime.now(UTC).strftime("%Y-%m-01")
        if workspace_id is None:
            row = self._execute(
                "SELECT COALESCE(SUM(amount), 0), COALESCE(SUM(cost_usd), 0) FROM usage_ledger "
                "WHERE kind = 'run' AND created_at >= ?",
                [month_start],
            ).fetchone()
        else:
            row = self._execute(
                "SELECT COALESCE(SUM(amount), 0), COALESCE(SUM(cost_usd), 0) FROM usage_ledger "
                "WHERE kind = 'run' AND created_at >= ? AND workspace_id = ?",
                [month_start, workspace_id],
            ).fetchone()
        return {"runs": int(row[0] or 0), "cost_usd": float(row[1] or 0.0)}

    def add_eval_report(
        self,
        *,
        name: str,
        manifest_path: str,
        task_count: int,
        passed_count: int,
        pass_rate: float,
        report_path: str,
    ) -> int:
        return self._insert_generic(
            "eval_reports",
            {
                "name": name,
                "manifest_path": manifest_path,
                "task_count": task_count,
                "passed_count": passed_count,
                "pass_rate": pass_rate,
                "report_path": report_path,
                "created_at": _now(),
            },
        )

    def list_eval_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        cursor = self._execute("SELECT * FROM eval_reports ORDER BY id DESC LIMIT ?", [limit])
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def ensure_schema(self) -> None:
        if self.kind == "sqlite":
            self._execute("PRAGMA foreign_keys = ON", [])
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY,
              issue_url TEXT NOT NULL,
              repo TEXT NOT NULL,
              branch TEXT NOT NULL,
              model TEXT NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              iterations INTEGER NOT NULL,
              commands TEXT NOT NULL,
              tool_calls TEXT NOT NULL DEFAULT '[]',
              patches TEXT NOT NULL,
              test_results TEXT NOT NULL,
              token_usage TEXT NOT NULL DEFAULT '{}',
              estimated_cost_usd REAL,
              status TEXT NOT NULL,
              pr_url TEXT,
              summary TEXT,
              logs_path TEXT,
              max_iterations INTEGER NOT NULL DEFAULT 5,
              open_pr INTEGER NOT NULL DEFAULT 0,
              queued_at TEXT,
              leased_until TEXT,
              worker_id TEXT,
              attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT
            )
            """,
            [],
        )
        self._ensure_columns(
            {
                "tool_calls": "TEXT NOT NULL DEFAULT '[]'",
                "token_usage": "TEXT NOT NULL DEFAULT '{}'",
                "estimated_cost_usd": "REAL",
                "summary": "TEXT",
                "logs_path": "TEXT",
                "max_iterations": "INTEGER NOT NULL DEFAULT 5",
                "open_pr": "INTEGER NOT NULL DEFAULT 0",
                "installation_id": "TEXT",
                "workspace_id": "INTEGER",
                "max_attempts": "INTEGER NOT NULL DEFAULT 3",
                "queued_at": "TEXT",
                "leased_until": "TEXT",
                "worker_id": "TEXT",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT",
            }
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
              id INTEGER PRIMARY KEY,
              created_at TEXT NOT NULL,
              actor TEXT NOT NULL,
              event TEXT NOT NULL,
              target TEXT NOT NULL,
              result TEXT NOT NULL,
              metadata TEXT NOT NULL DEFAULT '{}'
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY,
              email TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL,
              github_user_id TEXT,
              login TEXT,
              avatar_url TEXT,
              created_at TEXT NOT NULL
            )
            """,
            [],
        )
        self._ensure_columns(
            {"github_user_id": "TEXT", "login": "TEXT", "avatar_url": "TEXT"},
            table="users",
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              slug TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS memberships (
              id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL,
              workspace_id INTEGER NOT NULL,
              role TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(user_id, workspace_id),
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
              FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS repositories (
              id INTEGER PRIMARY KEY,
              workspace_id INTEGER,
              full_name TEXT NOT NULL UNIQUE,
              default_branch TEXT,
              python_setup TEXT,
              test_command TEXT,
              config_status TEXT NOT NULL DEFAULT 'detected',
              last_run_id INTEGER,
              created_at TEXT NOT NULL,
              FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE SET NULL,
              FOREIGN KEY(last_run_id) REFERENCES runs(id) ON DELETE SET NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS provider_keys (
              id INTEGER PRIMARY KEY,
              workspace_id INTEGER,
              provider TEXT NOT NULL,
              key_hint TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(workspace_id, provider),
              FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS github_connections (
              id INTEGER PRIMARY KEY,
              user_id INTEGER,
              login TEXT NOT NULL UNIQUE,
              token_hint TEXT NOT NULL,
              scopes TEXT,
              installation_id TEXT,
              connected_at TEXT NOT NULL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
              id INTEGER PRIMARY KEY,
              run_id INTEGER NOT NULL,
              kind TEXT NOT NULL,
              path TEXT NOT NULL,
              size_bytes INTEGER,
              created_at TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS eval_reports (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              manifest_path TEXT NOT NULL,
              task_count INTEGER NOT NULL,
              passed_count INTEGER NOT NULL,
              pass_rate REAL NOT NULL,
              report_path TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS issues (
              id INTEGER PRIMARY KEY,
              repository_id INTEGER NOT NULL,
              number INTEGER NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              body TEXT NOT NULL DEFAULT '',
              labels TEXT NOT NULL DEFAULT '[]',
              state TEXT NOT NULL DEFAULT 'open',
              github_url TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT,
              UNIQUE(repository_id, number),
              FOREIGN KEY(repository_id) REFERENCES repositories(id) ON DELETE CASCADE
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS pull_requests (
              id INTEGER PRIMARY KEY,
              repository_id INTEGER NOT NULL,
              run_id INTEGER,
              number INTEGER NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              state TEXT NOT NULL DEFAULT 'open',
              base_branch TEXT NOT NULL DEFAULT 'main',
              head_branch TEXT NOT NULL,
              github_url TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT,
              UNIQUE(repository_id, number),
              FOREIGN KEY(repository_id) REFERENCES repositories(id) ON DELETE CASCADE,
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE SET NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS run_iterations (
              id INTEGER PRIMARY KEY,
              run_id INTEGER NOT NULL,
              iteration_number INTEGER NOT NULL,
              status TEXT NOT NULL,
              plan TEXT,
              failure_analysis TEXT,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              UNIQUE(run_id, iteration_number),
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS run_commands (
              id INTEGER PRIMARY KEY,
              run_id INTEGER NOT NULL,
              iteration_id INTEGER,
              phase TEXT NOT NULL,
              command TEXT NOT NULL,
              exit_code INTEGER,
              runtime_seconds REAL,
              stdout_path TEXT,
              stderr_path TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
              FOREIGN KEY(iteration_id) REFERENCES run_iterations(id) ON DELETE SET NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS run_patches (
              id INTEGER PRIMARY KEY,
              run_id INTEGER NOT NULL,
              iteration_id INTEGER,
              file_path TEXT NOT NULL,
              patch TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
              FOREIGN KEY(iteration_id) REFERENCES run_iterations(id) ON DELETE SET NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS run_test_results (
              id INTEGER PRIMARY KEY,
              run_id INTEGER NOT NULL,
              iteration_id INTEGER,
              command TEXT NOT NULL,
              status TEXT NOT NULL,
              passed INTEGER NOT NULL DEFAULT 0,
              failed INTEGER NOT NULL DEFAULT 0,
              skipped INTEGER NOT NULL DEFAULT 0,
              runtime_seconds REAL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
              FOREIGN KEY(iteration_id) REFERENCES run_iterations(id) ON DELETE SET NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY,
              workspace_id INTEGER,
              run_id INTEGER,
              kind TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued',
              payload TEXT NOT NULL DEFAULT '{}',
              attempts INTEGER NOT NULL DEFAULT 0,
              max_attempts INTEGER NOT NULL DEFAULT 3,
              available_at TEXT NOT NULL,
              leased_until TEXT,
              worker_id TEXT,
              last_error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT,
              FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE SET NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
              id INTEGER PRIMARY KEY,
              workspace_id INTEGER,
              run_id INTEGER,
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              estimated_cost_usd REAL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE SET NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE SET NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS github_app_installations (
              id INTEGER PRIMARY KEY,
              installation_id TEXT NOT NULL UNIQUE,
              account_login TEXT NOT NULL,
              account_type TEXT NOT NULL DEFAULT 'User',
              status TEXT NOT NULL DEFAULT 'active',
              installed_at TEXT NOT NULL,
              updated_at TEXT
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS github_app_repositories (
              id INTEGER PRIMARY KEY,
              installation_id TEXT NOT NULL,
              full_name TEXT NOT NULL,
              github_repo_id INTEGER,
              private INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'active',
              added_at TEXT NOT NULL,
              removed_at TEXT,
              UNIQUE(installation_id, full_name)
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
              id INTEGER PRIMARY KEY,
              delivery_id TEXT NOT NULL UNIQUE,
              event TEXT NOT NULL,
              action TEXT,
              received_at TEXT NOT NULL,
              processed_at TEXT,
              status TEXT NOT NULL DEFAULT 'received',
              error TEXT
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_customers (
              id INTEGER PRIMARY KEY,
              workspace_id INTEGER NOT NULL UNIQUE,
              stripe_customer_id TEXT NOT NULL UNIQUE,
              email TEXT,
              created_at TEXT NOT NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
              id INTEGER PRIMARY KEY,
              workspace_id INTEGER NOT NULL,
              stripe_subscription_id TEXT NOT NULL UNIQUE,
              stripe_price_id TEXT,
              plan TEXT NOT NULL DEFAULT 'free',
              status TEXT NOT NULL DEFAULT 'trialing',
              current_period_end TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS usage_ledger (
              id INTEGER PRIMARY KEY,
              workspace_id INTEGER,
              run_id INTEGER,
              kind TEXT NOT NULL DEFAULT 'run',
              amount INTEGER NOT NULL DEFAULT 1,
              cost_usd REAL,
              created_at TEXT NOT NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_limits (
              id INTEGER PRIMARY KEY,
              workspace_id INTEGER NOT NULL UNIQUE,
              plan TEXT NOT NULL DEFAULT 'free',
              monthly_run_cap INTEGER,
              monthly_spend_cap_usd REAL,
              updated_at TEXT NOT NULL
            )
            """,
            [],
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_settings (
              id INTEGER PRIMARY KEY,
              workspace_id INTEGER NOT NULL,
              key TEXT NOT NULL,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(workspace_id, key),
              FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
            )
            """,
            [],
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)",
            "CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs(repo)",
            "CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at)",
            "CREATE INDEX IF NOT EXISTS idx_runs_queue ON runs(status, leased_until, attempts)",
            "CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_memberships_workspace ON memberships(workspace_id)",
            "CREATE INDEX IF NOT EXISTS idx_repositories_workspace ON repositories(workspace_id)",
            "CREATE INDEX IF NOT EXISTS idx_issues_repository_state ON issues(repository_id, state)",
            "CREATE INDEX IF NOT EXISTS idx_pull_requests_repository_state ON pull_requests(repository_id, state)",
            "CREATE INDEX IF NOT EXISTS idx_run_iterations_run ON run_iterations(run_id, iteration_number)",
            "CREATE INDEX IF NOT EXISTS idx_run_commands_run ON run_commands(run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_run_patches_run ON run_patches(run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_run_test_results_run ON run_test_results(run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_available ON jobs(status, available_at)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(status, leased_until)",
            "CREATE INDEX IF NOT EXISTS idx_usage_events_workspace ON usage_events(workspace_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_eval_reports_created_at ON eval_reports(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_app_repos_full_name ON github_app_repositories(full_name, status)",
            "CREATE INDEX IF NOT EXISTS idx_usage_ledger_workspace ON usage_ledger(workspace_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_workspace ON subscriptions(workspace_id)",
            "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_received ON webhook_deliveries(received_at)",
        ):
            self._execute(statement, [])

    def close(self) -> None:
        self._conn.close()

    def _connect(self) -> Any:
        if self.kind == "postgres":
            import psycopg

            return psycopg.connect(self.database_url)
        if self.database_url == "sqlite:///:memory:":
            return sqlite3.connect(":memory:", check_same_thread=False)
        if self.database_url.startswith("sqlite:////"):
            path = Path("/" + self.database_url.removeprefix("sqlite:////")).expanduser()
        elif self.database_url.startswith("sqlite:///"):
            path = Path(self.database_url.removeprefix("sqlite:///") or "agent_runs.sqlite3").expanduser()
        else:
            parsed = urlparse(self.database_url)
            path = Path(parsed.path or "agent_runs.sqlite3").expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(path, check_same_thread=False)

    def _insert(self, payload: dict[str, Any]) -> int:
        fields = list(payload)
        placeholders = ", ".join("?" for _ in fields)
        sql = f"INSERT INTO runs ({', '.join(fields)}) VALUES ({placeholders})"
        if self.kind == "postgres":
            sql += " RETURNING id"
        cursor = self._execute(sql, [_json_value(payload[field]) for field in fields])
        if self.kind == "postgres":
            return int(cursor.fetchone()[0])
        return int(cursor.lastrowid)

    def _insert_generic(self, table: str, payload: dict[str, Any]) -> int:
        fields = list(payload)
        placeholders = ", ".join("?" for _ in fields)
        sql = f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({placeholders})"
        if self.kind == "postgres":
            sql += " RETURNING id"
        cursor = self._execute(sql, [_json_value(payload[field]) for field in fields])
        if self.kind == "postgres":
            return int(cursor.fetchone()[0])
        return int(cursor.lastrowid)

    def _rollback(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.rollback()

    def _fetch_one(self, sql: str, values: list[Any]) -> dict[str, Any] | None:
        cursor = self._execute(sql, values)
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [description[0] for description in cursor.description]
        return dict(zip(columns, row, strict=True))

    def _execute(self, sql: str, values: list[Any]) -> Any:
        if self.kind == "postgres":
            sql = sql.replace("?", "%s").replace("id INTEGER PRIMARY KEY", "id SERIAL PRIMARY KEY")
        cursor = self._conn.cursor()
        cursor.execute(sql, values)
        self._conn.commit()
        return cursor

    def _ensure_columns(self, columns: dict[str, str], table: str = "runs") -> None:
        existing = self._existing_columns(table)
        for name, definition in columns.items():
            if name not in existing:
                self._execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}", [])

    def _existing_columns(self, table: str) -> set[str]:
        if self.kind == "postgres":
            cursor = self._execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [table],
            )
            return {row[0] for row in cursor.fetchall()}
        cursor = self._execute(f"PRAGMA table_info({table})", [])
        return {row[1] for row in cursor.fetchall()}


def _payload(record: RunRecord) -> dict[str, Any]:
    data = asdict(record)
    data["commands"] = data["commands"] or []
    data["tool_calls"] = data["tool_calls"] or []
    data["patches"] = data["patches"] or []
    data["test_results"] = data["test_results"] or []
    data["token_usage"] = data["token_usage"] or {}
    return data


def _decode_run(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("commands", "tool_calls", "patches", "test_results", "token_usage"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key] or ("{}" if key == "token_usage" else "[]"))
    if "open_pr" in data:
        data["open_pr"] = bool(data["open_pr"])
    return data


def _last_command(run: dict[str, Any]) -> str | None:
    commands = run.get("commands") or run.get("test_results") or []
    if not commands:
        return None
    command = commands[-1]
    if isinstance(command, dict):
        return str(command.get("command") or command.get("args") or command.get("phase") or "")
    return str(command)


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, bool):
        return int(value)
    return value


def _now(*, offset_seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat()
