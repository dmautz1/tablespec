"""Tests for the dbt run-results parser and the HTML report renderer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tablespec.models.quality import QualityCheckResult, QualityCheckRun
from tablespec.validation.dbt_results import (
    DbtResultsError,
    dbt_validation_report,
    parse_dbt_run_results,
)
from tablespec.validation.html_report import (
    render_validation_report_html,
    write_validation_report,
)
from tablespec.validation.report import ValidationReport


def _write_target(
    project: Path,
    results: list[dict[str, Any]],
    nodes: dict[str, Any] | None = None,
) -> None:
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/run-results/v6.json",
                    "generated_at": "2026-07-11T10:00:00Z",
                    "invocation_id": "inv-123",
                },
                "results": results,
            }
        )
    )
    if nodes is not None:
        (target / "manifest.json").write_text(json.dumps({"nodes": nodes}))


def test_parse_run_results_maps_models_and_tests(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_target(
        project,
        results=[
            {
                "unique_id": "model.demo.raw_orders",
                "status": "success",
                "message": "OK",
                "relation_name": '"db"."main"."raw_orders"',
            },
            {
                "unique_id": "test.demo.unique_orders_order_id.abc123",
                "status": "fail",
                "message": "Got 2 results, configured to fail if != 0",
                "failures": 2,
            },
            {
                "unique_id": "test.demo.accepted_values_orders_status.def456",
                "status": "warn",
                "message": "warned",
                "failures": 1,
            },
            {
                "unique_id": "model.demo.orders",
                "status": "skipped",
                "message": None,
            },
        ],
        nodes={
            "test.demo.unique_orders_order_id.abc123": {
                "column_name": "order_id",
                "attached_node": "model.demo.orders",
                "test_metadata": {"name": "unique"},
            },
            "test.demo.accepted_values_orders_status.def456": {
                "column_name": "status",
                "attached_node": "model.demo.orders",
                "test_metadata": {"name": "accepted_values"},
            },
        },
    )

    run = parse_dbt_run_results(project)

    assert run.run_id == "inv-123"
    assert run.run_timestamp.isoformat().startswith("2026-07-11T10:00:00")
    by_id = {r.check_id: r for r in run.results}
    assert len(by_id) == 4

    raw_model = by_id["model.demo.raw_orders"]
    assert raw_model.success and raw_model.expectation_type == "dbt_model"

    failed = by_id["test.demo.unique_orders_order_id.abc123"]
    assert not failed.success
    assert failed.severity == "critical"
    assert failed.expectation_type == "dbt_test:unique"
    assert failed.column_name == "order_id"
    assert failed.unexpected_count == 2
    assert "[orders]" in (failed.description or "")

    warned = by_id["test.demo.accepted_values_orders_status.def456"]
    assert warned.success and warned.severity == "warning"

    skipped = by_id["model.demo.orders"]
    assert skipped.success and skipped.severity == "info"
    assert "skipped" in (skipped.description or "")

    # A critical failure blocks; the report reflects it.
    assert run.should_block
    report = dbt_validation_report(project)
    assert report.failed == 1
    assert "3/4" in report.summary()


def test_parse_run_results_without_manifest(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_target(
        project,
        results=[
            {
                "unique_id": "test.demo.unique_orders_order_id.abc123",
                "status": "pass",
                "failures": 0,
            }
        ],
    )
    run = parse_dbt_run_results(project)
    (result,) = run.results
    assert result.success
    # Without a manifest the test name falls back to the unique_id node name.
    assert result.expectation_type == "dbt_test:unique_orders_order_id"
    assert result.column_name is None


def test_parse_run_results_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(DbtResultsError, match="run dbt"):
        parse_dbt_run_results(tmp_path)


def test_as_rows_flattens_per_validation(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_target(
        project,
        results=[
            {
                "unique_id": "model.demo.ingested_orders",
                "status": "success",
                "message": "OK",
                "relation_name": '"db"."main"."ingested_orders"',
            },
            {
                "unique_id": "test.demo.unique_orders_order_id.abc123",
                "status": "fail",
                "message": "Got 2 results",
                "failures": 2,
            },
        ],
        nodes={
            "test.demo.unique_orders_order_id.abc123": {
                "column_name": "order_id",
                "attached_node": "model.demo.ingested_orders",
                "test_metadata": {"name": "unique"},
            }
        },
    )
    rows = dbt_validation_report(project).as_rows()

    assert len(rows) == 2
    by_check = {r["check_id"]: r for r in rows}
    model_row = by_check["model.demo.ingested_orders"]
    assert model_row["model"] == "ingested_orders"
    assert model_row["success"] is True
    test_row = by_check["test.demo.unique_orders_order_id.abc123"]
    assert test_row["model"] == "ingested_orders"
    assert test_row["column_name"] == "order_id"
    assert test_row["success"] is False
    assert test_row["unexpected_count"] == 2
    # Rows are persistence-ready: identical scalar keys on every row.
    assert all(set(r) == set(rows[0]) for r in rows)
    assert all(r["run_id"] == "inv-123" for r in rows)
    assert all(r["run_timestamp"] is not None for r in rows)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _report() -> ValidationReport:
    return ValidationReport(
        QualityCheckRun(
            pipeline_name="dbt",
            table_name="demo",
            run_id="r1",
            results=[
                QualityCheckResult(
                    check_id="a",
                    expectation_type="dbt_test:unique",
                    success=True,
                    severity="info",
                    column_name="order_id",
                ),
                QualityCheckResult(
                    check_id="b",
                    expectation_type="dbt_test:accepted_values",
                    success=False,
                    severity="critical",
                    column_name="status",
                    description="<b>2 bad rows</b>",
                    unexpected_count=2,
                ),
            ],
            should_block=True,
        )
    )


def test_render_html_escapes_and_marks_failures() -> None:
    html_text = render_validation_report_html(_report())
    assert "<!DOCTYPE html>" in html_text
    assert "1 passed" in html_text and "1 failed" in html_text
    assert ">FAIL<" in html_text and ">PASS<" in html_text
    # Descriptions are escaped, never raw HTML.
    assert "<b>2 bad rows</b>" not in html_text
    assert "&lt;b&gt;2 bad rows&lt;/b&gt;" in html_text


def test_write_validation_report(tmp_path: Path) -> None:
    json_path, html_path = write_validation_report(_report(), tmp_path / "reports")
    assert json_path.name == "validation-report.json"
    data = json.loads(json_path.read_text())
    assert data["failed"] == 1 and data["should_block"] is True
    assert html_path.read_text().startswith("<!DOCTYPE html>")
