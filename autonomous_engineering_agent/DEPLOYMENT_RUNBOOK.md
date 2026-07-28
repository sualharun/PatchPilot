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

Fill every production value in `.env`, then run:

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

## 6. First Beta Test

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

## 7. Invite Users

Before inviting users:

- Keep beta private and invite-only.
- Use one workspace.
- Cap LLM spend.
- Back up Postgres daily.
- Review logs daily for the first week.
- Do not support untrusted public repos until worker isolation is stronger.

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
- Provider billing caps are configured.
