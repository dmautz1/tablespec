"""Golden file tests for schema generators.

Each test case is a pair of files in tests/golden/<generator>/:
  - <name>.input.yaml  — UMF input data
  - <name>.expected.*   — expected generator output

The test runner discovers all input files, runs the corresponding generator,
and compares the output against the expected file.
"""

import json
from pathlib import Path

import pytest
import yaml

from tablespec.schemas.dbt_generator import generate_dbt_project
from tablespec.schemas.generators import (
    generate_json_schema,
    generate_pyspark_schema,
    generate_sql_ddl,
)
from tablespec.schemas.ingest_generator import generate_ingest_sql

# Golden file tests are pure-Python text comparisons -- no Spark/JVM.
pytestmark = [pytest.mark.no_spark]

GOLDEN_DIR = Path(__file__).parent / "golden"


@pytest.fixture(autouse=True)
def _force_local_emit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin emit defaults to the local (duckdb) lane.

    Unspecified dialect/target resolve by environment
    (``tablespec.dialects.resolve_emit_defaults``); golden text must stay
    deterministic even when the suite runs on a Databricks cluster.
    """
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)


def _discover_cases(subdir: str, expected_ext: str) -> list[tuple[str, Path, Path]]:
    """Discover golden test cases in a subdirectory.

    Returns list of (test_name, input_path, expected_path) tuples.
    """
    case_dir = GOLDEN_DIR / subdir
    if not case_dir.exists():
        return []

    cases = []
    for input_file in sorted(case_dir.glob("*.input.yaml")):
        name = input_file.name.replace(".input.yaml", "")
        expected_file = case_dir / f"{name}.expected.{expected_ext}"
        if expected_file.exists():
            cases.append((name, input_file, expected_file))
    return cases


# --- SQL DDL golden tests ---

sql_ddl_cases = _discover_cases("sql_ddl", "sql")


@pytest.mark.parametrize(
    "name,input_path,expected_path",
    sql_ddl_cases,
    ids=[c[0] for c in sql_ddl_cases],
)
def test_sql_ddl_golden(name: str, input_path: Path, expected_path: Path) -> None:
    """Verify SQL DDL output matches golden file."""
    umf_data = yaml.safe_load(input_path.read_text())
    actual = generate_sql_ddl(umf_data)
    expected = expected_path.read_text().rstrip("\n")
    assert actual == expected, (
        f"SQL DDL golden mismatch for '{name}'.\n"
        f"--- expected ---\n{expected}\n"
        f"--- actual ---\n{actual}"
    )


# --- Ingest SQL golden tests ---

ingest_sql_cases = _discover_cases("ingest_sql", "sql")


@pytest.mark.parametrize(
    "name,input_path,expected_path",
    ingest_sql_cases,
    ids=[c[0] for c in ingest_sql_cases],
)
def test_ingest_sql_golden(name: str, input_path: Path, expected_path: Path) -> None:
    """Verify raw->ingest SQL artifact matches golden file."""
    umf_data = yaml.safe_load(input_path.read_text())
    actual = generate_ingest_sql(umf_data)
    expected = expected_path.read_text().rstrip("\n")
    assert actual == expected, (
        f"Ingest SQL golden mismatch for '{name}'.\n"
        f"--- expected ---\n{expected}\n"
        f"--- actual ---\n{actual}"
    )


# --- dbt project golden tests ---
#
# Each case is a single ``<name>.input.yaml`` under tests/golden/dbt_project/.
# The expected project files live beside it under ``<name>/<relative_path>`` (the
# same relative paths generate_dbt_project returns, e.g. ``models/<table>.sql``).
# The test asserts the generated file set and every file's bytes match.

DBT_PROJECT_DIR = GOLDEN_DIR / "dbt_project"


def _discover_dbt_cases() -> list[tuple[str, Path, Path]]:
    if not DBT_PROJECT_DIR.exists():
        return []
    cases = []
    for input_file in sorted(DBT_PROJECT_DIR.glob("*.input.yaml")):
        name = input_file.name.replace(".input.yaml", "")
        expected_dir = DBT_PROJECT_DIR / name
        if expected_dir.is_dir():
            cases.append((name, input_file, expected_dir))
    return cases


dbt_project_cases = _discover_dbt_cases()


@pytest.mark.parametrize(
    "name,input_path,expected_dir",
    dbt_project_cases,
    ids=[c[0] for c in dbt_project_cases],
)
def test_dbt_project_golden(name: str, input_path: Path, expected_dir: Path) -> None:
    """Verify generated dbt(+duckdb) project files match the golden files."""
    umf_data = yaml.safe_load(input_path.read_text())
    actual_files = generate_dbt_project(umf_data, dialect="duckdb")

    expected_files = {
        str(p.relative_to(expected_dir)): p.read_text()
        for p in expected_dir.rglob("*")
        if p.is_file()
    }

    assert set(actual_files) == set(expected_files), (
        f"dbt project file set mismatch for '{name}'.\n"
        f"  generated: {sorted(actual_files)}\n"
        f"  expected:  {sorted(expected_files)}"
    )
    for rel, content in actual_files.items():
        assert content == expected_files[rel], (
            f"dbt project golden mismatch for '{name}' file '{rel}'.\n"
            f"--- expected ---\n{expected_files[rel]}\n--- actual ---\n{content}"
        )


# --- PySpark schema golden tests ---

pyspark_cases = _discover_cases("pyspark_schema", "py")


@pytest.mark.parametrize(
    "name,input_path,expected_path",
    pyspark_cases,
    ids=[c[0] for c in pyspark_cases],
)
def test_pyspark_schema_golden(
    name: str, input_path: Path, expected_path: Path
) -> None:
    """Verify PySpark schema output matches golden file."""
    umf_data = yaml.safe_load(input_path.read_text())
    actual = generate_pyspark_schema(umf_data)
    expected = expected_path.read_text().rstrip("\n")
    assert actual == expected, (
        f"PySpark schema golden mismatch for '{name}'.\n"
        f"--- expected ---\n{expected}\n"
        f"--- actual ---\n{actual}"
    )


# --- JSON Schema golden tests ---

json_schema_cases = _discover_cases("json_schema", "json")


@pytest.mark.parametrize(
    "name,input_path,expected_path",
    json_schema_cases,
    ids=[c[0] for c in json_schema_cases],
)
def test_json_schema_golden(name: str, input_path: Path, expected_path: Path) -> None:
    """Verify JSON Schema output matches golden file."""
    umf_data = yaml.safe_load(input_path.read_text())
    actual = generate_json_schema(umf_data)
    expected = json.loads(expected_path.read_text())
    assert actual == expected, (
        f"JSON Schema golden mismatch for '{name}'.\n"
        f"--- expected ---\n{json.dumps(expected, indent=2)}\n"
        f"--- actual ---\n{json.dumps(actual, indent=2)}"
    )
