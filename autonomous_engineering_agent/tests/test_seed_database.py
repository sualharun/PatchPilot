import sqlite3

import pytest

from agent.infrastructure.db.seeds import seed_database
from agent.persistence import RunStore


def test_seed_database_is_idempotent_and_populates_core_tables():
    store = RunStore("sqlite:///:memory:")

    first = seed_database(store, profile="local-dev")
    second = seed_database(store, profile="local-dev")

    assert first["repositories"] == 3
    assert first["runs_created"] == 3
    assert second["runs_created"] == 0
    assert len(store.list_repositories()) >= 3
    assert len(store.list_provider_keys()) == 2
    assert store.get_github_connection()["login"] == "alex-morgan"
    assert len(store.list_runs()) == 3


def test_sqlite_foreign_keys_are_enforced():
    store = RunStore("sqlite:///:memory:")

    with pytest.raises(sqlite3.IntegrityError):
        store._execute(
            """
            INSERT INTO run_commands (run_id, phase, command, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [999, "test", "pytest -q", "2026-01-01T00:00:00Z"],
        )

