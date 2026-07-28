# Deployment

PatchPilot has two deployable surfaces:

- the dashboard/API server
- the worker path used by `agent run`
- the Kafka PR-analysis worker used by GitHub pull request webhooks

The current beta deployment runs API and workers as separate containers. The issue-fixing worker mounts the host Docker socket so repository setup and tests can run in Docker. The PR-analysis worker consumes Kafka jobs and talks to GitHub only.

## Local Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

Open:

```text
http://127.0.0.1:8080
```

## Cheapest Private Beta VPS

For a student-budget deployment, use one small VPS and Docker Compose with the VPS override:

```bash
cp .env.vps.example .env
./scripts/vps-deploy.sh
```

This runs Caddy, the API, issue worker, PR worker, Postgres, and Redpanda on one machine. Caddy terminates HTTPS with Let's Encrypt and only exposes ports 80/443. See `CHEAP_PRIVATE_BETA.md` for the full checklist.

Expected monthly cost:

| Item | Cost |
| --- | ---: |
| Small VPS | $5-$12 |
| Domain | $10-$15/year |
| HTTPS | $0 |
| Postgres + Redpanda | included on VPS |
| LLM API cap | $10-$25 |
| Total | roughly $15-$40/month |

## Required Environment

```bash
GITHUB_TOKEN=github_pat_...
GITHUB_WEBHOOK_SECRET=...
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=postgresql://patchpilot:patchpilot@postgres:5432/patchpilot
AGENT_LOGS_DIR=/data/logs
ARTIFACT_STORAGE_DIR=/data/artifacts
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=replace-with-a-long-random-password
DASHBOARD_SESSION_SECRET=replace-with-a-long-random-secret
DASHBOARD_SECURE_COOKIES=true
DASHBOARD_DEMO_DATA_ENABLED=false
GITHUB_OAUTH_CLIENT_ID=...
GITHUB_OAUTH_CLIENT_SECRET=...
GITHUB_OAUTH_CALLBACK_URL=https://app.example.com/auth/github/callback
GITHUB_OAUTH_MOCK_ENABLED=false
GITHUB_APP_INSTALL_URL=https://github.com/apps/YOUR_APP/installations/new
GITHUB_APP_ID=...
GITHUB_APP_INSTALLATION_ID=...
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
PATCHPILOT_WORKER_ID=worker-1
PATCHPILOT_WORKER_LEASE_SECONDS=900
PATCHPILOT_WORKER_MAX_ATTEMPTS=3
KAFKA_BOOTSTRAP_SERVERS=redpanda:9092
KAFKA_PR_ANALYSIS_TOPIC=pr-analysis-jobs
KAFKA_CONSUMER_GROUP=patchpilot-pr-workers
PR_ANALYSIS_STATUS_CONTEXT=patchpilot/pr-analysis
PATCHPILOT_PRODUCTION=true
```

Optional cost override:

```bash
AGENT_MODEL_PRICES_JSON='{"gpt-4.1":{"input":2.0,"output":8.0}}'
```

Prices are USD per 1M input/output tokens.

## Production Notes

- Run `agent doctor` and `agent migrate` during release setup. In production mode, preflight fails if PostgreSQL, auth, GitHub OAuth, GitHub App install URL, artifact storage, secure cookies, or demo-data settings are unsafe.
- Run the dashboard/API and `agent worker --limit 1 --loop --sleep-seconds 5` as separate processes so queued dashboard runs are actually executed.
- Run `agent pr-worker --limit 1 --loop --sleep-seconds 5` as a separate process for Kafka-backed PR analysis jobs.
- Queue workers claim jobs with database leases and attempt limits. Run more than one worker only after assigning unique `PATCHPILOT_WORKER_ID` values and isolating Docker execution.
- PR-analysis workers are Kafka consumer-group members and can scale horizontally without changing API replicas.
- Prefer GitHub App installation-token auth for deployed workers. `GITHUB_TOKEN` remains useful for local development, but production should set `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, and a private key path.
- Prefer isolated worker machines for untrusted repository execution.
- Use short-lived GitHub tokens with least privilege.
- Enable dashboard auth and still prefer GitHub SSO or an internal network boundary.
- Mounting `/var/run/docker.sock` is convenient for beta but powerful; use a dedicated worker pool before public multi-tenant use.
- Persist `/data/logs` and PostgreSQL backups.
- Persist `/data/artifacts` or mount a durable artifact store.
- Keep `--open-pr false` as the default and require explicit user approval for PR creation.
- Use `/health` for liveness checks and `/ready` for database-backed readiness checks.
- Dashboard-created runs are persisted as `queued`; the worker process claims and executes them.

## Release Checks

Use these checks before cutting a deployment:

```bash
agent doctor --check-docker-run
agent migrate
python -m pytest
python -m ruff check .
agent eval-synthetic --manifest evals/synthetic/python_bugs.yaml --output synthetic-eval-report.json
```

## Kubernetes

Local/minikube-compatible manifests live in `deploy/kubernetes`.

```bash
kubectl apply -k deploy/kubernetes
kubectl -n patchpilot get pods
kubectl -n patchpilot port-forward svc/patchpilot-api 8080:80
```

Scale PR workers independently:

```bash
kubectl -n patchpilot scale deployment/patchpilot-pr-worker --replicas=4
```

## Terraform

Terraform lives in `infra/terraform` and manages namespace, ConfigMap, Secret, and optionally applies workload manifests.

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

Do not commit `terraform.tfvars` with real secrets.

## GitHub Verification

After configuring OAuth/App values, verify the dashboard status:

```bash
python - <<'PY'
from urllib.request import urlopen
print(urlopen("http://127.0.0.1:8080/api/github/status").read().decode())
PY
```

With `GITHUB_TOKEN` set, verify a repository:

```text
http://127.0.0.1:8080/api/github/verify?repo=OWNER/REPO
```
