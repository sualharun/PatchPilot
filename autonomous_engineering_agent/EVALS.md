# Evaluations

Evaluations turn the project from a demo into an engineering system. They answer:

- How often does the agent solve issues?
- How expensive is a run?
- How many iterations are needed?
- Which failure modes dominate?

## Manifest

Create a YAML file:

```yaml
max_iterations: 5
tasks:
  - name: small-bug-1
    issue: owner/repo#123
    model: gpt-4.1
    open_pr: false
```

Run it:

```bash
agent eval --manifest evals/example.yaml --output eval-report.json
```

The report contains task count, solve count, solve rate, runtime, and per-task exit codes.

## Curated Synthetic Benchmark

The repo includes a deterministic local benchmark:

```bash
agent eval-synthetic --manifest evals/synthetic/python_bugs.yaml --output synthetic-eval-report.json
```

These tasks create tiny Python repositories, seed issue context, apply deterministic benchmark patches through the same agent pipeline, run tests, and write replay artifacts. They are useful for smoke-testing the orchestration before spending model tokens on live GitHub issues.

## Resume-Grade Benchmarking

For a strong public benchmark, curate 20-50 Python tasks:

- 10 simple bug fixes
- 10 test failures
- 10 documentation or small feature issues
- 5-20 harder multi-file bugs

Record:

- pass/fail status
- runtime
- iterations
- commands run
- final diff size
- failure reason
- model and cost

Publish the report in the README once the benchmark is stable.
