-- GitHub App production flow: installations, installed repositories, webhook deliveries.
-- SQLite-compatible; RunStore rewrites INTEGER PRIMARY KEY to SERIAL for Postgres.

CREATE TABLE IF NOT EXISTS github_app_installations (
  id INTEGER PRIMARY KEY,
  installation_id TEXT NOT NULL UNIQUE,
  account_login TEXT NOT NULL,
  account_type TEXT NOT NULL DEFAULT 'User',
  status TEXT NOT NULL DEFAULT 'active',
  installed_at TEXT NOT NULL,
  updated_at TEXT
);

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
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id INTEGER PRIMARY KEY,
  delivery_id TEXT NOT NULL UNIQUE,
  event TEXT NOT NULL,
  action TEXT,
  received_at TEXT NOT NULL,
  processed_at TEXT,
  status TEXT NOT NULL DEFAULT 'received',
  error TEXT
);

ALTER TABLE runs ADD COLUMN installation_id TEXT;

CREATE INDEX IF NOT EXISTS idx_app_repos_full_name ON github_app_repositories(full_name, status);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_received ON webhook_deliveries(received_at);
