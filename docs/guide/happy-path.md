# Happy Path: From Tables to Pipelines

This is the canonical end-to-end route for tablespec. Follow this flow instead of
discovering the low-level APIs piecemeal.

The supported composition today is:

- Path A: reflect existing tables with `umfs_from_tables(...)` or the convenience
  wrapper `bootstrap_from_tables(...)`
- Path B: load authored specs with `umfs_from_specs(...)`
- Sample data: `SampleDataGenerator`
- Validation: `TableValidator` for row-level checks, `tablespec validate` for UMF
  structure
- Review/edit: `UMFToExcelConverter` and `ExcelToUMFConverter`
- Pipeline compilation: `compile_umfs(...)`, which drives `generate_sql_plan`,
  `generate_dbt_project`, `generate_dbt_dag_project`, and `generate_ldp_project`
- Pipeline execution: `run_backbone(...)`

Current boundary notes:

- Split YAML UMF or JSON interchange is the canonical authoring surface. Inline
  YAML is legacy/migration-only.
- Raw storage preserves source records for audit and replay. The compiled
  ingested artifact set preserves source semantics in platform-native form:
  typed columns, validation criteria, relationships, aliases, keys, raw-to-ingest
  SQL, validation suites, and manifest entries.
- Ingested is still source-preserving bronze, not silver. Cross-source
  conformance, survivorship, entity resolution, enrichment, and dimensional
  modeling remain downstream responsibilities.
- Databricks-facing compile UX accepts `dialect="databricks"` for the
  Spark-family SQL emitted by tablespec; internal emitters may normalize to
  `spark` when the rendered SQL is identical.
  The active bead trail is `tablespec-ed74497c` and child beads
  `tablespec-0b146671`, `tablespec-171e409c`, and `tablespec-0fb0d1c2`.
- Production runs consume the committed artifact tree and installed packages,
  not source-time orchestration. See the deployment checklist for the release
  boundary.

## 1. Generate UMF from existing Spark or Databricks tables

Use Path A when the tables already exist and you want UMF plus optional profiling
inference.

```python
from tablespec.e2e import umfs_from_tables

umfs, suites = umfs_from_tables(
    spark,
    ["member", "claims"],
    profile=True,
)
```

`umfs_from_tables(...)` reflects each table into a strict UMF model. When
`profile=True`, it also returns profile-derived expectation suites keyed by table
name.

Reflection captures the table's declared structure. Profiling enriches validation
evidence. The compile step is what turns those source semantics into the
committed ingested contract consumed downstream.

If you do not need the intermediate `UMF` list, the convenience wrapper
`bootstrap_from_tables(...)` reflects, profiles, compiles, and returns the
manifest in one call.

For authored specs, use the Path B loader instead:

```python
from tablespec.e2e import umfs_from_specs

umfs = umfs_from_specs([
    "tables/member/table.yaml",
    "tables/claims/table.yaml",
])
```

## 2. Generate sample data from the UMF or spec inputs

Sample data is generated from UMF/spec inputs, not from the compiled artifact
tree.

```python
from pathlib import Path
from tablespec import GenerationConfig, SampleDataGenerator

generator = SampleDataGenerator(
    input_dir=Path("tables"),
    output_dir=Path("build/sample-data"),
    config=GenerationConfig(
        num_members=1_000,
        relationship_density=0.7,
        temporal_range_days=365,
    ),
    spark=spark,  # optional: useful when cross-pipeline FK seeding needs Spark
)

generator.run_generation()
```

The generated CSVs can be reused as dbt seeds or as validation fixtures, but the
generator itself stays at the UMF/spec layer.

## 3. Validate real source data and generated sample data

Use the same UMF contract to validate both the real source DataFrame and the
generated sample DataFrame.

```python
from pathlib import Path
from tablespec import TableValidator

validator = TableValidator(spark)
umf_path = Path("tables/member/table.yaml")

real_errors = validator.validate_table(spark.table("member"), umf_path, "member")
sample_errors = validator.validate_table(
    spark.read.csv("build/sample-data/member/member.csv", header=True),
    umf_path,
    "member",
)
```

`tablespec validate ...` is the CLI for UMF structure and relationship checks.
`TableValidator` is the row-level path for actual source data and generated sample
data.

## 4. Generate table-spec and validation Excel workbooks

Use the Excel converters when domain experts want to review or edit the UMF in a
spreadsheet.

```python
from tablespec import ExcelToUMFConverter, UMFToExcelConverter, load_umf_from_yaml

umf = load_umf_from_yaml("tables/member/table.yaml")

workbook = UMFToExcelConverter().convert(umf)
workbook.save("build/member-review.xlsx")

reviewed_umf, _ = ExcelToUMFConverter().convert("build/member-review.xlsx")
```

The workbook is a review/edit surface. The canonical runtime inputs remain the
UMF/spec files and the compiled artifact tree.

## 5. Define a derived table from source UMFs

Derived tables are modeled as `table_type="generated"` plus column derivation
metadata. Each output column can point back to one or more source tables via
`UMFColumnDerivation` and `DerivationCandidate`.

