# Databricks notebook source
# MAGIC %md
# MAGIC # CSVs / specs → UMFs → dbt build
# MAGIC
# MAGIC Derive UMF specs from CSVs in a UC volume (or load authored YAML/JSON/Excel
# MAGIC specs via `spec_dir`), emit a dbt project, and run `dbt build` in-notebook —
# MAGIC the emitted `raw_<t>` models `read_files(...)` the CSVs directly, so dbt
# MAGIC itself lands the raw tables and builds the typed tables in
# MAGIC `<catalog>.<schema>`. Specs and the dbt project are written to `out_dir`
# MAGIC (a workspace directory) for inspection and editing.

# COMMAND ----------

for _name, _default in [("repo_path", ""), ("catalog", "main"), ("schema", "tablespec_dbt_demo"), ("volume", "raw"), ("csv_dir", ""), ("spec_dir", ""), ("out_dir", "")]:
    dbutils.widgets.text(_name, _default)

# COMMAND ----------

from pathlib import Path

_nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
REPO = dbutils.widgets.get("repo_path").strip() or str((Path("/Workspace") / Path(_nb).relative_to("/")).parents[2])

# COMMAND ----------

# MAGIC %pip install dbt-core dbt-spark tablespec -e /Workspace/Users/david.mautz@synaptiq.ai/tablespec-fork -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

import os
import sys
sys.path.insert(0, "/Workspace/Users/david.mautz@synaptiq.ai/tablespec-fork/src")

CATALOG, SCHEMA, VOLUME, CSV_DIR, SPEC_DIR, OUT_DIR = (dbutils.widgets.get(w).strip().rstrip("/") for w in ("catalog", "schema", "volume", "csv_dir", "spec_dir", "out_dir"))
CSV_DIR = CSV_DIR or f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
OUT_DIR = OUT_DIR or f"/Workspace/Users/{spark.sql('SELECT current_user()').first()[0]}/tablespec_out"
os.environ["DBT_SPARK_SCHEMA"] = SCHEMA
spark.sql(f"USE CATALOG `{CATALOG}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{SCHEMA}`")
spark.sql(f"CREATE VOLUME IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`.`{VOLUME}`")

# COMMAND ----------

# MAGIC %md ## UMF specs

# COMMAND ----------

from tablespec.e2e import save_specs, umfs_from_csvs, umfs_from_spec_dir

umfs = umfs_from_spec_dir(SPEC_DIR, data_dir=CSV_DIR) if SPEC_DIR else umfs_from_csvs(CSV_DIR)
if not SPEC_DIR:
    save_specs(umfs, f"{OUT_DIR}/specs")
print("\n".join(f"{u.table_name}  <-  {u.effective_source().path}" for u in umfs))

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
