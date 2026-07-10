"""Environment-resolved dbt emit defaults (Databricks-notebook lane).

Covers ``tablespec.dialects.resolve_emit_defaults`` / ``is_databricks_runtime``,
the ``databricks_notebook`` profile rendering, the generator-level default
behavior on/off a Databricks runtime, and ``DbtRunner``'s in-process invocation
path. Pure Python -- no Spark, no dbt (the runner test fakes ``dbt.cli.main``).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from tablespec.dbt.profiles import render_profiles_yml
from tablespec.dbt.project import generate_dbt_dag_project
from tablespec.dbt.runner import DbtRunner
from tablespec.dbt.single_table import generate_dbt_project
from tablespec.dialects import is_databricks_runtime, resolve_emit_defaults
from tests.builders import UMFBuilder

pytestmark = [pytest.mark.no_spark]

_DBR_ENV = "DATABRICKS_RUNTIME_VERSION"


@pytest.fixture
def databricks_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_DBR_ENV, "15.4")


@pytest.fixture
def local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_DBR_ENV, raising=False)


def _umf():
    return (
        UMFBuilder("metrics")
        .column("metric_id", "INTEGER", nullable=False)
        .column("label", "VARCHAR", length=32)
        .primary_key("metric_id")
        .build()
    )


# ---------------------------------------------------------------------------
# is_databricks_runtime / resolve_emit_defaults
# ---------------------------------------------------------------------------


def test_is_databricks_runtime_strict_env_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_DBR_ENV, raising=False)
    # Deliberately NOT a SPARK_HOME heuristic -- only the runtime version var.
    monkeypatch.setenv("SPARK_HOME", "/databricks/spark")
    assert not is_databricks_runtime()
    monkeypatch.setenv(_DBR_ENV, "15.4")
    assert is_databricks_runtime()


@pytest.mark.usefixtures("local_env")
def test_resolve_defaults_local_matches_historical_behavior() -> None:
    assert resolve_emit_defaults(None, None) == ("duckdb", "duckdb")
    # target mirrors the dialect (the pre-existing rule).
    assert resolve_emit_defaults("spark", None) == ("spark", "spark")
    assert resolve_emit_defaults("databricks", None) == ("databricks", "databricks")
    assert resolve_emit_defaults("duckdb", None) == ("duckdb", "duckdb")


@pytest.mark.usefixtures("databricks_env")
def test_resolve_defaults_on_databricks_runtime() -> None:
    # Fully-unspecified: the runnable notebook lane.
    assert resolve_emit_defaults(None, None) == ("databricks", "databricks_notebook")
    # Spark-family dialects default onto the notebook session target too.
    assert resolve_emit_defaults("spark", None) == ("spark", "databricks_notebook")
    assert resolve_emit_defaults("databricks", None) == (
        "databricks",
        "databricks_notebook",
    )
    # duckdb dialect stays fully local even on a cluster.
    assert resolve_emit_defaults("duckdb", None) == ("duckdb", "duckdb")


@pytest.mark.usefixtures("databricks_env")
def test_resolve_defaults_explicit_values_always_win() -> None:
    assert resolve_emit_defaults("databricks", "databricks") == (
        "databricks",
        "databricks",
    )
    assert resolve_emit_defaults(None, "duckdb") == ("databricks", "duckdb")


@pytest.mark.usefixtures("local_env")
def test_resolve_defaults_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="Unsupported dialect: 'postgres'"):
        resolve_emit_defaults("postgres", None)
    with pytest.raises(ValueError, match="Unsupported profile target: 'postgres'"):
        resolve_emit_defaults(None, "postgres")


# ---------------------------------------------------------------------------
# databricks_notebook profile rendering
# ---------------------------------------------------------------------------


def test_render_databricks_notebook_profile_is_session_method() -> None:
    text = render_profiles_yml("tablespec_gold", target="databricks_notebook")
    assert "type: spark" in text
    assert "method: session" in text
    # No warehouse credentials -- the session method attaches to the active
    # SparkSession.
    assert "token" not in text
    assert "http_path" not in text
    # Identical adapter body to the local spark session target.
    assert text == render_profiles_yml("tablespec_gold", target="spark")


# ---------------------------------------------------------------------------
# Generator-level defaults
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("local_env")
def test_generators_default_to_duckdb_locally() -> None:
    dag_files = generate_dbt_dag_project([_umf()])
    single_files = generate_dbt_project(_umf().model_dump(exclude_none=True))
    assert dag_files == generate_dbt_dag_project([_umf()], dialect="duckdb")
    assert single_files == generate_dbt_project(
        _umf().model_dump(exclude_none=True), dialect="duckdb"
    )
    assert "type: duckdb" in dag_files["profiles.yml"]
    assert "type: duckdb" in single_files["profiles.yml"]


@pytest.mark.usefixtures("databricks_env")
def test_generators_default_to_notebook_lane_on_databricks() -> None:
    dag_files = generate_dbt_dag_project([_umf()])
    single_files = generate_dbt_project(_umf().model_dump(exclude_none=True))
    for files in (dag_files, single_files):
        assert "method: session" in files["profiles.yml"]
        assert "token" not in files["profiles.yml"]
    # The databricks cast dialect pins delta for incremental+pk models.
    assert "file_format='delta'" in dag_files["models/staging/ingested_metrics.sql"]
    assert "file_format='delta'" in single_files["models/metrics.sql"]


@pytest.mark.usefixtures("databricks_env")
def test_generator_explicit_args_untouched_on_databricks() -> None:
    files = generate_dbt_dag_project([_umf()], dialect="duckdb", target="duckdb")
    assert "type: duckdb" in files["profiles.yml"]


# ---------------------------------------------------------------------------
# DbtRunner in-process invocation
# ---------------------------------------------------------------------------


class _FakeDbtResult:
    success = True
    exception = None


def _install_fake_dbt(
    monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]
) -> None:
    """Insert a fake ``dbt.cli.main`` module exposing a recording dbtRunner."""

    class _FakeDbtRunner:
        def invoke(self, args: list[str]) -> _FakeDbtResult:
            calls.append(list(args))
            print("fake dbt ran")
            return _FakeDbtResult()

    main_mod = types.ModuleType("dbt.cli.main")
    main_mod.dbtRunner = _FakeDbtRunner  # type: ignore[attr-defined]
    cli_mod = types.ModuleType("dbt.cli")
    dbt_mod = types.ModuleType("dbt")
    monkeypatch.setitem(sys.modules, "dbt", dbt_mod)
    monkeypatch.setitem(sys.modules, "dbt.cli", cli_mod)
    monkeypatch.setitem(sys.modules, "dbt.cli.main", main_mod)
    # find_spec-based availability probing is bypassed; the fake module above is
    # what the lazy import resolves.
    monkeypatch.setattr(
        "tablespec.dbt.runner._require_dbt", lambda *, hint: None
    )


def test_runner_in_process_invokes_programmatic_dbt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv(_DBR_ENV, raising=False)
    calls: list[list[str]] = []
    _install_fake_dbt(monkeypatch, calls)

    runner = DbtRunner()
    project = runner.emit(
        _umf(), tmp_path, dialect="databricks", target="databricks_notebook"
    )
    result = runner.build(project, in_process=True)

    assert result.success
    assert result.returncode == 0
    assert "fake dbt ran" in result.stdout
    (args,) = calls
    assert args[0] == "--no-use-colors"
    assert "build" in args
    assert "--profiles-dir" in args


def test_runner_in_process_is_default_on_databricks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv(_DBR_ENV, "15.4")
    calls: list[list[str]] = []
    _install_fake_dbt(monkeypatch, calls)

    runner = DbtRunner()
    project = runner.emit(_umf(), tmp_path)  # defaults resolve to the notebook lane
    assert "method: session" in project.files["profiles.yml"]

    result = runner.build(project)  # in_process=None -> in-process on DBR
    assert result.success
    assert len(calls) == 1


def test_runner_subprocess_path_pins_duckdb_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv(_DBR_ENV, raising=False)
    seen_envs: list[dict[str, str]] = []

    def fake_run(cmd, *, env, capture_output, text, check):  # noqa: ANN001, ANN202
        seen_envs.append(dict(env))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tablespec.dbt.runner.subprocess.run", fake_run)
    monkeypatch.setattr(
        "tablespec.dbt.runner._dbt_argv", lambda: [sys.executable, "-m", "dbt"]
    )

    runner = DbtRunner()
    duckdb_project = runner.emit(_umf(), tmp_path / "duck")
    session_project = runner.emit(
        _umf(), tmp_path / "session", dialect="spark", target="databricks_notebook"
    )
    runner.build(duckdb_project, in_process=False)
    runner.build(session_project, in_process=False)

    duck_env, session_env = seen_envs
    assert "DBT_DUCKDB_PATH" in duck_env
    assert "DBT_DUCKDB_PATH" not in session_env
