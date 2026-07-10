"""Functional + unit coverage for model contracts (item 2: model_contracts).

These tests do NOT run dbt -- they assert the GENERATED contract text (config +
per-column ``data_type`` + ``constraints``) derived from the UMF column set, and
the core ``column_contracts`` derivation. They are JVM-free and fast.

  * AC2.1 (contract enforced + per-column data_type) over the contract_drift UMF,
    including DECIMAL(p,s) and VARCHAR(n) modifiers.
  * AC2.2 (not_null constraint) on a non-nullable column; absent on a nullable one.
  * AC2.6 (import-safe) ``tablespec.dbt.contracts`` imports no ``dbt`` package.
"""

# dbt contract derivation coverage.
# @covers US-025-AC2

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tablespec.core.schema_facts import ColumnContract, column_contracts
from tablespec.dbt.contracts import (
    contract_sql_type,
    render_column_contract,
    render_contract_config_arg,
)
from tablespec.dbt.profiles import render_profiles_yml
from tablespec.dbt.single_table import generate_dbt_project
from tablespec.models.umf import UMF

pytestmark = [pytest.mark.no_spark, pytest.mark.fast]

CD_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_roadmap" / "contract_drift"


def _umf() -> UMF:
    return UMF(**yaml.safe_load((CD_DIR / "metrics.umf.yaml").read_text()))


def _model(schema_yaml: str, name: str) -> dict:
    for m in yaml.safe_load(schema_yaml)["models"]:
        if m["name"] == name:
            return m
    raise AssertionError(f"model {name!r} not in schema.yml")


def _col(model: dict, name: str) -> dict:
    for c in model.get("columns") or []:
        if c["name"] == name:
            return c
    raise AssertionError(f"column {name!r} not in model {model['name']!r}")


# ---------------------------------------------------------------------------
# core derivation: column_contracts
# ---------------------------------------------------------------------------


def test_column_contracts_derivation() -> None:
    """core.column_contracts reflects UMF type + precision/scale/length + not_null."""
    contracts = {
        c.name: c for c in column_contracts(_umf().model_dump(exclude_none=True))
    }
    assert contracts["metric_id"] == ColumnContract(
        name="metric_id", data_type="INTEGER", not_null=True
    )
    assert contracts["amount"] == ColumnContract(
        name="amount", data_type="DECIMAL", not_null=False, precision=18, scale=2
    )
    assert contracts["as_of_date"] == ColumnContract(
        name="as_of_date", data_type="DATE", not_null=False
    )
    assert contracts["label"] == ColumnContract(
        name="label", data_type="VARCHAR", not_null=True, length=32
    )


def test_contract_sql_type_mapping() -> None:
    """The duckdb adapter SQL types carry DECIMAL(p,s)/VARCHAR(n) modifiers."""
    c = {x.name: x for x in column_contracts(_umf().model_dump(exclude_none=True))}
    assert contract_sql_type(c["metric_id"], dialect="duckdb") == "INTEGER"
    assert contract_sql_type(c["amount"], dialect="duckdb") == "DECIMAL(18,2)"
    assert contract_sql_type(c["as_of_date"], dialect="duckdb") == "DATE"
    assert contract_sql_type(c["label"], dialect="duckdb") == "VARCHAR(32)"


def test_contract_sql_type_defaults_and_spark() -> None:
    """DECIMAL defaults (10,2); spark dialect uses STRING for varchar/text."""
    bare_decimal = ColumnContract(name="x", data_type="DECIMAL", not_null=False)
    assert contract_sql_type(bare_decimal, dialect="duckdb") == "DECIMAL(10,2)"
    text = ColumnContract(name="t", data_type="TEXT", not_null=False, length=20)
    assert contract_sql_type(text, dialect="duckdb") == "VARCHAR(20)"
    assert contract_sql_type(text, dialect="spark") == "STRING"
    assert contract_sql_type(text, dialect="databricks") == "STRING"
    flt = ColumnContract(name="f", data_type="FLOAT", not_null=False)
    assert contract_sql_type(flt, dialect="duckdb") == "DOUBLE"


