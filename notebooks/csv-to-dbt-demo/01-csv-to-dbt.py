# Databricks notebook source
# MAGIC %md
# MAGIC # CSVs / specs → UMFs → dbt build, end to end on Databricks
# MAGIC
# MAGIC The seamless tablespec → dbt story, one tablespec call per step:
# MAGIC
# MAGIC 1. **Install** tablespec from this workspace repo + the dbt stack (one pip cell).
# MAGIC 2. **Generate UMF specs** from CSVs in a Unity Catalog volume — or load
# MAGIC    authored specs (UMF YAML split dirs, flat YAML/JSON, or Excel workbooks).
# MAGIC 3. **Validate** the specs with the `tablespec validate` CLI.
# MAGIC 4. **Emit** a dbt project with `DbtRunner.emit` — on a Databricks runtime the
# MAGIC    defaults auto-resolve to `dialect="databricks"` and the runnable
# MAGIC    `databricks_notebook` profile (dbt-spark `method: session`, attached to
# MAGIC    this notebook's SparkSession — no host/token/http_path).
# MAGIC 5. **Run** `dbt build` in-process. Because each UMF declares
# MAGIC    `source: {kind: delimited, path: ...}`, the emitted `raw_<t>` models read
# MAGIC    the CSVs **directly** (`read_files(...)`) — dbt itself lands the all-STRING
# MAGIC    raw tables and builds the typed tables on top. Nothing is pre-loaded.
# MAGIC 6. **Verify**: query the raw + typed tables dbt created.
# MAGIC
# MAGIC **Input modes** (widget-driven)
# MAGIC - CSV mode (default): every `*.csv` in `csv_dir` (empty = the volume root)
# MAGIC   becomes one table; derived specs are persisted for later editing.
# MAGIC - Spec mode: set `spec_dir` to a directory of specs (split `table.yaml` dirs,
# MAGIC   flat `*.yaml`/`*.json`, or `*.xlsx` schema workbooks). Each spec must carry
# MAGIC   `source: {kind: delimited, path: ...}`; relative paths resolve against
# MAGIC   `csv_dir`. Non-empty `spec_dir` wins over CSV mode.
# MAGIC
# MAGIC **Widgets**
# MAGIC - `repo_path` — workspace path of the tablespec repo (empty = derived from
# MAGIC   this notebook's own location).
# MAGIC - `catalog` — Unity Catalog catalog (must already exist; default `main`).
# MAGIC - `schema` — schema where dbt materializes every table (default
# MAGIC   `tablespec_dbt_demo`; created if missing).
# MAGIC - `volume` — UC volume holding the input CSVs (default `raw`).
# MAGIC - `csv_dir` — directory of CSVs (empty = volume root).
# MAGIC - `spec_dir` — directory of specs; non-empty switches to spec mode.

# COMMAND ----------

