# Production Checklist

Use this before pointing real users at a deployed PatchPilot instance. It assumes the
DigitalOcean VPS + Docker Compose setup described in `DEPLOYMENT.md` /
`CHEAP_PRIVATE_BETA.md`.

## 1. Environment

- [ ] `PATCHPILOT_PRODUCTION=true`
- [ ] `DATABASE_URL` points at PostgreSQL (not SQLite)
- [ ] `DASHBOARD_AUTH_ENABLED=true`
- [ ] `DASHBOARD_SESSION_SECRET` set to a long random value (`openssl rand -hex 32`)
- [ ] `DASHBOARD_SECURE_COOKIES=true`
- [ ] `DASHBOARD_DEMO_DATA_ENABLED=false`
- [ ] `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` / `GITHUB_OAUTH_CALLBACK_URL` set, `GITHUB_OAUTH_MOCK_ENABLED=false`
- [ ] `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` (or `GITHUB_APP_PRIVATE_KEY_PATH`) set
- [ ] `GITHUB_APP_WEBHOOK_SECRET` set and matches the GitHub App's webhook secret
- [ ] `GITHUB_APP_INSTALL_URL` points at the app's public install page
- [ ] At least one of `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` set
- [ ] `PATCHPILOT_PUBLIC_BASE_URL` set to the real HTTPS domain (used for Stripe redirect URLs)
- [ ] `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` / `STRIPE_PRICE_ID_STARTER` / `STRIPE_PRICE_ID_PRO` set if billing is enabled for this launch (optional for a free-only beta)

Confirm with:

```bash
agent doctor
```

`agent doctor` calls `load_config()`, which raises and refuses to boot if any of the
checks above are missing — see `validate_production_config()` in
`agent/infrastructure/config/settings.py`.

## 2. Database

- [ ] `agent migrate` has been run against the production database (applies
      `migrations/001` through `migrations/006` in order)
- [ ] Confirm new tables exist: `github_app_installations`, `github_app_repositories`,
      `webhook_deliveries`, `stripe_customers`, `subscriptions`, `usage_ledger`,
      `workspace_limits`
- [ ] Database backups are configured (VPS snapshot or `pg_dump` cron)

## 3. GitHub App

- [ ] App created with permissions: `metadata: read`, `issues: read`, `contents: write`,
      `pull_requests: write`
- [ ] Webhook URL set to `https://YOUR_DOMAIN/webhooks/github-app`
- [ ] Webhook events subscribed: `installation`, `installation_repositories`, `issues`,
      `pull_request`
- [ ] App installed on the beta test repository: `sualharun/patchpilot-test-repo`
- [ ] `GITHUB_APP_TRIGGER_LABEL` documented for beta testers if you want `labeled` events
      gated by a label (default `patchpilot`)

## 4. Stripe (only if billing is live for this launch)

- [ ] Products/prices created in Stripe for Starter and Pro
- [ ] Webhook endpoint added in Stripe pointing at `https://YOUR_DOMAIN/webhooks/stripe`,
      subscribed to `checkout.session.completed` and `customer.subscription.*`
- [ ] Test a checkout in Stripe test mode end-to-end before flipping to live keys

## 5. Security

- [ ] Dashboard is only reachable over HTTPS (Caddy/TLS terminates in front of FastAPI)
- [ ] `agent_runs.sqlite3` / other local dev artifacts are not present on the VPS
- [ ] Secrets are only in `.env` (gitignored) or the deployment's secret store, never in
      git history or logs
- [ ] Spot-check logs after a test run for accidental secret leakage (tokens, private
      keys, Stripe keys should all render as `[REDACTED]`)

## 6. Smoke Test

Run through `docs/BETA_TEST_PLAN.md` end-to-end against `patchpilot-test-repo` before
inviting real users:

- [ ] Manual issue queueing still works (`/api/runs`)
- [ ] GitHub App issue-opened event enqueues a run automatically
- [ ] Worker completes the run and (if `open_pr` is set) opens a draft PR
- [ ] Dashboard shows the run, correct workspace, correct account name/avatar
- [ ] Duplicate webhook delivery does not double-queue
- [ ] Login rate limiting returns 429 after repeated bad attempts (`curl` loop is fine)
- [ ] `/health` and `/ready` return 200

## 7. Budget Guardrails

- [ ] Free plan run cap (5 runs/month) is in effect for any workspace without an active
      subscription
- [ ] Provider API key has a spend cap or billing alert configured on the OpenAI/Anthropic
      side, independent of PatchPilot's own tracking
- [ ] `docker-compose.vps.yml` sandbox CPU/memory/timeout limits reviewed for the VPS size
      in use
