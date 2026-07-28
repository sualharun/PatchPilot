-- Real user records from GitHub OAuth and workspace-aware runs.

ALTER TABLE users ADD COLUMN github_user_id TEXT;
ALTER TABLE users ADD COLUMN login TEXT;
ALTER TABLE users ADD COLUMN avatar_url TEXT;

ALTER TABLE runs ADD COLUMN workspace_id INTEGER;