```python
from tablespec import (
    DerivationCandidate,
    Nullable,
    UMF,
    UMFColumn,
    UMFColumnDerivation,
    load_umf_from_yaml,
    generate_sql_plan,
)

medical_claims_umf = load_umf_from_yaml("tables/medical_claims/table.yaml")
providers_umf = load_umf_from_yaml("tables/providers/table.yaml")

claims_summary = UMF(
    version="1.0",
    table_name="Claims_Summary",
    table_type="generated",
    columns=[
        UMFColumn(
            name="claim_id",
            data_type="VARCHAR",
            length=50,
            nullable=Nullable(MD=False, MP=False, ME=False),
            derivation=UMFColumnDerivation(
                strategy="primary_key",
                candidates=[
                    DerivationCandidate(
                        table="Medical_Claims",
                        column="claim_id",
                        priority=1,
                    )
                ],
            ),
        ),
    ],
)

plan_sql = generate_sql_plan(
    claims_summary,
    {"Medical_Claims": medical_claims_umf, "Providers": providers_umf},
    mode="views",
)
```

The same UMF set also feeds the dbt DAG and LDP emitters, so the derived table
stays aligned with the rest of the pipeline.

## 6. Generate Spark, LDP, and dbt pipeline artifacts

`compile_umfs(...)` is the current orchestration seam. It persists the ingest
SQL, schema artifacts, validation suite, single-table dbt project, whole-set dbt
DAG, LDP project, and any requested gold plan artifacts.

```python
from tablespec.e2e import compile_umfs

artifacts = compile_umfs(
    umfs,
    out_dir="build/tablespec",
    source="tables",
    profile_enriched=True,
    dialect="databricks",  # Databricks-facing compile UX spelling for Spark-family SQL.
    gold_targets=["Claims_Summary"],
)

print(artifacts.manifest_path)
```

The ingested outputs are the source-semantic bronze completion point. They make
source meaning explicit without claiming to solve silver-layer conformance or
entity-resolution concerns.

If you only need one backend family, call the emitters directly:

```python
from tablespec.dbt import generate_dbt_dag_project, generate_dbt_project
from tablespec.ldp import generate_ldp_project
```

The supported split today is still composition, not a separate `tablespec compile`
CLI facade.

### Run an emitted dbt project from a Databricks notebook

On a Databricks runtime (detected via `DATABRICKS_RUNTIME_VERSION`) the dbt
emitters default to `dialect="databricks"` and the runnable
`databricks_notebook` profile target — a dbt-spark `method: session` profile that
attaches to the notebook's active SparkSession, so no host/http_path/token is
needed. `DbtRunner` also defaults to invoking dbt **in-process** there (a
subprocess dbt would build its own SparkSession instead of attaching). Reuse one
runner instance per notebook session; dbt's programmatic entrypoint mutates
global logging state on each invoke.

```python
# In a Databricks notebook (requires dbt-core + dbt-spark[session]):
from tablespec.dbt import DbtRunner

runner = DbtRunner()
project = runner.emit(umfs, "/tmp/tablespec_dbt")  # defaults resolve to databricks
result = runner.build(project)                     # runs in-process, session method
assert result.success, result.stderr
```

Off-Databricks nothing changes: the defaults stay `duckdb`/`duckdb`, and explicit
`dialect=`/`target=` arguments always win (e.g. `target="databricks_notebook"` to
emit the session profile from anywhere).

## 7. Run the generated pipelines

Use the runtime backbone to execute the committed artifacts that `compile_umfs(...)`
wrote.

```python
from pathlib import Path
from tablespec.e2e import run_backbone

result = run_backbone(
    artifacts,
    spark=spark,
    raw_batches={"member": [Path("tests/e2e/fixtures/member.raw.csv")]},
    backend="spark",
)

assert result.ok
```

Development bootstrap:

- Build the artifact tree in a dev shell or CI job.
- Review the diff of the committed files.
- Run the backbone against those artifacts.

Production install/run:

- Build and publish the wheel from the release process.
- Install the wheel and consume the committed artifact tree through
  `manifest.json`.
- Do not re-derive UMF or transforms from source at runtime.

Databricks-safe notes:

- In notebooks, run tests in-process with `pytest.main(...)` or `ipytest.run(...)`.
- Do not use `uv run pytest` from a Databricks runtime notebook kernel.
- Use `%pip install -e ...` when the notebook needs the local package.

## See Also

- [Bootstrap](bootstrap.md)
- [Sample Data](sample-data.md)
- [Excel Conversion](excel.md)
- [Profiling](profiling.md)
- [Deployment Checklist](../helix/05-deploy/deployment-checklist.md)
- [FEAT-026](../helix/01-frame/features/FEAT-026-compile-orchestrator-bootstrap.md)
- [FEAT-027](../helix/01-frame/features/FEAT-027-dbt-emitter.md)
- [FEAT-028](../helix/01-frame/features/FEAT-028-ldp-sibling-emitter.md)
