---
ddx:
  id: FEAT-027
---

# Feature Specification: FEAT-027 — dbt Project Emitter

**Feature ID**: FEAT-027
**Status**: Approved
**Priority**: P0
**Owner**: Platform / Compile Team
**Covered PRD Subsystem(s)**: Multi-Target Emission
**Covered PRD Requirements**: FR-19.2 (dbt emitter), FR-19.1 (shared target-agnostic core seam)
**Cross-Subsystem Rationale**: None — single subsystem. The emitter is one backend on the
shared Multi-Target Emission core seam (FR-19.1); the LDP and direct-SQL siblings are
separate features (FR-19.3 / FR-19.4) governed elsewhere.

## Overview

This feature is the dbt backend of tablespec's compiler: given one UMF (or a UMF set), it
deterministically emits a complete dbt project — model SQL, `schema.yml` contracts and
generic tests, `sources.yml`, and project scaffolding — as a committed, reviewable artifact.
It implements PRD FR-19.2 (dbt emitter) on the shared core seam (FR-19.1), and codifies the
decision that dbt is a dev-group, test-only tool — never a user-facing runtime dependency
(PRD Non-Goal: "Shipping dbt … as user-facing runtime dependencies").

## Ideal Future State

A data engineer changes a table's truth in its UMF, runs the compile step once, and gets a
dbt project written to a pinned layout that they review as an ordinary code-review diff. The
project's `ingested_<t>` / `gold_<t>` models carry enforced data contracts and generic tests
(`unique`, `relationships`, `accepted_values`) derived from the same UMF facts that feed the
GX suite, so the dbt artifact and the GX artifact can never silently disagree. The engineer
can generate the project anywhere `tablespec` is installed — without dbt present — and only a
CI/test lane that actually *runs* `dbt build` needs the dbt stack.

The `ingested_<t>` model name is the existing dbt representation of the source-semantic
ingested contract. It is not a silver-layer conformance model and this feature does not
rename the model family; it renders the typed, validated source meaning already governed by
FR-18/FR-19 into dbt artifacts.

## Problem Statement

- **Current situation**: dbt models, `schema.yml` tests, contracts, sources, and project
  scaffolding are hand-authored per table and reconciled against the SQL/GX representations
  by hand. Generation logic for `not_null`/`unique` was duplicated across `gx_baseline` and
  the dbt path with non-identical rules.
- **Pain points**: Per-tool drift (the dbt schema tests and the GX suite diverge), redundant
  manual authoring per onboarded table, and a risk that shipping dbt as a user dependency
  forces the dbt runtime onto every consumer of the library.
- **Desired outcome**: One UMF deterministically emits a complete, buildable dbt project
  whose constraints are provably the same UMF-derived facts as the GX suite; generation is
  import-safe (no `import dbt`); dbt stays test-only.

## Functional Areas

| Area | User question or job | Feature responsibility |
|------|----------------------|------------------------|
| Single-table ingest project | "Emit a dbt project that ingests one raw landing table into a typed table" | Render `models/<t>.sql` + contract + tests + `sources.yml` + scaffolding from one UMF (and its `related` siblings) |
| Multi-table gold DAG project | "Emit a dbt project for the whole UMF set with cross-table derivations" | Build the IR, decide materialization, render `ingested_<t>` staging + `gold_<t>` mart models with `{{ ref() }}`/`{{ source() }}` edges |
| Schema facts → tests + contracts | "Derive the same constraints the GX suite uses" | Render `not_null` (as contract constraint), `unique`, `relationships`, `accepted_values` from the shared `core.schema_facts` |
| CI selection + seeds | "Build/test only impacted models; smoke-test with fixtures" | Map a UMF diff `ChangeSet` to a `--select` expression; emit `sample_data` CSVs as dbt seeds |
| Dependency packaging | "Generate without forcing dbt on consumers" | Keep all emission pure-Python (no `import dbt`); confine dbt to the dev/test group |
| Opt-in runnable target | "Emit a project for a backend and actually run it" | `get_emitter(backend)` materializes a runnable project; `DbtRunner` runs it via dbt-duckdb; CLI `emit --backend dbt [--run]` |

