# CSV → dbt demo notebook

One notebook, a few lines per cell: install tablespec from the workspace repo,
derive UMF specs from CSVs in a Unity Catalog volume (`umfs_from_csvs`) or load
authored YAML/JSON/Excel specs (`umfs_from_spec_dir`), emit a dbt project, and
run `dbt build` in-notebook — **dbt itself reads the CSVs and creates the
tables**. No data is pre-loaded: each UMF declares
`source: {kind: delimited, path: ...}`, so the emitted `raw_<t>` models
`read_files(...)` the volume files directly, land them all-STRING (plus
`_source_file`/`_load_ts`), and the typed cast models build on top via
`{{ ref('raw_<t>') }}`. Specs and the dbt project are written to a workspace
directory (`out_dir`) so they can be inspected and edited in the workspace UI.

## Cluster requirements

- **DBR 15.4 LTS or later** (13.3+ minimum — `read_files` requires it).
- **Single-user (dedicated) access mode.** The emitted `databricks_notebook`
  profile is dbt-spark `method: session`, which attaches to the notebook's
  classic driver SparkSession — serverless / Spark-Connect-only compute will
  not work.
- Unity Catalog enabled; permission to `CREATE SCHEMA` (and `CREATE VOLUME`)
  in the target catalog. The catalog itself must already exist.

## Operating it

1. Add the tablespec repo to the workspace as a **Git folder** (until the
   feature branch merges, track `feat/databricks-notebook-dbt-defaults`).
2. Upload one or more CSVs (comma-delimited, `"`-quoted, header row) to the
   target volume via **Catalog ▸ … ▸ Volumes ▸ Upload**, or point `csv_dir`
   at any existing `/Volumes/...` directory.
3. Run `01-csv-to-dbt`, then check the results directly in Databricks:
   tables in `<catalog>.<schema>`, specs and the dbt project under `out_dir`
   (default `/Workspace/Users/<you>/tablespec_out/{specs,dbt}`).

### The iterate loop (CSV → edit specs → spec mode)

CSV mode persists validated, editable specs to `<out_dir>/specs`. Edit them —
narrow types, add `primary_key`, descriptions, enums — then re-run with
`spec_dir = <out_dir>/specs` to build the enriched pipeline. Spec mode accepts
split `table.yaml` dirs, flat `*.yaml`/`*.json`, and `*.xlsx` schema workbooks;
every spec must carry `source: {kind: delimited, path: ...}` (relative paths
resolve against `csv_dir`).

## Widget reference

| Widget | Default | Notes |
|---|---|---|
| `repo_path` | *(empty)* | tablespec repo workspace path; empty = derived from the notebook's own location (it lives at `<repo>/notebooks/csv-to-dbt-demo/`) |
| `catalog` | `main` | UC catalog; must exist |
| `schema` | `tablespec_dbt_demo` | Where dbt materializes every table (`DBT_SPARK_SCHEMA`); created if missing |
| `volume` | `raw` | UC volume with the input CSVs; created if missing |
| `csv_dir` | *(empty)* | CSV directory; empty = the volume root. Also the base for relative spec `source.path` |
| `spec_dir` | *(empty)* | Non-empty switches to spec mode |
| `out_dir` | *(empty)* | Workspace directory for specs + the dbt project; empty = `/Workspace/Users/<you>/tablespec_out` |

## Notes

- **Install cell**: plain `dbt-spark`, not `dbt-spark[session]` — the extra
  only adds a `pyspark>=3,<5` pin, and pip-installing pyspark on DBR shadows
  the runtime's bundled Spark. If the editable (`-e`) install misbehaves on
  the workspace FUSE mount, drop the `-e`.
- **No `sources.yml`**: with every UMF file-backed, the raw landing tables are
  dbt models, so nothing needs to pre-exist and no source schema is configured.
- **Idempotency**: `raw_<t>` models are `table` materializations — rebuilt from
  the file each run. Incremental+PK typed models re-MERGE the same batch
  safely; keyless-append models duplicate rows on re-run. Large CSVs are
  re-read on every build.
