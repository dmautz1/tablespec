"""Render file-reading ``raw_<t>`` landing MODELS for file-backed UMFs.

Historically the emitted dbt projects declare every raw landing table as a dbt
*source* (``models/sources.yml``) and expect ``raw_<t>`` to be created upstream.
When a UMF pins its input file -- ``source: {kind: delimited, path: ...}`` -- the
emitters can go one step further and emit ``raw_<t>`` as a dbt **model** that
reads the file directly, so ``dbt build`` performs the raw landing itself and the
downstream cast model consumes ``{{ ref('raw_<t>') }}`` instead of
``{{ source('raw', 'raw_<t>') }}``.

The rendered landing preserves the all-STRING raw contract (ADR-007) and the
raw-table meta columns the shared ingest SELECT depends on: the incremental+PK
dedup window orders by ``_load_ts`` (``ingest_generator._DEFAULT_ORDER_BY``), so
the model synthesizes ``_source_file`` (the declared path) and ``_load_ts``
(build timestamp) alongside the file's columns.

Dialect support (``RAW_FILE_MODEL_DIALECTS``):

  * ``databricks`` -- ``read_files(<path>, format => 'csv', header => true,
    inferSchema => false, ...)``; ``inferSchema => false`` lands every column as
    STRING. Explicit column list (never ``SELECT *``): ``read_files`` appends a
    ``_rescued_data`` column, and the explicit list fails the build loudly when
    the file header does not match the UMF columns.
  * ``duckdb`` -- ``read_csv(<path>, header=true, all_varchar=true, ...)`` for
    local parity: the exact same feature is testable without a cluster.

``spark`` (the plain OSS session lane) has no ``read_files`` table function, so
file-backed UMFs fall back to the source-declaration behavior there -- the local
conformance harnesses pre-create raw tables and keep working unchanged.

The fallback is deliberately conservative: a declared source carrying reader
knobs the file-read functions cannot express uniformly (``header=False``,
``skip_rows``, ``footer_rows``, ``line_terminator``, ``comment_char``,
``escape_char``, ``null_value``/``null_escape``, or a non-UTF-8 encoding) keeps
the source-declaration behavior rather than risking a silent misparse.

Pure-Python text emission: importing this module pulls in NO dbt package (the
same encapsulation contract as the rest of ``tablespec.dbt``).
"""

from __future__ import annotations

from typing import Any, Mapping

from tablespec.models.umf import UMF, DelimitedSource

#: Dialects whose SQL can read a delimited file directly in a model body.
RAW_FILE_MODEL_DIALECTS: tuple[str, ...] = ("databricks", "duckdb")


def supports_raw_file_models(dialect: str) -> bool:
    """Whether *dialect* can render a file-reading raw landing model."""
    return dialect in RAW_FILE_MODEL_DIALECTS


def _is_utf8(encoding: str) -> bool:
    return encoding.lower().replace("-", "").replace("_", "") == "utf8"


def file_backed_source(umf_data: Mapping[str, Any] | UMF) -> DelimitedSource | None:
    """The UMF's delimited source with a usable ``path``, else ``None``.

    ``None`` means "keep the source-declaration behavior": the UMF declares no
    ``source:``, the source is not delimited, it has no path, or it carries
    reader knobs ``read_files``/``read_csv`` cannot express (see module
    docstring) -- falling back beats silently misparsing the file.

    Accepts both the ``model_dump`` dict shape (the single-table generator
    passes dicts) and the ``UMF`` model (the DAG generator passes models).
    """
    raw = umf_data.source if isinstance(umf_data, UMF) else umf_data.get("source")
    if raw is None:
        return None
    if isinstance(raw, DelimitedSource):
        source = raw
    elif isinstance(raw, Mapping):
        if raw.get("kind") != "delimited":
            return None
        source = DelimitedSource.model_validate(raw)
    else:
        return None

    if not (source.path and source.path.strip()):
        return None
    unexpressible = (
        not source.header
        or source.skip_rows > 0
        or bool(source.footer_rows)
        or source.line_terminator is not None
        or source.comment_char is not None
        or source.escape_char is not None
        or source.null_value is not None
        or source.null_escape is not None
        or not _is_utf8(source.encoding)
    )
    if unexpressible:
        return None
    return source


def _sql_str(value: str) -> str:
    """Render *value* as a single-quoted SQL string literal (quotes doubled)."""
    return "'" + value.replace("'", "''") + "'"


def _ident(name: str, *, dialect: str) -> str:
    """Quote a column identifier for *dialect* (backticks vs double quotes)."""
    if dialect == "databricks":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def _read_relation(source: DelimitedSource, *, dialect: str) -> str:
    """Render the file-reading table function for *source* under *dialect*.

    When ``quote_char`` is undeclared the argument is omitted so each engine
    keeps its default (``"`` in both ``read_files`` and ``read_csv``).
    """
    assert source.path is not None  # guaranteed by file_backed_source
    path = _sql_str(source.path)
    if dialect == "databricks":
        args = [
            path,
            "format => 'csv'",
            "header => true",
            "inferSchema => false",
            f"sep => {_sql_str(source.delimiter)}",
        ]
        if source.quote_char is not None:
            args.append(f"quote => {_sql_str(source.quote_char)}")
        joined = ",\n    ".join(args)
        return f"read_files(\n    {joined}\n)"
    # duckdb
    args = [
        path,
        "header=true",
        "all_varchar=true",
        f"delim={_sql_str(source.delimiter)}",
    ]
    if source.quote_char is not None:
        args.append(f"quote={_sql_str(source.quote_char)}")
    joined = ",\n    ".join(args)
    return f"read_csv(\n    {joined}\n)"


def render_raw_model_sql(
    table: str,
    source: DelimitedSource,
    column_names: list[str],
    *,
    dialect: str,
) -> str:
    """Render the ``raw_<table>`` file-reading landing model body.

    The SELECT lists the UMF columns explicitly (all-STRING via the reader
    options) and appends the raw-contract meta columns: ``_source_file`` (the
    declared path literal) and ``_load_ts`` (the build timestamp; duckdb's
    ``current_timestamp`` is TIMESTAMPTZ, so it is CAST to the raw contract's
    plain TIMESTAMP there).
    """
    if not supports_raw_file_models(dialect):
        msg = (
            f"dialect {dialect!r} cannot render a file-reading raw model "
            f"(supported: {', '.join(RAW_FILE_MODEL_DIALECTS)})"
        )
        raise ValueError(msg)

    assert source.path is not None  # guaranteed by file_backed_source
    load_ts = (
        "current_timestamp() AS _load_ts"
        if dialect == "databricks"
        else "CAST(current_timestamp AS TIMESTAMP) AS _load_ts"
    )
    select_lines = [f"    {_ident(name, dialect=dialect)}," for name in column_names]
    select_lines.append(f"    {_sql_str(source.path)} AS _source_file,")
    select_lines.append(f"    {load_ts}")

    note = (
        f"-- File-backed raw landing for {table}: dbt reads the declared source\n"
        "-- file directly and materializes the all-STRING landing table (ADR-007)\n"
        "-- plus the raw-contract meta columns (_source_file, _load_ts).\n"
    )
    body = "\n".join(
        [
            "SELECT",
            *select_lines,
            f"FROM {_read_relation(source, dialect=dialect)}",
        ]
    )
    config = "{{\n    config(\n        materialized='table',\n    )\n}}"
    return f"{config}\n\n{note}{body}\n"


__all__ = [
    "RAW_FILE_MODEL_DIALECTS",
    "file_backed_source",
    "render_raw_model_sql",
    "supports_raw_file_models",
]
