# GitHub App Setup

The PatchPilot GitHub App connects your repositories to PatchPilot so that runs
start automatically — no manual queueing. When an issue in an installed
repository is labeled with the trigger label (default: `patchpilot`), PatchPilot
receives a webhook, clones the repository, works on a fix inside a Docker
sandbox, and opens a draft pull request linked back to the issue.

Installing the app is **optional**. Everything also works by queueing runs
manually from the dashboard's Runs page.

## What the app does

- Listens for `issues`, `installation`, and `installation_repositories` webhook
  events from GitHub.
- Starts a run when an issue is labeled with the trigger label.
- Posts the resulting draft PR back to the triggering issue.
- Keeps the dashboard's installation and repository lists up to date.

## Required permissions

| Permission | Access | Why |
| --- | --- | --- |
| Contents | Read & write | Clone code and push fix branches |
| Issues | Read | Receive issue events and read issue bodies/comments |
| Pull requests | Read & write | Open draft PRs with the fix |
| Metadata | Read | List repositories the installation can access |

Subscribed events: `Issues`, `Installation`, `Installation repositories`,
`Pull request`.

## Environment variables

Set these on the PatchPilot server (`.env` or your deployment environment):

```bash
GITHUB_APP_ID=123456                       # from the app's settings page
GITHUB_APP_PRIVATE_KEY_PATH=/etc/patchpilot/app-key.pem
# or inline: GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----..."
GITHUB_APP_WEBHOOK_SECRET=<random secret, also set on the app's webhook config>
GITHUB_APP_INSTALL_URL=https://github.com/apps/patchpilot/installations/new
GITHUB_APP_TRIGGER_LABEL=patchpilot        # optional, defaults to "patchpilot"
```

The app's **Webhook URL** must point at your deployment:
`https://<your-host>/webhooks/github-app`. Its **Setup URL** (in the app's
settings, "Post installation") should be
`https://<your-host>/github-app-setup/callback` so users land back on the
dashboard after installing.

## Installing

1. Sign in to the dashboard and open **/github-app-setup** (also linked from the
   onboarding checklist and Settings → GitHub App).
2. Click **Install on GitHub** and pick the account and repositories to grant.
3. GitHub redirects you back to the setup page, which shows the installation
   with its repository count. Repository details arrive with the first webhook
   delivery, usually within seconds.

To uninstall or change repository access later, use **Manage or uninstall on
GitHub** — PatchPilot picks up the change from the `installation` webhook and
marks the installation deleted or suspended automatically.

## Troubleshooting

- **Installation never shows up:** the webhook likely isn't reaching your
  server. Check the app's "Advanced → Recent Deliveries" page on GitHub for
  errors, and confirm the Webhook URL and that `/webhooks/github-app` is
  reachable from the internet.
- **Deliveries fail with 401/403:** `GITHUB_APP_WEBHOOK_SECRET` doesn't match
  the secret configured on the app.
- **Labeled issues don't trigger runs:** confirm the label matches
  `GITHUB_APP_TRIGGER_LABEL`, the repository is part of the installation, and
  the app credentials (`GITHUB_APP_ID`, private key) are set so PatchPilot can
  act on the repository.
