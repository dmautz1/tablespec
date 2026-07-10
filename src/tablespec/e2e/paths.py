"""The two compile ENTRY POINTS that produce the UMF set fed to the orchestrator.

Both return ``list[UMF]`` so :func:`tablespec.e2e.compile.compile_umfs` is
path-agnostic.

Path A -- existing tables (``bootstrap_from_tables``)
=====================================================
``spark.table(name)`` -> :meth:`SparkToUmfMapper.map_dataframe_to_umf` (SCHEMA-only
inference). RECOMMENDED to additionally ENRICH each table via
:class:`NativeSparkProfiler` + :class:`ProfileToGxMapper` so the compiled suite
carries data-derived expectations.

    Why profile-enriched (recommended), not schema-only:
      * Schema-only inference yields a UMF with column names + types only -- the
        compiled baseline suite degrades to structural + type checks. That is a
        weak runtime contract for tables that ALREADY exist and whose data we can
        observe for free.
      * The profiling seam already exists and emits GX expectation dicts in the
        SAME shape ``execute_staged`` consumes (completeness, uniqueness, ranges,
        value sets, patterns, lengths) -- enrichment is additive and reuses merged
        code, no new harness.
      * Cost is bounded: profiling is opt-in per call and the backbone runs on a
        local Spark/Connect session, so the extra scan is acceptable for the demo.
    Therefore Path A DEFAULTS to ``profile=True`` and the enriched expectations are
    handed to the orchestrator as precompiled ``suites``; ``profile=False`` remains
    available for the pure schema-only contract.

Path B -- specs (``bootstrap_from_specs``)
==========================================
:func:`tablespec.models.umf.load_umf_from_yaml` per spec file. No Spark required to
LOAD; the backbone still needs a session to EXECUTE the compiled artifacts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tablespec.models.umf import UMF


def umfs_from_tables(
    spark: Any,
    table_names: list[str],
    *,
    profile: bool = True,
    infer_key_candidates: bool = False,
    key_candidates_out: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[list[UMF], dict[str, list[dict]]]:
    """Path A: infer a UMF set from existing Spark tables (optionally enriched).

    For each name: ``spark.table(name)`` -> ``SparkToUmfMapper.map_dataframe_to_umf``
    (schema-only) -> :class:`UMF`. When *profile* is True, additionally run
    ``NativeSparkProfiler.profile`` + ``ProfileToGxMapper.build_expectations`` and
    return those expectation dicts so the orchestrator persists them as the
    compiled suite for that table.

    Args:
        spark: an active Spark (classic or Connect) session.
        table_names: tables to reflect + (optionally) profile.
        profile: enrich with profile-derived expectations (recommended default).
        infer_key_candidates: ask the native profiler to attach advisory key
            candidates when profiling runs.
        key_candidates_out: optional mutable sink populated with serialized
            candidates keyed by bare table name.

    Returns:
        ``(umfs, suites)`` where ``suites`` maps table name -> precompiled
        expectation list (empty dict when ``profile`` is False -- the orchestrator
        then generates baseline suites from the UMF).
    """
    from tablespec.models.umf import UMF
    from tablespec.profiling.spark_mapper import SparkToUmfMapper

    mapper = SparkToUmfMapper()
    umfs: list[UMF] = []
    suites: dict[str, list[dict]] = {}

    for full_name in table_names:
        # Reflect the (possibly schema-qualified) table to a bare-name UMF: the
        # compiled raw/ingested tables are unqualified (raw_<t>/ingested_<t>).
        df = spark.table(full_name)
        table = full_name.split(".")[-1]
        umf_data = _to_strict_umf_data(mapper.map_dataframe_to_umf(df, table))
        umfs.append(UMF(**umf_data))

        if profile:
            from tablespec.profiling.gx_expectation_builder import ProfileToGxMapper
            from tablespec.profiling.native_profiler import NativeSparkProfiler

            profile_result = NativeSparkProfiler(
                spark,
                infer_key_candidates=infer_key_candidates,
            ).profile(df)
            suites[table] = ProfileToGxMapper().build_expectations(profile_result)
            if key_candidates_out is not None:
                key_candidates_out[table] = [
                    asdict(candidate) for candidate in profile_result.key_candidates
                ]

    return umfs, suites


def umfs_from_specs(spec_paths: list[str | Path]) -> list[UMF]:
    """Path B: load a UMF set from spec YAML files via ``load_umf_from_yaml``.

    Args:
        spec_paths: UMF ``.yaml`` spec files (one table each).

    Returns:
        The loaded :class:`UMF` models, in input order.
    """
    from tablespec.models.umf import load_umf_from_yaml

    return [load_umf_from_yaml(p) for p in spec_paths]


def _csv_table_name(stem: str) -> str:
    """Sanitize a CSV filename stem into a table name (``t_``-prefixed if digit-led)."""
    import re

    table = re.sub(r"[^0-9a-zA-Z_]+", "_", stem).strip("_").lower()
    if table and table[0].isdigit():
        table = f"t_{table}"
    if not table:
        msg = f"CSV filename {stem!r} sanitizes to an empty table name"
        raise ValueError(msg)
    return table


def _read_encoding(encoding: str) -> str:
    """The codec used to READ csv text: utf-8-sig strips a leading BOM."""
    is_utf8 = encoding.lower().replace("-", "").replace("_", "") == "utf8"
    return "utf-8-sig" if is_utf8 else encoding


def _sniff_delimiter(path: Path, encoding: str) -> str:
    """Pick pipe or comma for *path* by counting them in the header line."""
    with path.open(encoding=_read_encoding(encoding), errors="replace") as f:
        first = f.readline()
    return "|" if first.count("|") > first.count(",") else ","


def _csv_header(
    path: Path, *, delimiter: str, quote_char: str | None, encoding: str
) -> list[str]:
    """Read *path*'s header row into a clean column-name list (BOM stripped)."""
    import csv

    with path.open(encoding=_read_encoding(encoding), newline="") as f:
        reader = csv.reader(f, delimiter=delimiter, quotechar=quote_char or '"')
        header = next(reader, [])
    names = [h.strip() for h in header]
    if not names or any(not n for n in names):
        msg = f"{path} has an empty or partially-empty header row: {header!r}"
        raise ValueError(msg)
    if len(set(names)) != len(names):
        msg = f"{path} has duplicate header columns: {names!r}"
        raise ValueError(msg)
    return names


