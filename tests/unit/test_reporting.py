"""Tests for the report writer (first consumer of metadata.output_config)."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tablespec.models.umf import UMF
from tablespec.reporting import (
    render_report_filename,
    write_report,
    write_report_from_spark,
)

_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


def _umf(**metadata: Any) -> UMF:
    data: dict[str, Any] = {
        "version": "1.0",
        "table_name": "member_claims_summary",
        "columns": [
            {"name": "member_id", "data_type": "VARCHAR"},
            {
                "name": "member_name",
                "canonical_name": "Member Name",
                "data_type": "VARCHAR",
            },
            {"name": "total_amount", "data_type": "DECIMAL"},
            {"name": "meta_source_name", "data_type": "VARCHAR", "source": "metadata"},
        ],
    }
    if metadata:
        data["metadata"] = metadata
    return UMF.model_validate(data)


_CONFIGURED = _umf(
    output_config={
        "include_footer": True,
        "line_terminator": "CRLF",
        "file_naming_example": "member_claims_summary_YYYYMMDD.csv",
    }
)

_COLUMNS = ["total_amount", "member_id", "member_name", "extra_passthrough"]
_ROWS = [
    (120.5, "M001", "Ana Torres", "x"),
    (75.0, "M002", 'Ben "Benny" Okafor, Jr.', "y"),
]


def test_render_report_filename_substitutes_date_tokens() -> None:
    assert (
        render_report_filename(_CONFIGURED, ext="csv", now=_NOW)
        == "member_claims_summary_20260711.csv"
    )
    assert (
        render_report_filename(_CONFIGURED, ext="xlsx", now=_NOW)
        == "member_claims_summary_20260711.xlsx"
    )


def test_render_report_filename_default_without_example() -> None:
    assert render_report_filename(_umf(), ext="csv") == "member_claims_summary.csv"


def test_write_report_csv_projection_footer_and_crlf(tmp_path: Path) -> None:
    paths = write_report(_CONFIGURED, _COLUMNS, _ROWS, tmp_path, now=_NOW)

    raw = paths.csv_path.read_bytes()
    assert raw.count(b"\r\n") == 4  # header + 2 rows + footer
    lines = raw.decode("utf-8").split("\r\n")
    # Spec order + canonical_name header; metadata + passthrough columns dropped.
    assert lines[0] == "member_id,Member Name,total_amount"
    assert lines[1] == "M001,Ana Torres,120.5"
    assert lines[3] == "2"  # bare row-count footer
    # Embedded quote/comma survives a csv round trip.
    parsed = list(csv.reader(io.StringIO("\r\n".join(lines[:3]))))
    assert parsed[2][1] == 'Ben "Benny" Okafor, Jr.'


def test_write_report_defaults_no_footer_lf(tmp_path: Path) -> None:
    paths = write_report(_umf(), _COLUMNS, _ROWS, tmp_path)
    raw = paths.csv_path.read_bytes()
    assert b"\r\n" not in raw
    # No footer: the last line is the final data row (in SPEC column order).
    assert raw.decode("utf-8").rstrip("\n").split("\n")[-1].startswith("M002,")


def test_write_report_missing_spec_column_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="member_name"):
        write_report(_umf(), ["member_id", "total_amount"], [], tmp_path)


def test_write_report_xlsx_roundtrip(tmp_path: Path) -> None:
    from openpyxl import load_workbook

    paths = write_report(_CONFIGURED, _COLUMNS, _ROWS, tmp_path, now=_NOW)
    sheet = load_workbook(paths.xlsx_path).active
    assert [c.value for c in sheet[1]] == ["member_id", "Member Name", "total_amount"]
    assert sheet[1][0].font.bold
    assert sheet[2][0].value == "M001"
    assert sheet[4][0].value == 2  # footer count row


class _FakeDf:
    columns = _COLUMNS

    def __init__(self, n: int) -> None:
        self._n = n

    def count(self) -> int:
        return self._n

    def collect(self) -> list[tuple]:
        return list(_ROWS)


class _FakeSpark:
    def __init__(self, n: int) -> None:
        self._df = _FakeDf(n)

    def table(self, name: str) -> _FakeDf:
        return self._df


def test_write_report_from_spark(tmp_path: Path) -> None:
    paths = write_report_from_spark(
        _FakeSpark(2), "cat.sch.gold_t", _CONFIGURED, tmp_path, now=_NOW
    )
    assert paths.csv_path.exists() and paths.xlsx_path.exists()


def test_write_report_from_spark_row_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="report limit"):
        write_report_from_spark(
            _FakeSpark(3), "cat.sch.gold_t", _CONFIGURED, tmp_path, row_limit=2
        )
