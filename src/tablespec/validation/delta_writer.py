"""Persist a :class:`ValidationReport` to a Delta table (one row per check).

``dbt build`` (or the staged GX lane) produces a
:class:`~tablespec.validation.report.ValidationReport`; this module appends its
per-check outcomes to a warehouse table so runs accumulate into a queryable
validation log. The row shape is exactly ``ValidationReport.as_rows()``.

PySpark is required (``tablespec[spark]``); it is imported at module scope, so
``tablespec.validation.__init__`` swallows the ImportError on a base install and
these symbols are simply absent there -- same convention as ``table_validator``.
The target table is created if missing (``CREATE TABLE IF NOT EXISTS`` from this
same schema), so an all-pass run still registers the table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyspark.sql.types import (
    BooleanType,
    DoubleType,
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from tablespec.session import get_session

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

    from tablespec.validation.report import ValidationReport

#: One row per validation check -- matches ``ValidationReport.as_rows()`` keys.
VALIDATION_RESULT_SCHEMA = StructType(
    [
        StructField("run_id", StringType(), True),
        StructField("run_timestamp", TimestampType(), True),
        StructField("model", StringType(), True),
        StructField("check_id", StringType(), True),
        StructField("expectation_type", StringType(), True),
        StructField("column_name", StringType(), True),
        StructField("success", BooleanType(), True),
        StructField("severity", StringType(), True),
        StructField("unexpected_count", LongType(), True),
        StructField("unexpected_percent", DoubleType(), True),
        StructField("observed_value", StringType(), True),
        StructField("description", StringType(), True),
    ]
)


class ValidationDeltaWriter:
    """Append validation results to a Delta table, creating it if missing."""

    def __init__(self, spark: SparkSession | None = None) -> None:
        """Bind a Spark session (the active/created one when not supplied)."""
        self._spark = spark or get_session()

    def ensure_table(self, table: str) -> None:
        """``CREATE TABLE IF NOT EXISTS`` *table* with the results schema.

        The column DDL is derived from :data:`VALIDATION_RESULT_SCHEMA` via
        ``StructType.toDDL()`` so the created table's types cannot drift from the
        DataFrame written into it. *table* is used verbatim, so the caller
        controls qualification/quoting (e.g. a back-ticked
        ``\\`cat\\`.\\`sch\\`.\\`validation_results\\``).
        """
        self._spark.sql(
            f"CREATE TABLE IF NOT EXISTS {table} "
            f"({VALIDATION_RESULT_SCHEMA.toDDL()}) USING DELTA"
        )

    def write(
        self, report: ValidationReport, table: str, *, mode: str = "append"
    ) -> DataFrame:
        """Ensure *table* exists, then write *report*'s rows to it.

        Returns the written DataFrame (not the report -- this is a persistence
        call). ``mode`` is the Spark save mode (default ``append``).
        """
        self.ensure_table(table)
        df = self._spark.createDataFrame(report.as_rows(), VALIDATION_RESULT_SCHEMA)
        df.write.mode(mode).saveAsTable(table)
        return df


def write_validation_results(
    report: ValidationReport,
    table: str,
    *,
    spark: SparkSession | None = None,
    mode: str = "append",
) -> DataFrame:
    """Append *report*'s per-check rows to Delta *table* (created if missing).

    Convenience wrapper over :class:`ValidationDeltaWriter`. Returns the written
    DataFrame.
    """
    return ValidationDeltaWriter(spark).write(report, table, mode=mode)


__all__ = [
    "VALIDATION_RESULT_SCHEMA",
    "ValidationDeltaWriter",
    "write_validation_results",
]
