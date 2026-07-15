"""End-to-end demo test: the notebook's full CSV -> Excel -> UMF -> dbt flow.

Mirrors notebooks/csv-to-dbt-demo exactly, against a SINGLE reused dbt project
(the "same warehouse" across runs):

  1. Month-1 volume files -> generate specs (``umfs_from_csvs`` with inferred
     types + declared primary keys => incremental MERGE) -> export Excel spec
     workbooks -> convert BACK from Excel -> UMF YAML specs -> dbt build ->
     validation report.
  2. Gold: convert the SHIPPED gold Excel spec to UMF, rebuild with the gold
     model, export the report files.
  3. Month swap: REMOVE the month-1 claim files, ADD month-2 (the med feed
     gains ``telehealth_indicator``) -> ``sync_specs_with_csvs`` adds the new
     column -> optional authored gold column (shipped ripple yaml) -> rebuild
     the SAME project -> BOTH months are in the tables (merge), the new column
     appended (on_schema_change), and the gold report reflects everything.
  4. NEGATIVE: syncing/editing specs before the data arrives fails the build.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = [pytest.mark.no_spark, pytest.mark.slow]

duckdb = pytest.importorskip("duckdb", reason="duckdb required for dbt e2e")
pytest.importorskip("dbt", reason="dbt-core required for dbt e2e")
pytest.importorskip("dbt.adapters.duckdb", reason="dbt-duckdb required for dbt e2e")
pytest.importorskip("openpyxl", reason="openpyxl required for the Excel spec path")

from tablespec.authoring.mutations import add_column  # noqa: E402
from tablespec.dbt import DbtRunner  # noqa: E402
from tablespec.e2e import (  # noqa: E402
    save_specs,
    sync_specs_with_csvs,
    umfs_from_csvs,
    umfs_from_spec_dir,
)
from tablespec.excel_converter import (  # noqa: E402
    ExcelToUMFConverter,
    UMFToExcelConverter,
)
from tablespec.reporting import write_report  # noqa: E402
from tablespec.umf_loader import UMFLoader  # noqa: E402

DEMO = Path(__file__).resolve().parents[2] / "notebooks" / "csv-to-dbt-demo"

_MONTH1 = ["members.csv", "med_claims_20260601.csv", "rx_claims_20260601.csv"]
_MONTH2 = ["med_claims_20260701.csv", "rx_claims_20260701.csv"]
_PKS = {
    "members": ["member_id"],
    "med_claims": ["ps_unique_id"],
    "rx_claims": ["ps_unique_id"],
}


@pytest.fixture(autouse=True)
def _force_local_emit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the duckdb subprocess lane even when the suite runs on Databricks."""
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)


def _copy_data(tmp_path: Path, files: list[str]) -> Path:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    for name in files:
        shutil.copy(DEMO / "sample-data" / name, data / name)
    return data


def _generate_specs_via_excel(tmp_path: Path, data: Path) -> Path:
    """The notebook's first-run path: CSVs -> Excel workbooks -> UMF specs."""
    umfs = umfs_from_csvs(data, infer_types=True, primary_keys=_PKS)
    excel_dir = tmp_path / "excel"
    excel_dir.mkdir(exist_ok=True)
    for u in umfs:
        UMFToExcelConverter().convert(u).save(excel_dir / f"{u.table_name}.xlsx")
    converted = [
        ExcelToUMFConverter().convert(p)[0] for p in sorted(excel_dir.glob("*.xlsx"))
    ]
    specs = tmp_path / "specs"
    save_specs(converted, specs)
    med = next(u for u in converted if u.table_name == "med_claims")
    # The Excel round trip preserved the pipeline-critical fields.
    assert med.primary_key == ["ps_unique_id"]
    assert med.ingestion is not None and med.ingestion.mode == "incremental"
    assert med.source is not None and med.source.path.endswith("med_claims_*.csv")
    return specs


def _build(runner: DbtRunner, project_dir: Path, specs: Path, data: Path):
    umfs = umfs_from_spec_dir(specs, data_dir=data)
    project = runner.emit(umfs, project_dir, dialect="duckdb", target="duckdb")
    return umfs, runner.build(project)


