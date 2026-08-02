# Autonomous Engineering Agent

An autonomous engineering agent for Python GitHub repositories. It accepts a GitHub issue, clones the repository, runs setup and tests inside Docker, uses provider tool calls to inspect and edit code, retries on failures, commits to a new branch, and can optionally open a draft pull request.

This is intentionally scoped: Python repositories only, existing test suites assumed, CLI first, and safety over breadth.

## Setup

Requirements:

- Python 3.11+
- Docker
- Git
- A GitHub token for private repositories or PR creation
- Either an OpenAI or Anthropic API key for real code edits

Install locally:

```bash
cd autonomous_engineering_agent
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
cp .env.example .env
agent doctor
agent migrate
```

Edit `.env`:

```bash
GITHUB_TOKEN=github_pat_...
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=sqlite:///agent_runs.sqlite3
```

## Usage

```bash
agent run --issue https://github.com/OWNER/REPO/issues/123 --model gpt-4.1 --max-iterations 5 --open-pr false
```

Short forms are also supported:

```bash
agent run --issue OWNER/REPO#123 --model claude-3-5-sonnet-latest
agent run --issue "OWNER/REPO 123" --model gpt-4.1
```

The agent prints final status, change summary, tests run, pass/fail, branch name, PR URL when created, and the JSON log path.

## Preflight Check

Before a real run, check your local setup:

```bash
agent doctor
```

For a stronger Docker check that runs the configured Python image:

```bash
agent doctor --check-docker-run
```

## Dashboard

Serve a local run dashboard:

```bash
agent dashboard --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080` to inspect recent runs from the configured database.

Dashboard routes:

- `/`: polished public hero page
- `/login`: signed dashboard login screen
- `/overview`: workspace overview with run submission form
- `/runs`: operational run dashboard
- `/runs/{id}`: run detail, tool calls, tests, and patch preview
- `/demo`: workflow video placeholder page
- `/health` and `/ready`: deployment health checks

Dashboard auth is off by default for local development. Enable it before exposing the dashboard:

```bash
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD='use-a-long-random-password'
DASHBOARD_SESSION_SECRET='use-a-long-random-secret'
DASHBOARD_SECURE_COOKIES=true
DASHBOARD_DEMO_DATA_ENABLED=false
GITHUB_OAUTH_CLIENT_ID=...
GITHUB_OAUTH_CLIENT_SECRET=...
GITHUB_OAUTH_CALLBACK_URL=https://app.example.com/auth/github/callback
GITHUB_APP_INSTALL_URL=https://github.com/apps/YOUR_APP/installations/new
ARTIFACT_STORAGE_DIR=/data/artifacts
PATCHPILOT_PRODUCTION=true
```

Run the database migration command before serving a deployed dashboard:

```bash
agent migrate
```

The dashboard can queue a run from `/overview`. Run a worker process alongside the web server to execute queued jobs:

```bash
agent worker --limit 1 --loop --sleep-seconds 5
```

Seed local development data from the database layer instead of hardcoded UI fixtures:

```bash
agent db-seed --profile local-dev
```

For production, `PATCHPILOT_PRODUCTION=true` disables silent PostgreSQL-to-SQLite fallback and `agent doctor` fails closed when secure cookies, auth, PostgreSQL, or demo-data settings are unsafe.

## Architecture

PatchPilot uses hexagonal architecture with onion-style dependency rules:

```text
agent/domain
  Pure entities, values, invariants, transitions, and business policies.

agent/application
  Command handlers, query handlers, and workflow orchestration.

agent/application/ports
  Explicit inbound use-case contracts and outbound dependency contracts.

agent/infrastructure
  SQL, GitHub, LLM, Kafka, Docker, Git, configuration, and artifact adapters.

agent/interfaces
  CLI, FastAPI dashboard, workers, and webhook inbound adapters.

agent/bootstrap.py
  Composition root that wires ports to concrete adapters.
```

The principal flows are wired through the application layer:

```text
FastAPI /api/runs
  -> QueueRunCommand / QueueRunHandler
  -> RunRepository + AuditLog ports
  -> SQL adapters

Dashboard route
  -> DashboardQueryService
  -> read-side ports
  -> SQL/artifact adapters

Run worker
  -> ProcessQueuedRunsHandler
  -> ExecuteEngineeringRunHandler
  -> GitHub / Git / Docker / LLM / artifact ports
```

Architecture tests prevent the domain from importing outward, prevent the
application layer from importing infrastructure, and prevent HTTP routes from
querying `RunStore` directly. See `ARCHITECTURE.md` for the full adapter map and
feature-development rules.

The database uses normalized production tables alongside the compatibility `runs` table:

