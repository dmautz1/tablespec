# CSV → dbt demo notebook

One notebook, run top to bottom as a step-by-step guide: read the CSVs you
uploaded to the volume → generate reviewable **Excel spec workbooks** (monthly
file families grouped into one table per feed, DATE/DECIMAL types inferred,
declared primary keys ⇒ incremental MERGE) → convert them to **UMF YAML
specs** → emit and run **dbt** (the raw models read the volume files directly;
nothing is pre-loaded) → **validation report** → convert the gold report's
**Excel spec** (shipped in the repo) to UMF and rebuild with the **gold
table**, exporting the CSV+Excel **report files**. Then the guide pauses: you
swap the month-1 claim files for month-2 in the volume (the new med file
carries a NEW column), `sync_specs_with_csvs` appends it to the specs, and the
final rebuild MERGEs the new month into the existing tables — month-1 rows
persist even though their files are gone. Everything lands in `OUT_DIR`
(`excel/`, `specs/`, `dbt/`, `reports/`).

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
3. Declare the primary keys for your feeds in the notebook's `PRIMARY_KEYS`
   dict (a declared key switches the table to incremental MERGE, so new
   monthly batches ADD to the table; keyless tables fully rebuild).
4. Run `01-csv-to-dbt`, then check the results directly in Databricks:
   tables in `<catalog>.<schema>`; Excel workbooks, specs, the dbt project,
   and reports under `OUT_DIR/{excel,specs,dbt,reports}`.

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

### LLM enrichment

Step 3 enhances the generated **Excel spec workbooks in place**
(`enrich_excel_specs`, using the `LLM_ENDPOINT` serving endpoint):
table/column **descriptions**, business **notes**, illustrative **sample
values**, GX **expectations** (Validation Rules sheet), and cross-table **FK
relationships** (Relationships sheet — these become dbt `relationships`
tests). Enrichment is fill-only — your edits are never overwritten — and
skips the `meta_*` provenance columns. Review the updated workbooks before
converting them to YAML in step 4. `enrich_specs` does the same for YAML
spec dirs.

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

CSV mode persists validated, editable specs to `<OUT_DIR>/umf`. Edit them —
narrow types, add `primary_key`, descriptions, enums — then re-run with
`SPEC_DIR = f"{OUT_DIR}/umf"` to build the enriched pipeline. Spec mode accepts
split `table.yaml` dirs, flat `*.yaml`/`*.json`, and `*.xlsx` schema workbooks;
every spec must carry `source: {kind: delimited, path: ...}` (relative paths
resolve against `CSV_DIR`).

## Guided demo: report spec + schema-evolution ripple

Upload the month-1 sample files to the volume (`sample-data/`: `members.csv`,
`med_claims_20260601.csv`, `rx_claims_20260601.csv` — a member roster plus
realistic ACAS-style monthly claim extracts, 1,000/500 lines, ~176/74 columns)
and run steps 1–8. The flow generates the Excel workbooks and UMF specs from
your files, builds the typed tables, then converts the shipped gold Excel spec
(`sample-specs/member_claims_summary.xlsx`) and builds the gold report table
(689 members) plus `member_claims_summary_<date>.{csv,xlsx}` (CRLF + row-count
footer, per the spec's `metadata.output_config`).

### What each report column demonstrates

| Column | Mechanism | See |
|---|---|---|
| `member_id` | `strategy: primary_key` over the member universe (`union_sources`) | all 689 members |
| `member_name` | embedded SQL `expression: CONCAT_WS(' ', first_name, last_name)` | all |
| `birth_dt` | 3-way `highest_priority` survivorship (members → med_claims → rx_claims) | ~150 members have a blank roster birth date the claim feeds fill in |
| `med_claim_count` | `expression: COUNT(*)` + `default_value: 0` | `W000000001` (roster-only in month 1: 0) |
| `total_med_paid` | `expression: SUM(paid_amt)` | any |
| `latest_claim_status` | embedded SQL `expression: MAX_BY(clm_ln_status_cd, srv_start_dt)` | `W000000004` (P across 5 lines) |
| `rx_claim_count` / `total_rx_paid` | pharmacy-side aggregates | `W000000002` (rx-only member) |
| `last_activity_date` | `max_across_sources` survivorship → `GREATEST(MAX(srv_start_dt), MAX(disp_dt))` | `W000000063` (rx dispense wins) |

### Month 2: new files, new column, data ADDS to the tables

The 2026-07 med file carries a NEW `telehealth_indicator` column (Y/N).
At step 9, remove the month-1 claim files from the volume, upload
`med_claims_20260701.csv` + `rx_claims_20260701.csv`, and continue with
steps 10–11:

1. `sync_specs_with_csvs` reads the files now present and APPENDS
   `telehealth_indicator` to the med_claims spec (typed by sampling; nothing
   is ever removed or retyped).
2. The incremental MERGE (from your `PRIMARY_KEYS`) upserts the new batch —
   month-1 rows persist in the tables even though their files are gone, and
   the new column is appended in place (`on_schema_change:
   append_new_columns`).
3. The sync cell also copies the shipped `ripple/telehealth_visit_count.yaml`
   into the gold spec (the authored derived column:
   `SUM(CASE WHEN telehealth_indicator = 'Y' THEN 1 ELSE 0 END)`; month-1
   rows read the missing column as NULL and contribute nothing).
4. The report files regenerate over both months: 2,000 med lines, 170
   telehealth lines across 146 members, and the activity dates/statuses move.

Notes: report column order is alphabetical (split-format load order); glob
schema evolution uses duckdb `union_by_name=true` / Databricks
`mergeSchema => true` (columns matched by NAME — verify mergeSchema once on
your DBR version); `file_dt` on the claim specs is captured from the file
names via `filename_pattern`.

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

Configuration lives in the notebook's **Step 1 — Setup** cell — edit and run:

| Variable | Default | Notes |
|---|---|---|
| `CATALOG` / `SCHEMA` / `VOLUME` | `dev` / `demo` / `data` | UC location; catalog and volume must exist, schema is created |
| `CSV_DIR` | the volume root | Directory of the uploaded input CSVs |
| `OUT_DIR` | `/Workspace/Users/<you>/tablespec_out` | Workspace directory for Excel workbooks, specs, the dbt project, and reports |
| `PRIMARY_KEYS` | demo feed keys | `{table: [key columns]}` — a declared key ⇒ incremental MERGE (months add up); keyless ⇒ full rebuild |

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