## Requirements

### Functional Requirements by Area

#### Single-table ingest project

DBT-01. Given one UMF, `generate_dbt_project` returns a `{relative_path: contents}` mapping
for a complete dbt project (model SQL, `models/schema.yml`, `models/sources.yml`,
`dbt_project.yml`, `profiles.yml`), optionally written to `out_dir`.
(`src/tablespec/dbt/single_table.py:322`)

DBT-02. The model body reuses the shared `build_ingest_select` cast SELECT and dedup-latest
window, so the cast logic is identical to the committed direct-SQL ingest artifact.
(`src/tablespec/dbt/single_table.py:107`, `:45`)

DBT-03. dbt owns the write: the model `{{ config }}` (not hand-written DML) selects
`materialized='incremental'` + `incremental_strategy='merge'` + `unique_key` for
incremental-with-PK, keyless `incremental` append, or `materialized='table'` rebuild for
snapshot, derived from `ingestion.mode` + `primary_key`.
(`src/tablespec/dbt/single_table.py:51`)

DBT-04. Each `related` sibling UMF is emitted as its own ingest model so a FK
`relationships` test resolves to a model dbt actually builds; an unresolvable FK target is
skipped, never rendered as a `ref()` to a non-emitted model.
(`src/tablespec/dbt/single_table.py:150`, `:371`)

#### Multi-table gold DAG project

DBT-05. Given a UMF set, `generate_dbt_dag_project` builds the logical-plan IR via
`NodeRegistry`, decides materialization on the graph, and renders one `ingested_<t>` staging
model and one `gold_<t>` mart model per table, plus `sources.yml`/`schema.yml`/scaffolding.
(`src/tablespec/dbt/project.py:354`)

DBT-06. Gold models reuse the core `SQLPlanGenerator` with a `DbtRefRenderer` so every
inter-table relation becomes a static `{{ ref('ingested_<t>') }}` / `{{ source() }}` literal
and the temp-view step chain collapses into CTEs inside one model.
(`src/tablespec/dbt/project.py:132`)

DBT-07. The DAG emitter fails closed: a gold table referencing a relation in no UMF (and not
marked external) raises `DbtProjectError`; a dependency cycle raises `DbtProjectError`.
(`src/tablespec/dbt/project.py:408`, `:419`)

#### Schema facts → tests + contracts

DBT-08. Every emitted model declares an ENFORCED data contract: each column carries a
`data_type` (adapter SQL type for its UMF type) and, when non-nullable, a `not_null`
`constraints:` entry, both enforced by the adapter at `dbt build`. `not_null` is the
contract constraint, not a duplicate generic test.
(`src/tablespec/dbt/project.py:229`, `src/tablespec/dbt/contracts.py:120`)

DBT-09. Generic `data_tests:` are derived from the shared `core.schema_facts`: `unique` for
single-column PKs / single-column `unique_constraints`; `relationships` for each resolvable,
non-cross-pipeline FK; `accepted_values` for each set-membership expectation. The same facts
feed the GX baseline, so the two backends assert the same UMF-derived constraint set.
(`src/tablespec/core/schema_facts.py`, `src/tablespec/dbt/schema_tests.py:93`)

#### CI selection + seeds

DBT-10. A UMF-diff `ChangeSet` maps to a dbt `--select` expression that selects each changed
model plus descendants (`ingested_x+ gold_y+`); an empty `ChangeSet` returns the
provably-unsatisfiable `EMPTY_SELECTION`, never the whole project.
(`src/tablespec/dbt/selection.py:69`, `:49`)

