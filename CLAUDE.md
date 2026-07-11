# tablespec - Development Guide

Python library for working with table schemas in Universal Metadata Format (UMF). Provides type-safe models, validation, profiling integration, and schema generation tools.

## Project Structure

Top-level layout:

- `src/tablespec/` - the library (details below).
- `apps/data-profiling/` - Streamlit **Databricks App**: profiling, A/B comparison,
  drift, nightly Load Results, and an in-app Guidebook tab. First-party code, same
  Apache-2.0 license as the library (`NOTICE` records its origin). Formatted with
  `ruff format`, and its suite runs in `make test`/CI via `testpaths` +
  `pythonpath` (see `[tool.pytest.ini_options]`). `make lint` and pyright still
  scope to `src/`. See `docs/guide/data-profiling-app.md`.
- `app.yaml` + `requirements.txt` (repo root) - Databricks Apps manifest for that
  app. The source root must be the repo root so `pip install .` provides
  `tablespec` to the app; the command chdir's into `apps/data-profiling` because
  the app resolves `connections.yaml` relative to the working directory.

`src/tablespec/` is organized as a small public surface plus feature-focused subpackages:

- `authoring/` - Apply-response models and mutation/preview helpers for authoring flows.
- `core/` - Shared IR, relation, registry, selection, and schema-fact primitives.
- `dbt/` - dbt project renderers, routing, seeds, registry, and runner wrappers.
- `e2e/` - Compile UMF inputs into runtime artifacts and execute the end-to-end backbone.
- `formatting/` - YAML formatting helpers and shared formatting constants.
- `guidebook/` - Static HTML guidebook generator (discovery, lineage, rendering) for UMF directories.
- `inference/` - Domain-type inference and registry helpers.
- `ingestion/` - Raw/JDBC ingestion helpers and ingestion constants.
- `ldp/` - Local data-product project rendering and expectations.
- `llm/` - OpenAI-compatible LLM client (Databricks-native defaults) and the spec-enrichment orchestrator behind `tablespec enrich`.
- `models/` - Pydantic UMF, quality, changelog, and pipeline models.
- `profiling/` - Native profiler types plus Spark/JDBC profile-to-UMF mappers, and `sql_reflect` (Spark-free UMF reflection from INFORMATION_SCHEMA rows).
- `prompts/` - LLM prompt templates for docs, validation, filenames, relationships, and survivorship.
- `quality/` - Baseline capture/storage and quality execution helpers.
- `sample_data/` - Synthetic data generation, registry, validation, and filename helpers.
- `schemas/` - Schema generators, relationship resolution, and packaged JSON schema assets.
- `validation/` - GX processors, staged reports, custom expectations, and the Spark-backed table validator.

Flat modules at the package root hold the remaining cross-cutting helpers and CLI entrypoints:

- `bootstrap.py` - Bootstrap UMFs from tables.
- `canonical.py` - Canonicalization helpers for stable field/value handling.
- `casting_utils.py` - Dialect-aware casting and format conversion utilities.
- `cli.py` - Typer CLI for validation, inspection, conversion, and TUI launch.
- `compatibility.py` - Compatibility checks and report types.
- `date_formats.py` - Shared date and timestamp format constants.
- `dependency_resolver.py` - Dependency and relation resolution helpers.
- `excel_converter.py` / `excel_import_git.py` - Excel import/export helpers.
- `gx_baseline.py`, `gx_constraint_extractor.py`, `gx_schema_validator.py`, `gx_wrapper.py` - Great Expectations integration entrypoints.
- `merge.py`, `relationship_validator.py`, `completeness_validator.py`, `validator.py` - Validation and merge orchestration.
- `naming.py`, `naming_validator.py`, `type_lattice.py`, `type_mappings.py` - Naming and type-system utilities.
- `output_formatting.py`, `survivorship_display.py`, `format_utils.py` - User-facing formatting helpers.
- `session.py`, `spark_factory.py` - Spark session helpers and factory wiring.
- `sync_baseline.py`, `umf_change_applier.py`, `umf_diff.py`, `umf_loader.py`, `umf_validator.py` - UMF diff/load/change-management utilities.
- `tui.py` - Optional Textual-based terminal UI.

