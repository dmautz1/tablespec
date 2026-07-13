"""Tests for LLM spec enrichment (no network -- a canned fake client).

Covers the appliers' fill-only semantics, the enrich_specs orchestration and
round trip, JSON extraction, client config resolution, and the CLI command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tablespec.authoring.enrich import (
    apply_enrichment_response,
    apply_relationships_response,
)
from tablespec.e2e import save_specs, umfs_from_spec_dir
from tablespec.llm.client import LlmConfigError, extract_json
from tablespec.llm.enrich import enrich_specs
from tablespec.models.umf import UMF

runner = CliRunner(env={"NO_COLOR": "1", "TERM": "dumb"})


def _umf(table: str = "orders") -> UMF:
    return UMF.model_validate(
        {
            "version": "1.0",
            "table_name": table,
            "columns": [
                {
                    "name": f"{table}_id",
                    "data_type": "VARCHAR",
                    "nullable": {"default": True},
                },
                {
                    "name": "label",
                    "data_type": "VARCHAR",
                    "nullable": {"default": True},
                    "description": "existing human description",
                },
                {
                    "name": "meta_source_name",
                    "data_type": "VARCHAR",
                    "source": "metadata",
                },
            ],
        }
    )


_ENRICHMENT_RESPONSE = {
    "table_description": "Orders placed by customers.",
    "columns": {
        "orders_id": {
            "description": "Unique order identifier.",
            "notes": ["One row per order."],
            "sample_values": ["1001", "1002", "1003"],
        },
        "label": {"description": "LLM description that must NOT clobber"},
        "not_a_column": {"description": "ignored"},
    },
}


class FakeClient:
    """Returns canned JSON per prompt kind; records the prompts it saw."""

    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete_json(self, prompt: str, *, system: str | None = None) -> Any:
        self.prompts.append(prompt)
        if "foreign-key relationships" in prompt:
            return {
                "relationships": [
                    {
                        "source_table": "items",
                        "source_column": "items_id",
                        "target_table": "orders",
                        "target_column": "orders_id",
                        "relationship_type": "foreign_key",
                        "confidence": 0.9,
                    },
                    {
                        "source_table": "items",
                        "source_column": "missing_col",
                        "target_table": "orders",
                        "target_column": "orders_id",
                    },
                    {
                        "source_table": "items",
                        "source_column": "items_id",
                        "target_table": "not_in_set",
                        "target_column": "x",
                    },
                ]
            }
        if "GX" in prompt or "expectation" in prompt.lower():
            return {
                "expectations": [
                    {
                        "type": "expect_column_values_to_not_be_null",
                        "kwargs": {"column": "label"},
                    }
                ]
            }
        return _ENRICHMENT_RESPONSE


# ---------------------------------------------------------------------------
# appliers
# ---------------------------------------------------------------------------


def test_apply_enrichment_fill_only() -> None:
    applied = apply_enrichment_response(_umf(), _ENRICHMENT_RESPONSE)
    umf = applied.updated_umf
    assert umf is not None
    cols = {c.name: c for c in umf.columns}
    assert umf.description == "Orders placed by customers."
    assert cols["orders_id"].description == "Unique order identifier."
    assert cols["orders_id"].notes == ["One row per order."]
    assert cols["orders_id"].sample_values == ["1001", "1002", "1003"]
    # Existing human description is preserved (fill-only).
    assert cols["label"].description == "existing human description"
    assert applied.descriptions == ["orders_id"]


def test_apply_enrichment_overwrite() -> None:
    applied = apply_enrichment_response(_umf(), _ENRICHMENT_RESPONSE, overwrite=True)
    assert applied.updated_umf is not None
    cols = {c.name: c for c in applied.updated_umf.columns}
    assert cols["label"].description == "LLM description that must NOT clobber"


def test_apply_relationships_upserts_and_skips() -> None:
    orders, items = _umf("orders"), _umf("items")
    response = FakeClient().complete_json(
        "identifying foreign-key relationships in a schema"
    )
    result = apply_relationships_response([orders, items], response)
    items_umf, added = result["items"]
    assert added == 1
    assert items_umf.relationships is not None
    (fk,) = items_umf.relationships.foreign_keys or []
    assert (fk.column, fk.references_table, fk.references_column) == (
        "items_id",
        "orders",
        "orders_id",
    )
    assert fk.detection_method == "llm"
    assert result["orders"][1] == 0

    # Re-applying the same response dedupes.
    again = apply_relationships_response([result["orders"][0], items_umf], response)
    assert again["items"][1] == 0


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------


def test_extract_json_tolerates_fences_and_prose() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": 1} hope that helps') == {"a": 1}
    with pytest.raises(ValueError, match="not parseable"):
        extract_json("no json here")


# ---------------------------------------------------------------------------
# enrich_specs round trip
# ---------------------------------------------------------------------------


def test_enrich_specs_round_trip(tmp_path: Path) -> None:
    specs = save_specs([_umf("orders"), _umf("items")], tmp_path / "specs")
    fake = FakeClient()

    summary = enrich_specs(specs, client=fake)  # type: ignore[arg-type]

    # 2 tables x (descriptions + validations) + 1 relationships call.
    assert len(fake.prompts) == 5
    reloaded = {u.table_name: u for u in umfs_from_spec_dir(specs)}
    orders = reloaded["orders"]
    assert orders.description == "Orders placed by customers."
    assert orders.expectations is not None and orders.expectations.expectations
    items = reloaded["items"]
    assert items.relationships is not None
    assert items.relationships.foreign_keys
    stats = {t.table: t for t in summary.tables}
    assert stats["items"].foreign_keys_added == 1
    assert stats["orders"].expectations_added == 1


def test_enrich_specs_include_subset(tmp_path: Path) -> None:
    specs = save_specs([_umf("orders")], tmp_path / "specs")
    fake = FakeClient()
    enrich_specs(specs, client=fake, include=("descriptions",))  # type: ignore[arg-type]
    assert len(fake.prompts) == 1  # no validations call, no relationships (1 table)
    (orders,) = umfs_from_spec_dir(specs)
    assert orders.expectations is None or not orders.expectations.expectations


def test_enrich_specs_unknown_include_raises(tmp_path: Path) -> None:
    specs = save_specs([_umf()], tmp_path / "specs")
    with pytest.raises(ValueError, match="Unknown enrichment pass"):
        enrich_specs(specs, client=FakeClient(), include=("bogus",))  # type: ignore[arg-type]


def test_enrich_specs_no_save(tmp_path: Path) -> None:
    specs = save_specs([_umf()], tmp_path / "specs")
    before = (specs / "orders" / "table.yaml").read_text()
    enrich_specs(specs, client=FakeClient(), save=False)  # type: ignore[arg-type]
    assert (specs / "orders" / "table.yaml").read_text() == before


# ---------------------------------------------------------------------------
# client config
# ---------------------------------------------------------------------------


def test_client_requires_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from tablespec.llm.client import LlmClient

    for var in (
        "TABLESPEC_LLM_BASE_URL",
        "TABLESPEC_LLM_API_KEY",
        "TABLESPEC_LLM_MODEL",
        "DATABRICKS_RUNTIME_VERSION",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(LlmConfigError, match="No LLM endpoint configured"):
        LlmClient()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_enrich(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tablespec.llm.enrich as enrich_mod
    from tablespec.cli import app

    specs = save_specs([_umf("orders"), _umf("items")], tmp_path / "specs")
    monkeypatch.setattr(enrich_mod, "LlmClient", lambda **kwargs: FakeClient())

    result = runner.invoke(app, ["enrich", str(specs)])

    assert result.exit_code == 0, result.output
    assert "orders:" in result.output
    reloaded = {u.table_name: u for u in umfs_from_spec_dir(specs)}
    assert reloaded["orders"].description == "Orders placed by customers."


def test_cli_enrich_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tablespec.llm.enrich as enrich_mod
    from tablespec.cli import app

    specs = save_specs([_umf("orders")], tmp_path / "specs")
    before = (specs / "orders" / "table.yaml").read_text()
    monkeypatch.setattr(enrich_mod, "LlmClient", lambda **kwargs: FakeClient())

    result = runner.invoke(app, ["enrich", str(specs), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert (specs / "orders" / "table.yaml").read_text() == before


def test_cli_enrich_no_config_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tablespec.cli import app

    for var in (
        "TABLESPEC_LLM_BASE_URL",
        "TABLESPEC_LLM_API_KEY",
        "TABLESPEC_LLM_MODEL",
        "DATABRICKS_RUNTIME_VERSION",
    ):
        monkeypatch.delenv(var, raising=False)
    specs = save_specs([_umf()], tmp_path / "specs")

    result = runner.invoke(app, ["enrich", str(specs)])

    assert result.exit_code == 1
    assert "No LLM endpoint configured" in result.output


def test_cli_enrich_response_shapes_are_json(tmp_path: Path) -> None:
    """The canned fake responses must stay valid JSON-serializable shapes."""
    assert json.loads(json.dumps(_ENRICHMENT_RESPONSE)) == _ENRICHMENT_RESPONSE


def test_enrich_excel_specs_updates_workbooks_in_place(tmp_path: Path) -> None:
    from tablespec.excel_converter import ExcelToUMFConverter, UMFToExcelConverter
    from tablespec.llm.enrich import enrich_excel_specs

    excel_dir = tmp_path / "excel"
    excel_dir.mkdir()
    for table in ("orders", "items"):
        umf = _umf(table).model_copy(update={"canonical_name": table})
        UMFToExcelConverter().convert(umf).save(excel_dir / f"{table}.xlsx")

    fake = FakeClient()
    summary = enrich_excel_specs(excel_dir, client=fake)  # type: ignore[arg-type]

    # 2 tables x (descriptions + validations) + 1 relationships call.
    assert len(fake.prompts) == 5
    orders, _ = ExcelToUMFConverter().convert(excel_dir / "orders.xlsx")
    assert orders.description == "Orders placed by customers."
    cols = {c.name: c for c in orders.columns}
    assert cols["orders_id"].description == "Unique order identifier."
    assert cols["orders_id"].sample_values == ["1001", "1002", "1003"]
    # Expectations come back through the workbook's Validation Rules sheet
    # (the canonical accessor reads either carrier field).
    from tablespec.expectation_utils import expectation_dicts_from_umf

    assert any(
        e["type"] == "expect_column_values_to_not_be_null"
        for e in expectation_dicts_from_umf(orders)
    )
    items, _ = ExcelToUMFConverter().convert(excel_dir / "items.xlsx")
    assert items.relationships is not None and items.relationships.foreign_keys
    stats = {t.table: t for t in summary.tables}
    assert stats["items"].foreign_keys_added == 1


def test_enrich_excel_specs_no_workbooks_raises(tmp_path: Path) -> None:
    from tablespec.llm.enrich import enrich_excel_specs

    with pytest.raises(FileNotFoundError, match="No spec workbooks"):
        enrich_excel_specs(tmp_path, client=FakeClient())  # type: ignore[arg-type]
