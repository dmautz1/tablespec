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
# umfs_from_csvs (reader + mapper monkeypatched)
# ---------------------------------------------------------------------------


class _FakeDf:
    columns = ["id", "label"]


class _FakeReader:
    def read(self, source: DelimitedSource, spark: Any) -> _FakeDf:
        assert spark == "spark-session"
        assert source.path is not None
        return _FakeDf()


def _fake_map_dataframe_to_umf(self: Any, df: Any, table: str) -> dict[str, Any]:
    return {
        "table_name": table,
        "columns": [
            {"name": "id", "data_type": "VARCHAR", "nullable": True},
            {"name": "label", "data_type": "VARCHAR", "nullable": True},
        ],
    }


@pytest.fixture
def _patched_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    import tablespec.ingestion as ingestion
    from tablespec.profiling.spark_mapper import SparkToUmfMapper

    monkeypatch.setattr(ingestion, "get_reader", lambda source: _FakeReader())
    monkeypatch.setattr(
        SparkToUmfMapper, "map_dataframe_to_umf", _fake_map_dataframe_to_umf
    )


def test_umfs_from_csvs_dir(tmp_path: Path, _patched_seams: None) -> None:
    for name in ("Orders.csv", "line items.csv"):
        (tmp_path / name).write_text("id,label\n1,a\n")
    (tmp_path / "notes.txt").write_text("ignored")

    umfs = umfs_from_csvs("spark-session", tmp_path)

    # sorted() on paths is ASCII-ordered: "Orders.csv" < "line items.csv".
    assert [u.table_name for u in umfs] == ["orders", "line_items"]
    for umf in umfs:
        src = umf.source
        assert isinstance(src, DelimitedSource)
        assert src.path is not None and src.path.endswith(".csv")
        assert src.delimiter == ","
        assert umf.version == "1.0"
        # nullable bools were wrapped into the strict Nullable shape.
        assert umf.columns[0].nullable.default is True


def test_umfs_from_csvs_empty_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"No \*\.csv"):
        umfs_from_csvs("spark-session", tmp_path)


def test_umfs_from_csvs_duplicate_names_raise(
    tmp_path: Path, _patched_seams: None
) -> None:
    (tmp_path / "orders.csv").write_text("id\n1\n")
    (tmp_path / "Orders .csv").write_text("id\n1\n")
    with pytest.raises(ValueError, match="Duplicate table name"):
        umfs_from_csvs("spark-session", tmp_path)


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
