"""End-to-end demo test: sample claim feeds -> gold report table -> report files.

Mirrors the notebook's DEMO flow exactly: the source specs are GENERATED from
the shipped sample data (member roster + monthly ACAS-style medical/pharmacy
claim extracts, 1000/500 lines per month) via ``umfs_from_csvs(infer_types=
True)`` -- monthly families grouped into glob specs, DATE/DECIMAL types
inferred -- then the SHIPPED authored gold report spec is added, and a real
dbt build runs on duckdb:

  1. Month-1 files only: the gold ``member_claims_summary`` materializes with
     the survivorship winners, embedded-SQL aggregates, and GREATEST activity
     dates the README documents.
  2. The RIPPLE: month-2 files (the med feed adds ``telehealth_indicator``) +
     the two documented spec edits (``add_column`` on med_claims; the shipped
     ``ripple/telehealth_visit_count.yaml`` pasted into the report spec) ->
     rebuild -> the telehealth count flows into the gold table.
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
from tablespec.e2e import save_specs, umfs_from_csvs, umfs_from_spec_dir  # noqa: E402
from tablespec.reporting import write_report  # noqa: E402
from tablespec.umf_loader import UMFLoader  # noqa: E402

DEMO = Path(__file__).resolve().parents[2] / "notebooks" / "csv-to-dbt-demo"

_MONTH1 = ["members.csv", "med_claims_20260601.csv", "rx_claims_20260601.csv"]
_MONTH2 = ["med_claims_20260701.csv", "rx_claims_20260701.csv"]


@pytest.fixture(autouse=True)
def _force_local_emit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the duckdb subprocess lane even when the suite runs on Databricks."""
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)


def _stage(tmp_path: Path, files: list[str]) -> tuple[Path, Path]:
    """The notebook's demo flow: generate source specs, add the report spec."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    for name in files:
        shutil.copy(DEMO / "sample-data" / name, data / name)
    specs = tmp_path / "specs"
    if not specs.exists():
        generated = umfs_from_csvs(data, infer_types=True)
        assert {u.table_name for u in generated} == {
            "members",
            "med_claims",
            "rx_claims",
        }
        save_specs(generated, specs)
        shutil.copytree(DEMO / "sample-specs", specs, dirs_exist_ok=True)
    return specs, data


def _build(tmp_path: Path, specs: Path, data: Path, project_name: str):
    umfs = umfs_from_spec_dir(specs, data_dir=data)
    runner = DbtRunner()
    project = runner.emit(
        umfs, tmp_path / project_name, dialect="duckdb", target="duckdb"
    )
    return umfs, runner.build(project), project


def _apply_ripple_edits(specs: Path) -> None:
    """The two documented ripple edits (same code paths as the CLI/README)."""
    loader = UMFLoader()
    med = loader.load(specs / "med_claims")
    med = add_column(
        med,
        "telehealth_indicator",
        "VARCHAR",
        length=1,
        description="Y/N telehealth flag (arrived in the 2026-07 med file).",
    )
    loader.save(med, specs / "med_claims")
    shutil.copy(
        DEMO / "ripple" / "telehealth_visit_count.yaml",
        specs / "member_claims_summary" / "columns" / "telehealth_visit_count.yaml",
    )


def test_month1_build_and_ripple(tmp_path: Path) -> None:
    # --- Month 1: the documented survivorship / embedded-SQL outcomes. ---
    specs, data = _stage(tmp_path, _MONTH1)
    umfs, result, project = _build(tmp_path, specs, data, "m1")
    assert result.success, f"{result.stdout}\n{result.stderr}"

    con = duckdb.connect(str(result.project_dir / "tablespec.duckdb"))
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM gold_member_claims_summary"
        ).fetchone() == (689,)

        got = {
            r[0]: r[1:]
            for r in con.execute(
                "SELECT member_id, member_name, CAST(birth_dt AS VARCHAR), "
                "med_claim_count, latest_claim_status, rx_claim_count, "
                "CAST(last_activity_date AS VARCHAR) "
                "FROM gold_member_claims_summary WHERE member_id IN "
                "('W000000001','W000000002','W000000004','W000000063')"
            ).fetchall()
        }
        # Roster-only member with a blanked roster birth_dt and no month-1
        # claims: everything NULL/0 -- the union universe still carries them.
        assert got["W000000001"] == ("Sebastian Harris", None, 0, None, 0, None)
        # rx-only member: activity date comes from the pharmacy feed.
        assert got["W000000002"] == (
            "Logan Brown",
            "1978-09-22",
            0,
            None,
            1,
            "2026-05-09",
        )
        # Both feeds: MAX_BY latest status + GREATEST activity date.
        assert got["W000000004"] == (
            "Zoey Lee",
            "1973-07-27",
            5,
            "P",
            1,
            "2026-05-16",
        )
        assert got["W000000063"] == (
            "Matthew Taylor",
            "1967-02-26",
            1,
            "P",
            2,
            "2026-04-24",
        )

        # Survivorship at scale: members whose roster birth_dt is blank but a
        # claim feed supplied one (priority fallthrough).
        assert con.execute(
            "SELECT COUNT(*) FROM gold_member_claims_summary g "
            "JOIN ingested_members m USING (member_id) "
            "WHERE m.birth_dt IS NULL AND g.birth_dt IS NOT NULL"
        ).fetchone() == (154,)

        cursor = con.execute("SELECT * FROM gold_member_claims_summary")
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
    finally:
        con.close()

    # --- Report files from the gold rows (output_config consumer). ---
    report_umf = next(u for u in umfs if u.table_name == "member_claims_summary")
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    paths = write_report(report_umf, columns, rows, tmp_path / "reports", now=now)
    assert paths.csv_path.name == "member_claims_summary_20260712.csv"
    lines = paths.csv_path.read_bytes().decode("utf-8").split("\r\n")
    assert lines[-2] == "689"  # row-count footer

    # --- The RIPPLE: month-2 files + the two documented spec edits. ---
    _stage(tmp_path, _MONTH2)
    _apply_ripple_edits(specs)
    _, result2, _ = _build(tmp_path, specs, data, "m2")
    assert result2.success, f"{result2.stdout}\n{result2.stderr}"

    con = duckdb.connect(str(result2.project_dir / "tablespec.duckdb"))
    try:
        # Every month-2 telehealth line (170 in the file) reached the report;
        # month-1 lines carry NULL indicators and contribute nothing.
        totals = con.execute(
            "SELECT SUM(telehealth_visit_count), "
            "COUNT(*) FILTER (WHERE telehealth_visit_count > 0) "
            "FROM gold_member_claims_summary"
        ).fetchone()
        assert totals == (170, 146)
        # A previously idle member picked up month-2 claims (none telehealth).
        row = con.execute(
            "SELECT med_claim_count, telehealth_visit_count, "
            "CAST(last_activity_date AS VARCHAR) "
            "FROM gold_member_claims_summary WHERE member_id = 'W000000001'"
        ).fetchone()
        assert row == (3, 0, "2026-06-04")
    finally:
        con.close()


def test_ripple_ordering_rule_spec_before_data_fails(tmp_path: Path) -> None:
    """Editing the spec BEFORE any file carries the column fails the build --
    the reason the README says upload month-2 data first."""
    specs, data = _stage(tmp_path, _MONTH1)
    _apply_ripple_edits(specs)  # telehealth declared; no file has it yet
    _, result, _ = _build(tmp_path, specs, data, "premature")
    assert not result.success
    assert "telehealth_indicator" in (result.stdout + result.stderr)
