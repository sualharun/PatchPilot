# Getting Started with PatchPilot

PatchPilot takes a GitHub issue, works out a fix, runs your tests inside a
Docker sandbox, and hands you a patch — or a draft pull request if you ask for
one. This guide takes you from a new account to your first completed run.

The in-app version of this checklist lives at **/onboarding**, and short answers
to common questions are at **/faq**.

## 1. Create an account

Open the dashboard and choose **Create an account** on the sign-in page
(`/signup`). Enter an email address and a password of at least 8 characters. A
private workspace is created for you automatically — your runs, repositories,
and billing are scoped to it and are never shared with another account.

If your deployment is configured for GitHub-only sign-in, the signup form is
hidden and you sign in with **Continue with GitHub** instead. Operators control
this with `DASHBOARD_AUTH_MODE` (`password`, `github-oauth`, or `both`).

If the server has `DASHBOARD_REQUIRE_EMAIL_VERIFICATION=true`, you also get a
confirmation email. Verification is *soft*: your account works right away, and
the Account page shows a reminder with a **Resend verification email** button
until you click the link. Links expire after 24 hours.

## 2. Connect GitHub

Click **Continue with GitHub** on the sign-in page, or open **/github** once
signed in. The OAuth grant lets PatchPilot read issues and clone the
repositories you authorize. You can confirm the connection on the onboarding
checklist — it shows the GitHub login it is connected as.

## 3. Add an API key

PatchPilot calls OpenAI or Anthropic to generate patches. You need one key:

- OpenAI — create one at <https://platform.openai.com/api-keys>
- Anthropic — create one at <https://console.anthropic.com/settings/keys>

Set it in the server environment as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` and
restart PatchPilot. Keys are never written to the database; the dashboard stores
only a masked hint.

Then open **Settings → Provider Keys** (`/settings#providers`) and press **Test
OpenAI key** or **Test Anthropic key**. A valid key reports success immediately.
A rejected key (401) usually means a typo or a revoked key; a 429 means the key
works but the account needs billing credit.

The same panel shows estimated per-run costs for a few common models. You pay
your provider directly — PatchPilot adds no per-token markup.

## 4. Install the GitHub App (optional)

Installing the app lets PatchPilot start runs by itself: label an issue with
`patchpilot` and a run begins, with the resulting draft PR posted back to the
issue. Without it, everything still works — you queue each run by hand.

Open **/github-app-setup** and follow the prompts. The full guide, including
required permissions and environment variables, is in
[github-app-setup.md](github-app-setup.md).

## 5. Queue your first run

Go to **Overview** or **Runs** and use the *Start Agent Run* form:

- **Issue** — a GitHub issue URL, or the short form `owner/repo#123`
- **Model** — for example `gpt-4o-mini` to start cheap, or a frontier model for
  harder issues
- **Max iterations** — how many fix-and-test cycles to allow (5 is a good
  default)
- **Open PR** — leave off for your first run so nothing is pushed to GitHub

From the CLI, the same run looks like:

```bash
agent run \
  --issue https://github.com/acme/api/issues/4821 \
  --model gpt-4o-mini \
  --max-iterations 5 \
  --open-pr false
```

## 6. Watch it work

The run appears in **Runs** with a live status. Open it to follow along:

- **Commands** — every command executed in the sandbox, with exit codes
- **Tests** — captured test output for each attempt
- **Patch** — the diff the agent produced
- **Tool calls** — what the agent read, searched, and edited, and why

## 7. Review the results

A finished run gives you a patch and a test summary. Read the diff before
trusting it — the agent is capable, not infallible. When you are ready to let it
open pull requests, re-run with **Open PR** enabled; PatchPilot pushes a branch
named `agent/fix-issue-<number>-<id>` and opens a **draft** PR for review. It
never pushes or opens a PR unless you enable that option.

## Where to go next

- **/faq** — requirements, cost, supported languages, private repositories,
  troubleshooting
- **/security** — sandboxing, secret redaction, and the audit trail
- **/billing** — plan limits and month-to-date spend
- **/account** — change your email or password, sign out

## Troubleshooting

- **The run fails during setup:** the sandbox image is missing a dependency.
  Configure `install_commands` in `agent.yaml` for the repository.
- **Tests never pass:** check that the detected test command is the one you
  actually use; set `test_commands` in `agent.yaml` to override it.
- **No patch was produced:** without a provider key PatchPilot runs in stub mode
  and analyzes without editing. Confirm the key in Settings → Provider Keys.
- **Labeled issues do not start runs:** that requires the GitHub App — see
  [github-app-setup.md](github-app-setup.md).
