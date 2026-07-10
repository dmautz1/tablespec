"""Fixtures for the dbt roadmap (contracts/tests/seeds) tests."""

import pytest


@pytest.fixture(autouse=True)
def _force_local_emit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin emit defaults to the local (duckdb) lane.

    Unspecified dialect/target now resolve by environment
    (``tablespec.dialects.resolve_emit_defaults``), and this suite's
    duckdb/golden assertions must stay deterministic even when the suite itself
    runs on a Databricks cluster.
    """
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)
