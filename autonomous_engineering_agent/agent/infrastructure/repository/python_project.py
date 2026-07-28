from __future__ import annotations

from pathlib import Path

PYTHON_PROJECT_MARKERS = ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile", "poetry.lock")


def detect_install_commands(repo_path: Path) -> list[str]:
    commands: list[str] = ["python -m venv .venv", ".venv/bin/python -m pip install --upgrade pip"]
    if (repo_path / "poetry.lock").exists() or _pyproject_uses_poetry(repo_path):
        commands.extend([".venv/bin/python -m pip install poetry", ".venv/bin/poetry install"])
    elif (repo_path / "Pipfile").exists():
        commands.extend([".venv/bin/python -m pip install pipenv", ".venv/bin/pipenv install --dev"])
    elif (repo_path / "requirements.txt").exists():
        commands.append(".venv/bin/python -m pip install -r requirements.txt")
        if (repo_path / "setup.py").exists() or (repo_path / "pyproject.toml").exists():
            commands.append(".venv/bin/python -m pip install -e .")
    elif (repo_path / "setup.py").exists() or (repo_path / "pyproject.toml").exists():
        commands.append(".venv/bin/python -m pip install -e .")
    else:
        commands.append(".venv/bin/python -m pip install pytest")
    return commands


def detect_test_commands(repo_path: Path) -> list[str]:
    if (repo_path / "pytest.ini").exists() or (repo_path / "conftest.py").exists() or (repo_path / "tests").exists():
        return [".venv/bin/python -m pytest"]
    if (repo_path / "setup.cfg").exists() or (repo_path / "pyproject.toml").exists():
        return [".venv/bin/python -m pytest"]
    return [".venv/bin/python -m unittest discover"]


def summarize_project(repo_path: Path, max_files: int = 120) -> str:
    files = []
    for path in sorted(repo_path.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            rel = path.relative_to(repo_path)
            if rel.parts and rel.parts[0] in {".venv", "venv", "dist", "build", "__pycache__"}:
                continue
            files.append(str(rel))
        if len(files) >= max_files:
            break
    markers = [marker for marker in PYTHON_PROJECT_MARKERS if (repo_path / marker).exists()]
    return f"Project markers: {markers or 'none'}\nFiles:\n" + "\n".join(files)


def _pyproject_uses_poetry(repo_path: Path) -> bool:
    pyproject = repo_path / "pyproject.toml"
    return pyproject.exists() and "tool.poetry" in pyproject.read_text(encoding="utf-8", errors="replace")
