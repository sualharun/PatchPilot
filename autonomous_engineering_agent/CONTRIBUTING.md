# Contributing

Thanks for working on the autonomous engineering agent.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
cp .env.example .env
agent doctor
```

## Quality Gates

Run these before opening a PR:

```bash
python3 -m ruff check .
python3 -m pytest
```

Or:

```bash
make check
```

## Design Rules

- Keep the agent Python-repository focused until the Python path is reliable.
- Prefer small, auditable tool calls over broad shell execution.
- Do not log secrets, tokens, or raw environment dumps.
- Put untrusted repository setup and tests through the sandbox.
- Add tests for parser, persistence, sandbox, and run-loop behavior when changing those areas.

## Good First Issues

- Add provider-native OpenAI tool calling.
- Add per-phase Docker network policy.
- Add a richer dashboard diff view.
- Add real auth for the dashboard.
