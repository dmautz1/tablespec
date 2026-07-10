"""End-to-end test for the dbt runnable-product target (ADR-008 §4 item 6).

Proves the opt-in execution wiring: ``get_emitter('dbt')`` + :class:`DbtRunner`
materialize a runnable dbt project from a UMF, ``DbtRunner.build`` runs it via
dbt-duckdb, and the run produces the ingested model with the declared casts
applied:

  * the dbt ``build`` invocation returns success;
  * the expected model/table is produced in the duckdb target;
  * a declared DATE cast is correctly typed AND NULL-on-failure in the output
    (an unparseable raw date lands as NULL, a valid one as a real DATE).

dbt-core + dbt-duckdb are test/dev-only; this module ``importorskip``s them so the
suite stays green where the dbt stack is absent. Slow (a real dbt subprocess) and
JVM-free. Output is isolated to ``tmp_path`` (the DuckDB db + dbt ``target/``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.no_spark, pytest.mark.slow]

duckdb = pytest.importorskip("duckdb", reason="duckdb required for dbt runner e2e")
pytest.importorskip("dbt", reason="dbt-core required for dbt runner e2e")
pytest.importorskip(
    "dbt.adapters.duckdb", reason="dbt-duckdb required for dbt runner e2e"
)

from tablespec.dbt import DbtRunner, get_emitter  # noqa: E402
from tablespec.dbt.emitter import DbtEmitter, EmittedProject  # noqa: E402
from tablespec.models.umf import UMF  # noqa: E402


@pytest.fixture(autouse=True)
def _force_local_emit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the duckdb subprocess lane even when the suite runs on Databricks.

    Both the emit defaults (``tablespec.dialects.resolve_emit_defaults``) and
    ``DbtRunner``'s in-process-vs-subprocess choice key off
    ``DATABRICKS_RUNTIME_VERSION``; this e2e explicitly exercises the duckdb
    subprocess path.
    """
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)

# A self-contained UMF exercising the declared casts: an INTEGER PK, a DECIMAL, a
# DATE (the cast under test -- NULL-on-failure), and a not-null VARCHAR.
_UMF_YAML = """
version: "1.0"
table_name: metrics
description: dbt-runner e2e fixture (INTEGER/DECIMAL/DATE/VARCHAR casts).
primary_key:
  - metric_id
ingestion:
  mode: incremental
  order_by:
    - _load_ts
columns:
  - name: metric_id
    data_type: INTEGER
    nullable:
      default: false
  - name: amount
    data_type: DECIMAL
    precision: 18
    scale: 2
    nullable:
      default: true
  - name: as_of_date
    data_type: DATE
    format: YYYYMMDD
    nullable:
      default: true
  - name: label
    data_type: VARCHAR
    length: 32
    nullable:
      default: false
"""

# Raw columns the all-STRING landing table carries (data columns + audit cols the
# dedup window orders by). One row has a valid date, one has an unparseable date
# (-> the DATE cast must yield NULL, not error).
_RAW_DATA_COLS = ["metric_id", "amount", "as_of_date", "label"]
_RAW_ROWS = [
    ("1", "100.50", "20240101", "alpha"),
    ("2", "250.00", "not-a-date", "beta"),
]


def _umf() -> UMF:
    return UMF(**yaml.safe_load(_UMF_YAML))


def _load_raw(db: Path) -> None:
    """Create the all-VARCHAR ``raw_metrics`` landing table + audit cols and rows."""
    con = duckdb.connect(str(db))
    try:
        con.execute("SET TimeZone='UTC'")
        coldefs = ", ".join(f'"{c}" VARCHAR' for c in _RAW_DATA_COLS)
        coldefs += ', "_source_file" VARCHAR, "_load_ts" TIMESTAMP'
        con.execute(f"CREATE TABLE raw_metrics ({coldefs})")
        for i, row in enumerate(_RAW_ROWS):
            placeholders = ", ".join("?" for _ in _RAW_DATA_COLS)
            con.execute(
                f"INSERT INTO raw_metrics VALUES ({placeholders}, 'seed.csv', "
                f"TIMESTAMP '2024-01-0{i + 1} 00:00:00')",
                list(row),
            )
    finally:
        con.close()


def test_get_emitter_returns_dbt_emitter() -> None:
    """The backend selector returns the dbt emitter for backend='dbt'."""
    emitter = get_emitter("dbt")
    assert isinstance(emitter, DbtEmitter)
    assert emitter.backend == "dbt"


def test_dbt_runner_emit_then_build(tmp_path: Path) -> None:
    """ADR-008 §4.6 acceptance: emit a dbt project from a UMF, run it via
    dbt-duckdb, and assert the model is produced with the declared casts applied
    (DATE typed + NULL-on-failure)."""
    project_dir = tmp_path / "project"
    runner = DbtRunner()

    # (1) emit -- pure-Python, no dbt needed yet.
    project = runner.emit(_umf(), project_dir, project_name="runner_e2e")
    assert isinstance(project, EmittedProject)
    assert (project_dir / "models" / "metrics.sql").exists()
    assert (project_dir / "dbt_project.yml").exists()

    # The DuckDB db the runner targets (isolated under the project dir).
    db = project_dir / "tablespec.duckdb"
    _load_raw(db)

    # (2) run -- real dbt build via dbt-duckdb.
    result = runner.build(project)
    assert result.success, (
        f"dbt build should succeed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert result.duckdb_path == db

    # (3) assert the model table exists and the declared casts ran.
    con = duckdb.connect(str(db))
    try:
        con.execute("SET TimeZone='UTC'")
        # The ingested model materialized as a relation named after the table.
        catalog = dict(
            con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name='metrics' ORDER BY ordinal_position"
            ).fetchall()
        )
        assert catalog, "the 'metrics' model was not produced in the duckdb target"
        # The DATE cast is correctly typed in the output relation.
        assert catalog["as_of_date"] == "DATE", catalog
        assert catalog["metric_id"] == "INTEGER", catalog
        assert catalog["amount"] == "DECIMAL(18,2)", catalog

        # NULL-on-failure: the valid raw date casts to a real DATE; the
        # unparseable one casts to NULL (try_strptime returns NULL, no error).
        rows = dict(
            con.execute(
                "SELECT metric_id, as_of_date FROM metrics ORDER BY metric_id"
            ).fetchall()
        )
        assert rows[1] is not None, f"valid date should cast to a DATE: {rows}"
        assert str(rows[1]) == "2024-01-01", rows
        assert rows[2] is None, f"unparseable date must be NULL-on-failure: {rows}"
    finally:
        con.close()


def test_dbt_runner_parse_only(tmp_path: Path) -> None:
    """A lighter signal that emission + invocation is wired: ``dbt parse`` of the
    emitted project succeeds (no raw data needed, validates the project is real)."""
    project = DbtRunner().emit(_umf(), tmp_path / "proj", project_name="parse_e2e")
    result = DbtRunner().invoke(project, "parse")
    assert result.success, (
        f"dbt parse of the emitted project should succeed:\n"
        f"{result.stdout}\n{result.stderr}"
    )
