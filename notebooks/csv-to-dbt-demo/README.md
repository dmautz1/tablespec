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
directory (`OUT_DIR`) so they can be inspected and edited in the workspace UI.

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
2. Upload one or more CSVs (comma- or pipe-delimited — detected per file from
   the header row; `"`-quoted, header row required) to the target volume via
   **Catalog ▸ … ▸ Volumes ▸ Upload**, or point `CSV_DIR` at any existing
   `/Volumes/...` directory.
3. Run `01-csv-to-dbt`, then check the results directly in Databricks:
   tables in `<catalog>.<schema>`, specs and the dbt project under `OUT_DIR`
   (default `/Workspace/Users/<you>/tablespec_out/{specs,dbt}`).

### Generating specs from the command line

The same capability is available off-notebook (no Spark needed — only each
file's header row is read):

```bash
tablespec import-csv data/orders.csv tables/          # one file
tablespec import-csv /Volumes/main/demo/raw tables/   # a directory of CSVs
```

Pipe vs comma is detected per file (`--delimiter` overrides); each table gets
a validated split-format spec carrying `source: {kind: delimited, path: ...}`.
Headers that are not valid identifiers are sanitized into the column `name`
with the original label preserved as `canonical_name`.

### LLM enrichment (optional)

Set the `LLM_ENDPOINT` variable to a Databricks serving-endpoint name (e.g.
`databricks-claude-sonnet-4`) and the notebook enriches the derived specs
before emitting: table/column **descriptions**, business **notes**,
illustrative **sample values**, GX **expectations**, and cross-table **FK
relationships** (which become dbt `relationships` tests and the Excel
Relationships sheet). Enrichment is fill-only — your edits are never
overwritten — and skips the `meta_*` provenance columns.

The same capability off-notebook:

```bash
tablespec enrich tables/                              # zero config on Databricks
tablespec enrich tables/ --include descriptions --dry-run
```

Anywhere else, point it at any OpenAI-compatible endpoint via
`TABLESPEC_LLM_BASE_URL` / `TABLESPEC_LLM_API_KEY` / `TABLESPEC_LLM_MODEL`
(requires `pip install 'tablespec[llm]'`). Note: LLM-suggested expectations
and FKs run as real dbt tests at build time — review the enriched specs
(`tablespec export-excel`) before trusting a green build.

### The iterate loop (CSV → edit specs → spec mode)

CSV mode persists validated, editable specs to `<OUT_DIR>/specs`. Edit them —
narrow types, add `primary_key`, descriptions, enums — then re-run with
`SPEC_DIR = f"{OUT_DIR}/specs"` to build the enriched pipeline. Spec mode accepts
split `table.yaml` dirs, flat `*.yaml`/`*.json`, and `*.xlsx` schema workbooks;
every spec must carry `source: {kind: delimited, path: ...}` (relative paths
resolve against `CSV_DIR`).

## Guided demo: report spec + schema-evolution ripple

Set `DEMO = True` in the Variables cell and Run All. The seed cell copies the shipped
month-1 sample files (`sample-data/`) to the volume and the shipped specs
(`sample-specs/`) — three source specs plus the **generated (gold) report
spec** `member_claims_summary` — to `<OUT_DIR>/specs`, then runs the whole
flow: dbt ingests the files, builds the gold report table, and the report
cell writes `member_claims_summary_<date>.{csv,xlsx}` (CRLF line endings +
row-count footer, per the spec's `metadata.output_config`).

### What each report column demonstrates

| Column | Mechanism | See member |
|---|---|---|
| `member_id` | `strategy: primary_key` over the member universe (`union_sources`) | all 9 |
| `member_name` | embedded SQL `expression: CONCAT_WS(' ', first_name, last_name)` | all |
| `contact_phone` | 3-way `highest_priority` survivorship (members → claims → rx) + `default_value` | M001 (members wins over claims), M002 (claims), M004 (rx), M007 (`UNKNOWN`) |
| `medical_claim_count` | `expression: COUNT(*)` + `default_value: 0` | M004/M008 (0) |
| `total_medical_amount` | `expression: SUM(claim_amount)` | any |
| `latest_claim_status` | embedded SQL `expression: MAX_BY(claim_status, service_date)` | M002 (DENIED → PAID after month 2) |
| `rx_fill_count` / `total_rx_amount` | rx-side aggregates | M004 |
| `last_activity_date` | `max_across_sources` survivorship → `GREATEST(MAX(service_date), MAX(fill_date))` | M003 (rx fill wins), M008 (NULL) |

### The ripple: month 2 adds a column

`claims_202402.csv` carries a NEW `copay_amount` column. Order matters —
**upload data before editing specs** (the raw model's explicit column list
requires the glob's combined headers to contain every spec column; editing
first fails the build, and a test pins that):

1. Copy the month-2 files to the volume (a notebook cell or the UI):
   `claims_202402.csv`, `rx_202402.csv` from `sample-data/`.
2. Add the column to the claims source spec:
   `tablespec column-add <OUT_DIR>/specs/claims --name copay_amount --type DECIMAL`
3. Add the derived column to the report spec — copy the shipped
   `ripple/total_copay.yaml` into
   `<OUT_DIR>/specs/member_claims_summary/columns/` (it derives
   `SUM(copay_amount)` from claims; `SUM` ignores the NULLs month-1 rows get).
4. Run All again: `total_copay` ripples into the gold table and both report
   files; `latest_claim_status` and `last_activity_date` move with the new
   month's data.

Notes: report column order is alphabetical (split-format load order); glob
schema evolution uses duckdb `union_by_name=true` / Databricks
`mergeSchema => true` (verify the latter once on your DBR version — the demo
data also appends the new column LAST so month-1 files stay aligned);
`file_month` on claims/rx is captured from the file names via
`filename_pattern`.

### Validation report

Every `dbt build` already executes the spec's validations — enforced contracts
(types, not-null) and data tests (`unique`, `relationships`,
`accepted_values`, including LLM-enriched ones). The notebook's final cell
turns the build's `target/run_results.json` into the repo's canonical
`ValidationReport` and writes `<OUT_DIR>/reports/validation-report.{json,html}`
— the HTML renders inline in the notebook and is browsable in the workspace
UI. The same report is available anywhere via
`result.validation_report()` on any `DbtRunner` result, or
`tablespec.validation.dbt_validation_report(project_dir)`.

## Variables

Configuration lives in the notebook's **Variables** cell — edit and re-run:

| Variable | Default | Notes |
|---|---|---|
| `CATALOG` | `main` | UC catalog; must exist |
| `SCHEMA` | `tablespec_dbt_demo` | Where dbt materializes every table (`DBT_SPARK_SCHEMA`); created if missing |
| `VOLUME` | `raw` | UC volume with the input CSVs; created if missing |
| `CSV_DIR` | the volume root | Directory of input CSVs; also the base for relative spec `source.path` |
| `SPEC_DIR` | `""` | Non-empty switches to spec mode (authored specs instead of CSV-derived) |
| `OUT_DIR` | `/Workspace/Users/<you>/tablespec_out` | Workspace directory for specs, the dbt project, and reports |
| `LLM_ENDPOINT` | `""` | Serving-endpoint / model name for LLM spec enrichment; empty = skip (CSV mode only) |
| `DEMO` | `False` | `True` = seed the guided demo (sample CSVs + sample specs incl. the gold report spec) |

The tablespec repo itself is installed from the notebook's own repo checkout
(derived from the notebook path — the notebook lives at
`<repo>/notebooks/csv-to-dbt-demo/`).

## Notes

- **Install cell**: plain `dbt-spark`, not `dbt-spark[session]` — the extra
  only adds a `pyspark>=3,<5` pin, and pip-installing pyspark on DBR shadows
  the runtime's bundled Spark. If the editable (`-e`) install misbehaves on
  the workspace FUSE mount, drop the `-e`.
- **No `sources.yml`**: with every UMF file-backed, the raw landing tables are
  dbt models, so nothing needs to pre-exist and no source schema is configured.
- **Multi-file tables**: a spec's `source.path` may be a glob
  (`/Volumes/.../orders_*.csv`); add `filename_pattern: {regex: ..., captures:
  {1: col}}` to load only matching files and extract file-name metadata (e.g.
  a file date) into `source: filename` columns. `_source_file` records each
  row's actual file.
- **Idempotency**: `raw_<t>` models are `table` materializations — rebuilt from
  the file each run. Incremental+PK typed models re-MERGE the same batch
  safely; keyless-append models duplicate rows on re-run. Large CSVs are
  re-read on every build.