## Optional Dependencies

- **`[spark]`** - PySpark support for Spark session helpers, profiling, validation, and other Spark-backed APIs.
  - Install: `uv sync --extra spark`
- **`[duckdb]`** - DuckDB plus SQLAlchemy support for local dbt/SQL execution paths and dialect parity checks.
  - Install: `uv sync --extra duckdb`
- **`[tui]`** - Textual support for the optional terminal UI.
  - Install: `uv sync --extra tui`
- **`[llm]`** - OpenAI-compatible client for `tablespec enrich` (LLM spec enrichment).
  - Install: `uv sync --extra llm`

## Development Workflow

Run `make help` to see all available commands. Key targets:

```bash
make install-dev  # Install with dev dependencies
make check        # Run lint, type-check, and tests
make format       # Format code with ruff
make test         # Run all tests
make coverage     # Run tests with coverage report
```

## Testing Strategy

- **Unit tests**: `tests/unit/` - Pure Python logic, UMF models, type mappings
- **Integration tests**: `tests/integration/` - Tests requiring external dependencies
- Run specific tests: `uv run pytest tests/unit/test_gx_baseline.py`
- Coverage target: Use `make coverage` for HTML reports in `htmlcov/`

## Key Conventions

### Code Style

- **Formatter**: Ruff (opinionated, no config needed)
- **Linter**: Ruff with autofix via `make lint-fix`
- **Type checking**: pyright for `src/` directory
- **Python version**: 3.12+ (specified in pyproject.toml)

### UMF Format

- YAML-based schema format with Pydantic validation
- Column types: VARCHAR, CHAR, TEXT, INTEGER, DECIMAL, FLOAT, DATE, DATETIME, BOOLEAN
- Nullable configuration per LOB (MD/MP/ME for Medicaid/Medicare)
- See README.md for full UMF structure and examples

### Type Mappings

All type conversions go through `type_mappings.py`:
- UMF → PySpark: `map_to_pyspark_type()`
- UMF → JSON Schema: `map_to_json_type()`
- UMF → GX Spark: `map_to_gx_spark_type()`

### Module Import Pattern

Public API defined in `src/tablespec/__init__.py`. Conditional imports for Spark-dependent features:

```python
# Always available
from tablespec import UMF, load_umf_from_yaml, generate_sql_ddl

# Available only with tablespec[spark]
from tablespec import SparkToUmfMapper, TableValidator
```

## Common Tasks

### Adding a New UMF Field

1. Update `models/umf.py` with new Pydantic field
2. Update schema generators in `schemas/generators.py` if applicable
3. Add tests in `tests/unit/`
4. Update JSON schema in `schemas/umf.schema.json`

### Adding a New Schema Generator

1. Create function in `schemas/generators.py`
2. Export in `schemas/__init__.py` and top-level `__init__.py`
3. Add corresponding type mapping in `type_mappings.py` if needed
4. Add unit tests

### Working with Great Expectations

- **Baseline generation**: Use `BaselineExpectationGenerator` for deterministic expectations from UMF
- **Constraint extraction**: Use `GXConstraintExtractor` to reverse-engineer UMF from existing suites
- **Validation**: Use `TableValidator` (requires Spark) to validate DataFrames against UMF specs

## Notes for AI Assistants

- This is a **pure Python library** focused on schema metadata, not data processing
- Spark is an **optional dependency** - check if PySpark features are needed before suggesting
- UMF is the **single source of truth** - all conversions should be bidirectional when possible
- Great Expectations integration is **read/write** - both generate and extract constraints
- Keep the **Makefile self-documenting** - use `## comments` for help text