def test_full_flow_with_additive_month_swap(tmp_path: Path) -> None:
    data = _copy_data(tmp_path, _MONTH1)
    specs = _generate_specs_via_excel(tmp_path, data)
    runner = DbtRunner()
    project_dir = tmp_path / "dbt"  # ONE project = one warehouse across runs

    # --- Sources build + validation report. ---
    _, result = _build(runner, project_dir, specs, data)
    assert result.success, f"{result.stdout}\n{result.stderr}"
    assert result.validation_report().success

    # --- Gold from the SHIPPED gold Excel spec workbook (the demo's start). ---
    gold, _ = ExcelToUMFConverter().convert(
        DEMO / "sample-specs" / "member_claims_summary.xlsx"
    )
    UMFLoader().save(gold, specs / gold.table_name)
    umfs, result = _build(runner, project_dir, specs, data)
    assert result.success, f"{result.stdout}\n{result.stderr}"

    con = duckdb.connect(str(project_dir / "tablespec.duckdb"))
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM gold_member_claims_summary"
        ).fetchone() == (689,)
        got = con.execute(
            "SELECT member_name, CAST(birth_dt AS VARCHAR), med_claim_count, "
            "latest_claim_status, CAST(last_activity_date AS VARCHAR) "
            "FROM gold_member_claims_summary WHERE member_id = 'W000000004'"
        ).fetchone()
        assert got == ("Zoey Lee", "1973-07-27", 5, "P", "2026-05-16")
        # Survivorship at scale: blank roster birth dates filled by the feeds.
        assert con.execute(
            "SELECT COUNT(*) FROM gold_member_claims_summary g "
            "JOIN ingested_members m USING (member_id) "
            "WHERE m.birth_dt IS NULL AND g.birth_dt IS NOT NULL"
        ).fetchone() == (154,)
    finally:
        con.close()

    # --- Report files (output_config consumer). ---
    report_umf = next(u for u in umfs if u.table_name == "member_claims_summary")
    con = duckdb.connect(str(project_dir / "tablespec.duckdb"))
    try:
        cursor = con.execute("SELECT * FROM gold_member_claims_summary")
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
    finally:
        con.close()
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    paths = write_report(report_umf, columns, rows, tmp_path / "reports", now=now)
    assert paths.csv_path.name == "member_claims_summary_20260712.csv"
    assert paths.csv_path.read_bytes().decode("utf-8").split("\r\n")[-2] == "689"

    # --- Month swap: month-1 claim files REMOVED, month-2 added. ---
    (data / "med_claims_20260601.csv").unlink()
    (data / "rx_claims_20260601.csv").unlink()
    _copy_data(tmp_path, _MONTH2)

    added = sync_specs_with_csvs(specs, data_dir=data)
    assert added == {"med_claims": ["telehealth_indicator"]}
    # The authored gold column for the new field (the shipped ripple yaml).
    shutil.copy(
        DEMO / "ripple" / "telehealth_visit_count.yaml",
        specs / "member_claims_summary" / "columns" / "telehealth_visit_count.yaml",
    )

    _, result = _build(runner, project_dir, specs, data)
    assert result.success, f"{result.stdout}\n{result.stderr}"

    con = duckdb.connect(str(project_dir / "tablespec.duckdb"))
    try:
        # BOTH months persist: month-1 rows survived the file removal (MERGE),
        # month-2 rows were added, and the new column appended in place.
        months = dict(
            con.execute(
                "SELECT file_dt, COUNT(*) FROM ingested_med_claims GROUP BY 1"
            ).fetchall()
        )
        assert months == {"20260601": 1000, "20260701": 1000}
        assert con.execute(
            "SELECT SUM(telehealth_visit_count), "
            "COUNT(*) FILTER (WHERE telehealth_visit_count > 0) "
            "FROM gold_member_claims_summary"
        ).fetchone() == (170, 146)
        # Month-2 activity moved the derived values.
        assert con.execute(
            "SELECT med_claim_count, CAST(last_activity_date AS VARCHAR) "
            "FROM gold_member_claims_summary WHERE member_id = 'W000000001'"
        ).fetchone() == (3, "2026-06-04")
    finally:
        con.close()


def test_spec_edit_before_data_fails(tmp_path: Path) -> None:
    """Declaring the new column before any file carries it fails the build --
    the reason the demo syncs specs FROM the files actually present."""
    data = _copy_data(tmp_path, _MONTH1)
    specs = _generate_specs_via_excel(tmp_path, data)
    loader = UMFLoader()
    med = add_column(
        loader.load(specs / "med_claims"), "telehealth_indicator", "VARCHAR", length=1
    )
    loader.save(med, specs / "med_claims")

    _, result = _build(DbtRunner(), tmp_path / "dbt", specs, data)
    assert not result.success
    assert "telehealth_indicator" in (result.stdout + result.stderr)
