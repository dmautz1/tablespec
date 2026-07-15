"""Human-readable validation result reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tablespec.models.quality import QualityCheckResult, QualityCheckRun


@dataclass
class FailureDetail:
    """Structured detail about a single check failure."""

    expectation_type: str
    column: str | None
    severity: str
    description: str
    observed_value: Any = None
    unexpected_count: int = 0
    sample_values: list[Any] = field(default_factory=list)


class ValidationReport:
    """Human-readable validation results built from a QualityCheckRun."""

    def __init__(self, quality_run: QualityCheckRun) -> None:
        """Initialize from a QualityCheckRun.

        Args:
            quality_run: Completed quality check run with results.

        """
        self.quality_run = quality_run

    @property
    def results(self) -> list[QualityCheckResult]:
        """Shorthand access to check results."""
        return self.quality_run.results

    @property
    def total(self) -> int:
        """Total number of checks executed."""
        return len(self.results)

    @property
    def passed(self) -> int:
        """Number of passing checks."""
        return sum(1 for r in self.results if r.success)

    @property
    def failed(self) -> int:
        """Number of failing checks."""
        return sum(1 for r in self.results if not r.success)

    @property
    def success(self) -> bool:
        """True if all checks passed."""
        return self.failed == 0

    def summary(self) -> str:
        """One-line summary of validation results."""
        if self.total == 0:
            return "No expectations to validate"
        if self.failed == 0:
            return f"All {self.total} expectations passed"
        failure_word = "failure" if self.failed == 1 else "failures"
        return f"{self.passed}/{self.total} expectations passed ({self.failed} {failure_word})"

    def failures(self) -> list[FailureDetail]:
        """Structured failure details for all failing checks."""
        details = []
        for r in self.results:
            if not r.success:
                details.append(
                    FailureDetail(
                        expectation_type=r.expectation_type,
                        column=r.column_name,
                        severity=r.severity,
                        description=r.description
                        or f"{r.expectation_type} failed on column {r.column_name}",
                        observed_value=r.observed_value,
                        unexpected_count=r.unexpected_count or 0,
                    )
                )
        return details

    def as_dict(self) -> dict[str, Any]:
        """Machine-readable result dictionary."""
        return {
            "summary": self.summary(),
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "success": self.success,
            "table_name": self.quality_run.table_name,
            "should_block": self.quality_run.should_block,
            "failures": [
                {
                    "expectation_type": f.expectation_type,
                    "column": f.column,
                    "severity": f.severity,
                    "description": f.description,
                    "unexpected_count": f.unexpected_count,
                    "sample_values": f.sample_values,
                }
                for f in self.failures()
            ],
        }

    def as_rows(self) -> list[dict[str, Any]]:
        """One flat dict per validation, for persistence to a results table.

        Every value is a scalar (``observed_value`` is stringified), so the
        rows feed straight into ``spark.createDataFrame`` / ``pandas.DataFrame``
        and can be appended to a warehouse table per run. ``model`` is the
        dbt model / table the check ran against, when recoverable.
        """
        run = self.quality_run
        rows: list[dict[str, Any]] = []
        for r in self.results:
            # Tests carry the attached model in details; node results carry
            # the node name as their third tag (["dbt", <resource>, <name>]).
            model = (r.details or {}).get("model") or (
                r.tags[2] if len(r.tags) > 2 else None
            )
            rows.append(
                {
                    "run_id": run.run_id,
                    "run_timestamp": run.run_timestamp,
                    "model": model,
                    "check_id": r.check_id,
                    "expectation_type": r.expectation_type,
                    "column_name": r.column_name,
                    "success": r.success,
                    "severity": r.severity,
                    "unexpected_count": r.unexpected_count,
                    "unexpected_percent": r.unexpected_percent,
                    "observed_value": (
                        None if r.observed_value is None else str(r.observed_value)
                    ),
                    "description": r.description,
                }
            )
        return rows

    def as_rich_table(self):
        """Rich-formatted table for CLI output.

        Returns:
            A rich.table.Table instance with validation results.
            Requires the ``rich`` package.

        """
        from rich.table import Table

        table = Table(title="Validation Results")
        table.add_column("Status", style="bold")
        table.add_column("Type")
        table.add_column("Column")
        table.add_column("Details")

        for r in self.results:
            status = "[green]PASS[/green]" if r.success else "[red]FAIL[/red]"
            details = ""
            if not r.success and r.unexpected_count and r.unexpected_count > 0:
                details = f"{r.unexpected_count} unexpected values"
            table.add_row(status, r.expectation_type, r.column_name or "-", details)

        return table
