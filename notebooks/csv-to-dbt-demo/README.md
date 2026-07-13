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

Upload the month-1 sample files to the volume yourself (`sample-data/`:
`members.csv`, `med_claims_20260601.csv`, `rx_claims_20260601.csv` — a member
roster plus realistic ACAS-style monthly medical and pharmacy claim extracts,
1,000/500 lines, ~176/74 columns), then set `DEMO = True` in the Variables
cell and Run All. The source specs are **generated from your uploaded files**
(`umfs_from_csvs(infer_types=True)`): date-suffixed files are grouped into
monthly-family specs (`med_claims_*.csv` glob + a `file_dt` filename capture),
DATE/DECIMAL/INTEGER types are inferred from the data, and idempotent
`snapshot` ingestion is set. The demo then adds the one authored artifact —
the **gold report spec** `member_claims_summary` (`sample-specs/`) — and the
flow runs end to end: dbt ingests the files, builds the gold report table
(689 members), and the report cell writes
`member_claims_summary_<date>.{csv,xlsx}` (CRLF line endings + row-count
footer, per the spec's `metadata.output_config`). Later runs automatically
switch to spec mode, reusing the generated specs and any edits you make.

### What each report column demonstrates

| Column | Mechanism | See |
|---|---|---|
| `member_id` | `strategy: primary_key` over the member universe (`union_sources`) | all 689 members |
| `member_name` | embedded SQL `expression: CONCAT_WS(' ', first_name, last_name)` | all |
| `birth_dt` | 3-way `highest_priority` survivorship (members → med_claims → rx_claims) | ~150 members have a blank roster birth date that the claim feeds fill in |
| `med_claim_count` | `expression: COUNT(*)` + `default_value: 0` | `W000000001` (roster-only in month 1: 0) |
| `total_med_paid` | `expression: SUM(paid_amt)` | any |
| `latest_claim_status` | embedded SQL `expression: MAX_BY(clm_ln_status_cd, srv_start_dt)` | `W000000004` (P across 5 lines) |
| `rx_claim_count` / `total_rx_paid` | pharmacy-side aggregates | `W000000002` (rx-only member) |
| `last_activity_date` | `max_across_sources` survivorship → `GREATEST(MAX(srv_start_dt), MAX(disp_dt))` | `W000000063` (rx dispense wins) |

### The ripple: month 2 adds a column

The 2026-07 med file (`med_claims_20260701.csv`) carries a NEW
`telehealth_indicator` column (Y/N). Order matters — **upload data before
editing specs** (the raw model's explicit column list requires the glob's
combined headers to contain every spec column; editing first fails the build,
and a test pins that):

1. Copy the month-2 files to the volume (a notebook cell or the UI):
   `med_claims_20260701.csv`, `rx_claims_20260701.csv` from `sample-data/`.
2. Add the column to the med_claims source spec:
   `tablespec column-add <OUT_DIR>/specs/med_claims --name telehealth_indicator --type VARCHAR --length 1`
3. Add the derived column to the report spec — copy the shipped
   `ripple/telehealth_visit_count.yaml` into
   `<OUT_DIR>/specs/member_claims_summary/columns/` (it derives
   `SUM(CASE WHEN telehealth_indicator = 'Y' THEN 1 ELSE 0 END)` from
   med_claims; month-1 rows read the missing column as NULL and contribute
   nothing).
4. Run All again: `telehealth_visit_count` ripples into the gold table and
   both report files (170 telehealth lines across 146 members);
   `latest_claim_status` and `last_activity_date` move with the new month's
   data.

Notes: report column order is alphabetical (split-format load order); glob
schema evolution uses duckdb `union_by_name=true` / Databricks
`mergeSchema => true` (columns are matched by NAME, so the new column's
position mid-header doesn't matter — verify mergeSchema once on your DBR
version); `file_dt` on the claim specs is captured from the file names via
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
| `CATALOG` | `dev` | UC catalog; must exist |
| `SCHEMA` | `demo` | Where dbt materializes every table (`DBT_SPARK_SCHEMA`); created if missing |
| `VOLUME` | `data` | UC volume with the input CSVs; created if missing |
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
