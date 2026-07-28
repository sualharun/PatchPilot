# Architecture

PatchPilot uses hexagonal architecture with onion-style dependency rules. The
core does not know whether it is called by FastAPI, the CLI, or a worker, and it
does not know whether its data comes from PostgreSQL, SQLite, GitHub, Kafka,
Docker, OpenAI, or Anthropic.

## Dependency Rule

Dependencies point inward:

```text
Inbound adapters                       Outbound adapters
HTTP / CLI / workers                    SQL / GitHub / Kafka / Docker / LLM
        |                                          ^
        v                                          |
application commands, queries, services -> outbound ports
        |
        v
domain entities, values, invariants, policies
```

- `domain` imports only the Python standard library and other domain modules.
- `application` imports only `application` and `domain` modules.
- `infrastructure` implements outbound ports and never imports `interfaces`.
- `interfaces` translates transport input into commands and renders query results.
- `bootstrap.py` is the composition root that selects concrete adapters.

These rules are enforced by AST-based tests in `tests/test_architecture.py`.

## Package Map

```text
agent/
  domain/
    entities.py          Entities and lifecycle transitions
    enums.py             Run and job statuses
    errors.py            Domain invariant failures
    value_objects.py     Repository, issue, command, tool, and decision values
    services.py          Issue parsing, branch naming, webhook policy, metrics
    pricing.py           Provider-independent token cost policy

  application/
    commands/            Write-side use cases and command handlers
    queries/             Dashboard read models and query aggregation
    services/            Engineering loop, run worker, PR analysis, tool execution
    ports/inbound.py      Use cases exposed to primary adapters
    ports/outbound.py     Contracts required from secondary adapters
    dto.py                Application request/result data

  infrastructure/
    config/              Environment and agent.yaml settings
    db/                  SQLite/PostgreSQL store and repository adapters
    github/              GitHub API adapter
    kafka/               PR job producer and consumer adapters
    llm/                 OpenAI/Anthropic adapters, pricing config, tool schemas
    repository/          Git workspace, Python detection, and file tools
    sandbox/             Docker execution adapter
    artifacts/           JSON replay writer and artifact catalog
    security/            Webhook verification and secret redaction

  interfaces/
    http/                FastAPI dashboard and GitHub webhook adapter
    workers/             Queued-run and Kafka worker adapters
    cli.py               Command-line adapter

  bootstrap.py           Composition root
```

Top-level modules such as `agent.persistence`, `agent.github_client`, and
`agent.dashboard` are compatibility facades. They preserve the MVP import API
while all implementation lives in the layered packages above.

## Command Flow

Queueing an issue run:

```text
POST /api/runs
  -> QueueRunCommand
  -> QueueRunHandler (inbound port)
  -> branch/max-iteration domain policies
  -> RunRepository + AuditLog (outbound ports)
  -> SqlRunRepository + SqlAuditLog (outbound adapters)
```

Executing the run:

```text
run worker
  -> ProcessQueuedRunsHandler
  -> ExecuteEngineeringRunHandler
  -> GitHubGateway / WorkspaceManager / RepositoryToolFactory / LLMGateway
  -> Docker tests and provider-native tool calls
  -> RunRepository + ArtifactWriter
```

The application service owns orchestration and retry sequencing. Domain
policies own valid statuses, run transitions, issue parsing, branch naming,
webhook eligibility, metrics, and provider-independent pricing.

## Query Flow

Dashboard reads never access SQL directly:

```text
FastAPI route
  -> DashboardQueryService
  -> read-side outbound ports
  -> SQL/artifact adapters
  -> application projection
  -> HTML or JSON response
```

Run metrics, test summaries, billing aggregation, account state, repositories,
provider keys, GitHub state, artifacts, and audit history are handled by the
application query layer.

## Kafka PR Flow

```text
GitHub pull_request webhook
  -> HMAC verification in the HTTP adapter
  -> EnqueuePullRequestAnalysisHandler
  -> webhook eligibility domain policy
  -> PRJobPublisher port
  -> KafkaPRJobProducer adapter
  -> pr-analysis-jobs
  -> KafkaPRJobConsumer adapter
  -> ProcessPullRequestJobsHandler
  -> AnalyzePullRequestHandler
  -> GitHubGateway adapter
```

API and worker replicas scale independently. Kafka offsets are committed only
after a review succeeds.

## Persistence

`RunStore` is an infrastructure implementation behind repository ports. The
schema includes users, workspaces, memberships, repositories, issues, pull
requests, runs, iterations, commands, patches, test results, jobs, artifacts,
audit events, provider keys, usage events, and workspace settings. Foreign keys
and indexes protect ownership and common query paths.

The application and domain layers never import `RunStore`, SQL drivers, or SQL
table details.

## Adding A Feature

1. Put new invariants or business policies in `domain`.
2. Define a command/query handler in `application`.
3. Add only the outbound ports the handler needs.
4. Implement those ports in `infrastructure`.
5. Wire adapters in `bootstrap.py`.
6. Expose the handler through an inbound adapter in `interfaces`.
7. Add behavior tests and an architecture boundary test when needed.
