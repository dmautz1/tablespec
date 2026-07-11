"""End-to-end demo test: sample CSVs -> gold report table -> report files.

Runs the SHIPPED csv-to-dbt demo assets through a real dbt build on duckdb:

  1. Month-1 files only: the gold ``member_claims_summary`` materializes with
     the survivorship winners, embedded-SQL aggregates, and GREATEST activity
     dates the README documents per member.
  2. The RIPPLE: month-2 files (claims adds ``copay_amount``) + the two
     documented spec edits (``add_column`` on claims; the shipped
     ``ripple/total_copay.yaml`` pasted into the report spec) -> rebuild ->
     ``total_copay`` flows into the gold table; statuses/dates move.
  3. The report writer exports the gold rows as the CSV+Excel report files
     declared by the spec's ``output_config``.
  4. NEGATIVE: editing the spec BEFORE any file carries the new column fails
     the build (why the README orders "upload data first").
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

from tablespec.authoring.mutations import add_column  # noqa: E402
from tablespec.dbt import DbtRunner  # noqa: E402
from tablespec.e2e import umfs_from_spec_dir  # noqa: E402
from tablespec.reporting import write_report  # noqa: E402
from tablespec.umf_loader import UMFLoader  # noqa: E402

DEMO = Path(__file__).resolve().parents[2] / "notebooks" / "csv-to-dbt-demo"


@pytest.fixture(autouse=True)
def _force_local_emit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the duckdb subprocess lane even when the suite runs on Databricks."""
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)


def _stage(tmp_path: Path, files: list[str]) -> tuple[Path, Path]:
    """Copy sample specs + the named sample-data files into tmp."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    for name in files:
        shutil.copy(DEMO / "sample-data" / name, data / name)
    specs = tmp_path / "specs"
    if not specs.exists():
        shutil.copytree(DEMO / "sample-specs", specs)
    return specs, data


def _build(tmp_path: Path, specs: Path, data: Path, project_name: str):
    umfs = umfs_from_spec_dir(specs, data_dir=data)
    runner = DbtRunner()
    project = runner.emit(
        umfs, tmp_path / project_name, dialect="duckdb", target="duckdb"
    )
    return umfs, runner.build(project), project


def _gold_rows(project_dir: Path, columns: str) -> dict[str, tuple]:
    con = duckdb.connect(str(project_dir / "tablespec.duckdb"))
    try:
        rows = con.execute(
            f"SELECT member_id, {columns} FROM gold_member_claims_summary "
            "ORDER BY member_id"
        ).fetchall()
    finally:
        con.close()
    return {r[0]: r[1:] for r in rows}


def _apply_ripple_edits(specs: Path) -> None:
    """The two documented ripple edits (same code paths as the CLI/README)."""
    loader = UMFLoader()
    claims = loader.load(specs / "claims")
    claims = add_column(
        claims,
        "copay_amount",
        "DECIMAL",
        precision=18,
        scale=2,
        description="Member copay (arrived in the 2024-02 claims file).",
    )
    loader.save(claims, specs / "claims")
    shutil.copy(
        DEMO / "ripple" / "total_copay.yaml",
        specs / "member_claims_summary" / "columns" / "total_copay.yaml",
    )


def test_month1_build_and_ripple(tmp_path: Path) -> None:
    # --- Month 1: the documented survivorship / embedded-SQL outcomes. ---
    specs, data = _stage(
        tmp_path, ["members.csv", "claims_202401.csv", "rx_202401.csv"]
    )
    umfs, result, project = _build(tmp_path, specs, data, "m1")
    assert result.success, f"{result.stdout}\n{result.stderr}"

    got = _gold_rows(
        result.project_dir,
        "contact_phone, medical_claim_count, latest_claim_status, "
        "total_medical_amount, rx_fill_count, CAST(last_activity_date AS VARCHAR)",
    )
    assert len(got) == 9  # every member in any source file
    # M001: members phone beats the claims phone (priority 1).
    assert got["M001"][:3] == ("555-0101", 2, "PAID")
    # M002: members blank -> claims phone (priority 2); latest claim DENIED.
    assert got["M002"][:3] == ("555-0202", 2, "DENIED")
    # M003: rx fill (01-30) beats the last claim (01-28) -> GREATEST from rx.
    assert got["M003"][5] == "2024-01-30"
    # M004: no month-1 claims -> count default 0; phone only on rx (priority 3).
    assert got["M004"][0] == "555-0404"
    assert got["M004"][1] == 0
    # M007: phone in no source -> survivorship default.
    assert got["M007"][0] == "UNKNOWN"
    # M008: members-only -> counts 0, no activity anywhere.
    assert got["M008"][1] == 0 and got["M008"][4] == 0
    assert got["M008"][5] is None

    # --- Report files from the gold rows (output_config consumer). ---
    report_umf = next(u for u in umfs if u.table_name == "member_claims_summary")
    con = duckdb.connect(str(result.project_dir / "tablespec.duckdb"))
    try:
        cursor = con.execute("SELECT * FROM gold_member_claims_summary")
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
    finally:
        con.close()
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    paths = write_report(report_umf, columns, rows, tmp_path / "reports", now=now)
    assert paths.csv_path.name == "member_claims_summary_20260711.csv"
    lines = paths.csv_path.read_bytes().decode("utf-8").split("\r\n")
    assert lines[-2] == "9"  # row-count footer (trailing terminator after it)

    # --- The RIPPLE: month-2 files + the two documented spec edits. ---
    _stage(tmp_path, ["claims_202402.csv", "rx_202402.csv"])
    _apply_ripple_edits(specs)
    umfs2, result2, _ = _build(tmp_path, specs, data, "m2")
    assert result2.success, f"{result2.stdout}\n{result2.stderr}"

    got2 = _gold_rows(
        result2.project_dir,
        "latest_claim_status, CAST(last_activity_date AS VARCHAR), total_copay",
    )
    # The new column flowed through to the report table.
    assert got2["M001"][2] == pytest.approx(20.00)
    assert got2["M003"][2] == pytest.approx(0.00)
    assert got2["M008"][2] is None  # no claims at all -> NULL-safe
    # Month-2 data moved the derived values too.
    assert got2["M002"][0] == "PAID"  # was DENIED after month 1
    assert got2["M003"][1] == "2024-02-27"  # rx fill still wins GREATEST


def test_ripple_ordering_rule_spec_before_data_fails(tmp_path: Path) -> None:
    """Editing the spec BEFORE any file carries the column fails the build --
    the reason the README says upload month-2 data first."""
    specs, data = _stage(
        tmp_path, ["members.csv", "claims_202401.csv", "rx_202401.csv"]
    )
    _apply_ripple_edits(specs)  # copay_amount declared; no file has it yet
    _, result, _ = _build(tmp_path, specs, data, "premature")
    assert not result.success
    assert "copay_amount" in (result.stdout + result.stderr)
