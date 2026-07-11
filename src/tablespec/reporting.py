"""Write a spec's table data as report FILES (CSV + Excel).

The first consumer of ``UMF.metadata.output_config`` (previously spec-only
metadata): a spec that declares an ``output_config`` is a physical report --
this module renders its rows as a delimited file (honoring
``line_terminator`` and the ``include_footer`` row-count footer, the same
bare-count convention as ``tablespec.merge``'s disposition footer) plus an
Excel workbook, named from ``file_naming_example``'s date tokens.

Engine-neutral: callers hand in ``columns`` + ``rows`` (any engine's collect
output); :func:`write_report_from_spark` is the notebook convenience. Same-day
re-runs overwrite the dated files -- a report is the day's snapshot. Headers
use each column's ``canonical_name`` falling back to ``name`` (the documented
UMF contract for output file headers, same as seeds/sample-data).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tablespec.models.umf import UMF, OutputConfig

#: Column ``source`` values that are pipeline plumbing, not report content.
_NON_REPORT_SOURCES: frozenset[str] = frozenset({"metadata"})

_DATE_TOKENS: tuple[tuple[str, str], ...] = (
    ("YYYY", "%Y"),
    ("MM", "%m"),
    ("DD", "%d"),
)


@dataclass(frozen=True)
class ReportPaths:
    """Where :func:`write_report` landed the two report artifacts."""

    csv_path: Path
    xlsx_path: Path


def _output_config(umf: UMF) -> OutputConfig:
    from tablespec.models.umf import OutputConfig

    if umf.metadata is not None and umf.metadata.output_config is not None:
        return umf.metadata.output_config
    return OutputConfig()


def render_report_filename(umf: UMF, *, ext: str, now: datetime | None = None) -> str:
    """The report file name for *umf* with extension *ext*.

    Substitutes the date tokens ``YYYY``/``MM``/``DD`` (longest first) in
    ``output_config.file_naming_example``'s stem against *now* and swaps the
    extension. Without an example the name is the deterministic
    ``<table_name>.<ext>``.
    """
    now = now or datetime.now(tz=timezone.utc)
    example = _output_config(umf).file_naming_example
    if not example:
        return f"{umf.table_name}.{ext}"
    stem = Path(example).stem
    for token, fmt in _DATE_TOKENS:
        stem = stem.replace(token, now.strftime(fmt))
    return f"{stem}.{ext}"


def _report_columns(umf: UMF) -> list[tuple[str, str]]:
    """``(spec_name, header_label)`` per report column (plumbing excluded)."""
    return [
        (col.name, col.canonical_name or col.name)
        for col in umf.columns
        if (col.source or "data") not in _NON_REPORT_SOURCES
    ]


def _line_terminator(config: OutputConfig) -> str:
    value = config.line_terminator
    if value in (None, "", "LF", "\n"):
        return "\n"
    if value in ("CRLF", "\r\n"):
        return "\r\n"
    if value in ("CR", "\r"):
        return "\r"
    return str(value)


def write_report(
    umf: UMF,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    out_dir: str | Path,
    *,
    now: datetime | None = None,
) -> ReportPaths:
    """Write *umf*'s report as ``<name>.csv`` + ``<name>.xlsx`` under *out_dir*.

    Rows are projected/reordered to the SPEC's column order; *columns* names
    the incoming row positions (extra incoming columns -- e.g. gold
    passthroughs -- are dropped, a spec column missing from *columns* raises
    ``KeyError`` naming it). The CSV honors ``output_config.line_terminator``
    and, with ``include_footer``, ends with the bare row count; the workbook
    gets a bold header row and the same count as a final footer row.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font

    config = _output_config(umf)
    spec_columns = _report_columns(umf)
    index: dict[str, int] = {name: i for i, name in enumerate(columns)}
    missing = [name for name, _ in spec_columns if name not in index]
    if missing:
        msg = (
            f"Report columns missing from the data for {umf.table_name!r}: "
            f"{missing} (data columns: {list(columns)})"
        )
        raise KeyError(msg)
    positions = [index[name] for name, _ in spec_columns]
    headers = [label for _, label in spec_columns]
    projected = [[row[i] for i in positions] for row in rows]

    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    csv_path = base / render_report_filename(umf, ext="csv", now=now)
    xlsx_path = base / render_report_filename(umf, ext="xlsx", now=now)

    terminator = _line_terminator(config)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator=terminator)
        writer.writerow(headers)
        writer.writerows(projected)
        if config.include_footer:
            # The bare-count footer convention (see tablespec.merge).
            f.write(f"{len(projected)}{terminator}")

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None  # a fresh Workbook always has an active sheet
    sheet.title = umf.table_name[:31]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in projected:
        sheet.append(list(row))
    if config.include_footer:
        sheet.append([len(projected)])
    workbook.save(xlsx_path)

    return ReportPaths(csv_path=csv_path, xlsx_path=xlsx_path)


def write_report_from_spark(
    spark: Any,
    table_name: str,
    umf: UMF,
    out_dir: str | Path,
    *,
    row_limit: int = 100_000,
    now: datetime | None = None,
) -> ReportPaths:
    """Notebook convenience: export *table_name* per *umf*'s output_config.

    Reports are files, not datasets: a table over *row_limit* rows raises
    rather than silently producing an unusable artifact.
    """
    df = spark.table(table_name)
    count = df.count()
    if count > row_limit:
        msg = (
            f"{table_name} has {count:,} rows -- over the report limit of "
            f"{row_limit:,}. Reports are files, not datasets; aggregate "
            "further or raise row_limit explicitly."
        )
        raise ValueError(msg)
    rows = [tuple(r) for r in df.collect()]
    return write_report(umf, df.columns, rows, out_dir, now=now)


__all__ = [
    "ReportPaths",
    "render_report_filename",
    "write_report",
    "write_report_from_spark",
]
