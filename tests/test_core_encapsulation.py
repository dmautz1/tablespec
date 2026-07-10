"""Prove the dbt path is encapsulated: CORE never imports ``tablespec.dbt``.

The check is STATIC (AST over the core source files), not a runtime side-effect
check: importing any ``tablespec.X`` submodule executes the top-level
``tablespec/__init__.py`` (which is allowed to wire up the public API, including
dbt), so a runtime ``sys.modules`` probe can't distinguish a core->dbt dependency
from the package facade. The architectural invariant we actually care about is
that no *core module's own import statements* reference ``tablespec.dbt``.

CORE = the dbt-free seam every backend depends on:
  * ``tablespec.core`` (TableRenderer Protocol + logical-plan IR)
  * the cast/dialect layer (``casting_utils``, ``date_formats``)
  * the shared ingest seam (``schemas.ingest_generator.build_ingest_select``,
    ``schemas.generators``, ``schemas.relationship_resolver``)
  * the gold SQL generator (``schemas.sql_generator``)
  * the direct artifact emitter (``generate_ingest_sql``)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.no_spark, pytest.mark.fast]

SRC = Path(__file__).parent.parent / "src" / "tablespec"

CORE_MODULES = [
    SRC / "core" / "__init__.py",
    SRC / "core" / "ir.py",
    SRC / "core" / "registry.py",
    SRC / "core" / "relations.py",
    SRC / "core" / "schema_facts.py",
    SRC / "core" / "selection.py",
    SRC / "casting_utils.py",
    SRC / "date_formats.py",
    SRC / "schemas" / "__init__.py",
    SRC / "schemas" / "generators.py",
    SRC / "schemas" / "ingest_generator.py",
    SRC / "schemas" / "relationship_resolver.py",
    SRC / "schemas" / "sql_generator.py",
]

# Every module under the LDP implementation package (the PROTOTYPE sibling emitter).
LDP_MODULES = [
    SRC / "ldp" / "__init__.py",
    SRC / "ldp" / "expectations.py",
    SRC / "ldp" / "project.py",
    SRC / "ldp" / "renderer.py",
]

# Every module under the dbt implementation package.
DBT_MODULES = [
    SRC / "dbt" / "__init__.py",
    SRC / "dbt" / "contracts.py",
    SRC / "dbt" / "materialization.py",
    SRC / "dbt" / "project.py",
    SRC / "dbt" / "raw_models.py",
    SRC / "dbt" / "registry.py",
    SRC / "dbt" / "renderer.py",
    SRC / "dbt" / "routing.py",
    SRC / "dbt" / "schema_tests.py",
    SRC / "dbt" / "seeds.py",
    SRC / "dbt" / "selection.py",
    SRC / "dbt" / "single_table.py",
]


def _imported_modules(path: Path) -> set[str]:
    """Return the dotted module targets of every import statement in *path*."""
    tree = ast.parse(path.read_text(), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Absolute ``from tablespec.x import y`` -> module = node.module.
            # Relative ``from .x import y`` (level>0) stays inside the package and
            # cannot reach tablespec.dbt, so it is irrelevant here.
            if node.level == 0 and node.module:
                targets.add(node.module)
    return targets


def test_core_modules_do_not_import_dbt() -> None:
    offenders: dict[str, set[str]] = {}
    for path in CORE_MODULES:
        assert path.exists(), f"core module missing: {path}"
        bad = {m for m in _imported_modules(path) if m.startswith("tablespec.dbt")}
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    assert not offenders, (
        "CORE modules must not import tablespec.dbt (encapsulation breach):\n"
        + "\n".join(f"  {mod}: {sorted(imps)}" for mod, imps in offenders.items())
    )


def test_src_never_imports_external_dbt() -> None:
    """No src module may import the EXTERNAL ``dbt`` package (dbt-core).

    tablespec generates dbt projects as pure text; dbt-core is a TEST-ONLY
    dependency (it lives in the dev group, not a user extra). If any source
    module imported ``dbt`` / ``dbt.*`` -- even lazily inside a function -- the
    dbt emitter would break on a base ``pip install tablespec`` without the
    dev/test stack. (``tablespec.dbt``, the internal emitter package, is fine;
    only the external top-level ``dbt`` is forbidden.)
    """
    offenders: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        bad = {m for m in _imported_modules(path) if m == "dbt" or m.startswith("dbt.")}
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    assert not offenders, (
        "src/tablespec must NEVER import the external 'dbt' package (dbt is "
        "test-only; importing it would break dbt-project generation on a base "
        "install):\n"
        + "\n".join(f"  {mod}: {sorted(imps)}" for mod, imps in offenders.items())
    )


def test_src_never_imports_tests_package() -> None:
    """No src module may import the top-level ``tests`` package.

    A wheel ships ``src/tablespec`` but NOT the ``tests/`` tree, so any source
    module importing ``tests`` / ``tests.*`` -- even lazily inside a function --
    breaks ``import tablespec.<x>`` on a clean wheel install with
    ``ModuleNotFoundError: No module named 'tests'`` (caught running the e2e
    backbone on real Databricks serverless). Shared helpers must live IN the
    package and be re-exported FROM the test tree, never the reverse.
    """
    offenders: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        bad = {
            m for m in _imported_modules(path) if m == "tests" or m.startswith("tests.")
        }
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    assert not offenders, (
        "src/tablespec must NEVER import the top-level 'tests' package (a wheel "
        "ships no tests/ -- importing it breaks every consumer on a clean install). "
        "Move the shared helper INTO the package and re-export it from the test "
        "tree:\n"
        + "\n".join(f"  {mod}: {sorted(imps)}" for mod, imps in offenders.items())
    )


def test_core_modules_do_not_import_ldp() -> None:
    """The CORE seam must not import the PROTOTYPE LDP package either.

    LDP is a backend (a sibling of dbt) fed BY the core; nothing in core may depend
    on it, exactly as for dbt. ``core.registry`` (the IR builder both backends
    consume) is the shared seam -- it must not reach back into either emitter.
    """
    offenders: dict[str, set[str]] = {}
    for path in CORE_MODULES:
        assert path.exists(), f"core module missing: {path}"
        bad = {m for m in _imported_modules(path) if m.startswith("tablespec.ldp")}
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    assert not offenders, (
        "CORE modules must not import tablespec.ldp (encapsulation breach):\n"
        + "\n".join(f"  {mod}: {sorted(imps)}" for mod, imps in offenders.items())
    )


def test_dbt_and_ldp_never_import_each_other() -> None:
    """The two backends are independent siblings on the shared core.

    No module under ``tablespec.dbt`` may import ``tablespec.ldp`` and no module
    under ``tablespec.ldp`` may import ``tablespec.dbt``. Both depend only on the
    core seam (``tablespec.core`` + the shared cast/ingest/sql generators).
    """
    offenders: dict[str, set[str]] = {}
    for path in DBT_MODULES:
        assert path.exists(), f"dbt module missing: {path}"
        bad = {m for m in _imported_modules(path) if m.startswith("tablespec.ldp")}
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    for path in LDP_MODULES:
        assert path.exists(), f"ldp module missing: {path}"
        bad = {m for m in _imported_modules(path) if m.startswith("tablespec.dbt")}
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    assert not offenders, (
        "tablespec.dbt and tablespec.ldp must never import each other "
        "(sibling-independence breach):\n"
        + "\n".join(f"  {mod}: {sorted(imps)}" for mod, imps in offenders.items())
    )


def test_ldp_feeds_only_on_core_seam() -> None:
    """``tablespec.ldp`` imports only the core seam + UMF model + shared generators.

    It must reach the logical-plan registry / IR / cast layer / SQL generator
    through ``tablespec.core`` and ``tablespec.schemas`` -- never through
    ``tablespec.dbt``. (Guards the prototype from accidentally coupling to the dbt
    emitter when both could resolve the same name.)
    """
    allowed_prefixes = (
        "tablespec.core",
        "tablespec.schemas",
        "tablespec.models",
        "tablespec.ldp",
    )
    offenders: dict[str, set[str]] = {}
    for path in LDP_MODULES:
        imported = {m for m in _imported_modules(path) if m.startswith("tablespec.")}
        bad = {m for m in imported if not m.startswith(allowed_prefixes)}
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    assert not offenders, (
        "tablespec.ldp must be fed only by the core seam (tablespec.core / "
        "tablespec.schemas / tablespec.models):\n"
        + "\n".join(f"  {mod}: {sorted(imps)}" for mod, imps in offenders.items())
    )


def test_ldp_package_imports_no_databricks_runtime() -> None:
    """Generating an LDP project needs no Databricks/Spark runtime import.

    The emitter is pure text generation; it must not import ``pyspark`` /
    ``databricks`` (LDP runs only on Databricks, but GENERATING the project must
    work in any environment).
    """
    offenders: dict[str, set[str]] = {}
    for path in LDP_MODULES:
        imported = _imported_modules(path)
        bad = {
            m
            for m in imported
            if m == "pyspark"
            or m.startswith("pyspark.")
            or m == "databricks"
            or m.startswith("databricks.")
        }
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    assert not offenders, (
        "tablespec.ldp must not import a Databricks/Spark runtime to GENERATE SQL:\n"
        + "\n".join(f"  {mod}: {sorted(imps)}" for mod, imps in offenders.items())
    )


def test_dbt_package_depends_only_on_core_seam() -> None:
    """The dbt package may import core, but the direct-artifact emitter must not
    import the dbt package (the two backends never depend on each other)."""
    ingest = SRC / "schemas" / "ingest_generator.py"
    bad = {m for m in _imported_modules(ingest) if m.startswith("tablespec.dbt")}
    assert not bad, f"direct-artifact emitter imports dbt: {sorted(bad)}"


def test_selection_core_has_no_dbt() -> None:
    """AC3.5: ``core.selection`` knows nothing of dbt / ``state:modified``.

    The changed-table derivation is engine-agnostic: it must neither import any
    ``tablespec.dbt`` module nor mention the dbt-native ``state:modified`` selector
    literal (that mapping lives ONLY in ``tablespec.dbt.selection``).
    """
    path = SRC / "core" / "selection.py"
    assert path.exists(), f"core selection module missing: {path}"
    bad = {m for m in _imported_modules(path) if m.startswith("tablespec.dbt")}
    assert not bad, f"core.selection imports dbt (encapsulation breach): {sorted(bad)}"
    source = path.read_text()
    assert "state:modified" not in source, (
        "core.selection must not mention the dbt-native 'state:modified' selector; "
        "that engine-specific mapping belongs in tablespec.dbt.selection."
    )


def test_dbt_selection_imports_no_dbt_package() -> None:
    """The dbt selection mapper is pure text emission -- no ``dbt`` package import.

    Generating a CI selection expression must work with the ``[dbt]`` extra
    UNINSTALLED, so ``tablespec.dbt.selection`` may import core + the registry but
    must not import the ``dbt`` (dbt-core) package itself.
    """
    path = SRC / "dbt" / "selection.py"
    assert path.exists(), f"dbt selection module missing: {path}"
    imported = _imported_modules(path)
    bad = {m for m in imported if m == "dbt" or m.startswith("dbt.")}
    assert not bad, f"dbt.selection imports the dbt-core package: {sorted(bad)}"


def test_dbt_seeds_imports_no_dbt_core_package() -> None:
    """The seed emitter is pure text emission -- no ``dbt`` (dbt-core) package import.

    Emitting ``seeds/<t>.csv`` + the ``column_types`` config must work with the
    ``[dbt]`` extra UNINSTALLED, so ``tablespec.dbt.seeds`` may import core
    (``schema_facts``) + the sibling contracts text but must not import the
    ``dbt`` (dbt-core) package itself.
    """
    path = SRC / "dbt" / "seeds.py"
    assert path.exists(), f"dbt seeds module missing: {path}"
    imported = _imported_modules(path)
    bad = {m for m in imported if m == "dbt" or m.startswith("dbt.")}
    assert not bad, f"dbt.seeds imports the dbt-core package: {sorted(bad)}"


def test_dbt_seeds_does_not_import_sample_data_engine() -> None:
    """The seed emitter CONSUMES the generator's on-disk output -- it does not
    import or re-run the generator. It must not depend on the heavy
    ``sample_data.engine`` module (only on the public UMF model + core facts)."""
    path = SRC / "dbt" / "seeds.py"
    imported = _imported_modules(path)
    bad = {m for m in imported if m.startswith("tablespec.sample_data")}
    assert not bad, (
        "dbt.seeds must consume generated output, not import the generator: "
        f"{sorted(bad)}"
    )


def test_no_direct_database_driver_imports_anywhere() -> None:
    """DISC-01 / ADR-015: tablespec NEVER imports a database driver.

    All database connectivity -- reads AND discovery metadata queries -- rides
    Spark's JDBC connector (``spark.read.format("jdbc")``). A direct driver
    import (pyodbc / pymssql / JayDeBeApi) anywhere in ``src/tablespec`` would
    create a second connectivity seam and pull credential handling into the
    compile-time tool; the FEAT-031 operator decision also forbids them in
    the test tree (the SQL Server integration lane loads fixtures via
    ``docker exec sqlcmd``, not Python drivers).
    """
    forbidden = {"pyodbc", "pymssql", "jaydebeapi"}
    tests_dir = Path(__file__).parent
    offenders: dict[str, set[str]] = {}
    scan_roots = [(SRC, SRC.parent.parent), (tests_dir, tests_dir.parent)]
    for root, rel_base in scan_roots:
        for path in sorted(root.rglob("*.py")):
            bad = {
                m
                for m in _imported_modules(path)
                if m.split(".")[0].lower() in forbidden
            }
            if bad:
                offenders[str(path.relative_to(rel_base))] = bad
    assert not offenders, (
        "Direct database-driver imports are forbidden (all connectivity is "
        "Spark's JDBC connector, per ADR-015/DISC-01):\n"
        + "\n".join(f"  {mod}: {sorted(imps)}" for mod, imps in offenders.items())
    )


def test_core_relations_seam_is_dbt_free_and_usable() -> None:
    """The TableRenderer Protocol + LiteralRenderer live in core, no dbt needed.

    Importing the core seam must not require dbt, and the default LiteralRenderer
    must satisfy the Protocol both with and without a resolver (the direct-artifact
    rendering behaviour the dbt DbtRefRenderer mirrors).
    """
    from tablespec.core.relations import (
        LiteralRenderer,
        RelationRef,
        TableRenderer,
    )

    plain = LiteralRenderer()
    assert plain.render("member") == "member"  # passthrough
    qualified = LiteralRenderer(resolver=lambda n: f"cat.{n}")
    assert qualified.render("member") == "cat.member"  # resolver applied
    # Structural Protocol conformance (runtime_checkable).
    assert isinstance(plain, TableRenderer)
    assert RelationRef("member").kind == "table"  # advisory default
