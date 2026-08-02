import subprocess
from pathlib import Path

from agent.config import SandboxConfig
from agent.infrastructure.sandbox.docker import _host_visible_path
from agent.sandbox import DockerSandbox


def test_command_timeout_returns_timeout_result(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise subprocess.TimeoutExpired(args, timeout=1, output=b"partial", stderr=b"slow")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = DockerSandbox(SandboxConfig()).run(tmp_path, "python -m pytest", timeout_seconds=1)

    assert result.exit_code == 124
    assert result.timed_out is True
    assert "partial" in result.stdout
    assert "Command timed out after 1s" in result.stderr
    assert any(call[:3] == ["docker", "rm", "-f"] for call in calls)


def test_sandbox_rejects_shell_control_operators(tmp_path):
    sandbox = DockerSandbox(SandboxConfig())

    try:
        sandbox.run(tmp_path, "python -m pytest; rm -rf .")
    except ValueError as exc:
        assert "Shell control operators" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_non_install_commands_default_to_no_network(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    DockerSandbox(SandboxConfig()).run(tmp_path, "python -m pytest")

    docker_run_call = next(call for call in calls if call[:2] == ["docker", "run"])
    assert "--network" in docker_run_call
    assert docker_run_call[docker_run_call.index("--network") + 1] == "none"
    assert not any(call[:2] == ["docker", "network"] for call in calls)


def test_install_commands_use_dedicated_egress_network(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    DockerSandbox(SandboxConfig()).run(tmp_path, "pip install -e .", needs_network=True)

    docker_run_call = next(call for call in calls if call[:2] == ["docker", "run"])
    assert docker_run_call[docker_run_call.index("--network") + 1] == "patchpilot-sandbox-egress"
    assert any(call[:3] == ["docker", "network", "create"] for call in calls)


def test_host_visible_path_translates_shared_worker_workspace(monkeypatch):
    monkeypatch.setenv("PATCHPILOT_WORKSPACE_ROOT", "/data/workspaces")
    monkeypatch.setenv("PATCHPILOT_HOST_WORKSPACE_ROOT", "/opt/patchpilot/runtime/workspaces")

    translated = _host_visible_path(Path("/data/workspaces/agent-run-123/patchpilot-test-repo"))

    assert str(translated) == "/opt/patchpilot/runtime/workspaces/agent-run-123/patchpilot-test-repo"
