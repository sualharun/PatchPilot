from __future__ import annotations

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

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
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

    def claim_next_queued_run(self, *, worker_id: str, lease_seconds: int, max_attempts: int) -> dict[str, Any] | None:
        now = _now()
        lease_until = _now(offset_seconds=lease_seconds)
        cursor = self._execute(
            """
            SELECT * FROM runs
            WHERE status = ?
              AND attempts < ?
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
        self.update_run(run_id, status="running", worker_id=worker_id, leased_until=lease_until, attempts=attempts)
        data["status"] = "running"
        data["worker_id"] = worker_id
        data["leased_until"] = lease_until
        data["attempts"] = attempts
        return data

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

    def get_account_context(self) -> dict[str, Any]:
        workspace = self._fetch_one("SELECT * FROM workspaces ORDER BY id ASC LIMIT 1", []) or {
            "id": 0,
            "name": "PatchPilot",
            "slug": "default",
        }
        user = self._fetch_one("SELECT * FROM users ORDER BY id ASC LIMIT 1", []) or {
            "id": 0,
            "email": "local@patchpilot.dev",
            "name": "PatchPilot",
        }
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
              created_at TEXT NOT NULL
            )
            """,
            [],
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

    def _ensure_columns(self, columns: dict[str, str]) -> None:
        existing = self._existing_columns("runs")
        for name, definition in columns.items():
            if name not in existing:
                self._execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}", [])

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
