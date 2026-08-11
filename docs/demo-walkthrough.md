# PatchPilot: 3-Minute Demo Walkthrough

A recorded walkthrough that shows PatchPilot taking a real GitHub issue and shipping a
pull request that fixes it, end to end, on live infrastructure.

- **Live app:** https://patchpilot.159.223.182.144.sslip.io
- **Demo target repo:** https://github.com/sualharun/patchpilot-demo
- **Demo issue:** https://github.com/sualharun/patchpilot-demo/issues/1

The target repo is a small storefront pricing module with an off-by-one bug: the bulk
discount is documented as applying to orders of "10 or more" but only fires at 11. Two
of its four tests fail because of it. The fix is one character. That matters for a demo
— it is instantly legible to a viewer, and it reliably completes in one agent iteration.

---

## One-time setup

**Done.** The GitHub App has been granted access to `patchpilot-demo`, and the full run
has been verified end to end on production: 60 seconds from queue to open pull request,
one file changed, all four tests passing.

If you ever point the demo at a different repo, you must grant the app access to it first
at https://github.com/settings/installations/149688471 — otherwise the agent clones and
patches fine but fails to push, and the run dead-letters with a 403.

---

## Before every take

```bash
gh auth status                          # make sure the CLI is authenticated
./reset-demo.sh                         # from a clone of patchpilot-demo
```

`reset-demo.sh` closes any PR the agent opened, deletes its branches, restores the bug
in `pricing.py`, and reopens the issue. It is idempotent — run it as often as you like.

Then open these four tabs in order, so you never search for a URL on camera:

1. `https://github.com/sualharun/patchpilot-demo/issues/1` — the issue
2. `https://patchpilot.159.223.182.144.sslip.io/overview` — signed in already
3. `https://patchpilot.159.223.182.144.sslip.io/runs` — run history
4. `https://github.com/sualharun/patchpilot-demo/pulls` — where the PR will land

A terminal in a clone of `patchpilot-demo` with `pytest -q` ready to run.

---

## The script

Total target: **2:45**. Timings are cumulative.

### 0:00–0:25 — The problem

*Screen:* terminal in the demo repo, then the GitHub issue.

```bash
pytest -q
```

> "This is a small pricing module for a storefront. Two of its four tests fail. The bulk
> discount is supposed to apply at ten units or more, but it only kicks in at eleven —
> a classic off-by-one at a threshold. There's a GitHub issue describing it."

Switch to the issue tab. Scroll once so the expected-vs-actual block is visible.

**Do not** open `pricing.py`. Let the fix be a reveal later.

### 0:25–0:45 — The product

*Screen:* the PatchPilot dashboard at `/overview`.

> "PatchPilot is an autonomous engineering agent I built. You give it an issue; it clones
> the repo, plans a fix, edits code, runs the test suite inside a Docker sandbox, and
> opens a pull request. This is running on a live server, not localhost."

Let the overview metrics be visible — runs, success rate, cost tracked.

### 0:45–1:05 — Queue the run

*Screen:* the *Start Agent Run* form on `/overview`.

- Paste the issue URL
- Model: `gpt-4o-mini`
- Max iterations: `3`
- Open PR: **on**
- Submit

> "I'll point it at that issue, pick a cheap model, and allow it to open a pull request.
> Nothing else — no branch name, no instructions about what to change."

You land on the run detail page automatically.

### 1:05–1:55 — Watch it work

*Screen:* the run detail page. Refresh once or twice.

Narrate over the wait rather than sitting in silence:

> "It's cloning into a Docker container that has no network access while tests run — so
> a malicious repository can't call out. It reads the issue, looks at the failing tests
> to understand intent, proposes a patch, and re-runs the suite to check itself. Every
> command, its exit code, and the token cost are recorded."

As the panels fill in, point at:

- **Commands** — the actual shell commands and exit codes
- **Tests** — `2 failed` becoming `4 passed`
- **Patch** — the one-line diff

> "There's the fix: greater-than becomes greater-than-or-equal. One character. And the
> two tests that were failing now pass, without breaking the two that already worked."

### 1:55–2:25 — The pull request

*Screen:* the PR on GitHub.

> "It pushed a branch and opened a draft PR. This is the same thing a junior engineer
> would hand you — except it costs about a fifth of a cent and took under a minute."

Show the diff, then the run's cost figure back in the dashboard.

### 2:25–2:45 — What's underneath

*Screen:* `/settings` or `/audit-log`, then close.

Pick three, not all:

> "A few things I'd point out. It's hexagonal — domain, application, and infrastructure
> are separated, and an architecture test fails the build if a layer reaches the wrong
> way. It's multi-tenant: every account gets an isolated workspace, and there's an audit
> log of every action. Runs execute in Docker with the cloud metadata endpoint firewalled
> off. And it's deployed with Postgres, a worker queue, nightly verified backups, and
> Sentry."

Close on the PR or the run detail page.

---

## Recording notes

**Do not wait in real time for the run if it is slow.** Cut from submit to the completed
run. Nobody will object, and a 40-second silent pause kills the video. If you would rather
not edit, queue a run before you start recording, then re-queue live and cut to the
finished one.

**Record at 1080p or higher and zoom the browser to 125%.** The dashboard has dense tables
that turn to mush at small sizes on a shrunken video player.

**Have a fallback.** If the live run fails on camera, cut to a previously completed run at
`/runs` and narrate that instead. Demos fail; footage doesn't.

**The cost figure is a real number.** Around $0.002 per run. Say it — it lands well and it
is genuinely verifiable from the dashboard.

**Check the diff before you commit to a take.** `gpt-4o-mini` reliably produces the correct
one-line fix, but it sometimes rewrites the file and drops the module docstring along the
way — harmless, but it adds noise to a diff you are about to put on screen. If the diff is
not clean, either re-run (it is nondeterministic) or switch the model to `gpt-4.1`, which
holds the surrounding file steady. A verified run took **60 seconds** end to end.

---

## If something breaks

| Symptom | Cause | Fix |
| --- | --- | --- |
| Run status `dead_letter`, 403 on push | GitHub App lacks access to the demo repo | Do the one-time setup above |
| Run stuck in `queued` | Worker container is down | `ssh root@159.223.182.144 'docker ps \| grep worker'` |
| Tests fail after the patch | Model produced a bad fix | Re-run; or switch model to `gpt-4.1` |
| Dashboard shows sample runs | Demo data enabled | `DASHBOARD_DEMO_DATA_ENABLED=false` (already set in production) |
| `reset-demo.sh` cannot push | `gh` not authenticated | `gh auth login` |

Check a run's failure reason directly:

```bash
ssh root@159.223.182.144 "cd /opt/patchpilot/autonomous_engineering_agent && \
  docker compose -f docker-compose.yml -f docker-compose.vps.yml exec -T postgres \
  psql -U patchpilot -d patchpilot -c \
  \"SELECT id, status, left(coalesce(last_error,summary,'none'),120) FROM runs ORDER BY id DESC LIMIT 3;\""
```

---

## For the résumé itself

One line, if you need it short:

> Built and deployed an autonomous software engineering agent that resolves GitHub issues
> end to end — plans a fix, edits code, runs tests in a network-isolated Docker sandbox,
> and opens a pull request — in Python/FastAPI on a hexagonal architecture, with
> multi-tenant workspaces, OAuth and GitHub App integration, Postgres, a leased job queue,
> and verified nightly backups.

Numbers worth quoting, all real and checkable from the dashboard: about **$0.002 per
resolved issue**, **one agent iteration** for a typical single-file fix, **130 tests**
in the project's own suite.