def test_contract_sql_type_rejects_unknown_dialect() -> None:
    with pytest.raises(
        ValueError,
        match=r"Unsupported contract dialect: 'postgres' \(expected one of spark, databricks, duckdb\)",
    ):
        contract_sql_type(
            ColumnContract(name="x", data_type="INTEGER", not_null=False),
            dialect="postgres",
        )


# ---------------------------------------------------------------------------
# render helpers
# ---------------------------------------------------------------------------


def test_render_contract_config_arg() -> None:
    assert render_contract_config_arg() == "        contract={'enforced': True},"


def test_render_column_contract_not_null() -> None:
    nn = ColumnContract(name="label", data_type="VARCHAR", not_null=True, length=32)
    assert render_column_contract(nn, dialect="duckdb") == [
        "      - name: label",
        "        data_type: VARCHAR(32)",
        "        constraints:",
        "          - type: not_null",
    ]


def test_render_column_contract_nullable_has_no_constraints() -> None:
    nl = ColumnContract(
        name="amount", data_type="DECIMAL", not_null=False, precision=18, scale=2
    )
    assert render_column_contract(nl, dialect="duckdb") == [
        "      - name: amount",
        "        data_type: DECIMAL(18,2)",
    ]


# ---------------------------------------------------------------------------
# AC2.1 / AC2.2 over the generated project (single-table path)
# ---------------------------------------------------------------------------


def test_contract_columns_match_umf_types() -> None:
    """AC2.1 + AC2.2: the generated schema.yml enforces the contract and declares
    every column's data_type; non-nullable columns carry a not_null constraint and
    nullable columns do not."""
    files = generate_dbt_project(_umf().model_dump(exclude_none=True), dialect="duckdb")
    model = _model(files["models/schema.yml"], "metrics")

    # AC2.1: contract enforced.
    assert model["config"]["contract"]["enforced"] is True

    # AC2.1: every column declares its adapter data_type.
    assert _col(model, "metric_id")["data_type"] == "INTEGER"
    assert _col(model, "amount")["data_type"] == "DECIMAL(18,2)"
    assert _col(model, "as_of_date")["data_type"] == "DATE"
    assert _col(model, "label")["data_type"] == "VARCHAR(32)"

    # AC2.2: not_null constraint on non-nullable columns only.
    assert _col(model, "metric_id").get("constraints") == [{"type": "not_null"}]
    assert _col(model, "label").get("constraints") == [{"type": "not_null"}]
    assert "constraints" not in _col(model, "amount")
    assert "constraints" not in _col(model, "as_of_date")


def test_model_config_block_enforces_contract() -> None:
    """AC2.1: the model SQL config opts into an enforced contract, and (being
    incremental) pins on_schema_change as dbt requires."""
    files = generate_dbt_project(_umf().model_dump(exclude_none=True), dialect="duckdb")
    model_sql = files["models/metrics.sql"]
    assert "contract={'enforced': True}" in model_sql
    assert "on_schema_change='fail'" in model_sql


# ---------------------------------------------------------------------------
# AC2.6 import-safe: contracts emits no dbt import
# ---------------------------------------------------------------------------


def test_contracts_module_imports_no_dbt() -> None:
    """AC2.6: tablespec.dbt.contracts' own import statements reference no dbt
    package (pure text emission; the [dbt] extra is a runtime-only concern)."""
    import ast

    src = Path(__file__).parents[2] / "src" / "tablespec" / "dbt" / "contracts.py"
    tree = ast.parse(src.read_text(), filename=str(src))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    dbt_imports = {m for m in imported if m == "dbt" or m.startswith("dbt.")}
    assert not dbt_imports, f"contracts.py must not import dbt: {sorted(dbt_imports)}"


def test_profiles_yml_rejects_unknown_target() -> None:
    with pytest.raises(
        ValueError,
        match=r"Unsupported profile target: 'postgres' \(expected one of duckdb, spark, databricks, databricks_notebook\)",
    ):
        render_profiles_yml("tablespec_ingest", target="postgres")
