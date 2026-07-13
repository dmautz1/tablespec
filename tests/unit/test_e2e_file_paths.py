"""Unit tests for the file-oriented e2e entry points (no Spark, no dbt).

``umfs_from_csvs`` (Path C) is tested with the reader seam and Spark mapper
monkeypatched (mirrors ``test_bootstrap.py``); ``umfs_from_spec_dir`` and
``save_specs`` are pure filesystem round-trips.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tablespec.e2e.paths import (
    _csv_table_name,
    save_specs,
    umfs_from_csvs,
    umfs_from_spec_dir,
)
from tablespec.models.umf import UMF, DelimitedSource


def _umf(table: str, *, source: dict[str, Any] | None = None) -> UMF:
    data: dict[str, Any] = {
        "version": "1.0",
        "table_name": table,
        "columns": [
            {"name": "id", "data_type": "INTEGER", "nullable": {"default": False}},
            {
                "name": "label",
                "data_type": "VARCHAR",
                "length": 16,
                "nullable": {"default": True},
            },
        ],
    }
    if source is not None:
        data["source"] = source
    return UMF.model_validate(data)


# ---------------------------------------------------------------------------
# table-name sanitization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("Orders", "orders"),
        ("AB NYC 2019", "ab_nyc_2019"),
        ("sales-2024.v2", "sales_2024_v2"),
        ("2024_sales", "t_2024_sales"),
    ],
)
def test_csv_table_name(stem: str, expected: str) -> None:
    assert _csv_table_name(stem) == expected


def test_csv_table_name_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty table name"):
        _csv_table_name("---")


# ---------------------------------------------------------------------------
# umfs_from_csvs (pure Python: header parse only)
# ---------------------------------------------------------------------------


def test_umfs_from_csvs_dir(tmp_path: Path) -> None:
    for name in ("Orders.csv", "line items.csv"):
        (tmp_path / name).write_text("id,label\n1,a\n")
    (tmp_path / "notes.txt").write_text("ignored")

    umfs = umfs_from_csvs(tmp_path)

    assert [u.table_name for u in umfs] == ["line_items", "orders"]  # by table name
    for umf in umfs:
        src = umf.source
        assert isinstance(src, DelimitedSource)
        assert src.path is not None and src.path.endswith(".csv")
        assert src.delimiter == ","
        assert umf.version == "1.0"
        data_cols = [c for c in umf.columns if c.source != "metadata"]
        assert [c.name for c in data_cols] == ["id", "label"]
        assert all(c.data_type == "VARCHAR" for c in data_cols)
        assert umf.columns[0].nullable.default is True
        # Pipeline-completeness: the canonical provenance columns are appended
        # (source: metadata), so the spec passes `tablespec validate`.
        meta_names = {c.name for c in umf.columns if c.source == "metadata"}
        assert "meta_source_name" in meta_names
        assert "meta_load_dt" in meta_names


def test_umfs_from_csvs_single_file(tmp_path: Path) -> None:
    path = tmp_path / "orders.csv"
    path.write_text("id,label\n1,a\n")
    (umf,) = umfs_from_csvs(path)
    assert umf.table_name == "orders"
    assert isinstance(umf.source, DelimitedSource)
    assert umf.source.path == str(path)


def test_umfs_from_csvs_quoted_header_and_bom(tmp_path: Path) -> None:
    (tmp_path / "q.csv").write_text('﻿"id","label, note"\n1,a\n')
    (umf,) = umfs_from_csvs(tmp_path)
    # Non-identifier headers sanitize into name; the label survives as
    # canonical_name (which the raw model aliases back from).
    assert [c.name for c in umf.columns[:2]] == ["id", "label_note"]
    assert umf.columns[0].canonical_name is None
    assert umf.columns[1].canonical_name == "label, note"


def test_umfs_from_csvs_detects_pipe_and_comma_per_file(tmp_path: Path) -> None:
    (tmp_path / "piped.csv").write_text("id|label\n1|a\n")
    (tmp_path / "commas.csv").write_text("id,label\n1,a\n")

    by_table = {u.table_name: u for u in umfs_from_csvs(tmp_path)}

    assert by_table["piped"].source.delimiter == "|"
    assert by_table["piped"].columns[1].name == "label"
    assert by_table["commas"].source.delimiter == ","


def test_umfs_from_csvs_explicit_delimiter_wins(tmp_path: Path) -> None:
    # A pipe-delimited header with an embedded comma; explicit delimiter is used as-is.
    (tmp_path / "piped.csv").write_text("id|label, extra\n1|a, b\n")
    (umf,) = umfs_from_csvs(tmp_path, delimiter="|")
    assert umf.source.delimiter == "|"
    assert [c.name for c in umf.columns[:2]] == ["id", "label_extra"]
    assert umf.columns[1].canonical_name == "label, extra"


def test_umfs_from_csvs_groups_dated_files_into_families(tmp_path: Path) -> None:
    (tmp_path / "orders_20240101.csv").write_text("id,label\n1,a\n")
    (tmp_path / "orders_20240102.csv").write_text("id,label\n2,b\n")
    (tmp_path / "fees_202401.csv").write_text("id,fee\n1,2.50\n")
    (tmp_path / "members.csv").write_text("id,name\n1,ann\n")

    umfs = {u.table_name: u for u in umfs_from_csvs(tmp_path)}

    assert set(umfs) == {"orders", "fees", "members"}
    orders = umfs["orders"]
    assert isinstance(orders.source, DelimitedSource)
    assert orders.source.path == str(tmp_path / "orders_*.csv")
    assert orders.source.filename_pattern is not None
    assert orders.source.filename_pattern.regex == r"orders_(\d{8})\.csv"
    assert orders.source.filename_pattern.captures == {1: "file_dt"}
    file_dt = next(c for c in orders.columns if c.name == "file_dt")
    assert file_dt.source == "filename"
    assert umfs["fees"].source.filename_pattern.regex == r"fees_(\d{6})\.csv"
    # Generated specs use idempotent snapshot ingestion (glob re-reads all files).
    assert orders.ingestion is not None and orders.ingestion.mode == "snapshot"
    # Undated files stay single-file specs without a pattern.
    assert umfs["members"].source.path == str(tmp_path / "members.csv")
    assert umfs["members"].source.filename_pattern is None


def test_umfs_from_csvs_group_dated_off(tmp_path: Path) -> None:
    (tmp_path / "orders_20240101.csv").write_text("id\n1\n")
    (umf,) = umfs_from_csvs(tmp_path, group_dated=False)
    assert umf.table_name == "orders_20240101"
    assert umf.source.filename_pattern is None


def test_umfs_from_csvs_infer_types(tmp_path: Path) -> None:
    (tmp_path / "claims.csv").write_text(
        "claim_id,member_cd,svc_dt,iso_dt,amount,units,code,blank\n"
        "0100,ABC,20240105,2024-01-05,120.50,3,007,\n"
        "0101,DEF,20240220,2024-02-20,80.00,12,019,\n"
    )
    (umf,) = umfs_from_csvs(tmp_path, infer_types=True)
    cols = {c.name: c for c in umf.columns}
    # Leading zeros keep claim_id / code as VARCHAR.
    assert cols["claim_id"].data_type == "VARCHAR"
    assert cols["code"].data_type == "VARCHAR"
    assert cols["member_cd"].data_type == "VARCHAR"
    assert cols["svc_dt"].data_type == "DATE" and cols["svc_dt"].format == "YYYYMMDD"
    assert cols["iso_dt"].data_type == "DATE" and cols["iso_dt"].format == "YYYY-MM-DD"
    assert cols["amount"].data_type == "DECIMAL" and cols["amount"].scale == 2
    assert cols["units"].data_type == "INTEGER"
    # All-empty columns stay VARCHAR.
    assert cols["blank"].data_type == "VARCHAR"


def test_umfs_from_csvs_empty_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"No \*\.csv"):
        umfs_from_csvs(tmp_path)


def test_umfs_from_csvs_empty_header_raises(tmp_path: Path) -> None:
    (tmp_path / "bad.csv").write_text("id,,label\n1,x,a\n")
    with pytest.raises(ValueError, match="empty"):
        umfs_from_csvs(tmp_path)


def test_umfs_from_csvs_duplicate_names_raise(tmp_path: Path) -> None:
    (tmp_path / "orders.csv").write_text("id\n1\n")
    (tmp_path / "Orders .csv").write_text("id\n1\n")
    with pytest.raises(ValueError, match="Duplicate table name"):
        umfs_from_csvs(tmp_path)


# ---------------------------------------------------------------------------
# save_specs + umfs_from_spec_dir round trip
# ---------------------------------------------------------------------------


def test_save_specs_and_spec_dir_round_trip(tmp_path: Path) -> None:
    src = {"kind": "delimited", "delimiter": ",", "header": True, "path": "/v/a.csv"}
    out = save_specs([_umf("alpha", source=src), _umf("beta")], tmp_path / "specs")

    assert (out / "alpha" / "table.yaml").exists()
    loaded = umfs_from_spec_dir(out)
    assert [u.table_name for u in loaded] == ["alpha", "beta"]
    alpha = loaded[0]
    assert isinstance(alpha.source, DelimitedSource)
    assert alpha.source.path == "/v/a.csv"


def test_save_specs_clears_stale_tables(tmp_path: Path) -> None:
    out_dir = tmp_path / "specs"
    save_specs([_umf("old_table")], out_dir)
    save_specs([_umf("new_table")], out_dir)
    assert not (out_dir / "old_table").exists()
    assert (out_dir / "new_table" / "table.yaml").exists()


def test_umfs_from_spec_dir_flat_json_and_relative_path(tmp_path: Path) -> None:
    data = _umf(
        "gamma",
        source={
            "kind": "delimited",
            "delimiter": ",",
            "header": True,
            "path": "gamma.csv",
        },
    ).model_dump(mode="json", exclude_none=True)
    (tmp_path / "gamma.json").write_text(json.dumps(data))

    loaded = umfs_from_spec_dir(tmp_path, data_dir="/Volumes/cat/sch/raw")
    assert isinstance(loaded[0].source, DelimitedSource)
    assert loaded[0].source.path == "/Volumes/cat/sch/raw/gamma.csv"


def test_umfs_from_spec_dir_xlsx(tmp_path: Path) -> None:
    from tablespec.excel_converter import UMFToExcelConverter

    # The workbook schema sheet requires a table-level canonical_name.
    umf = _umf("delta").model_copy(update={"canonical_name": "Delta"})
    UMFToExcelConverter().convert(umf).save(tmp_path / "delta.xlsx")
    loaded = umfs_from_spec_dir(tmp_path)
    assert [u.table_name for u in loaded] == ["delta"]


def test_umfs_from_spec_dir_empty_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No specs"):
        umfs_from_spec_dir(tmp_path)
