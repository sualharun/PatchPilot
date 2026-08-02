from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

from agent.doctor import format_doctor, has_errors, run_doctor
from agent.domain.services import parse_issue_ref
from agent.evals import run_eval_manifest
from agent.evals_synthetic import run_synthetic_eval
from agent.executor import EngineeringAgent
from agent.infrastructure.config.settings import load_config
from agent.infrastructure.db.seeds import seed_database
from agent.infrastructure.db.store import RunStore
from agent.infrastructure.github.client import GitHubClient
from agent.infrastructure.llm.providers import provider_for_model
from agent.infrastructure.observability import capture_exception, init_sentry
from agent.infrastructure.security.secrets import redact_text
from agent.interfaces.http.dashboard import serve_dashboard
from agent.interfaces.workers.pr_worker import process_pr_analysis_jobs
from agent.interfaces.workers.run_worker import process_queued_runs


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return run_command(args)
    if args.command == "eval":
        report = run_eval_manifest(Path(args.manifest), Path(args.output))
        print(f"task count: {report['task_count']}")
        print(f"solved: {report['solved_count']}")
        print(f"solve rate: {report['solve_rate']}")
        print(f"report: {args.output}")
        return 0
    if args.command == "dashboard":
        serve_dashboard(args.host, args.port, args.database_url)
        return 0
    if args.command == "eval-synthetic":
        report = run_synthetic_eval(Path(args.manifest), Path(args.output))
        config = load_config(env_file=Path(args.env_file))
        store = RunStore(config.database_url, allow_sqlite_fallback=not config.production)
        store.add_eval_report(
            name="synthetic-python",
            manifest_path=str(Path(args.manifest)),
            task_count=int(report["task_count"]),
            passed_count=int(report["passed_count"]),
            pass_rate=float(report["pass_rate"]),
            report_path=str(Path(args.output)),
        )
        store.close()
        print(f"task count: {report['task_count']}")
        print(f"passed: {report['passed_count']}")
        print(f"pass rate: {report['pass_rate']}")
        print(f"report: {args.output}")
        return 0 if report["passed_count"] == report["task_count"] else 2
    if args.command == "doctor":
        config = load_config(env_file=Path(args.env_file))
        checks = run_doctor(config, check_docker_run=args.check_docker_run, check_database=not args.skip_database)
        print(format_doctor(checks))
        return 1 if has_errors(checks) else 0
    if args.command == "migrate":
        config = load_config(env_file=Path(args.env_file))
        store = RunStore(config.database_url, allow_sqlite_fallback=not config.production)
        store.close()
        print("database schema is up to date")
        return 0
    if args.command == "db-seed":
        config = load_config(env_file=Path(args.env_file))
        store = RunStore(config.database_url, allow_sqlite_fallback=not config.production)
        seed_result = seed_database(store, profile=args.profile)
        store.close()
        print(f"profile: {seed_result['profile']}")
        print(f"repositories: {seed_result['repositories']}")
        print(f"runs created: {seed_result['runs_created']}")
        return 0
    if args.command == "worker":
        config = load_config(env_file=Path(args.env_file))
        init_sentry(config.sentry_dsn, environment="production" if config.production else "development")
        failed = 0
        while True:
            try:
                worker_result = process_queued_runs(config, limit=args.limit)
            except Exception:
                capture_exception()
                raise
            failed += worker_result.failed
            print(f"processed: {worker_result.processed}")
            print(f"succeeded: {worker_result.succeeded}")
            print(f"failed: {worker_result.failed}")
            if not args.loop:
                return 0 if failed == 0 else 2
            time.sleep(args.sleep_seconds)
    if args.command == "pr-worker":
        config = load_config(env_file=Path(args.env_file))
        init_sentry(config.sentry_dsn, environment="production" if config.production else "development")
        failed = 0
        while True:
            try:
                pr_result = process_pr_analysis_jobs(config, limit=args.limit)
            except Exception:
                capture_exception()
                raise
            failed += pr_result.failed
            print(f"processed: {pr_result.processed}")
            print(f"succeeded: {pr_result.succeeded}")
            print(f"failed: {pr_result.failed}")
            if not args.loop:
                return 0 if failed == 0 else 2
            time.sleep(args.sleep_seconds)
    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent", description="Autonomous engineering agent for Python repos")
    subparsers = parser.add_subparsers(dest="command")
    run = subparsers.add_parser("run", help="Run the agent against a GitHub issue")
    run.add_argument("--issue", required=True, help="GitHub issue URL, owner/repo#123, or 'owner/repo 123'")
    run.add_argument("--model", required=True, help="OpenAI or Anthropic model name")
    run.add_argument("--max-iterations", type=int, default=5)
    run.add_argument("--open-pr", default="false", choices=("true", "false"))
    run.add_argument("--env-file", default=".env")
    eval_parser = subparsers.add_parser("eval", help="Run an evaluation manifest")
    eval_parser.add_argument("--manifest", required=True, help="Path to eval YAML manifest")
    eval_parser.add_argument("--output", default="eval-report.json", help="Path to write JSON report")
    synthetic = subparsers.add_parser("eval-synthetic", help="Run curated local synthetic Python issue tasks")
    synthetic.add_argument("--manifest", default="evals/synthetic/python_bugs.yaml")
    synthetic.add_argument("--output", default="synthetic-eval-report.json")
    synthetic.add_argument("--env-file", default=".env")
    dashboard = subparsers.add_parser("dashboard", help="Serve a run dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8080)
    dashboard.add_argument("--database-url", default=None)
    doctor = subparsers.add_parser("doctor", help="Check local prerequisites and configuration")
    doctor.add_argument("--env-file", default=".env")
    doctor.add_argument("--check-docker-run", action="store_true", help="Run the configured Python Docker image")
    doctor.add_argument("--skip-database", action="store_true", help="Skip database reachability checks")
    migrate = subparsers.add_parser("migrate", help="Create or update database schema")
    migrate.add_argument("--env-file", default=".env")
    seed = subparsers.add_parser("db-seed", help="Seed users, workspace, repositories, provider hints, and demo runs")
    seed.add_argument("--env-file", default=".env")
    seed.add_argument("--profile", default="local-dev", choices=("local-dev", "demo"))
    worker = subparsers.add_parser("worker", help="Process queued dashboard runs")
    worker.add_argument("--env-file", default=".env")
    worker.add_argument("--limit", type=int, default=1)
    worker.add_argument("--loop", action="store_true", help="Continuously poll for queued runs")
    worker.add_argument("--sleep-seconds", type=float, default=5.0)
    pr_worker = subparsers.add_parser("pr-worker", help="Consume Kafka PR analysis jobs")
    pr_worker.add_argument("--env-file", default=".env")
    pr_worker.add_argument("--limit", type=int, default=1)
    pr_worker.add_argument("--loop", action="store_true", help="Continuously poll Kafka for PR jobs")
    pr_worker.add_argument("--sleep-seconds", type=float, default=5.0)
    return parser


def run_command(args: argparse.Namespace) -> int:
    try:
        issue = parse_issue_ref(args.issue)
        config = load_config(env_file=Path(args.env_file))
        github = _github_client_from_config(config)
        llm = provider_for_model(args.model, config.openai_api_key, config.anthropic_api_key)
        store = RunStore(config.database_url)
        agent = EngineeringAgent(config=config, github=github, llm=llm, store=store)
        result = agent.run(
            issue=issue,
            model=args.model,
            max_iterations=max(1, args.max_iterations),
            open_pr=args.open_pr == "true",
        )
        print(_format_result(result))
        return 0 if result.status in {"success", "pr_opened"} else 2
    except Exception:
        print(redact_text(traceback.format_exc()), file=sys.stderr)
        return 1


def _format_result(result) -> str:
    lines = [
        f"final status: {result.status}",
        f"summary of changes: {result.summary}",
        f"tests run: {', '.join(result.tests_run) if result.tests_run else 'none'}",
        f"tests passed: {str(result.tests_passed).lower()}",
        f"branch name: {result.branch}",
        f"PR URL: {result.pr_url or 'not created'}",
        f"path to full logs: {result.logs_path}",
        f"working copy: {result.repo_path}",
    ]
    try:
        import json

        artifact = json.loads(result.logs_path.read_text(encoding="utf-8"))
        usage = artifact.get("usage") or {}
        if usage:
            lines.insert(5, f"estimated LLM cost: {usage.get('estimated_cost_usd') or 'unpriced'}")
    except Exception:
        pass
    return "\n".join(lines)


def _github_client_from_config(config):
    if config.github_app_id and config.github_app_installation_id:
        private_key = config.github_app_private_key
        if not private_key and config.github_app_private_key_path:
            private_key = config.github_app_private_key_path.read_text(encoding="utf-8")
        if private_key:
            return GitHubClient.from_github_app_installation(
                app_id=config.github_app_id,
                private_key=private_key,
                installation_id=config.github_app_installation_id,
            )
    return GitHubClient(config.github_token)


if __name__ == "__main__":
    raise SystemExit(main())
