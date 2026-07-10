"""The backend-emitter seam: ``get_emitter(backend)`` -> a runnable project emitter.

ADR-008 §4 item 6 (the opt-in execution wiring) is implemented on top of the
already-shipped pure-Python project generators. This module is the small backend
selector that turns "I picked the *dbt* backend" into a concrete emitter that
materializes a runnable project on disk from a UMF set, WITHOUT duplicating any of
the existing generation logic: the dbt emitter delegates straight to
:func:`tablespec.dbt.single_table.generate_dbt_project` (one table) /
:func:`tablespec.dbt.project.generate_dbt_dag_project` (a set), which already build
the model SQL, ``schema.yml`` contracts/tests, ``sources.yml``, ``profiles.yml``,
and project scaffolding.

Encapsulation: this module is still pure-Python text emission -- it imports NO
``dbt`` package, so ``get_emitter('dbt')`` and ``Emitter.emit`` work anywhere
``tablespec`` is installed. ACTUALLY RUNNING the emitted project (``dbt build``)
needs the dbt stack and lives in :mod:`tablespec.dbt.runner`, which lazy-imports
the dbt CLI only when a run is requested.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from tablespec.dbt.project import generate_dbt_dag_project
from tablespec.dbt.single_table import generate_dbt_project
from tablespec.models.umf import UMF

# Backends the selector understands. Only ``dbt`` is a runnable product target
# today (ADR-008 / phase-4 eval: duckdb is the runnable dbt target; databricks is
# compile-only). The tuple is the single source of truth for the CLI's choices.
EMITTER_BACKENDS: tuple[str, ...] = ("dbt",)


class EmitterError(ValueError):
    """Raised for an unknown backend or an un-emittable UMF set."""


@dataclass(frozen=True)
class EmittedProject:
    """The result of :meth:`Emitter.emit`.

    Attributes:
        backend: the backend that produced the project (e.g. ``"dbt"``).
        project_dir: the directory the project was written to.
        files: ``{relative_path: contents}`` of every emitted file.
        project_name: the dbt project + profile name.
    """

    backend: str
    project_dir: Path
    files: dict[str, str]
    project_name: str


class Emitter(ABC):
    """A backend that materializes a runnable project from a UMF set."""

    #: the backend identifier this emitter answers to.
    backend: str

    @abstractmethod
    def emit(
        self,
        umfs: list[UMF],
        out_dir: str | Path,
        *,
        project_name: str | None = None,
        dialect: str | None = None,
        target: str | None = None,
    ) -> EmittedProject:
        """Materialize a runnable project for *umfs* under *out_dir*.

        ``dialect``/``target`` default to ``None`` and are resolved by the
        backend generators (:func:`tablespec.dialects.resolve_emit_defaults`
        for dbt): duckdb locally, the runnable databricks-notebook session
        lane on a Databricks runtime.
        """
        raise NotImplementedError


class DbtEmitter(Emitter):
    """Emit a runnable dbt project from a UMF set (the ``dbt`` backend).

    Delegates to the existing pure-Python generators -- a SINGLE UMF emits the
    single-table ingest project (``generate_dbt_project``); a multi-UMF set emits
    the gold DAG project (``generate_dbt_dag_project``). No generation logic is
    re-implemented here; this is purely the backend-selection + on-disk
    materialization seam.
    """

    backend = "dbt"

    def emit(
        self,
        umfs: list[UMF],
        out_dir: str | Path,
        *,
        project_name: str | None = None,
        dialect: str | None = None,
        target: str | None = None,
    ) -> EmittedProject:
        umfs = list(umfs)
        if not umfs:
            msg = "DbtEmitter.emit requires at least one UMF"
            raise EmitterError(msg)

        out = Path(out_dir)
        if len(umfs) == 1:
            name = project_name or "tablespec_ingest"
            files = generate_dbt_project(
                umfs[0].model_dump(exclude_none=True),
                dialect=dialect,
                target=target,
                out_dir=out,
                project_name=name,
            )
        else:
            name = project_name or "tablespec_gold"
            files = generate_dbt_dag_project(
                umfs,
                dialect=dialect,
                target=target,
                out_dir=out,
                project_name=name,
            )

        return EmittedProject(
            backend=self.backend,
            project_dir=out,
            files=files,
            project_name=name,
        )


def get_emitter(backend: str) -> Emitter:
    """Return the :class:`Emitter` for *backend*.

    Args:
        backend: the backend identifier (only ``"dbt"`` is supported today).

    Raises:
        EmitterError: *backend* is not a known emitter backend.
    """
    if backend == "dbt":
        return DbtEmitter()
    msg = (
        f"Unknown emitter backend: {backend!r} "
        f"(supported: {', '.join(EMITTER_BACKENDS)})"
    )
    raise EmitterError(msg)


__all__ = [
    "EMITTER_BACKENDS",
    "DbtEmitter",
    "EmittedProject",
    "Emitter",
    "EmitterError",
    "get_emitter",
]
