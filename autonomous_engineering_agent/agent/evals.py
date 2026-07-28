from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class EvalTask:
    name: str
    issue: str
    model: str
    max_iterations: int = 5
    open_pr: bool = False


def load_manifest(path: Path) -> list[EvalTask]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tasks_raw = raw.get("tasks", raw if isinstance(raw, list) else [])
    tasks: list[EvalTask] = []
    for index, item in enumerate(tasks_raw):
        tasks.append(
            EvalTask(
                name=str(item.get("name") or f"task-{index + 1}"),
                issue=str(item["issue"]),
                model=str(item["model"]),
                max_iterations=int(item.get("max_iterations", raw.get("max_iterations", 5))),
                open_pr=bool(item.get("open_pr", False)),
            )
        )
    return tasks


def run_eval_manifest(manifest_path: Path, output_path: Path) -> dict[str, Any]:
    from .cli import run_command

    tasks = load_manifest(manifest_path)
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    for task in tasks:
        task_started = time.monotonic()
        exit_code = run_command(
            argparse.Namespace(
                issue=task.issue,
                model=task.model,
                max_iterations=task.max_iterations,
                open_pr="true" if task.open_pr else "false",
                env_file=".env",
            )
        )
        status = "success" if exit_code == 0 else "failed"
        results.append(
            {
                "name": task.name,
                "issue": task.issue,
                "model": task.model,
                "status": status,
                "exit_code": exit_code,
                "runtime_seconds": round(time.monotonic() - task_started, 3),
            }
        )
    report = summarize_eval(results, round(time.monotonic() - started, 3))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def summarize_eval(results: list[dict[str, Any]], runtime_seconds: float) -> dict[str, Any]:
    runtimes = [float(result["runtime_seconds"]) for result in results]
    solved = sum(1 for result in results if result["status"] == "success")
    total = len(results)
    return {
        "schema_version": 1,
        "task_count": total,
        "solved_count": solved,
        "solve_rate": round(solved / total, 4) if total else 0,
        "total_runtime_seconds": runtime_seconds,
        "median_task_runtime_seconds": statistics.median(runtimes) if runtimes else 0,
        "results": results,
    }
