import subprocess

from agent.config import AgentConfig
from agent.doctor import DoctorCheck, format_doctor, has_errors, run_doctor


def test_format_doctor_and_has_errors():
    checks = [
        DoctorCheck("git", "ok", "found"),
        DoctorCheck("docker", "error", "missing"),
    ]

    assert has_errors(checks) is True
    assert "[OK] git: found" in format_doctor(checks)
    assert "[ERROR] docker: missing" in format_doctor(checks)


def test_run_doctor_reports_missing_tools(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)

    checks = run_doctor(AgentConfig(database_url="sqlite:///:memory:"))

    statuses = {check.name: check.status for check in checks}
    assert statuses["executable:git"] == "error"
    assert statuses["executable:docker"] == "error"
    assert statuses["llm_api_key"] == "warn"


def test_run_doctor_checks_docker_daemon(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    checks = run_doctor(AgentConfig(database_url="sqlite:///:memory:"))

    statuses = {check.name: check.status for check in checks}
    assert statuses["docker_daemon"] == "ok"


def test_run_doctor_can_skip_database_check(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    checks = run_doctor(
        AgentConfig(database_url="postgresql://patchpilot:patchpilot@postgres:5432/patchpilot"),
        check_database=False,
    )

    database_check = next(check for check in checks if check.name == "database")
    assert database_check.status == "warn"
    assert "skipped" in database_check.message


def test_run_doctor_redacts_database_password(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    class FakeRunStore:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr("agent.doctor.RunStore", FakeRunStore)

    checks = run_doctor(
        AgentConfig(database_url="postgresql://patchpilot:super-secret@postgres:5432/patchpilot"),
    )

    database_check = next(check for check in checks if check.name == "database")
    assert database_check.status == "ok"
    assert "super-secret" not in database_check.message
    assert "patchpilot:***@" in database_check.message


def test_production_doctor_fails_closed_without_required_settings(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    checks = run_doctor(
        AgentConfig(
            database_url="sqlite:///:memory:",
            dashboard_auth_enabled=False,
            dashboard_secure_cookies=False,
            dashboard_demo_data_enabled=True,
            production=True,
        )
    )

    production_check = next(check for check in checks if check.name == "production_config")
    assert production_check.status == "error"
    assert "PostgreSQL" in production_check.message
    assert "DASHBOARD_AUTH_ENABLED" in production_check.message
