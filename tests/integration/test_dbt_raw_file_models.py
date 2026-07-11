"""End-to-end test: dbt itself ingests a declared source FILE (raw file models).

A UMF with ``source: {kind: delimited, path: <real csv>}`` emits ``raw_<t>`` as a
file-reading dbt model (duckdb ``read_csv`` -- the local-parity lane for the
Databricks ``read_files`` rendering). ``dbt build`` must then perform the raw
landing itself -- no pre-created raw table -- and the cast model builds on top:

  * ``raw_metrics`` is materialized all-VARCHAR with the ``_source_file`` /
    ``_load_ts`` raw-contract meta columns;
  * the typed ``metrics`` model applies the declared casts (DATE NULL-on-failure).

dbt-core + dbt-duckdb are test/dev-only; ``importorskip`` keeps the suite green
where the dbt stack is absent. Slow (a real dbt subprocess).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.no_spark, pytest.mark.slow]

duckdb = pytest.importorskip("duckdb", reason="duckdb required for dbt e2e")
pytest.importorskip("dbt", reason="dbt-core required for dbt e2e")
pytest.importorskip("dbt.adapters.duckdb", reason="dbt-duckdb required for dbt e2e")

from tablespec.dbt import DbtRunner  # noqa: E402
from tablespec.models.umf import UMF  # noqa: E402


@pytest.fixture(autouse=True)
def _force_local_emit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the duckdb subprocess lane even when the suite runs on Databricks."""
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)


_UMF_YAML = """
version: "1.0"
table_name: metrics
description: raw-file-model e2e fixture (dbt ingests the CSV itself).
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
  - name: meta_source_name
    data_type: VARCHAR
    source: metadata
  - name: meta_load_dt
    data_type: DATETIME
    source: metadata
  - name: meta_checksum
    data_type: VARCHAR
    source: metadata
"""

_CSV = (
    "metric_id,amount,as_of_date,label\n"
    "1,100.50,20240101,alpha\n"
    "2,250.00,not-a-date,beta\n"
)


def _umf(csv_path: Path) -> UMF:
    data = yaml.safe_load(_UMF_YAML)
    data["source"] = {
        "kind": "delimited",
        "delimiter": ",",
        "header": True,
        "quote_char": '"',
        "path": str(csv_path),
    }
    return UMF.model_validate(data)


def test_dbt_build_ingests_declared_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "metrics.csv"
    csv_path.write_text(_CSV)
    project_dir = tmp_path / "project"

    runner = DbtRunner()
    project = runner.emit(
        _umf(csv_path),
        project_dir,
        dialect="duckdb",
        target="duckdb",
    )
    # The landing is a file-reading MODEL; nothing is declared as a dbt source.
    assert (project_dir / "models" / "raw_metrics.sql").exists()
    assert not (project_dir / "models" / "sources.yml").exists()

    # No raw table is pre-created: dbt build performs the landing itself.
    result = runner.build(project)
    assert result.success, (
        f"dbt build should ingest the CSV (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )

    con = duckdb.connect(str(project_dir / "tablespec.duckdb"))
    try:
        con.execute("SET TimeZone='UTC'")
        raw_catalog = dict(
            con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name='raw_metrics' ORDER BY ordinal_position"
            ).fetchall()
        )
        assert raw_catalog, "raw_metrics was not materialized by dbt build"
        for col in ("metric_id", "amount", "as_of_date", "label"):
            assert raw_catalog[col] == "VARCHAR", raw_catalog
        assert raw_catalog["_source_file"] == "VARCHAR", raw_catalog
        assert raw_catalog["_load_ts"] == "TIMESTAMP", raw_catalog

        # Per-row provenance: the bare name of the row's source file.
        src_files = con.execute(
            "SELECT DISTINCT _source_file FROM raw_metrics"
        ).fetchall()
        assert src_files == [("metrics.csv",)]

        # Provenance (source: metadata) columns are never read from the file --
        # the raw model synthesizes them and the typed model casts them.
        meta = con.execute(
            "SELECT DISTINCT meta_source_name, meta_load_dt IS NOT NULL, "
            "meta_checksum FROM metrics"
        ).fetchall()
        assert meta == [("metrics.csv", True, None)]
        typed = dict(
            con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name='metrics'"
            ).fetchall()
        )
        assert typed["meta_load_dt"] == "TIMESTAMP", typed

        # The typed model built on top of the file-read landing.
        rows = dict(
            con.execute(
                "SELECT metric_id, as_of_date FROM metrics ORDER BY metric_id"
            ).fetchall()
        )
        assert len(rows) == 2, rows
        assert str(rows[1]) == "2024-01-01", rows
        assert rows[2] is None, f"unparseable date must be NULL-on-failure: {rows}"
    finally:
        con.close()

    # The build's validations (contracts + tests) surface as a report parsed
    # from REAL dbt run_results.json -- schema-drift protection for the parser.
    report = result.validation_report()
    assert report.success, report.as_dict()
    types = {r.expectation_type for r in report.results}
    assert "dbt_model" in types
    assert any(t.startswith("dbt_test:") for t in types), types
    unique = next(r for r in report.results if r.expectation_type == "dbt_test:unique")
    assert unique.column_name == "metric_id"

    from tablespec.validation import write_validation_report

    json_path, html_path = write_validation_report(report, tmp_path / "reports")
    assert json_path.exists()
    assert "expectations passed" in html_path.read_text()


