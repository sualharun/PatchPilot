# Security Model

This project executes untrusted repository code, so the sandbox is a core product boundary.

## Current Controls

- Repository commands run inside Docker.
- Docker runs with CPU, memory, and timeout limits.
- Commands are checked against an allowlist.
- Shell control operators are rejected.
- API keys and common token formats are redacted from logs.
- PR creation and push are disabled unless `--open-pr true`.
- Git remotes are sanitized after clone so tokens are not left in `origin`.
- Cost and token usage metadata avoid storing prompts beyond the replay artifact already written for debugging.
- The dashboard can require signed HTTP-only sessions using `DASHBOARD_AUTH_ENABLED=true`.
- Dashboard login/logout and queued runs are written to the audit log.

## Recommended Deployment Controls

- Run workers on isolated machines or ephemeral VMs.
- Use short-lived GitHub tokens with minimum repository permissions.
- Disable network during tests by default once dependency caching is implemented.
- Store secrets in the deployment platform, not in `.env` files.
- Retain artifacts for debugging, but expire command output after a defined period.
- Put human review in front of opening PRs for high-risk repositories.
- Enable dashboard auth and put the beta dashboard behind SSO or a private network before exposing it beyond localhost.

## Known Gaps

- Docker is not a perfect security boundary against malicious code.
- Dependency installation currently allows network access by default.
- The command allowlist is intentionally conservative but not yet policy-versioned.
- LLM tool calls should eventually be validated against a richer policy engine.
- Dashboard auth supports signed username/password sessions for local dev and GitHub OAuth for deployment.
- Production preflight requires GitHub OAuth configuration, secure cookies, a session secret, PostgreSQL, durable artifact storage, and disabled demo data.
