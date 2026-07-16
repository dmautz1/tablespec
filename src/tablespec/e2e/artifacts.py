"""Emit every runtime artifact for a UMF set from one call.

The demo's "generate SQL + Lakeflow + dbt" step, as a library function so the
notebook cell is just an import plus one call. Distinct from
:func:`tablespec.e2e.compile.compile_umfs` (which writes a fixed manifest layout
and returns paths): this returns the buildable :class:`EmittedProject` the demo's
``DbtRunner.build(project).validation_report()`` flow consumes directly.

All dbt / Lakeflow / schema seams are imported lazily so importing
``tablespec.e2e`` stays dbt- and Spark-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tablespec.dbt.runner import EmittedProject
    from tablespec.models.umf import UMF


def emit_artifacts(
    umfs: list[UMF], out_dir: str | Path, *, dialect: str = "databricks"
) -> EmittedProject:
    """Emit SQL, Lakeflow, and dbt artifacts from *umfs*; return the dbt project.

    Writes under *out_dir*:
      * ``sql/<t>.ddl.sql``  -- CREATE TABLE DDL for every table,
      * ``sql/<t>.plan.sql`` -- a SQL execution plan for each table that has a
        derived (gold) column,
      * ``ldp/``             -- a Lakeflow Declarative Pipelines project,
      * ``dbt/``             -- a runnable dbt project.

    Returns the dbt :class:`EmittedProject` so the caller can
    ``DbtRunner().build(project)`` it.
    """
    from tablespec.dbt import DbtRunner
    from tablespec.ldp import generate_ldp_project
    from tablespec.schemas import generate_sql_ddl, generate_sql_plan

    sql_dir = Path(f"{out_dir}/sql")
    sql_dir.mkdir(parents=True, exist_ok=True)
    by_name = {u.table_name: u for u in umfs}
    for u in umfs:
        (sql_dir / f"{u.table_name}.ddl.sql").write_text(
            generate_sql_ddl(u.model_dump(exclude_none=True))
        )
        if any(c.derivation for c in u.columns):  # derived/gold target -> plan
            related = {n: m for n, m in by_name.items() if n != u.table_name}
            (sql_dir / f"{u.table_name}.plan.sql").write_text(
                generate_sql_plan(u, related)
            )
    generate_ldp_project(list(umfs), dialect=dialect, out_dir=f"{out_dir}/ldp")
    print(f"sql -> {sql_dir}\nlakeflow -> {out_dir}/ldp")
    return DbtRunner().emit(umfs, f"{out_dir}/dbt", dialect=dialect)


__all__ = ["emit_artifacts"]
