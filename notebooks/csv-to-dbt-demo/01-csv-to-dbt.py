# Databricks notebook source
# MAGIC %md
# MAGIC # tablespec demo: CSVs → Excel specs → UMFs → dbt → gold report
# MAGIC
# MAGIC A step-by-step, top-to-bottom guide. Before starting, upload the month-1
# MAGIC files to the volume: `members.csv`, `med_claims_20260601.csv`,
# MAGIC `rx_claims_20260601.csv` (from `sample-data/`).

# COMMAND ----------

# MAGIC %pip install dbt-core dbt-spark openai /Workspace/Users/david.mautz@synaptiq.ai/tablespec-fork -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md ## Step 1 — Setup

# COMMAND ----------

import os
import shutil
from pathlib import Path

CATALOG, SCHEMA, VOLUME = "dev", "demo", "data"
CSV_DIR = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
OUT_DIR = "/Workspace/Users/david.mautz@synaptiq.ai/tablespec_out"
LLM_ENDPOINT = "databricks-claude-sonnet-4"  # serving endpoint for spec enrichment
VALIDATION_TABLE = f"`{CATALOG}`.`{SCHEMA}`.`validation_results`"

os.environ["DBT_SPARK_SCHEMA"] = SCHEMA
spark.sql(f"USE CATALOG `{CATALOG}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{SCHEMA}`")

_nb = (
    dbutils.notebook.entry_point.getDbutils()
    .notebook()
    .getContext()
    .notebookPath()
    .get()
)
REPO_DEMO = (Path("/Workspace") / Path(_nb).relative_to("/")).parent

# COMMAND ----------

# MAGIC %md ## Step 2 — Generate Excel spec workbooks from the volume's CSVs
# MAGIC Monthly files are grouped into one table per feed and types are inferred
# MAGIC from the data. The primary keys declared here set incremental MERGE; they
# MAGIC are written into the spec and carried forward automatically — every later
# MAGIC step reads keys and ingestion mode from the spec, not from this cell.

# COMMAND ----------

from tablespec.e2e import excel_specs_from_csvs

# Named once, at generation. The claim feeds carry many *_id / *_nbr columns, so
# the real key can't be guessed by name — declare it explicitly here. The keys
# are written into the spec and carried forward; no later step re-declares them.
PRIMARY_KEYS = {
    "members": ["member_id"],
    "med_claims": ["ps_unique_id"],
    "rx_claims": ["ps_unique_id"],
}

print(excel_specs_from_csvs(CSV_DIR, f"{OUT_DIR}/excel", primary_keys=PRIMARY_KEYS))

# COMMAND ----------

# MAGIC %md ## Step 3 — Enhance the Excel specs with an LLM
# MAGIC Descriptions, business notes, sample values, expectations, and FK
# MAGIC relationships are added to the workbooks in place (fill-only — nothing
# MAGIC you wrote is overwritten). Review the updated workbooks before continuing.

# COMMAND ----------

from tablespec.llm import enrich_excel_specs

print(enrich_excel_specs(f"{OUT_DIR}/excel", model=LLM_ENDPOINT))

# COMMAND ----------

# MAGIC %md ## Step 4 — Convert the workbooks to UMF YAML specs
# MAGIC The Excel workbook is the review surface; the YAML specs are the source
# MAGIC of truth for everything downstream.

# COMMAND ----------

from tablespec.e2e import specs_from_excel_dir

print(specs_from_excel_dir(f"{OUT_DIR}/excel", f"{OUT_DIR}/umf"))

# COMMAND ----------

# MAGIC %md ## The spec set
# MAGIC `umfs` is the working list of specs the rest of the notebook builds from.
# MAGIC It is refreshed in place whenever the specs change (Step 7 adds the gold
# MAGIC spec, Step 10 syncs a new column), so build cells never reload it.

# COMMAND ----------

from tablespec.e2e import umfs_from_spec_dir

umfs = umfs_from_spec_dir(f"{OUT_DIR}/umf", data_dir=CSV_DIR)

# COMMAND ----------

# MAGIC %md ## Step 5 — Generate the SQL, Lakeflow, and dbt artifacts
# MAGIC The one spec set drives three targets side by side — plain SQL (DDL +
# MAGIC gold plan), a Lakeflow Declarative Pipelines project, and the dbt project
# MAGIC built in the next cell. Keys and ingestion mode come from the spec.

# COMMAND ----------

from tablespec.dbt import DbtRunner
from tablespec.e2e import emit_artifacts

runner = DbtRunner()
project = emit_artifacts(umfs, OUT_DIR)

# COMMAND ----------

# MAGIC %md ## Step 6 — Build with dbt and log the validations
# MAGIC The dbt raw models read the volume files directly; the typed models MERGE
# MAGIC the batch into `<catalog>.<schema>`. `dbt build` runs the spec's
# MAGIC validations (contracts + tests); each outcome is appended to the
# MAGIC `validation_results` table (created if missing), then rendered as HTML.

# COMMAND ----------

from tablespec.validation import write_validation_report, write_validation_results

result = runner.build(project)
print(result.stdout)
assert result.success, f"{result.stdout}\n{result.stderr}"

report = result.validation_report()
write_validation_results(report, VALIDATION_TABLE)
json_path, html_path = write_validation_report(report, f"{OUT_DIR}/reports")
displayHTML(html_path.read_text())
display(spark.table(VALIDATION_TABLE))

# COMMAND ----------

# MAGIC %md ## Step 7 — Gold report table
# MAGIC The gold report arrives as an Excel spec workbook (shipped in the repo) —
# MAGIC the same review surface the source specs use. Convert it to a UMF spec in
# MAGIC the spec set, then rebuild with the gold model and re-check the
# MAGIC validations. From here on the UMF is the living artifact: developers and
# MAGIC analysts adjust derivations and rules there over time.

