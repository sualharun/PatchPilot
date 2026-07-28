# Cheap Private Beta Deployment

This is the lowest-cost setup that still looks and behaves like a real production system.

## Target Monthly Cost

| Item | Expected cost |
| --- | ---: |
| Small VPS, 2 GB RAM minimum | $5-$12 |
| Domain | $10-$15/year |
| HTTPS via Caddy/Let's Encrypt | $0 |
| Postgres container | included |
| Redpanda/Kafka container | included |
| Logs/backups on disk | included |
| OpenAI or Anthropic API cap | $10-$25 |
| Total | roughly $15-$40/month |

## VPS Requirements

- Ubuntu 22.04 or 24.04
- 2 GB RAM minimum, 4 GB preferred
- Docker and Docker Compose plugin
- Ports 80 and 443 open
- A domain A record pointing at the VPS

## One-Time Server Setup

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git ufw
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
```

Log out and back in so Docker group permissions apply.

## App Setup

```bash
git clone https://github.com/YOUR_USER/PatchPilot.git
cd PatchPilot/autonomous_engineering_agent
cp .env.vps.example .env
```

Edit `.env`:

- `PATCHPILOT_DOMAIN`
- `ACME_EMAIL`
- `GITHUB_WEBHOOK_SECRET`
- `GITHUB_OAUTH_CLIENT_ID`
- `GITHUB_OAUTH_CLIENT_SECRET`
- `GITHUB_OAUTH_CALLBACK_URL`
- `GITHUB_APP_INSTALL_URL`
- `GITHUB_APP_ID`
- `GITHUB_APP_INSTALLATION_ID`
- `GITHUB_APP_PRIVATE_KEY` or `GITHUB_APP_PRIVATE_KEY_PATH`
- one LLM key: `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`
- `DASHBOARD_PASSWORD`
- `DASHBOARD_SESSION_SECRET`

Use only one LLM provider at first.

## Deploy

```bash
./scripts/vps-deploy.sh
```

Open:

```text
https://YOUR_DOMAIN
```

## GitHub URLs

OAuth callback:

```text
https://YOUR_DOMAIN/auth/github/callback
```

Webhook URL:

```text
https://YOUR_DOMAIN/webhooks/github
```

## Validate

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml ps
docker compose -f docker-compose.yml -f docker-compose.vps.yml logs -f patchpilot
```

Check:

```text
https://YOUR_DOMAIN/health
https://YOUR_DOMAIN/ready
https://YOUR_DOMAIN/api/github/status
```

## Backups

Run manually:

```bash
./scripts/backup-postgres.sh
```

Optional daily cron:

```cron
15 3 * * * cd /home/ubuntu/PatchPilot/autonomous_engineering_agent && ./scripts/backup-postgres.sh
```

Copy backups off the VPS occasionally:

```bash
scp ubuntu@YOUR_DOMAIN:/home/ubuntu/PatchPilot/autonomous_engineering_agent/backups/*.gz ./backups/
```

## Cost Controls

- Set OpenAI/Anthropic hard billing caps.
- Keep `--open-pr false` for initial issue-fix tests.
- Use a cheaper model by default.
- Stop workers when you are not demoing expensive agent runs:

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml stop patchpilot-worker
```

The PR-analysis worker is cheaper than the full issue-fixing worker because it does not clone repos or run Dockerized tests.
