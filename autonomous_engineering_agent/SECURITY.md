# Security Model

This project executes untrusted repository code, so the sandbox is a core product boundary.

## Current Controls

- Repository commands run inside Docker.
- Docker runs with CPU, memory, and timeout limits.
- Commands are checked against an allowlist, defined in a checked-in, versioned policy file
  (`agent/infrastructure/sandbox/command_policy.json`) rather than a silent code default --
  changing what a sandboxed run may execute now shows up as a reviewable diff.
- Sandboxed commands have no network access by default (`--network none`). Only dependency
  installation (`install_commands`) runs with `needs_network=True`, and only on a dedicated
  Docker network (`patchpilot-sandbox-egress`, see `scripts/setup-sandbox-network.sh`) that is
  isolated from the other project containers (postgres, redpanda, the dashboard) by Docker's
  default inter-network isolation, and has the cloud metadata endpoint (`169.254.169.254`)
  blocked via a host `iptables` rule in the `DOCKER-USER` chain. Test runs and LLM-driven
  tool-call commands never get network access, regardless of what the repository requests.
- A repository's own `agent.yaml` cannot override the sandbox's network or command allowlist --
  those always come from the deployment-controlled base config, not the untrusted repo.
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
- **Fixed:** dependency installation used to get full outbound network access on the default
  Docker bridge (reachable from other containers/the host); it now runs on an isolated,
  install-only network, and all other sandboxed commands default to no network at all.
- **Fixed:** the command allowlist used to be a silent `set[str]` config default; it is now a
  checked-in, versioned JSON policy file.
- **Still open:** the metadata-endpoint block and the install network's isolation from the
  compose stack depend on `scripts/setup-sandbox-network.sh` having been run on the host (and
  its `iptables` rule surviving reboots, which is not automatic -- see the script's output for
  persistence options). `DockerSandbox` will lazily create the install network if the script
  was never run, but without the `iptables` rule the metadata endpoint would not be blocked in
  that case.
- **Still open:** the install network still gives outbound internet access to whatever a
  package installer downloads (typosquatted/compromised packages, build-time code execution)
  -- this is inherent to installing dependencies at all, not something network policy alone
  can close.
- LLM tool calls should eventually be validated against a richer policy engine.
- Dashboard auth supports signed username/password sessions for local dev and GitHub OAuth for deployment.
- Production preflight requires GitHub OAuth configuration, secure cookies, a session secret, PostgreSQL, durable artifact storage, and disabled demo data.
