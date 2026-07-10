"""Render a parametrized dbt ``profiles.yml`` for the conformance harness.

A single source of truth for the adapter targets the generated dbt projects
can execute against. Each target maps to a real, installable dbt adapter:

  * ``duckdb``     -- in-process DuckDB (the default; no JVM, no cluster).
  * ``spark``      -- a LOCAL embedded Spark session via ``dbt-spark[session]``
                      (``method: session``); no Thrift server / cluster. The
                      warehouse dir + metastore MUST be isolated per run by the
                      caller (env / spark config) for parallel safety.
  * ``databricks`` -- COMPILE-ONLY here (no cluster). Connection params come from
                      ``env_var`` defaults so ``dbt compile`` works without a live
                      workspace; ``dbt run`` would require a real Databricks target.
  * ``databricks_notebook`` -- RUNNABLE from a Databricks notebook: ``dbt-spark``
                      ``method: session`` attaches to the notebook's already-active
                      SparkSession, so no host/http_path/token is needed. This is
                      the auto-selected default target when emitting on a
                      Databricks runtime (see ``tablespec.dialects
                      .resolve_emit_defaults``). Run dbt IN-PROCESS on the driver
                      (``DbtRunner`` does this on Databricks) -- a subprocess would
                      spin up its own SparkSession instead of attaching.

The casts themselves are dialect-equivalent: Databricks SQL == Spark SQL for our
``try_to_timestamp`` + Java-token date casts, so a Databricks model reuses the
Spark cast rendering. This module owns ONLY the profile YAML text; it lives in
``tablespec.dbt`` so core stays dbt-free.
"""

from __future__ import annotations

from tablespec.dialects import PROFILE_TARGETS as _PROFILE_TARGETS
from tablespec.dialects import validate_profile_target

# Targets the generated profiles.yml can emit, each backed by a real dbt adapter.
PROFILE_TARGETS: tuple[str, ...] = _PROFILE_TARGETS


def render_profiles_yml(
    project_name: str,
    *,
    target: str = "duckdb",
    duckdb_path_default: str = "gold.duckdb",
) -> str:
    """Render ``profiles.yml`` for *target* (one of :data:`PROFILE_TARGETS`).

    Args:
        project_name: the dbt profile + project name.
        target: which adapter target to emit.
        duckdb_path_default: default DuckDB db path (overridable via
            ``DBT_DUCKDB_PATH``); only used for the duckdb target.

    Raises:
        ValueError: if *target* is not one of :data:`PROFILE_TARGETS`.
    """
    target = validate_profile_target(target)

    if target == "duckdb":
        # UTC-pinned so TIMESTAMP rendering is host-timezone independent and matches
        # the Spark baseline (which pins the whole stack to UTC).
        body = (
            "      type: duckdb\n"
            f"      path: \"{{{{ env_var('DBT_DUCKDB_PATH', '{duckdb_path_default}') }}}}\"\n"
            "      threads: 1\n"
            "      settings:\n"
            "        TimeZone: 'UTC'\n"
        )
    elif target in ("spark", "databricks_notebook"):
        # dbt-spark[session]: attach to the interpreter's active SparkSession.
        #   * spark               -- LOCAL embedded session (conformance lane); the
        #     warehouse dir + metastore are isolated per run by the caller.
        #   * databricks_notebook -- the notebook's cluster session; run dbt
        #     in-process on the driver so the session is actually shared.
        # `host` is required by dbt-spark profile validation but ignored by the
        # session method.
        body = (
            "      type: spark\n"
            "      method: session\n"
            "      host: localhost\n"
            "      schema: \"{{ env_var('DBT_SPARK_SCHEMA', 'default') }}\"\n"
            "      threads: 1\n"
        )
    else:  # databricks -- COMPILE-ONLY in this harness (no cluster present)
        body = (
            "      type: databricks\n"
            "      host: \"{{ env_var('DBT_DATABRICKS_HOST', 'example.databricks.net') }}\"\n"
            "      http_path: \"{{ env_var('DBT_DATABRICKS_HTTP_PATH', '/sql/1.0/warehouses/none') }}\"\n"
            "      token: \"{{ env_var('DBT_DATABRICKS_TOKEN', 'compile-only') }}\"\n"
            "      schema: \"{{ env_var('DBT_DATABRICKS_SCHEMA', 'default') }}\"\n"
            "      threads: 1\n"
        )

    return f"{project_name}:\n  target: dev\n  outputs:\n    dev:\n" + body


__all__ = ["PROFILE_TARGETS", "render_profiles_yml"]
