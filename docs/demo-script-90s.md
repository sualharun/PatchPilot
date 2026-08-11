# PatchPilot — 90 Second Demo

Two things below: the **narration** to paste into ElevenLabs, and the **shot list**
telling you what to click at each timestamp. Record the screen silently, generate the
voice track separately, then lay them together.

---

## 1. Narration for ElevenLabs

Paste everything between the rules. It is written to be read aloud — symbols are spelled
out so the voice model does not mangle them, and there are no stage directions to read by
mistake.

**203 words.** Measured against the 90-second ceiling:

| Pace | Runtime |
| --- | --- |
| 140 wpm (slow, deliberate) | 1:27 |
| 150 wpm (normal) | **1:21** |
| 160 wpm (brisk) | 1:16 |

Even a slow read lands under 1:30, so you have room for pauses rather than needing to
rush. Target 150.

Suggested ElevenLabs settings: a calm professional voice, **Stability 50**,
**Similarity 75**, **Speed 1.0**. Do not push speed above 1.1 — it starts to sound rushed
and undercuts the "this is a real system" tone.

---

This is a pricing module with a bug. The bulk discount is supposed to apply at ten units, but it only starts at eleven. Two of its four tests fail. There is a GitHub issue describing exactly that.

PatchPilot is an autonomous engineering agent I built and deployed. You give it an issue. It fixes the code, runs your test suite inside a Docker sandbox, and opens a pull request.

I paste in the issue URL, choose a model, and allow it to open a pull request. That is the entire input. No instructions about what to change, or which file to touch.

It clones the repository into a container that has no network access while tests run. It reads the failing tests to understand the intent, proposes a patch, then re-runs the suite to check its own work. Every command, exit code, and token cost is recorded.

Sixty seconds later, here is the fix. One character. Greater-than becomes greater-than-or-equal. The two failing tests now pass, and the two that already worked still do.

It pushed a branch and opened a draft pull request. One file changed. Two tenths of a cent.

Python and FastAPI, hexagonal architecture, multi-tenant, Postgres, running in production today.

---

## 2. Shot list

Timestamps are where each shot **starts**. Have all four tabs open before you hit record.

| Time | Screen | What you do |
| --- | --- | --- |
| 0:00 | Terminal in a `patchpilot-demo` clone | Run `pytest -q`. Let the red `2 failed, 2 passed` sit on screen for a beat. |
| 0:07 | GitHub issue #1 | Switch tabs. Scroll once so the expected-versus-actual block is visible. |
| 0:15 | Dashboard `/overview` | Switch tabs. Already signed in. Let the metric tiles show. |
| 0:27 | `/overview`, *Start Agent Run* card | Paste the issue URL. Model `gpt-4o-mini`. Max iterations `3`. Toggle **Open PR on**. Click submit. |
| 0:38 | Run detail page | Lands here automatically. Refresh once. Scroll slowly through Commands as they fill in. |
| 0:52 | Run detail — Tests panel | Scroll to test results. Pause on `4 passed`. |
| 1:02 | Run detail — Patch panel | Scroll to the diff. **Hold here** — this is the moment the video exists for. |
| 1:12 | GitHub pull request | Switch tabs. Show the Files-changed tab: one file. |
| 1:22 | Dashboard `/runs` | Switch back. Cursor near the cost column. Hold and end. |

### Before you record

```bash
./reset-demo.sh          # already run — repo is clean right now
gh pr list --repo sualharun/patchpilot-demo --state open   # expect zero
```

Tabs, in this order:

1. `https://github.com/sualharun/patchpilot-demo/issues/1`
2. `https://patchpilot.159.223.182.144.sslip.io/overview` — signed in
3. `https://patchpilot.159.223.182.144.sslip.io/runs`
4. `https://github.com/sualharun/patchpilot-demo/pulls`

Browser zoom **125%**, window at 1920x1080 or larger.

---

## 3. The one editing step that matters

The run takes about sixty seconds. Your narration only allocates roughly twenty-five
seconds to it. **You must cut.**

Record the screen straight through in real time, then in your editor cut out the dead
waiting between submitting the run and the results appearing. The narration line
"Sixty seconds later, here is the fix" is written to cover that cut — it tells the viewer
time passed, so the jump feels deliberate rather than like a glitch.

Everything else lines up roughly one-to-one with real time.

---

## 4. If the live run misbehaves

**Always look at the diff before committing to a take.** A verified good run is one file,
one line, four tests passing, in about sixty seconds. Anything else, reset and re-run —
generation is nondeterministic.

Observed failure modes while making this demo reliable, and what each looks like:

| What you see | What happened | Do |
| --- | --- | --- |
| Diff touches `tests/` | The model "fixed" the suite by deleting tests | Reset, re-run. Never ship this one. |
| `failed_tests`, syntax error in output | Patch mangled the file | Reset, re-run |
| `dead_letter`, "No valid patches in input" | Patch format was rejected outright | Reset, re-run. Seen with `gpt-4.1`; stay on `gpt-4o-mini`. |
| Redirect to `/billing?limit=...` | Monthly run cap | Raise the workspace's cap (see below) |

Stay on **`gpt-4o-mini`**. It is both cheaper and, on this repo, more reliable than
`gpt-4.1`, whose patches were rejected outright.

Two things about the target repo exist specifically for reliability: `pricing.py` uses
line comments rather than triple-quoted docstrings, because patch generation repeatedly
mangled the docstrings into syntax errors; and `conftest.py` exists so bare `pytest`
can import the module.

Reset and try again as many times as you like:

```bash
./reset-demo.sh
```

### Run cap

The free plan allows five runs a month, and the demo will refuse to queue past that. The
workspaces on the live instance are set to `pro` (250 runs). If it ever bites again:

```sql
UPDATE subscriptions SET plan='pro', status='active' WHERE workspace_id = <id>;
UPDATE workspace_limits SET plan='pro', monthly_run_cap=250 WHERE workspace_id = <id>;
```

Both rows are needed — `effective_plan()` only honours a paid plan while a subscription
is `active` or `trialing`.
