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
);

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
);

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
);

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
);

CREATE TABLE IF NOT EXISTS run_patches (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL,
  iteration_id INTEGER,
  file_path TEXT NOT NULL,
  patch TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
  FOREIGN KEY(iteration_id) REFERENCES run_iterations(id) ON DELETE SET NULL
);

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
);

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
);

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
);

CREATE TABLE IF NOT EXISTS workspace_settings (
  id INTEGER PRIMARY KEY,
  workspace_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id, key),
  FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_issues_repository_state ON issues(repository_id, state);
CREATE INDEX IF NOT EXISTS idx_pull_requests_repository_state ON pull_requests(repository_id, state);
CREATE INDEX IF NOT EXISTS idx_run_iterations_run ON run_iterations(run_id, iteration_number);
CREATE INDEX IF NOT EXISTS idx_run_commands_run ON run_commands(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_run_patches_run ON run_patches(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_run_test_results_run ON run_test_results(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status_available ON jobs(status, available_at);
CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(status, leased_until);
CREATE INDEX IF NOT EXISTS idx_usage_events_workspace ON usage_events(workspace_id, created_at);
