"""Opt-in dbt execution: emit a project, then run it with dbt (ADR-008 §4.6).

:class:`DbtRunner` is the runnable *product* target for the dbt backend. It pairs
the import-safe emitter seam (:func:`tablespec.dbt.emitter.get_emitter`) with a real
``dbt build`` invocation against the emitted project:

  1. ``get_emitter('dbt').emit(umfs, out_dir)`` writes a runnable project.
  2. :meth:`DbtRunner.build` invokes ``dbt build`` against it. For duckdb projects
     the DuckDB database is pinned under the project dir via ``DBT_DUCKDB_PATH`` so
     the run is fully isolated to *out_dir*.
  3. The :class:`DbtRunResult` reports success/failure (the dbt exit code) plus
     captured stdout/stderr for diagnostics.

dbt is a test/dev-time dependency, never a tablespec runtime import: this module
lazy-imports the dbt CLI only inside an actual invocation, so importing
``tablespec.dbt.runner`` (and constructing a runner / emitting a project) works with
no dbt installed.

Runnable scope: **duckdb** (subprocess, the ADR-008 / phase-4 lane) and the
**databricks_notebook** session target (in-process). On a Databricks runtime the
runner invokes dbt IN-PROCESS by default -- a subprocess dbt with
``method: session`` would build its own SparkSession instead of attaching to the
notebook's. In-process dbt mutates interpreter-global logging/flags state, so reuse
a single :class:`DbtRunner` per notebook session. The ``spark`` / ``databricks``
targets remain conformance-lane / compile-only concerns.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from tablespec.dbt.emitter import EmittedProject, get_emitter
from tablespec.dialects import is_databricks_runtime
from tablespec.models.umf import UMF

# The DuckDB database file the emitted profiles.yml resolves via env_var; pinning it
# under the project dir keeps every run isolated to out_dir.
_DUCKDB_DB_NAME = "tablespec.duckdb"
_DUCKDB_PATH_ENV = "DBT_DUCKDB_PATH"


class DbtRunnerError(RuntimeError):
    """Raised when the dbt stack cannot be located to run the emitted project."""


@dataclass(frozen=True)
class DbtRunResult:
    """The outcome of a ``dbt`` invocation.

    Attributes:
        success: True iff the dbt process exited 0.
        returncode: the dbt process exit code.
        command: the argv list that was run.
        stdout: captured standard output.
        stderr: captured standard error.
        project_dir: the dbt project the command ran against.
        duckdb_path: the DuckDB database file the run targeted.
    """

    success: bool
    returncode: int
    command: list[str]
    stdout: str
    stderr: str
    project_dir: Path
    duckdb_path: Path


def _require_dbt(*, hint: str) -> None:
    """Fail loudly (:class:`DbtRunnerError`) when the dbt stack is absent.

    Availability is probed with :func:`importlib.util.find_spec` -- deliberately
    NOT a static ``import dbt`` -- so this module never imports the external ``dbt``
    package at module scope (the encapsulation rule: ``src/`` must stay import-safe
    on a base install, dbt being a dev/test-only dependency).
    """
    spec = None
    try:
        spec = importlib.util.find_spec("dbt.cli.main")
    except ModuleNotFoundError:
        spec = None
    if spec is None:  # pragma: no cover - exercised via importorskip in tests
        msg = f"dbt-core is not installed; install {hint} to run an emitted dbt project."
        raise DbtRunnerError(msg)


def _dbt_argv() -> list[str]:
    """Return the argv prefix that invokes the dbt CLI as a subprocess.

    Uses ``<python> -m dbt.cli.main`` (works whenever ``dbt-core`` is installed for
    the current interpreter, with no reliance on a ``dbt`` script being on PATH).
    """
    _require_dbt(hint="the dev/test stack (dbt-core + dbt-duckdb)")
    return [sys.executable, "-m", "dbt.cli.main"]


class DbtRunner:
    """Emit a dbt project from UMF(s) and run it with dbt.

    Reuse ONE instance per interpreter when running in-process (notebooks): dbt's
    programmatic entrypoint mutates global logging/flags state on every invoke.
    """

    def __init__(self) -> None:
        self._emitter = get_emitter("dbt")

    def emit(
        self,
        umfs: UMF | list[UMF],
        out_dir: str | Path,
        *,
        project_name: str | None = None,
        dialect: str | None = None,
        target: str | None = None,
    ) -> EmittedProject:
        """Emit a runnable dbt project for *umfs* under *out_dir* (no dbt needed).

        Unspecified ``dialect``/``target`` resolve by environment
        (:func:`tablespec.dialects.resolve_emit_defaults`): duckdb locally, the
        runnable databricks-notebook session lane on a Databricks runtime.
        """
        umf_list = [umfs] if isinstance(umfs, UMF) else list(umfs)
        return self._emitter.emit(
            umf_list, out_dir, project_name=project_name, dialect=dialect,
            target=target,
        )

    def invoke(
        self,
        project: EmittedProject,
        *command: str,
        duckdb_path: str | Path | None = None,
        in_process: bool | None = None,
    ) -> DbtRunResult:
        """Invoke ``dbt <command>`` against an already-emitted *project*.

        For duckdb-profile projects the DuckDB database file (``DBT_DUCKDB_PATH``)
        defaults to ``<project_dir>/tablespec.duckdb`` so the run is isolated to the
        project dir. Lazy-imports the dbt CLI; raises :class:`DbtRunnerError` if dbt
        is absent.

        ``in_process`` selects the invocation style; the default (``None``) runs
        in-process on a Databricks runtime and as a subprocess elsewhere. In-process
        is REQUIRED for the ``databricks_notebook`` session profile -- a subprocess
        dbt would build its own SparkSession instead of attaching to the
        notebook's.
        """
        project_dir = project.project_dir
        db = (
            Path(duckdb_path)
            if duckdb_path is not None
            else project_dir / _DUCKDB_DB_NAME
        )
        # Pin the DuckDB path only when the emitted profile actually resolves it;
        # for session/databricks profiles the env var is meaningless noise.
        is_duckdb_profile = "type: duckdb" in project.files.get("profiles.yml", "")
        env_overrides = {_DUCKDB_PATH_ENV: str(db)} if is_duckdb_profile else {}

        cli_args = [
            *command,
            "--profiles-dir",
            str(project_dir),
            "--project-dir",
            str(project_dir),
        ]

        if in_process is None:
            in_process = is_databricks_runtime()
        if in_process:
            return self._invoke_in_process(
                cli_args, env_overrides, project_dir=project_dir, duckdb_path=db
            )

        cmd = [*_dbt_argv(), *cli_args]
        proc = subprocess.run(
            cmd,
            env=dict(os.environ, **env_overrides),
            capture_output=True,
            text=True,
            check=False,
        )
        return DbtRunResult(
            success=proc.returncode == 0,
            returncode=proc.returncode,
            command=cmd,
            stdout=proc.stdout,
            stderr=proc.stderr,
            project_dir=project_dir,
            duckdb_path=db,
        )

    def _invoke_in_process(
        self,
        cli_args: list[str],
        env_overrides: dict[str, str],
        *,
        project_dir: Path,
        duckdb_path: Path,
    ) -> DbtRunResult:
        """Run dbt via its programmatic entrypoint in THIS interpreter.

        Required for ``method: session`` profiles: the dbt-spark session adapter
        attaches to the interpreter's active SparkSession, which only exists in
        the calling process (e.g. a Databricks notebook driver).
        """
        _require_dbt(
            hint=(
                "dbt-core plus the adapter for the emitted profile "
                "(e.g. dbt-spark[session] for the databricks_notebook target)"
            )
        )
        from dbt.cli.main import dbtRunner  # noqa: PLC0415

        # Global flags precede the subcommand; no colors so the captured text is
        # clean notebook output (and one less global-state toggle per invoke).
        args = ["--no-use-colors", *cli_args]
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        saved = {k: os.environ.get(k) for k in env_overrides}
        os.environ.update(env_overrides)
        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                res = dbtRunner().invoke(args)
        finally:
            for key, prior in saved.items():
                if prior is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prior

        stderr_text = stderr_buf.getvalue()
        if res.exception is not None:
            exc_text = f"{type(res.exception).__name__}: {res.exception}"
            stderr_text = f"{stderr_text}\n{exc_text}" if stderr_text else exc_text
        return DbtRunResult(
            success=bool(res.success),
            returncode=0 if res.success else 1,
            command=["dbt", *args],
            stdout=stdout_buf.getvalue(),
            stderr=stderr_text,
            project_dir=project_dir,
            duckdb_path=duckdb_path,
        )

    def build(
        self,
        project: EmittedProject,
        *,
        duckdb_path: str | Path | None = None,
        in_process: bool | None = None,
    ) -> DbtRunResult:
        """Run ``dbt build`` against an emitted *project* (the default run target).

        ``dbt build`` runs models AND their data tests, so a green result means the
        models materialized and every emitted generic/contract test passed.
        """
        return self.invoke(
            project, "build", duckdb_path=duckdb_path, in_process=in_process
        )


__all__ = [
    "DbtRunResult",
    "DbtRunner",
    "DbtRunnerError",
]
