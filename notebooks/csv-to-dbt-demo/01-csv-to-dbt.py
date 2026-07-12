# Databricks notebook source
# MAGIC %md
# MAGIC # CSVs / specs → UMFs → dbt build
# MAGIC
# MAGIC Derive UMF specs from CSVs in a UC volume (or load authored specs via
# MAGIC `SPEC_DIR`), emit a dbt project, and run `dbt build` in-notebook — dbt reads
# MAGIC the files itself and builds the typed tables in `<catalog>.<schema>`. Specs,
# MAGIC the dbt project, and reports land in `OUT_DIR` for inspection and editing.
# MAGIC
# MAGIC Set `DEMO = True` for the guided demo (sample members/claims/rx files + a
# MAGIC gold report spec) — see the README.

# COMMAND ----------

from pathlib import Path

_nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
REPO = str((Path("/Workspace") / Path(_nb).relative_to("/")).parents[2])

# COMMAND ----------

# MAGIC %pip install dbt-core dbt-spark openai -e {REPO} -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md ## Setup

# COMMAND ----------

import os
import shutil
from pathlib import Path

CATALOG = "main"
SCHEMA = "tablespec_dbt_demo"
VOLUME = "raw"

CSV_DIR = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"  # input CSVs
SPEC_DIR = ""  # non-empty: use authored specs instead of deriving from CSVs
OUT_DIR = f"/Workspace/Users/{spark.sql('SELECT current_user()').first()[0]}/tablespec_out"

LLM_ENDPOINT = ""  # e.g. "databricks-claude-sonnet-4" to enrich specs
DEMO = False  # True: seed the guided demo (sample data + gold report spec)

os.environ["DBT_SPARK_SCHEMA"] = SCHEMA
spark.sql(f"USE CATALOG `{CATALOG}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{SCHEMA}`")
spark.sql(f"CREATE VOLUME IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`.`{VOLUME}`")

if DEMO:
    _nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
    _demo = (Path("/Workspace") / Path(_nb).relative_to("/")).parent
    for _f in ("members.csv", "claims_202401.csv", "rx_202401.csv"):
        shutil.copy(_demo / "sample-data" / _f, f"{CSV_DIR}/{_f}")
    if not Path(f"{OUT_DIR}/specs").exists():
        shutil.copytree(_demo / "sample-specs", f"{OUT_DIR}/specs")
    SPEC_DIR = SPEC_DIR or f"{OUT_DIR}/specs"

# COMMAND ----------

# MAGIC %md ## UMF specs

# COMMAND ----------

from tablespec.e2e import save_specs, umfs_from_csvs, umfs_from_spec_dir

umfs = umfs_from_spec_dir(SPEC_DIR, data_dir=CSV_DIR) if SPEC_DIR else umfs_from_csvs(CSV_DIR)
if not SPEC_DIR:
    save_specs(umfs, f"{OUT_DIR}/specs")

for u in umfs:
    print(f"{u.table_name}  <-  {u.effective_source().path or '(generated)'}")

# COMMAND ----------

# MAGIC %md ## Enrich specs with an LLM (optional)
# MAGIC Set `LLM_ENDPOINT` to add descriptions, sample values, expectations, and FK
# MAGIC relationships. Fill-only — your edits are never overwritten.

# COMMAND ----------

if LLM_ENDPOINT and not SPEC_DIR:
    from tablespec.llm import enrich_specs

    print(enrich_specs(f"{OUT_DIR}/specs", model=LLM_ENDPOINT))
    umfs = umfs_from_spec_dir(f"{OUT_DIR}/specs", data_dir=CSV_DIR)

# COMMAND ----------

# MAGIC %md ## Emit the dbt project

# COMMAND ----------

from tablespec.dbt import DbtRunner

runner = DbtRunner()
project = runner.emit(umfs, f"{OUT_DIR}/dbt")
print(f"{project.project_name} -> {project.project_dir}")

# COMMAND ----------

# MAGIC %md ## dbt build

# COMMAND ----------

result = runner.build(project)
print(result.stdout)
assert result.success, result.stderr

# COMMAND ----------

# MAGIC %md ## Validation report
# MAGIC The build already ran the spec's validations (contracts + tests).

# COMMAND ----------

from tablespec.validation import write_validation_report

report = result.validation_report()
json_path, html_path = write_validation_report(report, f"{OUT_DIR}/reports")
print(f"{report.summary()}\n{json_path}\n{html_path}")
displayHTML(html_path.read_text())

# COMMAND ----------

# MAGIC %md ## Report files
# MAGIC Specs with `metadata.output_config` export their gold tables as CSV + Excel.

# COMMAND ----------

from tablespec.reporting import write_report_from_spark

for u in umfs:
    if u.metadata and u.metadata.output_config:
        gold = f"`{CATALOG}`.`{SCHEMA}`.`gold_{u.table_name}`"
        paths = write_report_from_spark(spark, gold, u, f"{OUT_DIR}/reports")
        print(f"{paths.csv_path}\n{paths.xlsx_path}")
        display(spark.table(gold))
