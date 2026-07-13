"""Generate a multi-table GOLD dbt project from a UMF set (the DAG emitter).

Pipeline (corrected design, IR-first):

  1. :class:`~tablespec.dbt.registry.NodeRegistry` builds the logical-plan IR from
     the whole UMF set and the physical-name -> node index. Cycles fail loudly.
  2. Materialization is decided on that graph by
     :class:`~tablespec.dbt.materialization.MaterializationPolicy`.
  3. Rendering: staging models reuse the shared ``build_ingest_select`` cast
     SELECT; gold models reuse the core ``SQLPlanGenerator`` with a
     :class:`~tablespec.dbt.renderer.DbtRefRenderer` injected, so every inter-table
     relation becomes a static ``{{ ref() }}`` / ``{{ source() }}`` literal and the
     temp-view step chain collapses into CTEs inside one ``gold_<t>`` model.

The function returns ``{relative_path: contents}`` (optionally written to disk),
so it is golden-testable and runnable by ``dbt parse``/``compile``/``run`` against
duckdb.

All dbt-specific logic is contained in ``tablespec.dbt``; the core
(``build_ingest_select``, ``SQLPlanGenerator``, the ``TableRenderer`` seam, the IR)
has no dbt dependency.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from tablespec.core.ir import NodeRole
from tablespec.core.schema_facts import (
    ColumnTest,
    accepted_values_tests,
    column_contracts,
    relationship_tests,
)
from tablespec.dbt.contracts import (
    render_column_contract,
    render_contract_config_arg,
)
from tablespec.dbt.materialization import Materialization, MaterializationPolicy
from tablespec.dbt.profiles import PROFILE_TARGETS, render_profiles_yml
from tablespec.dbt.raw_models import (
    file_backed_source,
    render_raw_model_sql,
    supports_raw_file_models,
)
from tablespec.dbt.registry import NodeRegistry, NodeRegistryError
from tablespec.dbt.renderer import DbtRefRenderer
from tablespec.dbt.routing import RoutingPolicy
from tablespec.dbt.schema_tests import render_tests_for_column
from tablespec.dialects import resolve_emit_defaults
from tablespec.models.umf import UMF
from tablespec.schemas.ingest_generator import build_ingest_select
from tablespec.schemas.sql_generator import SQLPlanGenerator


class DbtProjectError(ValueError):
    """Raised on an un-renderable project (cycle, dangling ref, etc.)."""


# ---------------------------------------------------------------------------
# Config block rendering
# ---------------------------------------------------------------------------


def _config_block(
    mat: Materialization, *, contract: bool = False, file_format: str | None = None
) -> str:
    """Render the dbt ``{{ config(...) }}`` block for a materialization.

    When ``contract`` is set, the block opts the model into an ENFORCED data
    contract (``contract={'enforced': True}``) so the adapter validates the
    materialized relation against the per-column ``data_type`` + ``constraints:``
    declared in ``schema.yml``. Only the typed-cast staging models pass
    ``contract=True``; gold models (whose SELECT shape is derived, not a 1:1 cast)
    keep their existing config.
    """
    lines = ["{{", "    config("]
    lines.append(f"        materialized='{mat.strategy}',")
    if mat.incremental_strategy:
        lines.append(f"        incremental_strategy='{mat.incremental_strategy}',")
    if file_format:
        lines.append(f"        file_format='{file_format}',")
    if mat.unique_key:
        keys = ", ".join(f'"{k}"' for k in mat.unique_key)
        lines.append(f"        unique_key=[{keys}],")
    if contract:
        # An incremental model with an enforced contract MUST pin on_schema_change
        # (dbt rejects the default 'ignore'); 'append_new_columns' lets a grown
        # spec evolve the relation in place (removals/retypes still fail).
        if mat.strategy == "incremental":
            lines.append("        on_schema_change='append_new_columns',")
        lines.append(render_contract_config_arg())
    lines.append("    )")
    lines.append("}}")
    return "\n".join(lines)


def _merge_file_format(mat: Materialization, dialect: str) -> str | None:
    """Pin ``file_format='delta'`` for spark-family incremental MERGE models.

    dbt-spark / dbt-databricks REJECT ``incremental_strategy='merge'`` unless the
    relation's file_format is delta/iceberg/hudi (Spark's default is parquet), so
    a merge model would never materialize on those adapters. Mirrors the same
    rule in :mod:`tablespec.dbt.single_table`; duckdb has no file_format concept,
    so the key is omitted there to keep the config minimal/portable.
    """
    spark_family = dialect in ("spark", "databricks")
    if spark_family and mat.incremental_strategy == "merge":
        return "delta"
    return None


# ---------------------------------------------------------------------------
# Staging (ingested_<t>) models
# ---------------------------------------------------------------------------


def _staging_model_sql(
    umf: UMF,
    mat: Materialization,
    routing: RoutingPolicy,
    *,
    dialect: str,
) -> str:
    """Render an ``ingested_<t>`` staging model body (raw -> typed cast SELECT)."""
    umf_data = umf.model_dump(exclude_none=True)
    ingest = build_ingest_select(umf_data, dialect=dialect)
    source = routing.raw_literal(f"raw_{umf.table_name}")
    config = _config_block(
        mat, contract=True, file_format=_merge_file_format(mat, dialect)
    )

    if ingest.has_dedup:
        body = (
            "-- incremental + primary_key: dbt MERGEs on unique_key.\n"
            "-- The dedup-latest window keeps the newest row per key in the batch.\n"
            f"SELECT\n{ingest.select_block}\n"
            "FROM (\n"
            f"{ingest.dedup_window_sql(source)}\n"
            ") AS src_raw"
        )
    elif ingest.mode == "incremental":
        body = (
            "-- WARNING: no primary_key + incremental -> blind append (no dedup).\n"
            f"SELECT\n{ingest.select_block}\nFROM {source}"
        )
    else:
        body = (
            "-- snapshot: full drop/reload (materialized table rebuild).\n"
            f"SELECT\n{ingest.select_block}\nFROM {source}"
        )
    return f"{config}\n\n{body}\n"


# ---------------------------------------------------------------------------
# Gold (gold_<t>) models
# ---------------------------------------------------------------------------


def _gold_model_sql(
    umf: UMF,
    registry: NodeRegistry,
    mat: Materialization,
    routing: RoutingPolicy,
    *,
    dialect: str,
) -> str:
    """Render a ``gold_<t>`` model: the SQLPlanGenerator plan collapsed to CTEs.

    Inter-table relations are rendered as ``{{ ref('ingested_<other>') }}`` by the
    injected :class:`DbtRefRenderer`; the temp-view step chain is converted to a
    single ``WITH ... SELECT`` (cte mode) so the whole gold table is one model.

    Materialization note: every generated step of a gold table (``disposition_*``,
    ``*_agg``, ``*_first``, ``member_universe``) is PRIVATE to that one gold model
    and consumed exactly once, so the design's policy (ephemeral/inline for cheap,
    single-fanout, private steps) reduces to inlining them as CTEs here -- which is
    exactly what cte mode does. Promoting a step to its own materialized node only
    pays off when it is SHARED across >=2 gold models; cross-gold step sharing is
    not produced by the current single-table planner, so no per-step IR nodes are
    emitted. If that changes, add INTERMEDIATE nodes in the registry and let
    :meth:`MaterializationPolicy.for_node` decide table-vs-ephemeral on fanout.
    """
    renderer = DbtRefRenderer(registry, routing)
    generator = SQLPlanGenerator(table_renderer=renderer)
    related = {u.table_name: u for u in registry.all_umfs()}
    # cte mode collapses the CREATE TEMP VIEW chain into one WITH ... SELECT.
    plan_sql = generator.generate_for_table(umf, related, mode="cte")
    # A dbt model body is a single SELECT with NO terminating ';' (dbt wraps it in
    # a CREATE ... AS ( ... )); strip the statement terminator the CTE emitter adds.
    plan_sql = plan_sql.rstrip().rstrip(";")
    config = _config_block(mat, file_format=_merge_file_format(mat, dialect))
    return f"{config}\n\n{plan_sql.rstrip()}\n"


# ---------------------------------------------------------------------------
# sources.yml / schema.yml
# ---------------------------------------------------------------------------


def _sources_yml(registry: NodeRegistry, routing: RoutingPolicy) -> str | None:
    """Declare the local ``raw_<t>`` landing tables and any external relations.

    Two source groups: ``raw`` (this pipeline's all-STRING landing tables) and --
    only when present -- ``external`` (explicitly cross-pipeline references that
    fail-open by design to a ``source('external', ...)`` leaf).

    Raw nodes emitted as file-reading MODELS (``routing.model_backed_raw``) are
    excluded -- their edges are ``ref()``, not ``source()``. Returns ``None``
    when nothing is left to declare (the project then has no sources.yml).
    """
    nodes = sorted(registry.plan.nodes.values(), key=lambda n: n.node_id)
    raw_nodes = [
        n
        for n in nodes
        if n.role is NodeRole.SOURCE
        and not n.external
        and n.node_id not in routing.model_backed_raw
    ]
    ext_nodes = [n for n in nodes if n.role is NodeRole.SOURCE and n.external]
    if not raw_nodes and not ext_nodes:
        return None

    lines = ["version: 2", "", "sources:"]
    if raw_nodes:
        lines.append(f"  - name: {routing.source_name}")
        if routing.raw_database:
            lines.append(f"    database: {routing.raw_database}")
        lines.append(f"    schema: {routing.raw_schema}")
        lines.append("    tables:")
        for node in raw_nodes:
            lines.append(f"      - name: {node.node_id}")

    if ext_nodes:
        lines.append("  - name: external")
        lines.append(f"    schema: {routing.raw_schema}")
        lines.append("    tables:")
        for node in ext_nodes:
            lines.append(f"      - name: {node.node_id}")
    return "\n".join(lines) + "\n"


def _yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _registry_resolver(registry: NodeRegistry):
    """A ``resolve_target`` for ``core.schema_facts`` backed by the registry.

    Maps a logical referenced table name to the model node that will be emitted
    for it (``ingested_<t>`` for a landing table, ``gold_<t>`` for a pure-gold
    table), or ``None`` when the table is not in the rendered set -- so an
    external / unknown FK target is SKIPPED rather than pointed at a missing
    model.
    """

    def resolve(table: str) -> str | None:
        resolved = registry.resolve(table)
        return resolved.node_id if resolved is not None else None

    return resolve


def _tests_by_column(tests: list[ColumnTest]) -> dict[str, list[ColumnTest]]:
    """Group schema-test facts by their source column (order preserved)."""
    grouped: dict[str, list[ColumnTest]] = {}
    for t in tests:
        grouped.setdefault(t.column, []).append(t)
    return grouped


def _staging_schema_yml(umf: UMF, *, dialect: str) -> list[str]:
    """schema.yml entry for an ``ingested_<t>`` model.

    The staging model declares an ENFORCED data contract: each column carries a
    ``data_type`` (the adapter SQL type for its UMF type) and, when non-nullable, a
    ``not_null`` ``constraints:`` entry -- both enforced by the adapter at
    ``dbt build`` over the typed-cast SELECT. On top of the contract, columns carry
    generic ``data_tests:``: ``unique`` per single-column PK / unique-constraint and
    ``accepted_values`` for set-membership expectations. (FK ``relationships`` are
    emitted on the gold model that owns the FK, not the staging model. ``not_null``
    is the contract constraint, not a duplicate generic test.)
    """
    umf_data = umf.model_dump(exclude_none=True)
    pk = umf_data.get("primary_key") or []
    unique_cols: set[str] = set()
    if len(pk) == 1:
        unique_cols.add(pk[0])
    for uc in umf_data.get("unique_constraints") or []:
        if isinstance(uc, str):
            unique_cols.add(uc)
        elif isinstance(uc, list) and len(uc) == 1:
            unique_cols.add(uc[0])

    av_by_col = _tests_by_column(accepted_values_tests(umf_data))
    contracts = {c.name: c for c in column_contracts(umf_data)}

    lines = [f"  - name: ingested_{umf.table_name}"]
    if umf.description:
        lines.append(f"    description: {_yaml_scalar(umf.description)}")
    lines.append("    config:")
    lines.append("      contract:")
    lines.append("        enforced: true")
    lines.append("    columns:")
    for col in umf_data["columns"]:
        name = col["name"]
        is_unique = name in unique_cols
        av_tests = av_by_col.get(name, [])
        lines.extend(render_column_contract(contracts[name], dialect=dialect))
        if is_unique or av_tests:
            lines.append("        data_tests:")
            if is_unique:
                lines.append("          - unique")
            for t in av_tests:
                # render_tests_for_column emits the ``data_tests:`` header; here
                # the header is already present, so render the entry lines only.
                lines.extend(render_tests_for_column([t])[1:])
    return lines


def _gold_schema_yml(umf: UMF, registry: NodeRegistry) -> list[str]:
    """schema.yml entry for a ``gold_<t>`` model: FK relationships + accepted_values.

    A UMF ``foreign_keys`` entry ``column -> references_table.references_column``
    becomes a dbt ``relationships`` test on the gold model's column, pointing at
    the referenced table's emitted model (ingested staging for a landing table,
    gold model for a pure-gold table). Cross-pipeline / external / unresolvable
    FKs are skipped (they are not model edges). Set-membership expectations become
    ``accepted_values`` tests on their column.
    """
    lines = [f"  - name: gold_{umf.table_name}"]
    if umf.description:
        lines.append(f"    description: {_yaml_scalar(umf.description)}")

    umf_data = umf.model_dump(exclude_none=True)
    tests = relationship_tests(
        umf_data, _registry_resolver(registry)
    ) + accepted_values_tests(umf_data)
    if not tests:
        return lines

    grouped = _tests_by_column(tests)
    lines.append("    columns:")
    for column in sorted(grouped):
        lines.append(f"      - name: {column}")
        lines.extend(render_tests_for_column(grouped[column]))
    return lines


def _schema_yml(registry: NodeRegistry, *, dialect: str) -> str:
    """Assemble the project-wide schema.yml for all staging + gold models."""
    lines = ["version: 2", "", "models:"]
    for umf in registry.all_umfs():
        if umf.table_name in registry.staging_tables:
            lines.extend(_staging_schema_yml(umf, dialect=dialect))
    for umf in registry.all_umfs():
        if umf.table_name in registry.gold_tables:
            lines.extend(_gold_schema_yml(umf, registry))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# project / profiles scaffolding
# ---------------------------------------------------------------------------


def _dbt_project_yml(project_name: str) -> str:
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
    """Render profiles.yml for *target* (duckdb | spark | databricks).

    Delegates to the shared :func:`render_profiles_yml`; the three adapters all
    execute the SAME generated models (Databricks SQL == Spark SQL for our casts).
    """
    return render_profiles_yml(
        project_name, target=target, duckdb_path_default="gold.duckdb"
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def generate_dbt_dag_project(
    umfs: list[UMF],
    *,
    dialect: str | None = None,
    target: str | None = None,
    out_dir: str | Path | None = None,
    project_name: str = "tablespec_gold",
    routing: RoutingPolicy | None = None,
    materialization: MaterializationPolicy | None = None,
) -> dict[str, str]:
    """Generate a multi-table GOLD dbt project from a UMF set.

    Builds the logical-plan IR (failing loudly on a cycle), decides
    materialization on the graph, then renders one ``ingested_<t>`` staging model
    per table and one ``gold_<t>`` model per table with cross-table derivations,
    plus ``sources.yml`` / ``schema.yml`` / project scaffolding.

    Args:
        umfs: the table set (UMF models).
        dialect: cast dialect for staging models (``"duckdb"`` | ``"spark"`` |
            ``"databricks"``). ``None`` (default) resolves via
            :func:`tablespec.dialects.resolve_emit_defaults`: ``"duckdb"``
            locally, ``"databricks"`` on a Databricks runtime.
        target: profiles.yml adapter target (one of ``PROFILE_TARGETS``).
            ``None`` (default) mirrors *dialect* locally; on a Databricks runtime
            a spark-family dialect defaults to the runnable
            ``"databricks_notebook"`` session target. Pass explicitly to decouple
            (e.g. dialect=``"spark"`` cast SQL run against a databricks target).
        out_dir: if given, files are also written under this directory.
        project_name: dbt project + profile name.
        routing: source/model :class:`RoutingPolicy` (dev/prod placement).
        materialization: override :class:`MaterializationPolicy`.

    Returns:
        ``{relative_path: file_contents}`` for the whole project.

    Raises:
        DbtProjectError: the UMF graph has a dependency cycle, or a gold model
            references a relation that is neither a known table nor external.
    """
    routing = routing or RoutingPolicy()
    policy = materialization or MaterializationPolicy()
    # Unspecified dialect/target resolve by environment (duckdb locally; the
    # runnable databricks-notebook session lane on a Databricks runtime).
    dialect, profile_target = resolve_emit_defaults(dialect, target)

    try:
        registry = NodeRegistry(list(umfs))
    except NodeRegistryError as exc:
        # A physical-name collision is a project-build failure -- surface it under
        # the public DbtProjectError so callers handle one exception type.
        raise DbtProjectError(str(exc)) from exc

    # FAIL CLOSED: a gold table that references a relation present in no UMF (and
    # not marked external) is an error -- never silently drop the dependency nor
    # emit a phantom source('external', ...).
    if registry.dangling_refs:
        pairs = ", ".join(
            f"{tbl} -> {ref!r}" for tbl, ref in sorted(registry.dangling_refs)
        )
        msg = (
            "Gold table(s) reference unknown, non-external relations "
            f"(fail closed): {pairs}. Add the table to the UMF set or mark the "
            "reference external."
        )
        raise DbtProjectError(msg)

    cycle = registry.plan.detect_cycle()
    if cycle is not None:
        msg = "UMF dependency graph has a cycle: " + " -> ".join(cycle)
        raise DbtProjectError(msg)

    # File-backed landing tables (delimited source with a path, dialect can read
    # files) become raw_<t> MODELS; their edges render as ref() via the routing
    # policy, and they drop out of sources.yml (see tablespec.dbt.raw_models).
    file_sources = {
        umf.table_name: src
        for umf in registry.all_umfs()
        if umf.table_name in registry.staging_tables
        and supports_raw_file_models(dialect)
        and (src := file_backed_source(umf))
    }
    routing = dataclasses.replace(
        routing,
        model_backed_raw=routing.model_backed_raw | {f"raw_{t}" for t in file_sources},
    )

    files: dict[str, str] = {
        "dbt_project.yml": _dbt_project_yml(project_name),
        "profiles.yml": _profiles_yml(project_name, target=profile_target),
        "models/schema.yml": _schema_yml(registry, dialect=dialect),
    }
    sources_yml = _sources_yml(registry, routing)
    if sources_yml is not None:
        files["models/sources.yml"] = sources_yml

    # Staging models (one per real landing table; pure-gold tables have none).
    for umf in registry.all_umfs():
        if umf.table_name not in registry.staging_tables:
            continue
        node = registry.plan.nodes[f"ingested_{umf.table_name}"]
        assert node.role is NodeRole.INGESTED
        umf_data = umf.model_dump(exclude_none=True)
        ingestion = umf_data.get("ingestion") or {}
        mode = ingestion.get("mode", "incremental")
        mat = policy.for_ingested(
            mode=mode, primary_key=umf_data.get("primary_key") or []
        )
        if umf.table_name in file_sources:
            files[f"models/staging/raw_{umf.table_name}.sql"] = render_raw_model_sql(
                umf.table_name,
                file_sources[umf.table_name],
                [
                    (c.get("canonical_name") or c["name"], c["name"], c.get("source"))
                    for c in umf_data["columns"]
                ],
                dialect=dialect,
            )
        files[f"models/staging/ingested_{umf.table_name}.sql"] = _staging_model_sql(
            umf, mat, routing, dialect=dialect
        )

    # Gold models (only tables with cross-table derivations).
    for umf in registry.all_umfs():
        if umf.table_name not in registry.gold_tables:
            continue
        node = registry.plan.nodes[f"gold_{umf.table_name}"]
        mat = policy.for_node(node, registry.plan, table_name=umf.table_name)
        files[f"models/marts/gold_{umf.table_name}.sql"] = _gold_model_sql(
            umf, registry, mat, routing, dialect=dialect
        )

    if out_dir is not None:
        base = Path(out_dir)
        for rel, content in files.items():
            dest = base / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)

    return files


__all__ = ["PROFILE_TARGETS", "DbtProjectError", "generate_dbt_dag_project"]