DBT-11. Generated `sample_data` CSVs map to dbt seeds (`seeds/<t>.csv` + a `seeds:` config
with column `column_types` from UMF) for `dbt build` smoke tests and the duckdb parity
harness. (`src/tablespec/dbt/seeds.py:190`, `:232`)

#### Dependency packaging

DBT-12. All project emission is pure-Python text with no `import dbt`, so
`generate_dbt_project` / `generate_dbt_dag_project` work without the dbt stack installed;
dbt is required only to *execute* generated projects in the conformance/parity tests.
(`src/tablespec/dbt/__init__.py:11`, `pyproject.toml:51`)

DBT-13. dbt is NOT a user-facing extra: the dbt stack (`dbt-core`, `dbt-duckdb`,
`dbt-spark[session]`, `dbt-databricks`) lives in the `[dependency-groups] dev` group, so
`uv sync --group dev` yields a working test stack with no `--extra` to remember.
(`pyproject.toml:63`)

#### Opt-in runnable target

DBT-14. `get_emitter(backend)` returns an `Emitter` for the backend (`"dbt"` -> `DbtEmitter`);
an unknown backend raises `EmitterError`. `DbtEmitter.emit` materializes a runnable project
under `out_dir` by delegating to `generate_dbt_project` (single UMF) / `generate_dbt_dag_project`
(a set) — no generation logic is re-implemented in the seam.
(`src/tablespec/dbt/emitter.py`)

DBT-15. `DbtRunner.emit` emits a project (pure-Python, no dbt needed) and `DbtRunner.build`
runs it via dbt-duckdb (`dbt build`), pinning the DuckDB database under the project dir
(`DBT_DUCKDB_PATH`) so the run is isolated; the result reports success + the dbt exit code.
The dbt CLI is lazy-imported only inside `build`/`invoke`, so emitting needs no dbt installed.
The runnable target is duckdb only. (`src/tablespec/dbt/runner.py`)

DBT-16. The CLI `tablespec emit <umf> <out_dir> --backend dbt` writes the project dir; with
`--run` it also invokes `dbt build` against the emitted project and fails non-zero on a failed
build. (`src/tablespec/cli.py`)

### Non-Functional Requirements

- **Determinism**: Re-emitting from an unchanged UMF produces 0 byte diffs in
  project files (golden-testable as `{relative_path: contents}` without touching
  the filesystem).
- **Encapsulation**: `tablespec.core` never imports `tablespec.dbt`; the dbt and direct-SQL
  backends never import each other (enforced by `tests/test_core_encapsulation.py`).
- **Import-safety**: Importing `tablespec.dbt` and calling any generator must not
  require the dbt runtime packages to be installed; generation-time dependency
  violations are a test failure.
- **Multi-engine parity**: Generated projects build on the DuckDB and local Spark
  session tiers with byte-identical canonical cast results; Databricks compile
  output is pinned by golden tests and Databricks execution is opt-in.
## User Stories

- [US-025 — Emit a dbt Project from UMF](../user-stories/US-025-emit-dbt-project-from-umf.md)

## Edge Cases and Error Handling

- **Cross-pipeline / unresolvable FK**: the `relationships` test is skipped (the target is
  not an emitted model), never rendered as a dangling `ref()`.
- **Composite FK**: rendered as one `relationships` test per scalar column.
- **Incremental model with enforced contract**: `on_schema_change='fail'` is pinned (dbt
  rejects the default `ignore`), so a column-set drift fails the build loudly.
- **spark/databricks incremental merge**: `file_format='delta'` is pinned (dbt-spark rejects
  `incremental_strategy='merge'` on the default parquet format), so the merge actually
  materializes and downstream tests are not silently skipped.
- **Empty diff in CI selection**: returns the unsatisfiable `EMPTY_SELECTION`, not a
  whole-project build.

## Success Metrics

- Zero drift: a recompiled dbt project has 0 byte diffs against the committed
  artifact for an unchanged UMF (ties into the PRD Primary KPI).