- `users`, `workspaces`, `memberships`
- `repositories`, `issues`, `pull_requests`
- `runs`, `run_iterations`, `run_commands`, `run_patches`, `run_test_results`
- `jobs`, `artifacts`, `audit_events`, `provider_keys`, `usage_events`, `workspace_settings`

Foreign keys and indexes are created for workspace ownership, repository relationships, run artifacts, job leases, audit history, and cost tracking.

## Async PR Review Pipeline

PatchPilot also supports an asynchronous GitHub pull request review path:

```text
GitHub PR webhook
  -> FastAPI /webhooks/github
  -> HMAC signature validation
  -> Kafka topic: pr-analysis-jobs
  -> patchpilot-pr-worker consumer group
  -> GitHub API fetches PR/files/diff metadata
  -> PR analysis summary + commit status back to GitHub
```

The webhook payload is intentionally small and reproducible:

- repository owner/name
- pull request number
- head commit SHA
- GitHub delivery id
- action
- installation id when present
- sender login when present

The API service does not review the PR synchronously. It validates the GitHub webhook and publishes a Kafka job. PR worker replicas can scale independently from API replicas.

Required environment:

```bash
GITHUB_WEBHOOK_SECRET=...
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
KAFKA_PR_ANALYSIS_TOPIC=pr-analysis-jobs
KAFKA_CONSUMER_GROUP=patchpilot-pr-workers
PR_ANALYSIS_STATUS_CONTEXT=patchpilot/pr-analysis
```

Run the PR worker locally:

```bash
agent pr-worker --limit 1 --loop --sleep-seconds 5
```

## GitHub OAuth And App Setup

Create a GitHub OAuth App with callback URL:

```text
https://YOUR_DOMAIN/auth/github/callback
```

Set:

```bash
GITHUB_OAUTH_CLIENT_ID=...
GITHUB_OAUTH_CLIENT_SECRET=...
GITHUB_OAUTH_CALLBACK_URL=https://YOUR_DOMAIN/auth/github/callback
```

For CI/local callback tests without live GitHub credentials, set `GITHUB_OAUTH_MOCK_ENABLED=true` outside production.

Create a GitHub App using `github-app.manifest.example.json` as the permission reference. Required permissions are:

- `metadata: read`
- `issues: read`
- `contents: write`
- `pull_requests: write`

Set:

```bash
GITHUB_APP_INSTALL_URL=https://github.com/apps/YOUR_APP/installations/new
GITHUB_APP_ID=...
GITHUB_APP_INSTALLATION_ID=...
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
```

PatchPilot prefers GitHub App installation-token auth for workers when those values are present, and falls back to `GITHUB_TOKEN` for local development.

## GitHub App Production Flow

Beyond the manual `/api/runs` issue queueing, PatchPilot can run autonomously from a
GitHub App installed on a user's repositories:

```text
GitHub App webhook (installation, installation_repositories, issues, pull_request)
  -> POST /webhooks/github-app
  -> HMAC signature validation (GITHUB_APP_WEBHOOK_SECRET)
  -> deduplicated by X-GitHub-Delivery (webhook_deliveries table)
  -> installation/repository state persisted (github_app_installations, github_app_repositories)
  -> issue opened/reopened/labeled on an installed repo -> QueueRunHandler
  -> pull_request events -> existing Kafka PR-analysis pipeline
```

- Manual issue queueing (`/api/runs`, `agent run`) keeps working unchanged.
- `labeled` events only trigger a run when the added label matches `GITHUB_APP_TRIGGER_LABEL` (default `patchpilot`); `opened`/`reopened` always trigger.
- The worker resolves each queued run's `installation_id` to a short-lived GitHub App installation token instead of a broad personal access token, falling back to `GITHUB_TOKEN`/`GITHUB_APP_INSTALLATION_ID` for runs without one (e.g. manually queued).
- Every delivery is recorded with `delivery_id`, `event`, `action`, `received_at`, `processed_at`, `status`, and `error`; replays of the same delivery id are a no-op (HTTP 200 `duplicate`) instead of double-queueing.

Install the GitHub App on the test repository used for this beta:

```text
https://github.com/sualharun/patchpilot-test-repo
```

## Accounts, Workspaces, and Billing

GitHub OAuth login (`/auth/github/start`) creates or updates a real `users` row
(`github_user_id`, `login`, `name`, `email`, `avatar_url`) and a personal workspace with
an owner membership. Dashboard account info (`/settings`, `/account`) is read from that
row instead of a static placeholder, and `/logout` clears the session. Run listings,
stats, tests, and billing are filtered to the signed-in user's workspace; runs queued
before a workspace existed (`workspace_id IS NULL`) stay visible everywhere.

