from agent.persistence import RunRecord, RunStore


def test_run_status_persistence_round_trip():
    store = RunStore("sqlite:///:memory:")
    run_id = store.start_run(
        RunRecord(
            issue_url="https://github.com/octo/example/issues/1",
            repo="octo/example",
            branch="agent/fix-issue-1-20260101000000",
            model="test-model",
        )
    )

    store.finish_run(
        run_id,
        "failed_tests",
        iterations=2,
        commands=[{"command": "python -m pytest", "exit_code": 1}],
        tool_calls=[{"name": "apply_patch", "ok": True}],
        patches=["diff --git a/a.py b/a.py"],
        test_results=[{"command": "python -m pytest", "exit_code": 1}],
        token_usage={"total_tokens": 123, "estimated_cost_usd": 0.01},
        estimated_cost_usd=0.01,
    )
    saved = store.get_run(run_id)

    assert saved["status"] == "failed_tests"
    assert saved["iterations"] == 2
    assert saved["commands"][0]["command"] == "python -m pytest"
    assert saved["tool_calls"][0]["name"] == "apply_patch"
    assert saved["patches"] == ["diff --git a/a.py b/a.py"]
    assert saved["token_usage"]["total_tokens"] == 123
    assert saved["estimated_cost_usd"] == 0.01
    assert store.list_runs()[0]["id"] == run_id


def test_queued_run_fields_and_status_lookup_round_trip():
    store = RunStore("sqlite:///:memory:")
    queued_id = store.start_run(
        RunRecord(
            issue_url="https://github.com/octo/example/issues/2",
            repo="octo/example",
            branch="agent/fix-issue-2-20260101000000",
            model="gpt-4.1",
            status="queued",
            max_iterations=7,
            open_pr=True,
        )
    )
    store.start_run(
        RunRecord(
            issue_url="https://github.com/octo/example/issues/3",
            repo="octo/example",
            branch="agent/fix-issue-3-20260101000000",
            model="gpt-4.1",
            status="success",
        )
    )

    queued = store.list_runs_by_status("queued")

    assert [run["id"] for run in queued] == [queued_id]
    assert queued[0]["max_iterations"] == 7
    assert queued[0]["open_pr"] is True


def test_claim_next_queued_run_sets_worker_lease_and_attempt():
    store = RunStore("sqlite:///:memory:")
    run_id = store.start_run(
        RunRecord(
            issue_url="https://github.com/octo/example/issues/4",
            repo="octo/example",
            branch="agent/fix-issue-4-20260101000000",
            model="gpt-4.1",
            status="queued",
        )
    )

    claimed = store.claim_next_queued_run(worker_id="worker-a", lease_seconds=60, max_attempts=3)
    second_claim = store.claim_next_queued_run(worker_id="worker-b", lease_seconds=60, max_attempts=3)

    assert claimed is not None
    assert claimed["id"] == run_id
    assert claimed["status"] == "running"
    assert claimed["worker_id"] == "worker-a"
    assert claimed["attempts"] == 1
    assert claimed["leased_until"]
    assert second_claim is None


def test_audit_event_persistence_round_trip():
    store = RunStore("sqlite:///:memory:")
    event_id = store.add_audit_event(
        actor="alex",
        event="run.queued",
        target="https://github.com/octo/example/issues/1",
        metadata={"model": "gpt-4.1"},
    )

    event = store.list_audit_events()[0]
    assert event["id"] == event_id
    assert event["actor"] == "alex"
    assert event["event"] == "run.queued"
    assert event["metadata"]["model"] == "gpt-4.1"
