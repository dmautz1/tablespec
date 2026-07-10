"""Functional tests for file-reading ``raw_<t>`` landing models.

When a UMF declares ``source: {kind: delimited, path: ...}`` and the dialect can
read files (databricks/duckdb), the emitters render ``raw_<t>`` as a dbt MODEL
(``read_files`` / ``read_csv``) and the cast model consumes ``ref('raw_<t>')``;
otherwise the classic sources.yml declaration + ``source()`` edge is preserved
byte-identically. Pure text emission -- no dbt required.
"""

from __future__ import annotations

from typing import Any

import pytest

from tablespec.dbt.project import generate_dbt_dag_project
from tablespec.dbt.raw_models import (
    file_backed_source,
    render_raw_model_sql,
    supports_raw_file_models,
)
from tablespec.dbt.single_table import generate_dbt_project
from tablespec.models.umf import UMF, DelimitedSource


def _umf_data(table: str = "orders", **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": "1.0",
        "table_name": table,
        "primary_key": [f"{table}_id"],
        "ingestion": {"mode": "incremental"},
        "columns": [
            {
                "name": f"{table}_id",
                "data_type": "INTEGER",
                "nullable": {"default": False},
            },
            {
                "name": "label",
                "data_type": "VARCHAR",
                "length": 32,
                "nullable": {"default": True},
            },
        ],
    }
    data.update(overrides)
    return data


def _delimited_source(**overrides: Any) -> dict[str, Any]:
    source: dict[str, Any] = {
        "kind": "delimited",
        "delimiter": ",",
        "header": True,
        "quote_char": '"',
        "path": "/data/in/orders.csv",
    }
    source.update(overrides)
    return source


# ---------------------------------------------------------------------------
# file_backed_source detection
# ---------------------------------------------------------------------------


def test_detects_delimited_source_with_path_from_dict_and_model() -> None:
    data = _umf_data(source=_delimited_source())
    from_dict = file_backed_source(data)
    from_model = file_backed_source(UMF.model_validate(data))
    assert isinstance(from_dict, DelimitedSource)
    assert isinstance(from_model, DelimitedSource)
    assert from_dict.path == from_model.path == "/data/in/orders.csv"


def test_no_source_or_no_path_is_not_file_backed() -> None:
    assert file_backed_source(_umf_data()) is None
    assert file_backed_source(_umf_data(source=_delimited_source(path=None))) is None
    assert file_backed_source(_umf_data(source=_delimited_source(path="  "))) is None


@pytest.mark.parametrize(
    "knobs",
    [
        {"header": False},
        {"skip_rows": 2},
        {"footer_rows": 1},
        {"line_terminator": "CRLF"},
        {"comment_char": "#"},
        {"escape_char": "\\"},
        {"null_value": "NULL"},
        {"null_escape": "\\N"},
        {"encoding": "latin-1"},
    ],
)
def test_unexpressible_reader_knobs_fall_back(knobs: dict[str, Any]) -> None:
    """Knobs read_files/read_csv cannot express keep the source declaration."""
    data = _umf_data(source=_delimited_source(**knobs))
    assert file_backed_source(data) is None


def test_dialect_support_matrix() -> None:
    assert supports_raw_file_models("databricks")
    assert supports_raw_file_models("duckdb")
    assert not supports_raw_file_models("spark")


# ---------------------------------------------------------------------------
# render_raw_model_sql
# ---------------------------------------------------------------------------


def test_databricks_raw_model_sql() -> None:
    source = DelimitedSource.model_validate(_delimited_source(delimiter="|"))
    sql = render_raw_model_sql(
        "orders", source, ["orders_id", "label"], dialect="databricks"
    )
    assert "materialized='table'" in sql
    assert "read_files(" in sql
    assert "'/data/in/orders.csv'" in sql
    assert "format => 'csv'" in sql
    assert "header => true" in sql
    assert "inferSchema => false" in sql
    assert "sep => '|'" in sql
    assert "quote => '\"'" in sql
    # Explicit backticked column list + synthesized raw-contract meta columns.
    assert "    `orders_id`," in sql
    assert "    `label`," in sql
    assert "'/data/in/orders.csv' AS _source_file" in sql
    assert "current_timestamp() AS _load_ts" in sql
    assert "SELECT *" not in sql


def test_duckdb_raw_model_sql() -> None:
    source = DelimitedSource.model_validate(_delimited_source())
    sql = render_raw_model_sql(
        "orders", source, ["orders_id", "label"], dialect="duckdb"
    )
    assert "read_csv(" in sql
    assert "header=true" in sql
    assert "all_varchar=true" in sql
    assert "delim=','" in sql
    assert "quote='\"'" in sql
    assert '    "orders_id",' in sql
    # duckdb current_timestamp is TIMESTAMPTZ; the raw contract wants TIMESTAMP.
    assert "CAST(current_timestamp AS TIMESTAMP) AS _load_ts" in sql