Billing is Stripe-ready but works without Stripe configured (everything defaults to the
free plan):

```bash
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_ID_STARTER=price_...
STRIPE_PRICE_ID_PRO=price_...
PATCHPILOT_PUBLIC_BASE_URL=https://app.example.com
```

- `POST /billing/checkout` (form field `plan=starter|pro`) creates a Stripe Checkout session.
- `POST /billing/portal` opens the Stripe customer billing portal.
- `POST /webhooks/stripe` verifies the `Stripe-Signature` header and updates `subscriptions`/`workspace_limits` on `checkout.session.completed` and `customer.subscription.*` events.
- Plan caps: free = 5 runs/month, starter = 50 runs/month, pro = 250 runs/month + $100/month spend cap. A subscription only counts while `trialing` or `active`; `past_due`/`canceled` fall back to free.
- Limits are enforced before queueing, both from the dashboard (`POST /api/runs`) and from GitHub App issue events.

## Worker Hardening

- `claim_next_queued_run` claims a row with a conditional `UPDATE ... WHERE status = 'queued'`, so multiple worker replicas polling the same database cannot double-claim a run.
- Each run has its own `max_attempts` (defaults to `PATCHPILOT_WORKER_MAX_ATTEMPTS`); transient failures are requeued with linear backoff (`PATCHPILOT_WORKER_RETRY_BACKOFF_SECONDS * attempts`) instead of being immediately reclaimable.
- Runs that exhaust `max_attempts` move to a terminal `dead_letter` status instead of looping.
- Redacted failure messages are stored in `last_error`/`summary` and shown on the run detail page.

## Production Startup Checks

Set `PATCHPILOT_PRODUCTION=true` to fail boot (`agent doctor`, `agent dashboard`, `agent worker`) instead of running with an unsafe configuration. Boot fails if any of the following are missing or unsafe:

- `DATABASE_URL` pointing at PostgreSQL (no silent SQLite fallback)
- `DASHBOARD_AUTH_ENABLED=true`
- `DASHBOARD_SESSION_SECRET`
- `DASHBOARD_SECURE_COOKIES=true`
- `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` / `GITHUB_OAUTH_CALLBACK_URL` (and `GITHUB_OAUTH_MOCK_ENABLED` must be `false`)
- `GITHUB_APP_ID` and a private key (`GITHUB_APP_PRIVATE_KEY` or `GITHUB_APP_PRIVATE_KEY_PATH`)
- `GITHUB_APP_WEBHOOK_SECRET`
- `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`

`POST /login` and `POST /api/runs` are rate limited per client IP (in-process, fixed
window; see `agent/infrastructure/security/rate_limit.py`). Secret redaction
(`agent/infrastructure/security/secrets.py`) masks GitHub tokens, private key blocks,
Stripe secret/webhook keys, and generic `key=value`-shaped secrets before anything is
persisted to `last_error`/logs.

See `docs/PRODUCTION_CHECKLIST.md` before a real-user launch and `docs/BETA_TEST_PLAN.md`
for a smoke-test script against real repositories.

## Evaluations

Run an eval manifest:

```bash
agent eval --manifest evals/example.yaml --output eval-report.json
```

The eval report records solve rate, runtime, per-task status, and exit codes. See `EVALS.md` for benchmark design.

Run the curated synthetic Python benchmark:

```bash
agent eval-synthetic --manifest evals/synthetic/python_bugs.yaml --output synthetic-eval-report.json
```

Synthetic eval reports are stored in the configured database and shown on the Agents dashboard. The included corpus has 20 Python issue fixtures; add real GitHub-style fixtures before claiming public solve rates.

## `agent.yaml`

Repositories can override setup and test commands:

```yaml
install_commands:
  - python -m pip install --upgrade pip
  - python -m pip install -e .
test_commands:
  - python -m pytest
```

If no commands are configured, the agent detects common Python project files: `pyproject.toml`, `requirements.txt`, `setup.py`, `Pipfile`, and `poetry.lock`.

A `sandbox:` block (image, network, allowed commands, resource limits) is deliberately **not**
honored from a target repository's own `agent.yaml` -- that repository is untrusted input, and
letting it loosen its own sandbox would defeat the sandbox. Sandbox settings always come from the
deployment's own configuration (environment variables / the operator's `agent.yaml`, if any). See
`SECURITY.md`.

## Safety Model

