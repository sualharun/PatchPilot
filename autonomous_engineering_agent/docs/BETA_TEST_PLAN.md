# Beta Test Plan

A small, deliberately easy set of Python repos/issues to validate the autonomous
issue-fixing flow end-to-end before inviting real users. Each issue should be small
enough that a single-file patch and `pytest` run can fix and verify it — the goal here
is to validate the *pipeline* (GitHub App -> queue -> worker -> Docker sandbox -> PR),
not to benchmark model quality.

Use `sualharun/patchpilot-test-repo` (the repo the GitHub App is installed on) as the
primary target; the list below can also seed additional throwaway repos if you want more
variety.

## Setup

1. Install the GitHub App on `sualharun/patchpilot-test-repo` (and any other repos used
   below).
2. Confirm the repo appears in `github_app_repositories` (`/github` dashboard page, or
   query the table directly).
3. Confirm `GITHUB_APP_TRIGGER_LABEL` (default `patchpilot`) if you plan to use labeled
   issues instead of opened issues to trigger runs.

## Test Repos / Issues (5–10 small Python fixtures)

Pick 5–10 of these, or clone similarly small issues into `patchpilot-test-repo`:

1. **Off-by-one in a helper function** — a function like `is_in_range(n, low, high)` uses
   `<` instead of `<=`; an existing test asserts the boundary case fails.
2. **Wrong exception type** — a function raises `ValueError` where the test expects
   `TypeError` for a non-numeric input.
3. **String formatting bug** — an f-string swaps two variables (e.g. prints
   `"{price} {name}"` instead of `"{name} {price}"`); a test asserts the exact string.
4. **Missing None check** — a function crashes with `AttributeError` on `None` input
   instead of returning a default; a test covers the `None` case.
5. **Incorrect default argument** — a function's default parameter (e.g. `timeout=5`)
   doesn't match what the test expects (`timeout=30`).
6. **Broken sort comparator** — a `sorted(..., key=...)` call sorts descending instead of
   ascending (or vice versa); a test checks order.
7. **Off-by-one in a loop** — a `for i in range(n)` should be `range(n + 1)` (or the
   reverse), causing a test to see one element too few/many.
8. **Wrong dict key access** — code reads `data["nam"]` (typo) instead of `data["name"]`,
   raising `KeyError` in a test.
9. **Incorrect boolean logic** — an `and` should be an `or` (or negation is missing) in a
   validation function; a test exercises both branches.
10. **Rounding bug** — a function truncates instead of rounds (`int(x)` vs `round(x)`);
    a test checks a value like `2.5 -> 3`.

Each fixture should:

- Live in a repo with a working `pyproject.toml`/`requirements.txt` and a passing test
  suite except for the one failing test tied to the bug.
- Have a GitHub issue describing the bug in plain language (not the fix) with a link or
  reference to the failing test.
- Be small enough that a single-iteration patch is plausible, so you're testing
  pipeline correctness more than model capability.

## Test Script

For each of the 5–10 issues:

1. **Trigger**: open the issue on the installed repo (or add the trigger label to an
   existing issue). Confirm in the dashboard `/audit-log` that a `pr_analysis.enqueued`
   or run-queued event appears within a few seconds of the webhook firing.
2. **Dedup check**: re-deliver the same webhook from GitHub's "Recent Deliveries" UI (or
   `curl` the same payload with the same `X-GitHub-Delivery` header) and confirm the
   dashboard does **not** show a second run.
3. **Worker pickup**: confirm the run moves from `queued` to `running` within one worker
   poll interval.
4. **Docker sandbox**: confirm the run's commands include an install + test phase, and
   that the container is cleaned up afterward (`docker ps -a` should not accumulate
   PatchPilot containers).
5. **Result**: confirm the run reaches `success`, `pr_opened`, or a clear failure state
   (`failed_tests`, `setup_failed`, `dead_letter`) — never silently stuck in `running`.
6. **PR** (if `open_pr` is enabled): confirm a draft PR was opened on the repo with a
   sensible title/branch and that the diff matches the described bug fix.
7. **Dashboard correctness**: confirm the run appears under the correct workspace/account,
   with redacted logs (no raw tokens/keys visible anywhere in the run detail page).
8. **Manual path still works**: separately, queue one issue manually via `/api/runs` (or
   `agent run`) and confirm it behaves identically to the GitHub-App-triggered runs.

## Pass Criteria

- All 5–10 issues reach a terminal, correctly-labeled status.
- No duplicate runs from webhook redelivery.
- No container leaks after test runs.
- No secrets visible in any dashboard page or exported run JSON.
- Manual issue queueing still works alongside the GitHub App flow.
- At least one billing-limited scenario tested: temporarily set a workspace's
  `monthly_run_cap` to `0` (via `workspace_limits`) and confirm the next run is blocked
  with a clear message instead of silently queueing.

## Known Limitations To Communicate To Beta Testers

- Python repositories only.
- Best results on small, well-isolated bugs with an existing test that pins the
  expected behavior — not open-ended feature requests.
- The free plan caps runs per month; testers hitting the cap should see a clear message
  on `/billing`, not a silent failure.