# COMMAND ----------

from tablespec.excel_converter import ExcelToUMFConverter
from tablespec.umf_loader import UMFLoader

gold, _ = ExcelToUMFConverter().convert(
    REPO_DEMO / "sample-specs" / "member_claims_summary.xlsx"
)
UMFLoader().save(gold, f"{OUT_DIR}/umf/{gold.table_name}")

umfs = umfs_from_spec_dir(f"{OUT_DIR}/umf", data_dir=CSV_DIR)  # refreshed in place
project = emit_artifacts(umfs, OUT_DIR)

# COMMAND ----------

result = runner.build(project)
report = result.validation_report()
write_validation_results(report, VALIDATION_TABLE)  # failed runs are logged too
assert result.success, f"{result.stdout}\n{result.stderr}"
print(report.summary())

# COMMAND ----------

# MAGIC %md ## Step 8 — Report files
# MAGIC The gold spec's `metadata.output_config` drives a CSV (row-count footer,
# MAGIC CRLF) + Excel export of the gold table.

# COMMAND ----------

from tablespec.reporting import write_report_from_spark

GOLD_TABLE = f"`{CATALOG}`.`{SCHEMA}`.`gold_{gold.table_name}`"
paths = write_report_from_spark(spark, GOLD_TABLE, gold, f"{OUT_DIR}/reports")
print(f"{paths.csv_path}\n{paths.xlsx_path}")
display(spark.table(GOLD_TABLE))

# COMMAND ----------

# MAGIC %md ## Step 9 — Month 2 arrives (manual)
# MAGIC In the volume: **remove** `med_claims_20260601.csv` and
# MAGIC `rx_claims_20260601.csv`, **upload** `med_claims_20260701.csv` and
# MAGIC `rx_claims_20260701.csv`. The new med file carries a NEW column
# MAGIC (`telehealth_indicator`). Continue when done — if the session restarted
# MAGIC in the meantime, rerun the install cell and Step 1 first.

# COMMAND ----------

# MAGIC %md ## Step 10 — Sync the specs with the new files
# MAGIC New columns found in the current files are appended to the specs (typed
# MAGIC by sampling); the authored gold column for the new field comes from the
# MAGIC repo's ripple file.

# COMMAND ----------

from tablespec.e2e import sync_specs_with_csvs, umfs_from_spec_dir

print("added:", sync_specs_with_csvs(f"{OUT_DIR}/umf", data_dir=CSV_DIR))
shutil.copy(
    REPO_DEMO / "ripple" / "telehealth_visit_count.yaml",
    f"{OUT_DIR}/umf/member_claims_summary/columns/telehealth_visit_count.yaml",
)
umfs = umfs_from_spec_dir(f"{OUT_DIR}/umf", data_dir=CSV_DIR)  # refreshed in place

# COMMAND ----------

# MAGIC %md ## Step 11 — Rebuild: the new month ADDS to the tables
# MAGIC The MERGE keeps month-1 rows even though their files are gone, appends
# MAGIC the new column in place, and the gold report regenerates over both months.
# MAGIC Self-contained (needs only Step 1's setup), so a restarted cluster can
# MAGIC resume here after the manual upload.

# COMMAND ----------

from tablespec.dbt import DbtRunner
from tablespec.e2e import emit_artifacts, umfs_from_spec_dir
from tablespec.reporting import write_report_from_spark
from tablespec.validation import write_validation_results

runner = DbtRunner()
umfs = umfs_from_spec_dir(f"{OUT_DIR}/umf", data_dir=CSV_DIR)  # standalone resume
project = emit_artifacts(umfs, OUT_DIR)

# COMMAND ----------

result = runner.build(project)
write_validation_results(result.validation_report(), VALIDATION_TABLE)
assert result.success, f"{result.stdout}\n{result.stderr}"

display(
    spark.sql(
        f"SELECT file_dt, COUNT(*) AS claim_lines FROM `{CATALOG}`.`{SCHEMA}`.`ingested_med_claims` GROUP BY file_dt ORDER BY file_dt"
    )
)

gold = next(u for u in umfs if u.table_name == "member_claims_summary")
GOLD_TABLE = f"`{CATALOG}`.`{SCHEMA}`.`gold_{gold.table_name}`"
paths = write_report_from_spark(spark, GOLD_TABLE, gold, f"{OUT_DIR}/reports")
print(f"{paths.csv_path}\n{paths.xlsx_path}")
display(spark.table(GOLD_TABLE))

# COMMAND ----------

# MAGIC %md ## Appendix — Start over
# MAGIC Drops everything the demo built: the `raw_`/`ingested_`/`gold_` tables,
# MAGIC the `validation_results` log, and the generated outputs under `OUT_DIR`
# MAGIC (workbooks, UMF specs, dbt/sql/lakeflow artifacts, reports). The volume
# MAGIC and the CSVs you uploaded are untouched. Needs only Step 1; rerun from
# MAGIC Step 2 after.

# COMMAND ----------

for row in spark.sql(f"SHOW TABLES IN `{CATALOG}`.`{SCHEMA}`").collect():
    name = row.tableName
    if name.startswith(("raw_", "ingested_", "gold_")) or name == "validation_results":
        spark.sql(f"DROP TABLE IF EXISTS `{CATALOG}`.`{SCHEMA}`.`{name}`")
        print("dropped", name)
shutil.rmtree(OUT_DIR, ignore_errors=True)
print("removed", OUT_DIR)
