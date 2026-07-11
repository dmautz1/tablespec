"""Emission tests for the shipped demo report spec (gold + derivations).

The csv-to-dbt demo ships sample specs (members, claims, rx + the generated
``member_claims_summary`` report). These tests pin the derivation surface the
demo advertises -- primary_key, embedded-SQL expressions (CONCAT_WS, COUNT,
SUM, MAX_BY), 3-way highest_priority survivorship with a default, and
max_across_sources (GREATEST) -- byte-for-byte against the real generator.
Pure text emission; no dbt required.
"""

from __future__ import annotations

from pathlib import Path

from tablespec.dbt.project import generate_dbt_dag_project
from tablespec.e2e import umfs_from_spec_dir

SAMPLE_SPECS = (
    Path(__file__).resolve().parents[2]
    / "notebooks"
    / "csv-to-dbt-demo"
    / "sample-specs"
)


def _project() -> dict[str, str]:
    umfs = umfs_from_spec_dir(SAMPLE_SPECS)
    assert {u.table_name for u in umfs} == {
        "members",
        "claims",
        "rx",
        "member_claims_summary",
    }
    return generate_dbt_dag_project(umfs, dialect="duckdb", target="duckdb")


def test_report_spec_is_gold_with_file_backed_staging() -> None:
    files = _project()
    # Three file-backed landing models with glob paths + schema evolution.
    for table, glob in (
        ("members", "members.csv"),
        ("claims", "claims_*.csv"),
        ("rx", "rx_*.csv"),
    ):
        raw = files[f"models/staging/raw_{table}.sql"]
        assert glob in raw
        assert "union_by_name=true" in raw
    # Monthly feeds carry the filename regex filter + file_month capture.
    assert "regexp_matches" in files["models/staging/raw_claims.sql"]
    assert 'AS "file_month"' in files["models/staging/raw_claims.sql"]
    # The report table is GOLD: no raw/ingested models, one marts model.
    assert "models/staging/raw_member_claims_summary.sql" not in files
    assert "models/staging/ingested_member_claims_summary.sql" not in files
    assert "models/marts/gold_member_claims_summary.sql" in files


def test_gold_model_pins_the_derivation_surface() -> None:
    gold = _project()["models/marts/gold_member_claims_summary.sql"]

    # Cross-table edges are dbt refs to the staging models.
    for ref in ("ingested_members", "ingested_claims", "ingested_rx"):
        assert ref in gold

    # Embedded SQL: CONCAT_WS name assembly.
    assert "CONCAT_WS(' ', " in gold

    # 3-way highest_priority survivorship with NULLIF blanks + default.
    assert (
        "COALESCE(\n"
        "    NULLIF(base.members__phone, ''),\n"
        "    NULLIF(base.claims__contact_phone, ''),\n"
        "    NULLIF(base.rx__member_phone, ''),\n"
        "    'UNKNOWN'\n"
        ") AS contact_phone" in gold
    )

    # Aggregate expressions (pre-aggregation views) + defaults.
    assert "COUNT(*) AS medical_claim_count" in gold
    assert "SUM(claim_amount) AS total_medical_amount" in gold
    assert "MAX_BY(claim_status, service_date) AS latest_claim_status" in gold
    assert "COUNT(*) AS rx_fill_count" in gold
    assert "SUM(rx_amount) AS total_rx_amount" in gold
    assert "COALESCE(base.claims_agg__medical_claim_count, 0)" in gold

    # max_across_sources: GREATEST over the two per-source MAX aggregates.
    assert (
        "GREATEST(base.claims_agg__last_activity_date, "
        "base.rx_agg__last_activity_date) AS last_activity_date" in gold
    )


def test_report_spec_declares_output_config() -> None:
    umfs = umfs_from_spec_dir(SAMPLE_SPECS)
    report = next(u for u in umfs if u.table_name == "member_claims_summary")
    assert report.metadata is not None
    config = report.metadata.output_config
    assert config is not None
    assert config.include_footer is True
    assert config.line_terminator == "CRLF"
    assert config.file_naming_example == "member_claims_summary_YYYYMMDD.csv"
