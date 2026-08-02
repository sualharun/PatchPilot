# PatchPilot Deployment Runbook

This is the practical path from local demo to a private beta that can support real users.

## 0. Release Gate

Run this before deploying:

```bash
make release-check
```

Run this before exposing a real VPS/domain:

```bash
make production-check
```

`production-check` must fail if the app is still using SQLite, demo data, insecure cookies, missing OAuth/App config, missing webhook secret, or non-persistent artifact storage.
When running before Docker Compose is started, use the deploy script's predeploy mode, which skips only the database reachability check because `postgres` is a Compose-internal hostname.

## 1. Local Real-Data Mode

Goal: run API, worker, Postgres, and Redpanda locally with demo data disabled.

Steps:

```bash
cp .env.vps.example .env
```

Edit `.env` for local testing:

```text
PATCHPILOT_DOMAIN=127.0.0.1
GITHUB_WEBHOOK_SECRET=<random-local-secret>
OPENAI_API_KEY=<optional at first>
ANTHROPIC_API_KEY=<optional at first>
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=<long password>
DASHBOARD_SESSION_SECRET=<long random secret>
DASHBOARD_SECURE_COOKIES=false
DASHBOARD_DEMO_DATA_ENABLED=false
PATCHPILOT_PRODUCTION=false
```

Then run:

```bash
docker compose up --build
```

Optional local seed data:

```bash
agent db-seed --profile local-dev
```

Verify:

```text
http://127.0.0.1:8080/health
http://127.0.0.1:8080/ready
http://127.0.0.1:8080/runs
```

Expected result: dashboard works with real database rows or empty states, not demo sample rows.

## 2. GitHub OAuth

Goal: users can sign in with GitHub.

In GitHub, create an OAuth App:

```text
Homepage URL: https://YOUR_DOMAIN
Authorization callback URL: https://YOUR_DOMAIN/auth/github/callback
```

Set:

```text
GITHUB_OAUTH_CLIENT_ID=...
GITHUB_OAUTH_CLIENT_SECRET=...
GITHUB_OAUTH_CALLBACK_URL=https://YOUR_DOMAIN/auth/github/callback
GITHUB_OAUTH_MOCK_ENABLED=false
```

Verify:

```text
https://YOUR_DOMAIN/login
https://YOUR_DOMAIN/api/github/status
```

Expected result: login redirects to GitHub and returns to the dashboard with a signed session.

## 3. GitHub App And Webhook

Goal: GitHub can send PR events to PatchPilot and PatchPilot can access installed repositories.

Create a GitHub App using `github-app.manifest.example.json` as the permission reference.

Set:

```text
GITHUB_WEBHOOK_SECRET=...
GITHUB_APP_INSTALL_URL=https://github.com/apps/YOUR_APP/installations/new
GITHUB_APP_ID=...
GITHUB_APP_INSTALLATION_ID=...
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
```

Webhook URL:

```text
https://YOUR_DOMAIN/webhooks/github
```

Subscribe to pull request events first.

Expected result: GitHub webhook delivery returns `202`, and Redpanda receives a `pr-analysis-jobs` message.

## 4. LLM Provider

Goal: real agent reasoning and PR analysis.

Use one provider first:

```text
OPENAI_API_KEY=...
```

or:

```text
ANTHROPIC_API_KEY=...
```

Set a hard monthly billing cap in the provider dashboard before inviting users.

Expected result: queued jobs produce tool calls, patches, summaries, or PR review output.

## 5. VPS Deployment

Goal: cheapest private beta on one server.

Server requirements:

- Ubuntu 22.04 or 24.04
- 2 GB RAM minimum, 4 GB preferred
- Docker and Docker Compose plugin
- Ports 80 and 443 open
- A domain A record pointing to the VPS

Deploy:

```bash
git clone https://github.com/YOUR_USER/PatchPilot.git
cd PatchPilot/autonomous_engineering_agent
cp .env.vps.example .env
```

Fill every production value in `.env`, then run once (as root, or with `sudo`) to create the
isolated network sandboxed package installs run on and block it from reaching the cloud
metadata endpoint:

```bash
sudo ./scripts/setup-sandbox-network.sh
```

This is idempotent -- safe to re-run. Re-run it after any host reboot unless you've set up
`iptables` rule persistence (the script prints options when it runs). Without this step, the
sandbox worker will still create the network on first use and non-install commands still get no
network, but the cloud metadata endpoint will not be blocked for install commands until the
script has been run.

Then run:

```bash
./scripts/vps-deploy.sh
```

The deploy script runs production config preflight before starting containers, then the migration container and `/ready` endpoint validate database reachability after Postgres is up.

