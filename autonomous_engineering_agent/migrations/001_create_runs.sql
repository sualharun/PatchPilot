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
);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  event TEXT NOT NULL,
  target TEXT NOT NULL,
  result TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  workspace_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
  id INTEGER PRIMARY KEY,
  workspace_id INTEGER,
  full_name TEXT NOT NULL UNIQUE,
  default_branch TEXT,
  python_setup TEXT,
  test_command TEXT,
  config_status TEXT NOT NULL DEFAULT 'detected',
  last_run_id INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_keys (
  id INTEGER PRIMARY KEY,
  workspace_id INTEGER,
  provider TEXT NOT NULL,
  key_hint TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS github_connections (
  id INTEGER PRIMARY KEY,
  user_id INTEGER,
  login TEXT NOT NULL UNIQUE,
  token_hint TEXT NOT NULL,
  scopes TEXT,
  installation_id TEXT,
  connected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  size_bytes INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_reports (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  manifest_path TEXT NOT NULL,
  task_count INTEGER NOT NULL,
  passed_count INTEGER NOT NULL,
  pass_rate REAL NOT NULL,
  report_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs(repo);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_queue ON runs(status, leased_until, attempts);
CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at);
CREATE INDEX IF NOT EXISTS idx_repositories_workspace ON repositories(workspace_id);
CREATE INDEX IF NOT EXISTS idx_eval_reports_created_at ON eval_reports(created_at);
