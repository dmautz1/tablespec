# Databricks notebook source
# MAGIC %md
# MAGIC # tablespec demo: CSVs → Excel specs → UMFs → dbt → gold report
# MAGIC
# MAGIC A step-by-step, top-to-bottom guide. Before starting, upload the month-1
# MAGIC files to the volume: `members.csv`, `med_claims_20260601.csv`,
# MAGIC `rx_claims_20260601.csv` (from `sample-data/`).

# COMMAND ----------

# MAGIC %pip install dbt-core dbt-spark openai -e /Workspace/Users/david.mautz@synaptiq.ai/tablespec-fork/src -q

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
PRIMARY_KEYS = {  # declared keys => incremental MERGE (new months add to the tables)
    "members": ["member_id"],
    "med_claims": ["ps_unique_id"],
    "rx_claims": ["ps_unique_id"],
}

os.environ["DBT_SPARK_SCHEMA"] = SCHEMA
spark.sql(f"USE CATALOG `{CATALOG}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{SCHEMA}`")

_nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
REPO_DEMO = (Path("/Workspace") / Path(_nb).relative_to("/")).parent

# COMMAND ----------

# MAGIC %md ## Step 2 — Generate Excel spec workbooks from the volume's CSVs
# MAGIC Monthly files are grouped into one table per feed, types are inferred
# MAGIC from the data, and the declared primary keys set incremental MERGE.

# COMMAND ----------

from tablespec.e2e import umfs_from_csvs
from tablespec.excel_converter import UMFToExcelConverter

Path(f"{OUT_DIR}/excel").mkdir(parents=True, exist_ok=True)
for u in umfs_from_csvs(CSV_DIR, infer_types=True, primary_keys=PRIMARY_KEYS):
    UMFToExcelConverter().convert(u).save(f"{OUT_DIR}/excel/{u.table_name}.xlsx")
    print(f"{OUT_DIR}/excel/{u.table_name}.xlsx")

# COMMAND ----------

# MAGIC %md ## Step 3 — Convert the workbooks to UMF YAML specs
# MAGIC The Excel workbook is the review surface; the YAML specs are the source
# MAGIC of truth for everything downstream.

# COMMAND ----------

from tablespec.e2e import save_specs, umfs_from_spec_dir
from tablespec.excel_converter import ExcelToUMFConverter

umfs = [ExcelToUMFConverter().convert(p)[0] for p in sorted(Path(f"{OUT_DIR}/excel").glob("*.xlsx"))]
save_specs(umfs, f"{OUT_DIR}/specs")
print(f"{OUT_DIR}/specs")

# COMMAND ----------

# MAGIC %md ## Step 4 — Build with dbt
# MAGIC The emitted raw models read the volume files directly; the typed models
# MAGIC MERGE the batch into `<catalog>.<schema>`.

# COMMAND ----------

from tablespec.dbt import DbtRunner

runner = DbtRunner()
result = runner.build(runner.emit(umfs, f"{OUT_DIR}/dbt"))
print(result.stdout)
assert result.success, result.stderr

# COMMAND ----------

# MAGIC %md ## Step 5 — Validation report
# MAGIC The build already ran the spec's validations (contracts + tests).

# COMMAND ----------

from tablespec.validation import write_validation_report

json_path, html_path = write_validation_report(result.validation_report(), f"{OUT_DIR}/reports")
displayHTML(html_path.read_text())

# COMMAND ----------

# MAGIC %md ## Step 6 — Gold report table
# MAGIC Convert the gold report's Excel spec (shipped in the repo) to UMF, then
# MAGIC rebuild with the gold model and re-check the validations.

# COMMAND ----------

from tablespec.umf_loader import UMFLoader

gold, _ = ExcelToUMFConverter().convert(REPO_DEMO / "sample-specs" / "member_claims_summary.xlsx")
UMFLoader().save(gold, f"{OUT_DIR}/specs/{gold.table_name}")

umfs = umfs_from_spec_dir(f"{OUT_DIR}/specs", data_dir=CSV_DIR)
result = runner.build(runner.emit(umfs, f"{OUT_DIR}/dbt"))
assert result.success, result.stderr
print(result.validation_report().summary())

# COMMAND ----------

# MAGIC %md ## Step 7 — Report files
# MAGIC The gold spec's `metadata.output_config` drives a CSV (row-count footer,
# MAGIC CRLF) + Excel export of the gold table.

# COMMAND ----------

from tablespec.reporting import write_report_from_spark

GOLD_TABLE = f"`{CATALOG}`.`{SCHEMA}`.`gold_{gold.table_name}`"
paths = write_report_from_spark(spark, GOLD_TABLE, gold, f"{OUT_DIR}/reports")
print(f"{paths.csv_path}\n{paths.xlsx_path}")
display(spark.table(GOLD_TABLE))

# COMMAND ----------

# MAGIC %md ## Step 8 — Month 2 arrives (manual)
# MAGIC In the volume: **remove** `med_claims_20260601.csv` and
# MAGIC `rx_claims_20260601.csv`, **upload** `med_claims_20260701.csv` and
# MAGIC `rx_claims_20260701.csv`. The new med file carries a NEW column
# MAGIC (`telehealth_indicator`). Continue when done.

# COMMAND ----------

# MAGIC %md ## Step 9 — Sync the specs with the new files
# MAGIC New columns found in the current files are appended to the specs (typed
# MAGIC by sampling); the authored gold column for the new field comes from the
# MAGIC repo's ripple file.

# COMMAND ----------

from tablespec.e2e import sync_specs_with_csvs

print("added:", sync_specs_with_csvs(f"{OUT_DIR}/specs", data_dir=CSV_DIR))
shutil.copy(REPO_DEMO / "ripple" / "telehealth_visit_count.yaml",
            f"{OUT_DIR}/specs/member_claims_summary/columns/telehealth_visit_count.yaml")

# COMMAND ----------

# MAGIC %md ## Step 10 — Rebuild: the new month ADDS to the tables
# MAGIC The MERGE keeps month-1 rows even though their files are gone, appends
# MAGIC the new column in place, and the gold report regenerates over both months.

# COMMAND ----------

umfs = umfs_from_spec_dir(f"{OUT_DIR}/specs", data_dir=CSV_DIR)
result = runner.build(runner.emit(umfs, f"{OUT_DIR}/dbt"))
assert result.success, result.stderr

display(spark.sql(f"SELECT file_dt, COUNT(*) AS claim_lines FROM `{CATALOG}`.`{SCHEMA}`.`ingested_med_claims` GROUP BY file_dt ORDER BY file_dt"))

gold = next(u for u in umfs if u.table_name == gold.table_name)
paths = write_report_from_spark(spark, GOLD_TABLE, gold, f"{OUT_DIR}/reports")
print(f"{paths.csv_path}\n{paths.xlsx_path}")
display(spark.table(GOLD_TABLE))
