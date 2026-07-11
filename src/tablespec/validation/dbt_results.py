"""Parse a dbt project's ``target/run_results.json`` into a ValidationReport.

``dbt build`` on an emitted project already executes the spec's validations --
enforced contracts (materialization fails on type/not-null violations) and the
generated data tests (``unique``, ``relationships``, ``accepted_values``) --
but dbt leaves the outcomes in ``target/run_results.json``. This module maps
those outcomes onto the repo's canonical result model (``QualityCheckRun`` /
``QualityCheckResult``), so the dbt lane produces the SAME
:class:`~tablespec.validation.report.ValidationReport` as the staged GX lane:
one report shape everywhere.

Mapping:

* ``test.*`` nodes -> one result per data test. ``pass`` succeeds, ``warn``
  succeeds with ``severity: warning``, ``fail``/``error`` fail (``fail`` is a
  data failure -> critical; ``error`` is an execution error). ``failures``
  becomes ``unexpected_count``. When ``target/manifest.json`` is present the
  test's column and attached model are recovered from it.
* ``model.``/``seed.``/``snapshot.`` nodes -> one result each; a failed model
  IS a validation failure (enforced contracts reject bad shapes at
  materialization).
* ``skipped`` nodes are recorded as non-blocking ``info`` results so a report
  never silently hides work dbt did not run.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from tablespec.models.quality import QualityCheckResult, QualityCheckRun
from tablespec.validation.report import ValidationReport


class DbtResultsError(FileNotFoundError):
    """Raised when a project has no parseable ``target/run_results.json``."""


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_nodes(project_dir: Path) -> dict[str, dict[str, Any]]:
    manifest = project_dir / "target" / "manifest.json"
    if not manifest.exists():
        return {}
    try:
        return _load_json(manifest).get("nodes") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _run_timestamp(metadata: dict[str, Any]) -> datetime | None:
    generated_at = metadata.get("generated_at")
    if not generated_at:
        return None
    try:
        return datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except ValueError:
        return None


def _node_name(unique_id: str) -> str:
    # unique_id: "<resource>.<project>.<name>[.<hash>]"
    parts = unique_id.split(".")
    return parts[2] if len(parts) > 2 else unique_id


def _test_result(entry: dict[str, Any], node: dict[str, Any]) -> QualityCheckResult:
    unique_id = entry["unique_id"]
    status = entry.get("status")
    test_meta = node.get("test_metadata") or {}
    test_name = test_meta.get("name") or _node_name(unique_id)
    attached = node.get("attached_node") or ""
    model = _node_name(attached) if attached else None
    success = status in ("pass", "warn")
    severity = {
        "pass": "info",
        "warn": "warning",
        "fail": "critical",
    }.get(str(status), "error")
    failures = entry.get("failures")
    description = entry.get("message") or f"dbt test {test_name}"
    if model:
        description = f"[{model}] {description}"
    return QualityCheckResult(
        check_id=unique_id,
        expectation_type=f"dbt_test:{test_name}",
        success=success,
        severity=severity,
        column_name=node.get("column_name"),
        description=description,
        unexpected_count=int(failures) if isinstance(failures, (int, float)) else None,
        details={"status": status, "model": model},
        tags=["dbt", "test", *([model] if model else [])],
    )


def _node_result(entry: dict[str, Any], resource: str) -> QualityCheckResult:
    unique_id = entry["unique_id"]
    status = str(entry.get("status"))
    name = _node_name(unique_id)
    if status == "skipped":
        success, severity = True, "info"
        description = f"{resource} {name} skipped (an upstream node failed)"
    else:
        # A failed model IS a validation failure: enforced contracts reject
        # type/not-null violations at materialization time.
        success = status == "success"
        severity = "info" if success else "critical"
        description = entry.get("message") or f"dbt {resource} {name}: {status}"
    return QualityCheckResult(
        check_id=unique_id,
        expectation_type=f"dbt_{resource}",
        success=success,
        severity=severity,
        description=description,
        details={"status": status, "relation": entry.get("relation_name")},
        tags=["dbt", resource, name],
    )


def parse_dbt_run_results(
    project_dir: str | Path, *, pipeline_name: str = "dbt"
) -> QualityCheckRun:
    """Parse *project_dir*'s ``target/run_results.json`` into a QualityCheckRun.

    Raises:
        DbtResultsError: the project has no ``target/run_results.json``
            (run ``dbt build`` first).
    """
    base = Path(project_dir)
    run_results = base / "target" / "run_results.json"
    if not run_results.exists():
        msg = (
            f"No run results at {run_results} -- run dbt (e.g. DbtRunner.build) "
            "against the project first."
        )
        raise DbtResultsError(msg)

    data = _load_json(run_results)
    metadata = data.get("metadata") or {}
    nodes = _manifest_nodes(base)

    results: list[QualityCheckResult] = []
    for entry in data.get("results") or []:
        unique_id = entry.get("unique_id") or ""
        resource = unique_id.split(".", 1)[0]
        if resource == "test":
            results.append(_test_result(entry, nodes.get(unique_id) or {}))
        elif resource in ("model", "seed", "snapshot"):
            results.append(_node_result(entry, resource))

    run = QualityCheckRun(
        pipeline_name=pipeline_name,
        table_name=base.name,
        run_id=str(metadata.get("invocation_id") or "dbt-run"),
        results=results,
        should_block=any(not r.success and r.severity == "critical" for r in results),
    )
    ts = _run_timestamp(metadata)
    if ts is not None:
        run.run_timestamp = ts
    return run


def dbt_validation_report(
    project_dir: str | Path, *, pipeline_name: str = "dbt"
) -> ValidationReport:
    """The :class:`ValidationReport` for a built dbt project's last run."""
    return ValidationReport(
        parse_dbt_run_results(project_dir, pipeline_name=pipeline_name)
    )


__all__ = ["DbtResultsError", "dbt_validation_report", "parse_dbt_run_results"]
