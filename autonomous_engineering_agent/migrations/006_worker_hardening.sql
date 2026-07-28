-- Worker hardening: per-run retry cap so replicas can safely lease and retry queued runs.

ALTER TABLE runs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