- Target repository commands run inside Docker.
- Docker runs with CPU, memory, and runtime limits.
- Commands are checked against a versioned allowlist (`agent/infrastructure/sandbox/command_policy.json`), not honored from the target repository.
- Sandboxed commands have no network access by default; only dependency installation gets network, on a dedicated network isolated from other project containers and the cloud metadata endpoint. See `SECURITY.md`.
- Shell control operators such as `;`, `&&`, pipes, redirects, and command substitution are rejected.
- API keys and tokens are redacted from logs and command output.
- The agent does not push branches or open PRs unless `--open-pr true`.
- Run history and full logs are persisted for reproducibility.
- Every completed run writes a replayable JSON artifact with commands, tool calls, patches, test results, and metrics.
- LLM token usage and estimated cost are tracked when provider responses include usage data.

## Persistence

SQLite is the default:

```bash
DATABASE_URL=sqlite:///agent_runs.sqlite3
```

PostgreSQL is supported with:

```bash
DATABASE_URL=postgresql://user:password@localhost:5432/agent_runs
```

Production mode requires PostgreSQL and fails startup/preflight if PostgreSQL cannot be reached. Local development can still use SQLite.

Each run records issue URL, repo, branch, model, start/end time, iterations, commands, patches, test results, final status, and PR URL.
Audit events record dashboard login/logout, queued runs, and agent/tool activity suitable for compliance review.

Cost estimates use default model price estimates or `AGENT_MODEL_PRICES_JSON` overrides:

```bash
AGENT_MODEL_PRICES_JSON='{"gpt-4.1":{"input":2.0,"output":8.0}}'
```

Prices are USD per 1M input/output tokens.

## Status Values

- `queued`: dashboard accepted a run request for worker execution
- `running`: worker has started the run
- `success`: tests passed and changes were committed locally
- `pr_opened`: tests passed, branch pushed, and draft PR opened
- `failed_tests`: retries exhausted with failing tests
- `setup_failed`: install/setup command failed
- `agent_error`: unexpected agent failure
- `dead_letter`: worker exhausted `max_attempts` retrying a transient failure

## Known Limitations

- Python repositories only.
- Provider-native tool calling is implemented for OpenAI and Anthropic adapters, with JSON fallback retained for local tests and stub mode.
- Docker image selection is simple and may need project-specific overrides.
- Some projects require network access for tests; tune `sandbox.network` deliberately.
- Linked PR discovery is best effort.
- Dashboard auth supports signed HTTP-only sessions and GitHub OAuth. Production preflight requires the GitHub OAuth client and GitHub App install URL to be configured.
- The dashboard reads runs, repositories, provider key hints, account context, GitHub connection state, billing/cost totals, tests, and eval reports from persistence.
- The worker claims queued runs with a database lease, attempt counter, and retry/dead-letter behavior.

## Roadmap

- Richer repository indexing and targeted file selection.
- Per-command network policy for install versus test.
- Better Python environment caching.
- Auth-backed multi-user workspaces.
- Human approval gates for high-risk edits.
- Model comparison reports and larger public benchmark corpus.

## Development

```bash
python3 -m pytest
python3 -m ruff check .
make check
```

See `ARCHITECTURE.md`, `SECURITY.md`, `EVALS.md`, `REQUIREMENTS_COVERAGE.md`, and `CONTRIBUTING.md` for the production-oriented design notes.

## Deployment

Local beta deployment:

```bash
docker compose up --build
```

Docker Compose starts PostgreSQL, Redpanda, the dashboard/API, the existing issue-fixing worker, and the Kafka PR-analysis worker.

Cheap private beta deployment:

```bash
cp .env.vps.example .env
./scripts/vps-deploy.sh
```

This one-VPS setup runs Caddy HTTPS, FastAPI, workers, Postgres, and Redpanda for roughly `$15-$40/month` depending on VPS size and LLM billing caps. See `CHEAP_PRIVATE_BETA.md`.

Kubernetes manifests live in `deploy/kubernetes`:

```bash
kubectl apply -k deploy/kubernetes
kubectl -n patchpilot scale deployment/patchpilot-pr-worker --replicas=3
```

Terraform for local Kubernetes/minikube-compatible deployment lives in `infra/terraform`:

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform apply
```

Set `apply_workloads = true` if you want Terraform to apply the Kubernetes workload manifests with `kubectl`.

See `DEPLOYMENT.md` for production notes and Docker socket safety guidance.

## Resume Highlights

- Built a Kafka-backed asynchronous PR review pipeline that decouples GitHub webhook ingestion from pull request analysis workers.
- Deployed API and worker services with Kubernetes manifests, independent worker scaling, health probes, ConfigMaps, and Secret placeholders.
- Added Terraform-managed local Kubernetes infrastructure configuration with parameterized images, replicas, region/project naming, and sensitive variables.
- Preserved the existing GitHub issue agent while adding production-style async infrastructure around it.