dbutils.widgets.text("repo_path", "", "tablespec repo path (empty = auto-derive)")
dbutils.widgets.text("catalog", "main", "Unity Catalog catalog (must exist)")
dbutils.widgets.text("schema", "tablespec_dbt_demo", "Target schema for dbt tables")
dbutils.widgets.text("volume", "raw", "UC volume with input CSVs")
dbutils.widgets.text("csv_dir", "", "CSV directory (empty = volume root)")
dbutils.widgets.text("spec_dir", "", "Spec directory (non-empty = spec mode)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1 · Install tablespec (from this repo) + the dbt stack
# MAGIC
# MAGIC Plain `dbt-spark`, deliberately NOT `dbt-spark[session]`: the `[session]`
# MAGIC extra only adds a `pyspark` pin, and pip-installing pyspark on Databricks
# MAGIC shadows the runtime's bundled Spark. The session method needs nothing beyond
# MAGIC the runtime's own pyspark.

# COMMAND ----------

from pathlib import Path

_repo = dbutils.widgets.get("repo_path").strip()
if not _repo:
    # This notebook lives at <repo>/notebooks/csv-to-dbt-demo/, so the repo root
    # is two directories up from the notebook's own workspace path.
    _nb_path = (
        dbutils.notebook.entry_point.getDbutils()
        .notebook()
        .getContext()
        .notebookPath()
        .get()
    )
    _repo = str((Path("/Workspace") / Path(_nb_path).relative_to("/")).parents[2])
print(f"installing tablespec from {_repo}")

# COMMAND ----------

# MAGIC %pip install --quiet -e {_repo} dbt-core dbt-spark

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2 · Configuration
# MAGIC
# MAGIC The emitted profile resolves its target schema from `DBT_SPARK_SCHEMA` at
# MAGIC run time, so setting the env var here is the only dbt configuration needed.

# COMMAND ----------

import json
import os
from pathlib import Path

CATALOG = dbutils.widgets.get("catalog").strip() or "main"
SCHEMA = dbutils.widgets.get("schema").strip() or "tablespec_dbt_demo"
VOLUME = dbutils.widgets.get("volume").strip() or "raw"

VOLUME_BASE = Path(f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}")
CSV_DIR = Path(dbutils.widgets.get("csv_dir").strip().rstrip("/") or VOLUME_BASE)
SPEC_DIR_RAW = dbutils.widgets.get("spec_dir").strip().rstrip("/")
SPEC_DIR = Path(SPEC_DIR_RAW) if SPEC_DIR_RAW else None
MODE = "spec" if SPEC_DIR else "csv"

OUT_DIR = VOLUME_BASE / "tablespec_out"
SPECS_OUT = OUT_DIR / "specs"
DBT_DIR = Path("/tmp/tablespec_dbt")  # driver-local: dbt target/ churn is FUSE-unfriendly

# The emitted databricks_notebook profile reads the target schema from this env
# var when dbt runs (in-process, so the env var set here is visible to it).
os.environ["DBT_SPARK_SCHEMA"] = SCHEMA

try:
    spark.sql(f"DESCRIBE CATALOG `{CATALOG}`")
except Exception as e:
    raise RuntimeError(
        f"Catalog {CATALOG!r} is not accessible — set the 'catalog' widget to an "
        "existing Unity Catalog catalog (this notebook never creates catalogs)."
    ) from e
spark.sql(f"USE CATALOG `{CATALOG}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{SCHEMA}`")
spark.sql(f"CREATE VOLUME IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`.`{VOLUME}`")

print(f"mode        : {MODE}")
print(f"catalog     : {CATALOG}")
print(f"dbt schema  : {SCHEMA}  (DBT_SPARK_SCHEMA)")
print(f"csv dir     : {CSV_DIR}")
print(f"spec dir    : {SPEC_DIR or '(csv mode)'}")
print(f"outputs     : {OUT_DIR}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3a · CSV mode — derive one UMF spec per CSV
# MAGIC
# MAGIC Each CSV is read once here (all-STRING, via the ingestion reader seam) only
# MAGIC to derive the spec's column list and show a preview. **The data is not
# MAGIC loaded anywhere** — the emitted dbt project reads the files itself at build
# MAGIC time. Every spec carries `source: {kind: delimited, path: <the csv>}`, which
# MAGIC is what makes the emitter render file-reading `raw_<t>` models.

# COMMAND ----------

import re

umfs = []
if MODE == "csv":
    from tablespec.ingestion import get_reader
    from tablespec.models.umf import UMF, DelimitedSource
    from tablespec.profiling.spark_mapper import SparkToUmfMapper

    csv_files = sorted(CSV_DIR.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(
            f"No *.csv files in {CSV_DIR} — upload CSVs to the volume or point "
            "the 'csv_dir' widget at a directory that contains them."
        )

    mapper = SparkToUmfMapper()
    seen = set()
    for csv_path in csv_files:
        table = re.sub(r"[^0-9a-zA-Z_]+", "_", csv_path.stem).strip("_").lower()
        if table and table[0].isdigit():
            table = f"t_{table}"
        assert table and table not in seen, f"bad/duplicate table name from {csv_path.name}"
        seen.add(table)

        source = DelimitedSource(
            kind="delimited",
            delimiter=",",
            header=True,
            quote_char='"',
            encoding="UTF-8",
            path=str(csv_path),
        )
        raw_df = get_reader(source).read(source, spark)

        umf_dict = mapper.map_dataframe_to_umf(raw_df, table, table_type="inferred")
        umf_dict["version"] = "1.0"
        umf_dict["source"] = source.model_dump(exclude_none=True)
        for col in umf_dict["columns"]:
            if isinstance(col.get("nullable"), bool):
                col["nullable"] = {"default": col["nullable"]}
        umfs.append(UMF.model_validate(umf_dict))
        print(f"{csv_path.name:<40} -> {table} ({len(raw_df.columns)} columns)")

    display(raw_df.limit(5))  # preview of the last CSV
else:
    print("skipped — spec mode")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3b · CSV mode — persist the derived specs + `tablespec validate`
# MAGIC
# MAGIC The specs are the editable contract: enrich them (narrow types, primary
# MAGIC keys, descriptions, enums), then re-run this notebook with `spec_dir`
# MAGIC pointed here to build the enriched pipeline.

# COMMAND ----------

import shutil
import subprocess
import sys

if MODE == "csv":
    from tablespec.umf_loader import UMFLoader

    if SPECS_OUT.exists():
        shutil.rmtree(SPECS_OUT)  # clear stale specs from removed/renamed CSVs
    loader = UMFLoader()
    for umf in umfs:
        loader.save(umf, SPECS_OUT / umf.table_name)

    cli = Path(sys.executable).parent / "tablespec"
    proc = subprocess.run(
        [str(cli), "validate", str(SPECS_OUT)], capture_output=True, text=True
    )
    print(proc.stdout or proc.stderr)
    assert proc.returncode == 0, f"tablespec validate failed:\n{proc.stderr}"
    print(f"specs persisted + validated: {SPECS_OUT}")
else:
    print("skipped — spec mode")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3c · Spec mode — load authored specs (YAML, JSON, or Excel)
# MAGIC
# MAGIC Every spec must declare `source: {kind: delimited, path: ...}` so dbt can
# MAGIC read its file; relative paths resolve against `csv_dir`.

# COMMAND ----------

if MODE == "spec":
    from tablespec.e2e import umfs_from_specs
    from tablespec.excel_converter import ExcelToUMFConverter
    from tablespec.models.umf import DelimitedSource
    from tablespec.umf_loader import UMFLoader

    loader = UMFLoader()
    split_dirs = sorted({p.parent for p in SPEC_DIR.rglob("table.yaml")})
    if split_dirs:
        umfs = [loader.load(d) for d in split_dirs]
    else:
        umfs = umfs_from_specs(
            sorted([*SPEC_DIR.glob("*.yaml"), *SPEC_DIR.glob("*.yml")])
        )
        umfs += [loader.load(p) for p in sorted(SPEC_DIR.glob("*.json"))]
        umfs += [ExcelToUMFConverter().convert(p)[0] for p in sorted(SPEC_DIR.glob("*.xlsx"))]
    if not umfs:
        raise FileNotFoundError(
            f"No specs in {SPEC_DIR} (looked for split table.yaml dirs, "
            "*.yaml/*.yml, *.json, *.xlsx)."
        )

    resolved = []
    for umf in umfs:
        src = umf.effective_source()
        if not (isinstance(src, DelimitedSource) and src.path and src.path.strip()):
            raise ValueError(
                f"Spec {umf.table_name!r} has no delimited source path — add\n"
                "  source: {kind: delimited, path: /Volumes/.../file.csv}\n"
                "so the emitted dbt project can read its file."
            )
        path = Path(src.path)
        if not path.is_absolute():
            path = CSV_DIR / path  # relative paths resolve against csv_dir
        resolved.append(
            umf.model_copy(update={"source": src.model_copy(update={"path": str(path)})})
        )
    umfs = resolved
    for umf in umfs:
        print(f"{umf.table_name:<32} <- {umf.effective_source().path}")
else:
    print("skipped — csv mode")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4 · Emit the dbt project — one call
# MAGIC
# MAGIC On this runtime the defaults resolve to `dialect="databricks"` +
# MAGIC `target="databricks_notebook"`. Because every UMF is file-backed, the
# MAGIC project has **no `sources.yml` at all**: the `raw_<t>` landing tables are
# MAGIC dbt models that `read_files(...)` the CSVs directly.

# COMMAND ----------

from tablespec.dbt import DbtRunner

runner = DbtRunner()  # reuse this ONE instance for every dbt call in the notebook
project = runner.emit(umfs, DBT_DIR)

print(f"project '{project.project_name}' -> {project.project_dir}\n")
for rel in sorted(project.files):
    print(f"  {rel}")

raw_model = next(k for k in project.files if k.endswith(f"raw_{umfs[0].table_name}.sql"))
cast_model = (
    f"models/{umfs[0].table_name}.sql"
    if len(umfs) == 1
    else f"models/staging/ingested_{umfs[0].table_name}.sql"
)
print(f"\n=== profiles.yml ===\n{project.files['profiles.yml']}")
print(f"=== {raw_model} ===\n{project.files[raw_model]}")
print(f"=== {cast_model} ===\n{project.files[cast_model]}")

assert "method: session" in project.files["profiles.yml"]
assert "read_files" in project.files[raw_model]
assert "models/sources.yml" not in project.files

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5 · `dbt build` — dbt reads the CSVs and creates the tables
# MAGIC
# MAGIC Runs in-process, attached to this notebook's SparkSession. dbt materializes
# MAGIC `raw_<t>` (all-STRING, straight from the file) and the typed model(s) into
# MAGIC `<catalog>.<schema>`, and runs the generated contracts/tests.

# COMMAND ----------

result = runner.build(project)
print(result.stdout)
assert result.success, f"dbt build failed:\n{result.stderr}"
print("dbt build succeeded")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6 · Verify — the tables dbt created

# COMMAND ----------

display(spark.sql(f"SHOW TABLES IN `{CATALOG}`.`{SCHEMA}`"))

final_models = [
    u.table_name if len(umfs) == 1 else f"ingested_{u.table_name}" for u in umfs
]
for umf, model in zip(umfs, final_models):
    raw = f"`{CATALOG}`.`{SCHEMA}`.`raw_{umf.table_name}`"
    typed = f"`{CATALOG}`.`{SCHEMA}`.`{model}`"
    raw_count = spark.table(raw).count()
    typed_count = spark.table(typed).count()
    print(f"{umf.table_name}: raw={raw_count:,} rows, {model}={typed_count:,} rows")
    assert raw_count == typed_count > 0
    display(spark.table(typed).limit(5))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Scorecard

# COMMAND ----------

checks = {
    "UMF specs derived/loaded (one per input)": bool(umfs),
    "dbt project emitted with file-reading raw models (no sources.yml)": (
        "read_files" in project.files[raw_model]
        and "models/sources.yml" not in project.files
    ),
    "dbt build succeeded (dbt ingested the files itself)": result.success,
    "raw + typed tables materialized and queryable": all(
        spark.catalog.tableExists(f"{CATALOG}.{SCHEMA}.{m}") for m in final_models
    ),
}
for label, ok in checks.items():
    print(f"  {'✅' if ok else '❌'}  {label}")
assert all(checks.values())

dbutils.notebook.exit(
    json.dumps(
        {
            "status": "PASS",
            "mode": MODE,
            "tables": [u.table_name for u in umfs],
            "project": project.project_name,
            "models": final_models,
            "schema": f"{CATALOG}.{SCHEMA}",
        }
    )
)