def _csv_column(header: str) -> dict[str, Any]:
    """Build the starter VARCHAR column dict for a header label.

    UMF column names must match ``^[A-Za-z][A-Za-z0-9_]*$``; a header that
    does not is sanitized into ``name`` while the original label is preserved
    as ``canonical_name`` (the UMF field for file-header labels) -- the
    emitted raw model then aliases ``<header> AS <name>``.
    """
    import re

    name = re.sub(r"[^0-9a-zA-Z_]+", "_", header).strip("_")
    if not name:
        msg = f"header column {header!r} sanitizes to an empty name"
        raise ValueError(msg)
    if not name[0].isalpha():
        name = f"c_{name}"
    col: dict[str, Any] = {
        "name": name,
        "data_type": "VARCHAR",
        "nullable": {"default": True},
    }
    if name != header:
        col["canonical_name"] = header
    return col


def umfs_from_csvs(
    csv_dir: str | Path | Sequence[str | Path],
    *,
    delimiter: str | None = None,
    quote_char: str | None = '"',
    encoding: str = "UTF-8",
) -> list[UMF]:
    """Path C: derive FILE-BACKED UMFs from delimited files (one table per file).

    Pure Python -- no Spark. Only the header row is read (the raw landing is
    all-STRING, so every column starts as VARCHAR); the returned UMF carries a
    ``source: {kind: delimited, path: <file>}`` declaration, so the dbt
    emitters render a file-reading ``raw_<t>`` model and ``dbt build`` ingests
    the file itself -- nothing is pre-loaded (see ``tablespec.dbt.raw_models``).

    Like JDBC discovery, each spec is made pipeline-complete by appending the
    canonical provenance metadata columns
    (:data:`tablespec.ingestion.constants.PROVENANCE_COLUMNS`, ``source:
    metadata``) so it passes ``tablespec validate`` unmodified; being
    metadata-sourced, they are synthesized at ingest and never read from the
    file.

    Args:
        csv_dir: a single ``.csv`` file, a directory (every ``*.csv`` in it
            becomes a table, table name = sanitized filename stem), or an
            explicit sequence of file paths.
        delimiter: the field delimiter. ``None`` (default) detects pipe vs
            comma PER FILE from the header line, so a directory may mix both.
        quote_char / encoding: reader options recorded in each UMF's source
            declaration (and used for the header parse).

    Returns:
        The derived :class:`UMF` models, sorted by file name.
    """
    from tablespec.ingestion.constants import PROVENANCE_COLUMNS
    from tablespec.models.umf import UMF, DelimitedSource

    if isinstance(csv_dir, (str, Path)):
        base = Path(csv_dir)
        if base.is_file():
            paths = [base]
        else:
            paths = sorted(base.glob("*.csv"))
            if not paths:
                msg = f"No *.csv files in {base}"
                raise FileNotFoundError(msg)
    else:
        paths = [Path(p) for p in csv_dir]

    umfs: list[UMF] = []
    seen: set[str] = set()
    for path in paths:
        table = _csv_table_name(path.stem)
        if table in seen:
            msg = f"Duplicate table name {table!r} (from {path.name})"
            raise ValueError(msg)
        seen.add(table)
        sep = delimiter or _sniff_delimiter(path, encoding)
        headers = _csv_header(
            path, delimiter=sep, quote_char=quote_char, encoding=encoding
        )
        columns = [_csv_column(h) for h in headers]
        names = [c["name"] for c in columns]
        if len(set(names)) != len(names):
            msg = f"{path} headers sanitize to duplicate column names: {names!r}"
            raise ValueError(msg)
        columns.extend(
            dict(prov)
            for prov in PROVENANCE_COLUMNS.values()
            if prov["name"] not in set(names)
        )
        source = DelimitedSource(
            kind="delimited",
            delimiter=sep,
            header=True,
            quote_char=quote_char,
            encoding=encoding,
            path=str(path),
        )
        umfs.append(
            UMF.model_validate(
                {
                    "version": "1.0",
                    "table_name": table,
                    "columns": columns,
                    "source": source,
                }
            )
        )
    return umfs


