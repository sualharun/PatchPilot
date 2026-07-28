
from agent.evals import load_manifest, summarize_eval
from agent.evals_synthetic import load_synthetic_manifest


def test_load_eval_manifest(tmp_path):
    manifest = tmp_path / "eval.yaml"
    manifest.write_text(
        """
max_iterations: 3
tasks:
  - name: tiny
    issue: owner/repo#1
    model: fake-model
""",
        encoding="utf-8",
    )

    tasks = load_manifest(manifest)

    assert len(tasks) == 1
    assert tasks[0].name == "tiny"
    assert tasks[0].max_iterations == 3


def test_summarize_eval_computes_solve_rate():
    report = summarize_eval(
        [
            {"status": "success", "runtime_seconds": 1.0},
            {"status": "failed", "runtime_seconds": 3.0},
        ],
        runtime_seconds=4.0,
    )

    assert report["task_count"] == 2
    assert report["solved_count"] == 1
    assert report["solve_rate"] == 0.5
    assert report["median_task_runtime_seconds"] == 2.0


def test_load_synthetic_manifest(tmp_path):
    manifest = tmp_path / "synthetic.yaml"
    manifest.write_text(
        """
tasks:
  - name: tiny
    issue_number: 1
    title: Broken add
    body: Fix add
    files:
      calc.py: |
        def add(a, b):
            return a - b
    patch: |
      diff --git a/calc.py b/calc.py
      --- a/calc.py
      +++ b/calc.py
      @@ -1,2 +1,2 @@
       def add(a, b):
      -    return a - b
      +    return a + b
""",
        encoding="utf-8",
    )

    tasks = load_synthetic_manifest(manifest)

    assert tasks[0].name == "tiny"
    assert tasks[0].issue_number == 1
    assert "calc.py" in tasks[0].files