Verify:

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml ps
docker compose -f docker-compose.yml -f docker-compose.vps.yml logs -f patchpilot
```

Browser checks:

```text
https://YOUR_DOMAIN/health
https://YOUR_DOMAIN/ready
https://YOUR_DOMAIN/login
```

## 6. Scheduled, Verified Backups

Goal: Postgres backups happen automatically, and a broken backup is caught before you need it.

`scripts/backup-postgres.sh` and `scripts/restore-postgres.sh` exist but nothing runs the backup
automatically, and nobody proves the backups are actually restorable. Install two systemd timers
for this (unit files are checked in under `deploy/systemd/`):

```bash
sudo mkdir -p /opt/patchpilot/backups
sudo cp deploy/systemd/patchpilot-backup.service deploy/systemd/patchpilot-backup.timer \
        deploy/systemd/patchpilot-backup-verify.service deploy/systemd/patchpilot-backup-verify.timer \
        /etc/systemd/system/
```

Edit `WorkingDirectory=` and `ExecStart=` in the two `.service` files if the repo is not cloned
at `/opt/patchpilot/app` (adjust to match wherever you ran `git clone` in step 5). Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now patchpilot-backup.timer
sudo systemctl enable --now patchpilot-backup-verify.timer
```

What each timer does:

- `patchpilot-backup.timer` (daily, 03:15 UTC) runs `scripts/backup-postgres.sh`, which dumps
  Postgres to `$BACKUP_DIR` and prunes backups older than `BACKUP_RETENTION_DAYS` (default 14).
- `patchpilot-backup-verify.timer` (weekly, Sunday 04:00 UTC) runs
  `scripts/verify-postgres-backup.sh`, which restores the most recent backup into a scratch
  database (`patchpilot_restore_check`, dropped afterward -- it never touches the live
  `patchpilot` database), asserts the `runs` table exists and is queryable, and exits non-zero
  with a message on stdout if anything about the restore fails. A corrupted or empty backup will
  not sit there silently until the day you need it.

Verify:

```bash
sudo systemctl list-timers | grep patchpilot
sudo systemctl start patchpilot-backup.service        # run once immediately to sanity-check
sudo systemctl start patchpilot-backup-verify.service  # run once immediately to sanity-check
journalctl -u patchpilot-backup.service -u patchpilot-backup-verify.service --since today
```

Expected result: both services show `status=0/SUCCESS` and a backup file appears in
`/opt/patchpilot/backups`.

## 7. First Beta Test

Use a small Python repository you control.

Test sequence:

1. Install the GitHub App on the test repo.
2. Open a small PR or issue with an obvious failing test.
3. Confirm GitHub webhook delivery is accepted.
4. Confirm the PR worker processes the Kafka job.
5. Confirm the dashboard shows the run/job.
6. Confirm logs and artifacts are persisted.
7. Confirm no secrets appear in logs.
8. Confirm provider spend is visible and under cap.

## 8. Error Visibility (Sentry)

Goal: find out about unhandled errors from a dashboard, not by grepping log files.

Today the only visibility into the running app is `/health`, `/ready`, and local log files. Sign
up for a free Sentry account (https://sentry.io -- the free developer tier is enough for a small
beta) or another Sentry-API-compatible alternative (e.g. self-hosted GlitchTip), create a
project, and copy its DSN. Set it in `.env`:

```text
SENTRY_DSN=https://examplePublicKey@o0.ingest.sentry.io/0
```

Leaving `SENTRY_DSN` unset is a supported choice -- error reporting is a true no-op without it,
nothing else changes. When set, unhandled exceptions from the dashboard (FastAPI), the issue
worker (`agent worker`), and the PR worker (`agent pr-worker`) are reported to Sentry. Exception
text is passed through the same secret-redaction used for logs before it leaves the process.

Verify: trigger an error (e.g. temporarily point `DATABASE_URL` somewhere invalid and hit
`/ready`, or stop Postgres while a worker is running), then confirm the event shows up in the
Sentry project within a minute or two. Revert the induced failure afterward.

## 9. Invite Users

Before inviting users:

- Keep beta private and invite-only.
- Use one workspace.
- Cap LLM spend.
- Confirm the backup timers from step 6 are enabled and the last verify run succeeded.
- Review logs daily for the first week (and set up Sentry per step 8 so you don't have to rely on log-grepping alone).
- Do not support untrusted public repos until worker isolation is stronger.
- Read `/privacy` and `/terms` on the deployed dashboard -- they're a first draft, not reviewed by a lawyer. Have them checked before relying on them with real users.

## Deployment Definition Of Done

PatchPilot is private-beta deployable when:

- `make production-check` passes.
- HTTPS domain is live.
- GitHub OAuth login works.
- GitHub App install works.
- Webhook deliveries return `202`.
- API, issue worker, PR worker, Postgres, and Redpanda are healthy.
- Demo data is disabled.
- A real GitHub PR/issue run completes end to end.
- Logs, artifacts, and DB backups are persistent.
- The daily backup timer and weekly backup-verify timer are enabled and their last runs succeeded.
- Provider billing caps are configured.
- `SENTRY_DSN` is set (or you've consciously decided to skip it) so unhandled errors aren't only visible via log-grepping.
- `/privacy` and `/terms` are live on the dashboard.