def umfs_from_spec_dir(
    spec_dir: str | Path, *, data_dir: str | Path | None = None
) -> list[UMF]:
    """Path B, directory form: load every spec under *spec_dir*.

    Accepts split-format table dirs (``**/table.yaml``, preferred when present),
    flat ``*.yaml``/``*.yml`` specs, ``*.json`` interchange files, and ``*.xlsx``
    schema workbooks (``ExcelToUMFConverter``).

    When *data_dir* is given, a relative delimited ``source.path`` in a spec is
    resolved against it (the loaded models are copied, never mutated on disk).

    Returns:
        The loaded :class:`UMF` models.
    """
    from tablespec.models.umf import DelimitedSource, load_umf_from_yaml
    from tablespec.umf_loader import UMFLoader

    base = Path(spec_dir)
    loader = UMFLoader()
    split_dirs = sorted({p.parent for p in base.rglob("table.yaml")})
    if split_dirs:
        umfs = [loader.load(d) for d in split_dirs]
    else:
        umfs = [
            load_umf_from_yaml(p)
            for p in sorted([*base.glob("*.yaml"), *base.glob("*.yml")])
        ]
        umfs += [loader.load(p) for p in sorted(base.glob("*.json"))]
        if xlsx := sorted(base.glob("*.xlsx")):
            from tablespec.excel_converter import ExcelToUMFConverter

            umfs += [ExcelToUMFConverter().convert(p)[0] for p in xlsx]
    if not umfs:
        msg = (
            f"No specs in {base} (looked for split table.yaml dirs, "
            "*.yaml/*.yml, *.json, *.xlsx)"
        )
        raise FileNotFoundError(msg)

    if data_dir is None:
        return umfs
    resolved: list[UMF] = []
    for umf in umfs:
        src = umf.source
        if (
            isinstance(src, DelimitedSource)
            and src.path
            and not Path(src.path).is_absolute()
        ):
            umf = umf.model_copy(
                update={
                    "source": src.model_copy(
                        update={"path": str(Path(data_dir) / src.path)}
                    )
                }
            )
        resolved.append(umf)
    return resolved


def save_specs(umfs: list[UMF], out_dir: str | Path, *, validate: bool = True) -> Path:
    """Persist a UMF set as editable split-format specs under *out_dir*.

    *out_dir* is cleared first (stale specs from removed tables would otherwise
    survive re-runs); each table lands at ``<out_dir>/<table_name>/``. With
    *validate* (default) every spec is checked against the UMF JSON schema and
    an invalid one raises.

    Returns:
        *out_dir* as a :class:`Path`.
    """
    import shutil

    from tablespec.umf_loader import UMFLoader
    from tablespec.umf_validator import UMFValidator

    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    loader = UMFLoader()
    validator = UMFValidator() if validate else None
    for umf in umfs:
        if validator is not None:
            validator.validate_data(
                umf.model_dump(mode="json", exclude_none=True),
                raise_on_error=True,
                source_name=umf.table_name,
            )
        loader.save(umf, out / umf.table_name)
    return out


def _to_strict_umf_data(base: dict[str, Any]) -> dict[str, Any]:
    """Normalize ``SparkToUmfMapper`` output into the strict UMF model shape.

    ``map_dataframe_to_umf`` emits a BASE schema dict (``nullable: bool``, no
    ``version``) tuned for downstream tooling; the strict :class:`UMF` model the
    compile orchestrator consumes requires a ``version`` and a ``Nullable``-shaped
    ``nullable``. This adapts the reflected dict in place so Path A produces the
    same ``UMF`` type as Path B.
    """
    data = dict(base)
    data.setdefault("version", "1.0")
    cols = []
    for col in data.get("columns", []):
        col = dict(col)
        nullable = col.get("nullable")
        if isinstance(nullable, bool):
            col["nullable"] = {"default": nullable}
        cols.append(col)
    data["columns"] = cols
    return data
