import subprocess
from pathlib import Path

from agent.domain.value_objects import IssueRef
from agent.infrastructure.repository.git import clone_repository, exclude_build_artifacts


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    ).stdout


def _origin_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "pricing.py").write_text("def total():\n    return 0\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "-c", "user.email=t@e.invalid", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return origin


def test_commit_all_excludes_env_and_cache_artifacts(tmp_path):
    """A virtualenv created by the setup step must not land in the pull request."""
    origin = _origin_repo(tmp_path)
    issue = IssueRef(owner="octo", repo="origin", number=1, url="https://github.com/octo/origin/issues/1")

    workspace = clone_repository(str(origin), issue, base_dir=tmp_path / "work")

    # What a setup step leaves behind alongside the agent's real edit.
    (workspace.path / "pricing.py").write_text("def total():\n    return 1\n", encoding="utf-8")
    for artifact in (".venv/bin", "__pycache__", ".pytest_cache", "node_modules/pkg"):
        directory = workspace.path / artifact
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "junk").write_text("x", encoding="utf-8")
    (workspace.path / "stale.pyc").write_text("x", encoding="utf-8")

    workspace.commit_all("Fix the thing")

    committed = _git(workspace.path, "show", "--name-only", "--pretty=format:", "HEAD").split()

    assert "pricing.py" in committed
    assert not [path for path in committed if path.startswith((".venv/", "__pycache__/", ".pytest_cache/"))]
    assert not [path for path in committed if path.startswith("node_modules/")]
    assert "stale.pyc" not in committed


def test_exclude_build_artifacts_is_idempotent_and_keeps_existing_rules(tmp_path):
    origin = _origin_repo(tmp_path)
    issue = IssueRef(owner="octo", repo="origin", number=2, url="https://github.com/octo/origin/issues/2")
    workspace = clone_repository(str(origin), issue, base_dir=tmp_path / "work")

    exclude_file = workspace.path / ".git" / "info" / "exclude"
    exclude_file.write_text("# pre-existing\nmy-local-notes.txt\n", encoding="utf-8")

    exclude_build_artifacts(workspace.path)
    once = exclude_file.read_text(encoding="utf-8")
    exclude_build_artifacts(workspace.path)
    twice = exclude_file.read_text(encoding="utf-8")

    assert once == twice, "re-running must not duplicate patterns"
    assert "my-local-notes.txt" in twice, "must not clobber rules that were already there"
    assert ".venv/" in twice


def test_tracked_files_are_still_committed_even_if_they_match_a_pattern(tmp_path):
    """Ignore rules apply only to untracked paths, so a repo that tracks one still works."""
    origin = _origin_repo(tmp_path)
    (origin / "node_modules").mkdir()
    (origin / "node_modules" / "vendored.js").write_text("// vendored\n", encoding="utf-8")
    _git(origin, "add", "-f", "node_modules/vendored.js")
    _git(origin, "-c", "user.email=t@e.invalid", "-c", "user.name=t", "commit", "-q", "-m", "vendor")

    issue = IssueRef(owner="octo", repo="origin", number=3, url="https://github.com/octo/origin/issues/3")
    workspace = clone_repository(str(origin), issue, base_dir=tmp_path / "work")

    (workspace.path / "node_modules" / "vendored.js").write_text("// patched\n", encoding="utf-8")
    workspace.commit_all("Patch the vendored file")

    committed = _git(workspace.path, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert "node_modules/vendored.js" in committed


def test_write_file_ends_with_a_newline(tmp_path):
    """Models omit the trailing newline; without one every rewrite dirties the diff."""
    from agent.infrastructure.repository.tools import RepoTools

    tools = RepoTools(repo_path=tmp_path, sandbox=None)

    tools.write_file("a.py", "x = 1")
    assert (tmp_path / "a.py").read_text() == "x = 1\n"

    tools.write_file("b.py", "x = 1\n")
    assert (tmp_path / "b.py").read_text() == "x = 1\n", "must not double up"

    tools.write_file("c.py", "")
    assert (tmp_path / "c.py").read_text() == "", "an empty file stays empty"
