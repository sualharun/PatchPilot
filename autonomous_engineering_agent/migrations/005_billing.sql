-- Stripe-ready billing: customers, subscriptions, usage ledger, workspace limits.

CREATE TABLE IF NOT EXISTS stripe_customers (
  id INTEGER PRIMARY KEY,
  workspace_id INTEGER NOT NULL UNIQUE,
  stripe_customer_id TEXT NOT NULL UNIQUE,
  email TEXT,
  created_at TEXT NOT NULL
);

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
);

CREATE TABLE IF NOT EXISTS usage_ledger (
  id INTEGER PRIMARY KEY,
  workspace_id INTEGER,
  run_id INTEGER,
  kind TEXT NOT NULL DEFAULT 'run',
  amount INTEGER NOT NULL DEFAULT 1,
  cost_usd REAL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_limits (
  id INTEGER PRIMARY KEY,
  workspace_id INTEGER NOT NULL UNIQUE,
  plan TEXT NOT NULL DEFAULT 'free',
  monthly_run_cap INTEGER,
  monthly_spend_cap_usd REAL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_ledger_workspace ON usage_ledger(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_subscriptions_workspace ON subscriptions(workspace_id);
