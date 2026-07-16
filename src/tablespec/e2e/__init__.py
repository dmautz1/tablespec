"""End-to-end bootstrap: COMPILE a UMF set to runtime artifacts, then run them.

Shared helper package behind the two demo scripts (``scripts/bootstrap_from_tables.py``
= Path A, ``scripts/bootstrap_from_specs.py`` = Path B), which also serve as the
asserted pytest e2e.

Layers:
    * :mod:`tablespec.e2e.paths`    -- the two entry points (UMF set producers).
    * :mod:`tablespec.e2e.compile`  -- the COMPILE ORCHESTRATOR (UMF -> artifacts).
    * :mod:`tablespec.e2e.manifest` -- artifact layout + ``CompiledArtifacts``.
    * :mod:`tablespec.e2e.backbone` -- run the COMPILED artifacts (ingest/validate/
                                       transform), reusing conformance facades.
"""

from __future__ import annotations

from tablespec.e2e.artifacts import emit_artifacts
from tablespec.e2e.backbone import BackboneResult, run_backbone
from tablespec.e2e.compile import compile_umfs
from tablespec.e2e.manifest import CompiledArtifacts, TableArtifacts
from tablespec.e2e.paths import (
    excel_specs_from_csvs,
    save_specs,
    specs_from_excel_dir,
    sync_specs_with_csvs,
    umfs_from_csvs,
    umfs_from_excel_dir,
    umfs_from_spec_dir,
    umfs_from_specs,
    umfs_from_tables,
)

__all__ = [
    "BackboneResult",
    "CompiledArtifacts",
    "TableArtifacts",
    "compile_umfs",
    "emit_artifacts",
    "excel_specs_from_csvs",
    "run_backbone",
    "save_specs",
    "specs_from_excel_dir",
    "sync_specs_with_csvs",
    "umfs_from_csvs",
    "umfs_from_excel_dir",
    "umfs_from_spec_dir",
    "umfs_from_specs",
    "umfs_from_tables",
]