def test_quote_argument_omitted_when_undeclared() -> None:
    source = DelimitedSource.model_validate(_delimited_source(quote_char=None))
    for dialect in ("databricks", "duckdb"):
        sql = render_raw_model_sql("orders", source, ["orders_id"], dialect=dialect)
        assert "quote" not in sql


def test_sql_literal_single_quotes_are_doubled() -> None:
    source = DelimitedSource.model_validate(
        _delimited_source(path="/data/o'brien/orders.csv")
    )
    sql = render_raw_model_sql("orders", source, ["orders_id"], dialect="duckdb")
    assert "'/data/o''brien/orders.csv'" in sql


def test_unsupported_dialect_raises() -> None:
    source = DelimitedSource.model_validate(_delimited_source())
    with pytest.raises(ValueError, match="spark"):
        render_raw_model_sql("orders", source, ["orders_id"], dialect="spark")


# ---------------------------------------------------------------------------
# single-table project wiring
# ---------------------------------------------------------------------------


def test_single_table_file_backed_emits_raw_model_and_ref() -> None:
    files = generate_dbt_project(
        _umf_data(source=_delimited_source()), dialect="duckdb", target="duckdb"
    )
    assert "models/raw_orders.sql" in files
    assert "read_csv(" in files["models/raw_orders.sql"]
    assert "{{ ref('raw_orders') }}" in files["models/orders.sql"]
    assert "source('raw'" not in files["models/orders.sql"]
    # Every raw relation is model-backed -> no sources.yml at all.
    assert "models/sources.yml" not in files


def test_single_table_path_less_output_unchanged() -> None:
    files = generate_dbt_project(_umf_data(), dialect="duckdb", target="duckdb")
    assert "models/raw_orders.sql" not in files
    assert "{{ source('raw', 'raw_orders') }}" in files["models/orders.sql"]
    assert "raw_orders" in files["models/sources.yml"]


def test_single_table_spark_dialect_falls_back_to_source() -> None:
    files = generate_dbt_project(
        _umf_data(source=_delimited_source()), dialect="spark", target="spark"
    )
    assert "models/raw_orders.sql" not in files
    assert "{{ source('raw', 'raw_orders') }}" in files["models/orders.sql"]
    assert "raw_orders" in files["models/sources.yml"]


def test_single_table_mixed_related_set() -> None:
    """A file-backed primary with a path-less related sibling: one raw model,
    and sources.yml keeps only the sibling's declaration."""
    related = UMF.model_validate(_umf_data(table="customers"))
    files = generate_dbt_project(
        _umf_data(source=_delimited_source()),
        dialect="duckdb",
        target="duckdb",
        related=[related],
    )
    assert "models/raw_orders.sql" in files
    assert "{{ ref('raw_orders') }}" in files["models/orders.sql"]
    assert "{{ source('raw', 'raw_customers') }}" in files["models/customers.sql"]
    sources = files["models/sources.yml"]
    assert "raw_customers" in sources
    assert "raw_orders" not in sources


# ---------------------------------------------------------------------------
# DAG project wiring
# ---------------------------------------------------------------------------


def test_dag_project_mixed_set() -> None:
    file_backed = UMF.model_validate(_umf_data(source=_delimited_source()))
    path_less = UMF.model_validate(_umf_data(table="customers"))
    files = generate_dbt_dag_project(
        [file_backed, path_less], dialect="databricks", target="databricks"
    )
    assert "models/staging/raw_orders.sql" in files
    assert "read_files(" in files["models/staging/raw_orders.sql"]
    assert "{{ ref('raw_orders') }}" in files["models/staging/ingested_orders.sql"]
    assert (
        "{{ source('raw', 'raw_customers') }}"
        in files["models/staging/ingested_customers.sql"]
    )
    sources = files["models/sources.yml"]
    assert "raw_customers" in sources
    assert "raw_orders" not in sources
    # Raw models carry no schema.yml entry (no contract on the all-STRING landing).
    assert "- name: raw_orders" not in files["models/schema.yml"]


def test_dag_project_all_file_backed_omits_sources_yml() -> None:
    umfs = [
        UMF.model_validate(
            _umf_data(
                table=t,
                source=_delimited_source(path=f"/data/in/{t}.csv"),
            )
        )
        for t in ("orders", "customers")
    ]
    files = generate_dbt_dag_project(umfs, dialect="databricks", target="databricks")
    assert "models/sources.yml" not in files
    assert "models/staging/raw_orders.sql" in files
    assert "models/staging/raw_customers.sql" in files


def test_dag_project_path_less_output_unchanged() -> None:
    umfs = [UMF.model_validate(_umf_data(table=t)) for t in ("orders", "customers")]
    files = generate_dbt_dag_project(umfs, dialect="duckdb", target="duckdb")
    assert "models/sources.yml" in files
    assert "raw_orders" in files["models/sources.yml"]
    assert "raw_customers" in files["models/sources.yml"]
    assert not [f for f in files if "raw_" in f and f.endswith(".sql")]
