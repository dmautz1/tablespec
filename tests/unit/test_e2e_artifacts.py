"""Unit tests for the batch Excel/artifact e2e helpers (no Spark, no dbt build).

Covers the thin wrappers that keep the demo notebook cells to "import + a call":
``excel_specs_from_csvs``, ``umfs_from_excel_dir``, ``specs_from_excel_dir``, and
``emit_artifacts``. dbt/Lakeflow EMISSION is pure Python, so these run without a
dbt install or a Spark session.
"""

from __future__ import annotations

from pathlib import Path

from tablespec.e2e import (
    emit_artifacts,
    excel_specs_from_csvs,
    specs_from_excel_dir,
    umfs_from_excel_dir,
    umfs_from_spec_dir,
)

_PKS = {"orders": ["order_id"]}


def _write_csvs(csv_dir: Path) -> None:
    csv_dir.mkdir(parents=True, exist_ok=True)
    (csv_dir / "orders.csv").write_text(
        "order_id,amount,note\n0001,120.50,hello\n0002,80.00,world\n"
    )
    (csv_dir / "customers.csv").write_text("customer_id,name\n0001,Acme\n0002,Globex\n")


def test_excel_specs_from_csvs_writes_one_workbook_per_table(tmp_path: Path) -> None:
    _write_csvs(tmp_path / "data")
    paths = excel_specs_from_csvs(
        tmp_path / "data", tmp_path / "excel", primary_keys=_PKS
    )
    names = sorted(p.name for p in paths)
    assert names == ["customers.xlsx", "orders.xlsx"]
    assert all(p.exists() for p in paths)


def test_excel_dir_roundtrip_preserves_key_and_ingestion(tmp_path: Path) -> None:
    _write_csvs(tmp_path / "data")
    excel_specs_from_csvs(tmp_path / "data", tmp_path / "excel", primary_keys=_PKS)

    # Excel dir -> UMFs directly.
    umfs = {u.table_name: u for u in umfs_from_excel_dir(tmp_path / "excel")}
    assert set(umfs) == {"orders", "customers"}
    assert umfs["orders"].primary_key == ["order_id"]
    assert umfs["orders"].ingestion is not None
    assert umfs["orders"].ingestion.mode == "incremental"

    # Excel dir -> saved YAML specs -> reloaded (guards the ingestion round-trip).
    specs_from_excel_dir(tmp_path / "excel", tmp_path / "umf")
    reloaded = {
        u.table_name: u
        for u in umfs_from_spec_dir(tmp_path / "umf", data_dir=tmp_path / "data")
    }
    assert reloaded["orders"].primary_key == ["order_id"]
    assert reloaded["orders"].ingestion.mode == "incremental"
    # customers has no declared key -> snapshot (no ingestion incremental).
    assert reloaded["customers"].primary_key is None


def test_specs_from_excel_dir_clears_out_dir(tmp_path: Path) -> None:
    _write_csvs(tmp_path / "data")
    excel_specs_from_csvs(tmp_path / "data", tmp_path / "excel", primary_keys=_PKS)
    umf_dir = tmp_path / "umf"
    umf_dir.mkdir()
    (umf_dir / "stale").mkdir()
    (umf_dir / "stale" / "table.yaml").write_text("junk")

    specs_from_excel_dir(tmp_path / "excel", umf_dir)

    # Destructive: the pre-existing stale spec is gone; only real tables remain.
    assert not (umf_dir / "stale").exists()
    assert (umf_dir / "orders" / "table.yaml").exists()


def test_emit_artifacts_writes_sql_ldp_and_dbt(tmp_path: Path) -> None:
    _write_csvs(tmp_path / "data")
    excel_specs_from_csvs(tmp_path / "data", tmp_path / "excel", primary_keys=_PKS)
    specs_from_excel_dir(tmp_path / "excel", tmp_path / "umf")
    umfs = umfs_from_spec_dir(tmp_path / "umf", data_dir=tmp_path / "data")

    project = emit_artifacts(umfs, tmp_path / "out", dialect="duckdb")

    # DDL for every table.
    for name in ("orders", "customers"):
        assert (tmp_path / "out" / "sql" / f"{name}.ddl.sql").exists()
    # No plan.sql: these are flat ingest tables (no derived columns).
    assert not list((tmp_path / "out" / "sql").glob("*.plan.sql"))
    # Lakeflow + dbt projects emitted.
    assert (tmp_path / "out" / "ldp" / "raw" / "raw_orders.sql").exists()
    assert Path(project.project_dir).exists()
    assert Path(project.project_dir) == tmp_path / "out" / "dbt"
