"""Shared dialect validation and normalization helpers.

This module centralizes the accepted dialect spellings used by the cast and dbt
renderers so alias handling and error text stay consistent across call sites.
"""

from __future__ import annotations

import os

CAST_DIALECTS: tuple[str, ...] = ("spark", "databricks", "duckdb")
PROFILE_TARGETS: tuple[str, ...] = (
    "duckdb",
    "spark",
    "databricks",
    "databricks_notebook",
)


def _format_accepted_values(values: tuple[str, ...]) -> str:
    return ", ".join(values)


def _unsupported_value_message(label: str, value: str, values: tuple[str, ...]) -> str:
    return (
        f"Unsupported {label}: {value!r} "
        f"(expected one of {_format_accepted_values(values)})"
    )


def normalize_cast_dialect(dialect: str, *, label: str = "dialect") -> str:
    """Validate a cast dialect and return the render-path dialect.

    ``databricks`` is an explicit public spelling, but it renders through the
    Spark-family cast path.
    """
    if dialect not in CAST_DIALECTS:
        raise ValueError(_unsupported_value_message(label, dialect, CAST_DIALECTS))
    return "duckdb" if dialect == "duckdb" else "spark"


def validate_profile_target(target: str) -> str:
    """Validate a dbt profile target and return it unchanged."""
    if target not in PROFILE_TARGETS:
        raise ValueError(
            _unsupported_value_message("profile target", target, PROFILE_TARGETS)
        )
    return target


def is_databricks_runtime() -> bool:
    """Detect a Databricks runtime (cluster / notebook) strictly.

    Deliberately checks ONLY ``DATABRICKS_RUNTIME_VERSION`` (set by every
    Databricks runtime) -- no ``SPARK_HOME`` heuristics -- so emit defaults can
    never flip on a false positive. Also deliberately NOT delegated to
    ``tablespec.spark_factory`` (whose import mutates env/warning state).
    """
    return "DATABRICKS_RUNTIME_VERSION" in os.environ


def resolve_emit_defaults(
    dialect: str | None, target: str | None
) -> tuple[str, str]:
    """Resolve the (cast dialect, profile target) pair for a dbt emission.

    Explicit values always win (validated, passed through). Unspecified values
    default by environment:

      * off Databricks: dialect ``"duckdb"``, target mirrors the dialect --
        byte-identical to the historical defaults.
      * on a Databricks runtime (:func:`is_databricks_runtime`): dialect
        ``"databricks"`` and, for spark-family dialects, the runnable
        ``"databricks_notebook"`` session target so the emitted project runs
        against the notebook's active SparkSession.

    Returns:
        ``(dialect, target)`` -- both validated members of
        :data:`CAST_DIALECTS` / :data:`PROFILE_TARGETS`.
    """
    on_databricks = is_databricks_runtime()

    if dialect is None:
        dialect = "databricks" if on_databricks else "duckdb"
    elif dialect not in CAST_DIALECTS:
        raise ValueError(_unsupported_value_message("dialect", dialect, CAST_DIALECTS))

    if target is None:
        spark_family = dialect in ("spark", "databricks")
        target = (
            "databricks_notebook" if (on_databricks and spark_family) else dialect
        )
    return dialect, validate_profile_target(target)