def test_dbt_build_ingests_glob_with_filename_pattern(tmp_path: Path) -> None:
    """A glob path + filename_pattern: the raw model reads only matching files
    and regexp-extracts the filename capture into its column."""
    (tmp_path / "orders_20240101.csv").write_text("order_id,label\n1,alpha\n")
    (tmp_path / "orders_20240102.csv").write_text("order_id,label\n2,beta\n")
    (tmp_path / "ignore_me.csv").write_text("order_id,label\n99,nope\n")

    umf = UMF.model_validate(
        {
            "version": "1.0",
            "table_name": "orders",
            "ingestion": {"mode": "snapshot"},
            "source": {
                "kind": "delimited",
                "delimiter": ",",
                "header": True,
                "quote_char": '"',
                "path": str(tmp_path / "*.csv"),
                "filename_pattern": {
                    "regex": r"orders_(\d{8})\.csv",
                    "captures": {1: "file_date"},
                },
            },
            "columns": [
                {
                    "name": "order_id",
                    "data_type": "INTEGER",
                    "nullable": {"default": False},
                },
                {
                    "name": "label",
                    "data_type": "VARCHAR",
                    "length": 32,
                    "nullable": {"default": True},
                },
                {
                    "name": "file_date",
                    "data_type": "VARCHAR",
                    "length": 8,
                    "source": "filename",
                    "nullable": {"default": True},
                },
            ],
        }
    )

    project_dir = tmp_path / "project"
    runner = DbtRunner()
    project = runner.emit(umf, project_dir, dialect="duckdb", target="duckdb")
    result = runner.build(project)
    assert result.success, f"{result.stdout}\n{result.stderr}"

    con = duckdb.connect(str(project_dir / "tablespec.duckdb"))
    try:
        rows = con.execute(
            "SELECT order_id, file_date, _source_file FROM raw_orders ORDER BY order_id"
        ).fetchall()
        # The regex filter excluded ignore_me.csv; captures landed per row.
        assert rows == [
            ("1", "20240101", "orders_20240101.csv"),
            ("2", "20240102", "orders_20240102.csv"),
        ]
        typed = con.execute(
            "SELECT order_id, file_date FROM orders ORDER BY order_id"
        ).fetchall()
        assert typed == [(1, "20240101"), (2, "20240102")]
    finally:
        con.close()
