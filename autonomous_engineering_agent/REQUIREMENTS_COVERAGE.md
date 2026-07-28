# Requirements Coverage

This document maps the original MVP requirements to the current implementation.

## Goal

| Requirement | Status | Implementation |
| --- | --- | --- |
| Accept GitHub issue URL or repo + issue number | Done | `agent.github_client.parse_issue_ref` |
| Clone repository | Done | `agent.repo.clone_repository` |
| Inspect issue | Done | `agent.github_client.GitHubClient.fetch_issue` |
| Propose fix | Done | `agent.llm` providers and structured decisions |
| Edit code | Done | `agent.tool_calls` and `agent.tools` |
| Run tests in Docker | Done | `agent.sandbox.DockerSandbox` |
| Iterate on failures | Done | `agent.executor.EngineeringAgent` |
| Commit changes to branch | Done | `agent.repo.RepoWorkspace.commit_all` |
| Optionally open PR | Done | `--open-pr true` and `GitHubClient.create_draft_pr` |

## Core Requirements

| Area | Status | Notes |
| --- | --- | --- |
| CLI command | Done | `agent run --issue ... --model ... --max-iterations ... --open-pr false` |
| `.env` support | Done | `python-dotenv` in `agent.config` |
| GitHub issue metadata | Done | title, body, comments, labels, linked PR references |
| Temporary clone and branch | Done | branch pattern `agent/fix-issue-<number>-<timestamp>` |
| Draft PR | Done | opt-in only |
| Docker command execution | Done | setup/test commands run in Docker |
| Timeouts and output capture | Done | stdout, stderr, exit code, runtime |
| Container cleanup | Done | `docker run --rm`, forced cleanup on timeout |
| Python project detection | Done | `pyproject.toml`, `requirements.txt`, `setup.py`, `Pipfile`, `poetry.lock` |
| Test detection | Done | pytest and unittest detection |
| `agent.yaml` overrides | Done | install, test, sandbox, logging, database |
| Code editing tools | Done | list/read/search/apply/write/run/diff/status |
| Secret redaction | Done | env-derived values and common token patterns |
| Command allowlist | Done | configured through sandbox settings |
| Resource limits | Done | CPU, memory, runtime |
| Persistence | Done | SQLite and PostgreSQL URL support |
| Final output summary | Done | CLI prints status, summary, tests, branch, PR URL, logs |
| Unit tests | Done | required areas plus end-to-end smoke test |
| README | Done | setup, env, examples, safety, limits, roadmap |

## Developer-Ready Additions

| Capability | Status | Implementation |
| --- | --- | --- |
| Preflight checks | Done | `agent doctor` |
| Eval harness | Done | `agent eval` |
| Curated synthetic evals | Done | `agent eval-synthetic` and `evals/synthetic/python_bugs.yaml` |
| Dashboard | Done | `agent dashboard` |
| Replay artifacts | Done | JSON schema in `agent.artifacts` |
| Cost tracking | Done | provider usage metadata and `agent.pricing` |
| Provider-native tool calling | Done | OpenAI and Anthropic adapters |
| CI | Done | `.github/workflows/ci.yml` |
| Lint config | Done | Ruff config in `pyproject.toml` |
| Contributor guide | Done | `CONTRIBUTING.md` |
| Security model | Done | `SECURITY.md` |

## Intentional Limitations

- Python repositories only.
- Docker is used for repository setup and tests; host Git is used for clone, branch, diff, commit, and push.
- LLM tool calls use provider-native tool APIs where available, with JSON fallback for stub/local modes.
- Dependency install networking is allowed by default.
- Docker is a practical sandbox, not a hardened isolation boundary.
- Authentication for the dashboard is currently UI-only.
