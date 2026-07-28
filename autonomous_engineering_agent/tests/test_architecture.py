import ast
from pathlib import Path

from agent.application.use_cases.queue_run import QueueRunCommand, QueueRunUseCase
from agent.domain.enums import RunStatus
from agent.domain.models import IssueRef, QueuedRun, RepositoryRef
from agent.domain.services import branch_name_for_issue, parse_issue_ref
from agent.infrastructure.db.repositories import SqlAuditLog, SqlRunRepository
from agent.persistence import RunStore

PROJECT_ROOT = Path(__file__).parents[1]


def test_queue_run_use_case_persists_run_and_audit_event():
    store = RunStore("sqlite:///:memory:")
    issue = IssueRef(
        repository=RepositoryRef(owner="octo", name="example"),
        number=12,
        url="https://github.com/octo/example/issues/12",
    )

    result = QueueRunUseCase(SqlRunRepository(store), SqlAuditLog(store)).execute(
        QueueRunCommand(
            issue=issue,
            model="gpt-4.1-mini",
            max_iterations=0,
            open_pr=False,
            requested_by="alex",
        )
    )

    run = store.get_run(result.run_id)
    event = store.list_audit_events(limit=1)[0]

    assert run["status"] == "queued"
    assert run["repo"] == "octo/example"
    assert run["max_iterations"] == 1
    assert result.branch.startswith("agent/fix-issue-12-")
    assert event["event"] == "run.queued"
    assert event["metadata"]["run_id"] == result.run_id


def test_domain_owns_run_invariants_and_naming_policy():
    issue = parse_issue_ref("octo/example#9")
    run = QueuedRun(
        issue=issue,
        model="gpt-4.1-mini",
        branch=branch_name_for_issue(issue.number, 1234),
        max_iterations=2,
        open_pr=False,
        requested_by="alex",
    )

    run.mark_running()
    run.finish(RunStatus.SUCCESS)

    assert run.status is RunStatus.SUCCESS
    assert run.branch == "agent/fix-issue-9-1234"


def test_domain_layer_has_no_outward_dependencies():
    violations = _forbidden_imports(
        PROJECT_ROOT / "agent" / "domain",
        forbidden_prefixes=("agent.application", "agent.infrastructure", "agent.interfaces"),
        forbidden_modules={"fastapi", "requests", "psycopg", "sqlite3", "subprocess", "yaml", "dotenv"},
    )

    assert violations == []


def test_application_layer_depends_only_on_domain_and_application():
    violations = _forbidden_imports(
        PROJECT_ROOT / "agent" / "application",
        forbidden_prefixes=("agent.infrastructure", "agent.interfaces"),
        forbidden_modules={"fastapi", "requests", "psycopg", "sqlite3", "subprocess", "kafka"},
    )

    assert violations == []


def test_infrastructure_never_imports_inbound_interfaces():
    violations = _forbidden_imports(
        PROJECT_ROOT / "agent" / "infrastructure",
        forbidden_prefixes=("agent.interfaces",),
        forbidden_modules=set(),
    )

    assert violations == []


def test_http_adapter_uses_application_queries_instead_of_sql_store():
    source = (PROJECT_ROOT / "agent" / "interfaces" / "http" / "dashboard.py").read_text(encoding="utf-8")

    assert "RunStore" not in source
    assert "store.list_" not in source
    assert "store.get_" not in source


def _forbidden_imports(
    root: Path,
    *,
    forbidden_prefixes: tuple[str, ...],
    forbidden_modules: set[str],
) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                top_level = name.split(".", 1)[0]
                if name.startswith(forbidden_prefixes) or top_level in forbidden_modules:
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}:{getattr(node, 'lineno', 0)} imports {name}")
    return violations
