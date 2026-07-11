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

# Define the notebook's configuration widgets (all optional except catalog/schema defaults).
for _name, _default in [("repo_path", ""), ("catalog", "main"), ("schema", "tablespec_dbt_demo"), ("volume", "raw"), ("csv_dir", ""), ("spec_dir", ""), ("out_dir", ""), ("llm_endpoint", ""), ("demo", "")]:
    dbutils.widgets.text(_name, _default)

# COMMAND ----------

# Resolve the tablespec repo path (widget, or derived from this notebook's own location).
from pathlib import Path

_nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
REPO = dbutils.widgets.get("repo_path").strip() or str((Path("/Workspace") / Path(_nb).relative_to("/")).parents[2])

# COMMAND ----------

# MAGIC %pip install dbt-core dbt-spark openai tablespec -e /Workspace/Users/david.mautz@synaptiq.ai/tablespec-fork -q

# COMMAND ----------

# Restart Python so the freshly installed packages are importable.
dbutils.library.restartPython()

# COMMAND ----------

# Read the widgets, set the dbt target schema, and create the schema/volume if missing.
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

# Demo seed (demo=1): copy the month-1 sample CSVs to the volume and the sample specs (incl. the gold report spec) to out_dir/specs, first run only.
if dbutils.widgets.get("demo").strip() == "1":
    import shutil
    from pathlib import Path

    _demo = Path(REPO) / "notebooks" / "csv-to-dbt-demo"
    for _f in ("members.csv", "claims_202401.csv", "rx_202401.csv"):
        shutil.copy(_demo / "sample-data" / _f, f"{CSV_DIR}/{_f}")
    if not Path(f"{OUT_DIR}/specs").exists():
        shutil.copytree(_demo / "sample-specs", f"{OUT_DIR}/specs")
    SPEC_DIR = SPEC_DIR or f"{OUT_DIR}/specs"

# COMMAND ----------

# MAGIC %md ## UMF specs

# COMMAND ----------

# Derive one spec per CSV (or load authored specs) and persist editable copies to out_dir.
from tablespec.e2e import save_specs, umfs_from_csvs, umfs_from_spec_dir

umfs = umfs_from_spec_dir(SPEC_DIR, data_dir=CSV_DIR) if SPEC_DIR else umfs_from_csvs(CSV_DIR)
if not SPEC_DIR:
    save_specs(umfs, f"{OUT_DIR}/specs")
print("\n".join(f"{u.table_name}  <-  {u.effective_source().path}" for u in umfs))

# COMMAND ----------

# MAGIC %md ## Enrich specs with an LLM (optional)
# MAGIC Set the `llm_endpoint` widget to a serving-endpoint name (e.g.
# MAGIC `databricks-claude-sonnet-4`) to add descriptions, notes, sample values,
# MAGIC expectations, and FK relationships. Fill-only: never overwrites your edits.

# COMMAND ----------

# LLM-enrich the saved specs (descriptions, expectations, FKs) when llm_endpoint is set.
LLM_ENDPOINT = dbutils.widgets.get("llm_endpoint").strip()
if LLM_ENDPOINT and not SPEC_DIR:
    from tablespec.llm import enrich_specs

    print(enrich_specs(f"{OUT_DIR}/specs", model=LLM_ENDPOINT))
    umfs = umfs_from_spec_dir(f"{OUT_DIR}/specs", data_dir=CSV_DIR)

# COMMAND ----------

# MAGIC %md ## Emit the dbt project

# COMMAND ----------

# Emit the dbt project (file-reading raw models + typed cast models) into out_dir.
from tablespec.dbt import DbtRunner

runner = DbtRunner()
project = runner.emit(umfs, f"{OUT_DIR}/dbt")
print(f"{project.project_name} -> {project.project_dir}")

# COMMAND ----------

# MAGIC %md ## dbt build

# COMMAND ----------

# Run dbt build in-process: dbt reads the CSVs and creates raw + typed tables.
result = runner.build(project)
print(result.stdout)
assert result.success, result.stderr

# COMMAND ----------

# MAGIC %md ## Validation report
# MAGIC Every build already ran the spec's validations (contracts + tests);
# MAGIC this turns `target/run_results.json` into a viewable report.

# COMMAND ----------

# Turn the build's validation results into a viewable JSON + HTML report.
from tablespec.validation import write_validation_report

report = result.validation_report()
json_path, html_path = write_validation_report(report, f"{OUT_DIR}/reports")
print(f"{report.summary()}\n{json_path}\n{html_path}")
displayHTML(html_path.read_text())

# COMMAND ----------

# MAGIC %md ## Report files
# MAGIC Specs declaring `metadata.output_config` are physical reports: their gold
# MAGIC tables are exported as CSV (row-count footer, declared naming/terminator)
# MAGIC and Excel.

# COMMAND ----------

# Export CSV + Excel report files for every spec that declares output_config.
from tablespec.reporting import write_report_from_spark

for _u in umfs:
    if _u.metadata and _u.metadata.output_config:
        _paths = write_report_from_spark(spark, f"`{CATALOG}`.`{SCHEMA}`.`gold_{_u.table_name}`", _u, f"{OUT_DIR}/reports")
        print(f"{_paths.csv_path}\n{_paths.xlsx_path}")
        display(spark.table(f"`{CATALOG}`.`{SCHEMA}`.`gold_{_u.table_name}`"))
