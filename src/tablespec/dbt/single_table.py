"""Generate a dbt(+DuckDB) project that ingests a raw landing table per a UMF spec.

This is the second emitter of the raw->ingest design. It shares the cast SELECT and
dedup-latest window with the committed Databricks artifact via
:func:`tablespec.schemas.ingest_generator.build_ingest_select`; the two emitters
differ only in *packaging* and *write strategy*.

dbt owns the write -- the model body never hand-writes a MERGE/INSERT. The write is
expressed entirely through the model ``config`` (derived from ``ingestion.mode`` +
``primary_key``):

  * incremental + primary_key  -> ``materialized='incremental'``,
                                  ``incremental_strategy='merge'``, ``unique_key=[...]``
  * incremental, no primary_key -> ``materialized='incremental'`` (append; no key)
  * snapshot                    -> ``materialized='table'`` (full drop/reload; this is a
                                    plain table rebuild, NOT dbt's SCD2 snapshot block)

The model reads from a dbt ``source`` (the all-STRING ``raw_<table>`` landing table)
and emits the typed cast columns. For the incremental+pk case the model body applies
the shared dedup-latest window so the newest row per key wins *within the current
batch*, exactly mirroring the Spark baseline.

:func:`generate_dbt_project` returns a ``{relative_path: file_contents}`` mapping (and
optionally writes those files to ``out_dir``), so it can be unit-tested as golden text
without touching the filesystem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tablespec.core.schema_facts import (
    accepted_values_tests,
    column_contracts,
    relationship_tests,
)
from tablespec.dbt.contracts import (
    render_column_contract,
    render_contract_config_arg,
)
from tablespec.dbt.profiles import render_profiles_yml as _render_profiles_yml
from tablespec.dbt.raw_models import (
    file_backed_source,
    render_raw_model_sql,
    supports_raw_file_models,
)
from tablespec.dbt.schema_tests import render_tests_for_column
from tablespec.dialects import resolve_emit_defaults
from tablespec.models.umf import UMF
from tablespec.schemas.ingest_generator import (
    IngestSelect,
    build_ingest_select,
)


def _model_config(ingest: IngestSelect) -> str:
    """Render the dbt ``{{ config(...) }}`` block for the table's write strategy.

    dbt owns the write -- this config is the entire write contract. The block also
    opts the model into an ENFORCED data contract (``contract={'enforced': True}``)
    so the adapter validates the materialized relation against the per-column
    ``data_type`` + ``constraints:`` declared in ``schema.yml``.

    An incremental model with an enforced contract MUST pin ``on_schema_change``
    (dbt rejects the default ``ignore``); we use ``'append_new_columns'`` so a
    spec that GROWS a column (schema evolution -- e.g. a new monthly file adds
    one) evolves the existing relation in place, while removals/retypes still
    fail the contract check.
    """
    contract = render_contract_config_arg()
    on_change = "        on_schema_change='append_new_columns',"
    # dbt-spark / dbt-databricks REJECT incremental_strategy='merge' unless the
    # relation's file_format is one of delta/iceberg/hudi (the default 'parquet'
    # raises "You can only choose this strategy when file_format is set to 'delta'
    # ..."). Spark's default is parquet, so an incremental+merge model never
    # materializes there and every downstream test (incl. the FK relationships
    # test) is SKIPPED. The Databricks runtime these casts target runs on Delta, so
    # pin file_format='delta' for the spark/databricks dialects. DuckDB has no
    # file_format concept (and ignores it), so the key is omitted for duckdb to keep
    # the emitted config minimal/portable.
    spark_family = ingest.dialect in ("spark", "databricks")
    file_format = "        file_format='delta',\n" if spark_family else ""
    if ingest.mode == "incremental" and ingest.primary_key:
        # Upsert: dbt emits a MERGE on the unique key.
        keys = ", ".join(f'"{k}"' for k in ingest.primary_key)
        return (
            "{{\n"
            "    config(\n"
            "        materialized='incremental',\n"
            "        incremental_strategy='merge',\n"
            f"{file_format}"
            f"        unique_key=[{keys}],\n"
            f"{on_change}\n"
            f"{contract}\n"
            "    )\n"
            "}}"
        )
    if ingest.mode == "incremental":
        # Keyless append: dbt's default incremental strategy blindly inserts the
        # batch; duplicate rows accumulate on re-ingest (matches the Spark baseline).
        return (
            "{{\n"
            "    config(\n"
            "        materialized='incremental',\n"
            f"{on_change}\n"
            f"{contract}\n"
            "    )\n"
            "}}"
        )
    # snapshot -> full drop/reload as a plain table rebuild (NOT dbt's SCD2 snapshot).
    return f"{{{{\n    config(\n        materialized='table',\n{contract}\n    )\n}}}}"


def _model_sql(table: str, ingest: IngestSelect, *, raw_relation: str) -> str:
    """Render the dbt model SQL for *table*.

    The body is the shared cast SELECT over *raw_relation* -- the
    ``{{ source('raw', 'raw_<t>') }}`` literal for a pre-existing landing table,
    or ``{{ ref('raw_<t>') }}`` when the landing is an emitted file-reading
    model. For incremental+pk it runs the shared dedup-latest window so the
    newest row per key wins within the batch; dbt then MERGEs that deduped set
    into the target.
    """
    config = _model_config(ingest)
    source = raw_relation

    if ingest.has_dedup:
        # incremental + pk: dbt MERGEs on the unique key; the model body dedups the
        # current batch (newest row per key wins) so the merge upserts one row/key.
        note = (
            "-- incremental + primary_key: dbt MERGEs on unique_key.\n"
            "-- The dedup-latest window keeps the newest row per key in the batch.\n"
        )
        body = (
            f"{note}SELECT\n"
            f"{ingest.select_block}\n"
            "FROM (\n"
            f"{ingest.dedup_window_sql(source)}\n"
            ") AS src_raw"
        )
    elif ingest.mode == "incremental":
        # keyless incremental: dbt blindly appends the current source rows. The
        # write contract is that raw_<table> holds ONE batch per run (it is replaced
        # per batch upstream); re-running against accumulated raw would re-append and
        # duplicate. This mirrors the Spark artifact's blind INSERT INTO branch.
        note = (
            "-- WARNING: no primary_key + incremental -> blind append (no dedup).\n"
            "-- Contract: raw source holds ONE batch per run; duplicates accumulate\n"
            "-- on re-ingest of the same rows (matches the Spark INSERT INTO branch).\n"
        )
        body = f"{note}SELECT\n{ingest.select_block}\nFROM {source}"
    else:
        # snapshot: full table rebuild from the current source each run.
        note = "-- snapshot: full drop/reload (materialized table rebuild).\n"
        body = f"{note}SELECT\n{ingest.select_block}\nFROM {source}"

    return f"{config}\n\n{body}\n"


def _emitted_resolver(emitted_tables: list[str]):
    """A ``resolve_target`` for ``core.schema_facts`` over the emitted model set.

    Each table in the single-table path is emitted as a model named after its own
    ``table_name`` (no ``ingested_``/``gold_`` prefix). A FK target resolves to
    that bare model name iff the referenced table is one of the EMITTED models
    (the primary table or a ``related`` sibling, both of which get a ``.sql``);
    otherwise it is unresolvable and the test is SKIPPED -- never a ``ref()`` to a
    model dbt does not build (the AC1.2/AC1.5 skip-when-unresolvable rule).
    """
    known = set(emitted_tables)

    def resolve(table: str) -> str | None:
        return table if table in known else None

    return resolve


def _model_schema_block(
    umf_data: dict[str, Any],
    table: str,
    ingest: IngestSelect,
    resolver,
    *,
    dialect: str,
) -> list[str]:
    """Render the ``models:`` entry lines for ONE table (contract + tests).

    The model declares an ENFORCED data contract: every column carries a
    ``data_type`` (the adapter SQL type for its UMF type) and, when non-nullable,
    a ``not_null`` ``constraints:`` entry -- both enforced by the adapter at
    ``dbt build``. On top of the contract, columns carry generic ``data_tests:``:

    * ``unique`` for single-column primary keys and any single-column
      ``unique_constraints`` entry. (Composite uniqueness is left to the merge key
      and not asserted as a per-column test.)
    * ``relationships`` for each non-cross-pipeline FK whose target resolves via
      ``resolver`` (skip-when-unresolvable; composite FKs -> one test per scalar
      column).
    * ``accepted_values`` for each column carrying an
      ``expect_column_values_to_be_in_set`` expectation.

    The ``not_null`` rule is expressed once -- as the contract constraint -- not
    also as a generic ``not_null`` data test (the adapter enforces the constraint
    at build; a duplicate generic test would be redundant).
    """
    cols: list[dict[str, Any]] = umf_data["columns"]
    pk = ingest.primary_key
    unique_constraints: list[Any] = umf_data.get("unique_constraints") or []

    # Collect the set of columns that should carry a `unique` test.
    unique_cols: set[str] = set()
    if len(pk) == 1:
        unique_cols.add(pk[0])
    for uc in unique_constraints:
        # A constraint may be a bare column name or a single-column list.
        if isinstance(uc, str):
            unique_cols.add(uc)
        elif isinstance(uc, list) and len(uc) == 1:
            unique_cols.add(uc[0])

    rel_by_col: dict[str, list] = {}
    for t in relationship_tests(umf_data, resolver):
        rel_by_col.setdefault(t.column, []).append(t)
    av_by_col: dict[str, list] = {}
    for t in accepted_values_tests(umf_data):
        av_by_col.setdefault(t.column, []).append(t)

    contracts = {c.name: c for c in column_contracts(umf_data)}

    lines: list[str] = [f"  - name: {table}"]
    description = umf_data.get("description")
    if description:
        lines.append(f"    description: {_yaml_scalar(description)}")
    lines.append("    config:")
    lines.append("      contract:")
    lines.append("        enforced: true")
    lines.append("    columns:")

    for col in cols:
        name = col["name"]
        is_unique = name in unique_cols
        extra = rel_by_col.get(name, []) + av_by_col.get(name, [])
        # Contract: data_type (+ not_null constraint) for the column.
        contract = contracts[name]
        lines.extend(render_column_contract(contract, dialect=dialect))
        # Generic data tests layered on top of the contract (unique / relationships
        # / accepted_values). not_null is the contract constraint, not a data test.
        if is_unique or extra:
            lines.append("        data_tests:")
            if is_unique:
                lines.append("          - unique")
            for t in extra:
                # The ``data_tests:`` header is already present; render the entry
                # lines only (drop render_tests_for_column's header line).
                lines.extend(render_tests_for_column([t])[1:])

    return lines


def _schema_yml(
    tables: list[tuple[dict[str, Any], str, IngestSelect]],
    resolver,
    *,
    dialect: str,
) -> str:
    """Render ``models/schema.yml`` for every emitted table.

    Each table (the primary plus any ``related`` siblings) gets its own ``models:``
    entry so a relationships ``ref('<parent>')`` resolves to a model dbt actually
    builds -- without the parent model, dbt drops the relationships test silently
    (a dangling-ref defect), so the FK is only really enforced when every FK target
    is also emitted here.
    """
    blocks: list[str] = ["version: 2", "", "models:"]
    for umf_data, table, ingest in tables:
        blocks.extend(
            _model_schema_block(umf_data, table, ingest, resolver, dialect=dialect)
        )
    return "\n".join(blocks) + "\n"


def _sources_yml(tables: list[str]) -> str:
    """Render ``models/sources.yml`` declaring each raw landing table as a source."""
    lines = [
        "version: 2",
        "",
        "sources:",
        "  - name: raw",
        "    schema: main",
        "    tables:",
    ]
    for table in tables:
        lines.append(f"      - name: raw_{table}")
    return "\n".join(lines) + "\n"


def _dbt_project_yml(project_name: str) -> str:
    """Render ``dbt_project.yml`` for the generated project."""
    return (
        f"name: '{project_name}'\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "\n"
        f"profile: '{project_name}'\n"
        "\n"
        'model-paths: ["models"]\n'
        'target-path: "target"\n'
        'clean-targets: ["target", "dbt_packages"]\n'
    )


def _profiles_yml(project_name: str, *, target: str = "duckdb") -> str:
    """Render a ``profiles.yml`` template for *target*.

    Delegates to the shared :func:`render_profiles_yml`. The DuckDB ``path`` is a
    placeholder runners override via ``DBT_DUCKDB_PATH``; the session is pinned to
    UTC so TIMESTAMP rendering is host-timezone independent and matches the Spark
    baseline. The ``spark`` (local session), ``databricks`` (compile-only), and
    ``databricks_notebook`` (notebook session) targets are also selectable.
    """
    return _render_profiles_yml(
        project_name, target=target, duckdb_path_default="ingest.duckdb"
    )


def _yaml_scalar(value: str) -> str:
    """Quote a scalar for safe single-line YAML emission."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def generate_dbt_project(
    umf_data: dict[str, Any],
    *,
    dialect: str | None = None,
    target: str | None = None,
    out_dir: str | Path | None = None,
    project_name: str = "tablespec_ingest",
    related: list[UMF] | None = None,
) -> dict[str, str]:
    """Generate a dbt(+DuckDB) project for a UMF table's raw->ingest transform.

    Reuses :func:`build_ingest_select` for the model body, so the cast logic and
    dedup window are identical to the committed Databricks artifact. dbt owns the
    write: the model ``config`` (not hand-written SQL) selects merge / append /
    table-rebuild based on ``ingestion.mode`` + ``primary_key``.

    Args:
    ----
        umf_data: UMF table data (e.g. ``umf.model_dump(exclude_none=True)``).
        dialect: SQL dialect for the cast expressions (``"duckdb"`` | ``"spark"``
            | ``"databricks"``). ``None`` (default) resolves via
            :func:`tablespec.dialects.resolve_emit_defaults`: ``"duckdb"``
            locally, ``"databricks"`` on a Databricks runtime.
        target: profiles.yml adapter target (one of ``PROFILE_TARGETS``).
            ``None`` (default) mirrors *dialect* locally; on a Databricks runtime
            a spark-family dialect defaults to the runnable
            ``"databricks_notebook"`` session target.
        out_dir: If given, the returned files are also written under this directory.
        project_name: dbt project + profile name.
        related: Optional sibling tables (as :class:`UMF`). Each is emitted as its
            OWN ingest model (``.sql`` + raw ``source`` + ``schema.yml`` contract),
            exactly like the primary table -- ``related`` means "also ingest these
            tables into the same project", NOT merely FK context. This is required
            for referential integrity: a FK ``relationships`` test is emitted only
            when its target table is this table or one of the emitted ``related``
            models (skip-when-unresolvable), and dbt enforces the FK only when the
            referenced model actually exists. With ``related=None`` only
            self-referential FKs resolve; every other FK is skipped (never a
            ``ref()`` to a model dbt does not build, which dbt would silently drop).
            Because each ``related`` table becomes a real ingest model, building the
            project requires a ``raw_<table>`` landing table for every emitted table.

    Returns:
    -------
        A mapping of ``{relative_path: file_contents}`` for the whole project.

    """
    # Unspecified dialect/target resolve by environment (duckdb locally; the
    # runnable databricks-notebook session lane on a Databricks runtime).
    dialect, profile_target = resolve_emit_defaults(dialect, target)

    # Emit the primary table PLUS every related sibling as its own model, so a FK
    # ``relationships`` test that resolves to ``ref('<parent>')`` points at a model
    # dbt actually builds. Emitting only the child (with the parent merely known to
    # the resolver) leaves a dangling ref that dbt drops the relationships test for
    # -- silently disabling referential-integrity enforcement (AC1.2/AC1.3).
    related_umfs = list(related or [])
    emitted: list[tuple[dict[str, Any], str, IngestSelect]] = []
    seen: set[str] = set()
    for u in [umf_data, *(r.model_dump(exclude_none=True) for r in related_umfs)]:
        t = u["table_name"]
        if t in seen:
            continue
        seen.add(t)
        emitted.append((u, t, build_ingest_select(u, dialect=dialect)))

    # One resolver shared across every emitted model: a FK target resolves iff its
    # referenced table is one of the emitted tables (skip-when-unresolvable).
    emitted_tables = [t for _, t, _ in emitted]
    resolver = _emitted_resolver(emitted_tables)

    # Partition file-backed tables (delimited source with a path, dialect can
    # read files) from source-backed ones: the former get a file-reading
    # ``raw_<t>`` MODEL and a ``ref()`` edge; the rest keep the sources.yml
    # declaration and the ``source()`` edge (see tablespec.dbt.raw_models).
    file_sources = {
        t: src
        for u, t, _ in emitted
        if supports_raw_file_models(dialect) and (src := file_backed_source(u))
    }
    source_backed = [t for t in emitted_tables if t not in file_sources]

    files: dict[str, str] = {
        "dbt_project.yml": _dbt_project_yml(project_name),
        "profiles.yml": _profiles_yml(project_name, target=profile_target),
        "models/schema.yml": _schema_yml(emitted, resolver, dialect=dialect),
    }
    if source_backed:
        files["models/sources.yml"] = _sources_yml(source_backed)
    for u, t, ing in emitted:
        if t in file_sources:
            files[f"models/raw_{t}.sql"] = render_raw_model_sql(
                t,
                file_sources[t],
                [
                    (c.get("canonical_name") or c["name"], c["name"], c.get("source"))
                    for c in u["columns"]
                ],
                dialect=dialect,
            )
            raw_relation = f"{{{{ ref('raw_{t}') }}}}"
        else:
            raw_relation = f"{{{{ source('raw', 'raw_{t}') }}}}"
        files[f"models/{t}.sql"] = _model_sql(t, ing, raw_relation=raw_relation)

    if out_dir is not None:
        base = Path(out_dir)
        for rel, content in files.items():
            dest = base / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)

    return files
