"""Emission tests for the demo report spec over GENERATED source specs.

Mirrors the notebook's DEMO flow: the source specs come from
``umfs_from_csvs(infer_types=True)`` over the shipped sample data (monthly
families grouped into glob specs, DATE/DECIMAL types inferred) and the SHIPPED
authored gold report spec joins them. These tests pin the derivation surface
the demo advertises -- primary_key, embedded-SQL expressions (CONCAT_WS,
COUNT, SUM, MAX_BY), 3-way highest_priority survivorship, and
max_across_sources (GREATEST) -- against the real generator. Pure text
emission; no dbt required.
"""

from __future__ import annotations

from pathlib import Path

from tablespec.dbt.project import generate_dbt_dag_project
from tablespec.e2e import umfs_from_csvs, umfs_from_spec_dir
from tablespec.models.umf import UMF

DEMO = Path(__file__).resolve().parents[2] / "notebooks" / "csv-to-dbt-demo"

_MONTH1 = ["members.csv", "med_claims_20260601.csv", "rx_claims_20260601.csv"]


def _umfs() -> list[UMF]:
    sources = umfs_from_csvs(
        [DEMO / "sample-data" / f for f in _MONTH1], infer_types=True
    )
    assert {u.table_name for u in sources} == {"members", "med_claims", "rx_claims"}
    report = umfs_from_spec_dir(DEMO / "sample-specs")
    assert [u.table_name for u in report] == ["member_claims_summary"]
    return [*sources, *report]


def _project() -> dict[str, str]:
    return generate_dbt_dag_project(_umfs(), dialect="duckdb", target="duckdb")


def test_generated_specs_type_the_report_columns() -> None:
    med = next(u for u in _umfs() if u.table_name == "med_claims")
    cols = {c.name: c for c in med.columns}
    # Inference: dates + amounts typed; leading-zero identifiers stay VARCHAR.
    assert cols["srv_start_dt"].data_type == "DATE"
    assert cols["srv_start_dt"].format == "YYYYMMDD"
    assert cols["birth_dt"].data_type == "DATE"
    assert cols["paid_amt"].data_type == "DECIMAL"
    assert cols["ps_unique_id"].data_type == "VARCHAR"
    # Monthly family: glob source + file_dt filename capture + snapshot mode.
    assert med.source is not None and med.source.path.endswith("med_claims_*.csv")
    assert med.source.filename_pattern.captures == {1: "file_dt"}
    assert med.ingestion is not None and med.ingestion.mode == "snapshot"
    # The ripple column is NOT in the month-1 header.
    assert "telehealth_indicator" not in cols


def test_report_spec_is_gold_with_file_backed_staging() -> None:
    files = _project()
    for table, path_end in (
        ("members", "members.csv"),
        ("med_claims", "med_claims_*.csv"),
        ("rx_claims", "rx_claims_*.csv"),
    ):
        raw = files[f"models/staging/raw_{table}.sql"]
        assert path_end in raw
        assert "union_by_name=true" in raw
    for table in ("med_claims", "rx_claims"):
        raw = files[f"models/staging/raw_{table}.sql"]
        assert "regexp_matches" in raw
        assert 'AS "file_dt"' in raw
    # The report table is GOLD: no raw/ingested models, one marts model.
    assert "models/staging/raw_member_claims_summary.sql" not in files
    assert "models/staging/ingested_member_claims_summary.sql" not in files
    assert "models/marts/gold_member_claims_summary.sql" in files


def test_gold_model_pins_the_derivation_surface() -> None:
    gold = _project()["models/marts/gold_member_claims_summary.sql"]

    # Cross-table edges are dbt refs to the staging models.
    for ref in ("ingested_members", "ingested_med_claims", "ingested_rx_claims"):
        assert ref in gold

    # Embedded SQL: CONCAT_WS name assembly from the roster.
    assert (
        "CONCAT_WS(' ', base.members__first_name, base.members__last_name) "
        "AS member_name" in gold
    )

    # 3-way highest_priority survivorship (DATE candidates: no NULLIF wrapping).
    assert (
        "COALESCE(base.members__birth_dt, base.med_claims__birth_dt, "
        "base.rx_claims__src_mbr_birth_dt) AS birth_dt" in gold
    )

    # Aggregate expressions (pre-aggregation views) + count defaults.
    assert "COUNT(*) AS med_claim_count" in gold
    assert "SUM(paid_amt) AS total_med_paid" in gold
    assert "MAX_BY(clm_ln_status_cd, srv_start_dt) AS latest_claim_status" in gold
    assert "COUNT(*) AS rx_claim_count" in gold
    assert "SUM(paid_amt) AS total_rx_paid" in gold
    assert "COALESCE(base.med_claims_agg__med_claim_count, 0)" in gold

    # max_across_sources: GREATEST over the two per-source MAX aggregates.
    assert (
        "GREATEST(base.med_claims_agg__last_activity_date, "
        "base.rx_claims_agg__last_activity_date) AS last_activity_date" in gold
    )


def test_report_spec_declares_output_config() -> None:
    (report,) = umfs_from_spec_dir(DEMO / "sample-specs")
    assert report.metadata is not None
    config = report.metadata.output_config
    assert config is not None
    assert config.include_footer is True
    assert config.line_terminator == "CRLF"
    assert config.file_naming_example == "member_claims_summary_YYYYMMDD.csv"
    # The ripple column ships as a paste-in file, not in the spec.
    assert "telehealth_visit_count" not in {c.name for c in report.columns}
    assert (DEMO / "ripple" / "telehealth_visit_count.yaml").exists()