- The `unique`/`relationships`/`accepted_values`/`not_null` set emitted to
  `schema.yml` matches 100% of the corresponding GX baseline constraint set for
  the same UMF (shared `schema_facts`).
- Generated projects build green on the DuckDB and Spark conformance tiers, and
  Databricks compile-golden tests stay green. Evidence: `uv run pytest
  tests/dbt_dag tests/conformance -k "dbt or Dbt"`.

## Constraints and Assumptions

- dbt-core 1.9+ / dbt-duckdb 1.9+ / dbt-spark 1.10+ / dbt-databricks 1.9+ (test-only).
- The Databricks runtime these casts target runs on Delta (justifies pinned
  `file_format='delta'` for the spark family).
- `raw_<t>` landing tables are all-STRING sources; one batch per run for keyless incremental. *(Forward note: ADR-015 / FEAT-031 generalize this — typed sources will land native-typed raw with identity/safe-narrowing staging casts; the all-STRING constraint then applies to text-landed sources only.)*

## Dependencies

- **Other features**: FEAT-019 (SQL CTE mode — gold models reuse `SQLPlanGenerator` cte
  mode); the shared `build_ingest_select` ingest core (ADR-007 / FR-19.4).
- **External services**: dbt-core, dbt-duckdb, dbt-spark, dbt-databricks (dev/test only).
- **PRD requirements**: FR-19.2 (P0), FR-19.1 (P0); supports FR-18.1 (the compile
  orchestrator emits both dbt projects as committed artifacts).

## Out of Scope

- Executing dbt as the *production runtime* surface — the deployed runtime consumes committed
  artifacts and does not invoke dbt (PRD FR-18.3). (The opt-in `DbtRunner` / `emit --backend
  dbt --run` is a developer/CI convenience that runs an emitted project locally via
  dbt-duckdb; it is NOT the production execution path.)
- Porting rich/stage-classified/statistical GX expectations to dbt generic tests — these stay
  native in GX (ADR-008).
- dbt-utils / dbt-expectations package adoption (deliberately avoided; ADR-008).
- LDP and direct-SQL emitters (FR-19.3 / FR-19.4 — separate features).
- A Databricks SQL-warehouse dbt *run* target — the `databricks` profile target stays
  compile-only / conformance-lane (ADR-008 / phase-4 eval). Carve-out: the
  `databricks_notebook` profile target (dbt-spark `method: session`) IS runnable — it is the
  auto-selected default when emitting on a Databricks runtime, and `DbtRunner` invokes dbt
  in-process there so the emitted project runs against the notebook's active SparkSession.

## Review Checklist

Use this checklist when reviewing a feature specification:

- [x] Covered PRD Subsystem(s) and Requirements (`FR-n`) are listed; single subsystem with explicit rationale
- [x] Functional areas are subordinate parts of one capability (the dbt emitter)
- [x] Overview connects this feature to a specific PRD requirement (FR-19.2)
- [x] Ideal future state describes the desired user-visible outcome
- [x] Problem statement describes what exists now and what is broken
- [x] Functional areas are mapped (multiple surfaces: single-table, DAG, facts, CI, packaging)
- [x] Requirements are grouped by functional area
- [x] Domain objects that sound similar are separated (staging `ingested_<t>` vs gold `gold_<t>`; contract `not_null` vs generic tests)
- [x] Every functional requirement is testable and cites source evidence
- [x] Acceptance criteria are defined in US-025, not here (ADR-009)
- [x] Non-functional requirements have specific targets
- [x] Edge cases cover realistic failure scenarios
- [x] Success metrics are specific to this feature
- [x] Dependencies reference real artifact IDs
- [x] Out of scope excludes plausibly-assumed items
- [x] Specifies WHAT not HOW where possible (HOW cited only as evidence)
- [x] Feature is consistent with governing PRD requirements
- [x] No unresolved `[NEEDS CLARIFICATION]` markers
