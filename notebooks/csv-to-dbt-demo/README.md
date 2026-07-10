# CSV → dbt demo notebook

One notebook, the whole tablespec → dbt story on Databricks: install tablespec
from the workspace repo, derive UMF specs from CSVs in a Unity Catalog volume
(or load authored YAML/JSON/Excel specs), emit a dbt project, and run
`dbt build` in-notebook — **dbt itself reads the CSVs and creates the tables**.
No data is pre-loaded: each UMF declares `source: {kind: delimited, path: ...}`,
so the emitted `raw_<t>` models `read_files(...)` the volume files directly,
land them all-STRING (ADR-007, plus `_source_file`/`_load_ts`), and the typed
cast models build on top via `{{ ref('raw_<t>') }}`.

## Notebook

| Notebook | What it does |
|---|---|
| `01-csv-to-dbt` | install → UMFs from CSVs or specs → `tablespec validate` → `DbtRunner.emit` → in-process `dbt build` → verify + scorecard |

## Cluster requirements

- **DBR 15.4 LTS or later** (13.3+ minimum — `read_files` requires it).
- **Single-user (dedicated) access mode.** The emitted `databricks_notebook`
  profile is dbt-spark `method: session`, which attaches to the notebook's
  classic driver SparkSession — serverless / Spark-Connect-only compute will
  not work.
- Unity Catalog enabled; permission to `CREATE SCHEMA` (and `CREATE VOLUME`)
  in the target catalog. The catalog itself must already exist — the notebook
  never creates catalogs.

## Operating it

1. Add the tablespec repo to the workspace as a **Git folder** (until the
   feature branch merges, track `feat/databricks-notebook-dbt-defaults`).
2. Upload one or more CSVs (comma-delimited, `"`-quoted, header row) to the
   target volume — the notebook creates `<catalog>.<schema>.<volume>` if
   missing; upload via **Catalog ▸ … ▸ Volumes ▸ Upload** after a first run,
   or point `csv_dir` at any existing `/Volumes/...` directory.
3. Run `01-csv-to-dbt`. Defaults: everything lands in
   `main.tablespec_dbt_demo`; the derived specs are persisted to
   `<volume>/tablespec_out/specs/`.

### The iterate loop (CSV → edit specs → spec mode)

CSV mode derives all-VARCHAR starter specs and persists them. Edit them —
narrow types, add `primary_key`, descriptions, enums — then re-run the notebook
with `spec_dir = /Volumes/<catalog>/<schema>/<volume>/tablespec_out/specs` to
build the enriched pipeline. Spec mode also accepts flat `*.yaml`/`*.json` and
`*.xlsx` schema workbooks (`ExcelToUMFConverter`); every spec must carry
`source: {kind: delimited, path: ...}` (relative paths resolve against
`csv_dir`).

## Widget reference

| Widget | Default | Notes |
|---|---|---|
| `repo_path` | *(empty)* | tablespec repo workspace path; empty = derived from the notebook's own location (it lives at `<repo>/notebooks/csv-to-dbt-demo/`) |
| `catalog` | `main` | UC catalog; must exist |
| `schema` | `tablespec_dbt_demo` | Where dbt materializes every table (`DBT_SPARK_SCHEMA`); created if missing |
| `volume` | `raw` | UC volume with the input CSVs; created if missing |
| `csv_dir` | *(empty)* | CSV directory; empty = the volume root. Also the base for relative spec `source.path` |
| `spec_dir` | *(empty)* | Non-empty switches to spec mode (CSV widgets still locate the volume/outputs) |

## Notes

- **Install cell**: `pip install -e <repo> dbt-core dbt-spark`. Plain
  `dbt-spark` deliberately — the `[session]` extra only adds a
  `pyspark>=3,<5` pin, and pip-installing pyspark on DBR shadows the runtime's
  bundled Spark; the session method needs nothing beyond the runtime's own
  pyspark. If the editable (`-e`) install misbehaves on the workspace FUSE
  mount, drop the `-e`.
- **dbt project dir** is `/tmp/tablespec_dbt` on the driver (dbt's `target/`
  churn is slow on FUSE volumes and the project is disposable — it is
  re-emitted every run and shown inline in the notebook).
- **No `sources.yml`**: with every UMF file-backed, the raw landing tables are
  dbt models, so nothing needs to pre-exist and no source schema is configured.
- **Idempotency**: `raw_<t>` models are `table` materializations — rebuilt from
  the file each run. Incremental+PK typed models re-MERGE the same batch
  safely; keyless-append models duplicate rows on re-run (the existing emitter
  contract). Large CSVs are re-read on every build — fine for a demo.
- Proven pairing target: dedicated-access DBR 15.4+; verify the plain
  `dbt-spark` install on your runtime first — it is the one deliberate
  deviation from the docs' `dbt-spark[session]`.
