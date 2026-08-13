"""SQL plan generation from UMF metadata and relationships.

Generates pure SQL execution plans from UMF metadata for building derived tables
through sequential joins. Creates transparent, verifiable SQL that can be executed
against any SQL engine supporting temporary views.

This module is engine-agnostic: it emits standard SQL with temporary views for
transparency. The generated SQL uses a sequential join strategy where each step
builds on the previous view.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Literal

import sqlglot
from sqlglot import exp

from tablespec.core.relations import LiteralRenderer, TableRenderer

from .relationship_resolver import RelationshipResolver

if TYPE_CHECKING:
    from tablespec.models.umf import UMF

logger = logging.getLogger(__name__)

_SQL_KEYWORDS = {
    "CASE",
    "WHEN",
    "THEN",
    "ELSE",
    "END",
    "AND",
    "OR",
    "NOT",
    "IN",
    "IS",
    "NULL",
    "TRUE",
    "FALSE",
    "LIKE",
    "BETWEEN",
    "AS",
    "FROM",
    "SELECT",
    "WHERE",
    "GROUP",
    "BY",
    "ORDER",
    "HAVING",
    "LIMIT",
    "COALESCE",
    "NULLIF",
    "CAST",
    "TRIM",
    "UPPER",
    "LOWER",
    "LENGTH",
    "SUBSTRING",
    "CONCAT",
    "REPLACE",
    "DATE",
    "TIMESTAMP",
    "INTERVAL",
    "COUNT",
    "SUM",
    "AVG",
    "MIN",
    "MAX",
    "ROW_NUMBER",
    "RANK",
    "PARTITION",
    "OVER",
    "ASC",
    "DESC",
    "DISTINCT",
    "ALL",
    "ANY",
    "EXISTS",
    "UNION",
    "INTERSECT",
    "EXCEPT",
    "JOIN",
    "LEFT",
    "RIGHT",
    "INNER",
    "OUTER",
    "FULL",
    "CROSS",
    "ON",
    "USING",
    "CONCAT_WS",
}

_IDENTIFIER_RE = re.compile(r"(?<![\w\.])([A-Za-z_][A-Za-z0-9_]*)")

_SPARK_TYPE_MAP = {
    "TEXT": "STRING",
    "CHAR": "STRING",
    "STRING": "STRING",
    "STRINGTYPE": "STRING",
    "INTEGER": "INT",
    "INTEGERTYPE": "INT",
    "INT": "INT",
    "BIGINT": "INT",
    "SMALLINT": "INT",
    "TINYINT": "INT",
    "DECIMAL": "DECIMAL(18,2)",
    "FLOAT": "FLOAT",
    "DOUBLE": "DOUBLE",
    "DATE": "DATE",
    "DATETYPE": "DATE",
    "DATETIME": "TIMESTAMP",
    "TIMESTAMP": "TIMESTAMP",
    "BOOLEAN": "BOOLEAN",
    "BOOLEANTYPE": "BOOLEAN",
}

_AGGREGATE_FUNCTIONS = [
    "COUNT(",
    "MIN(",
    "MAX_BY(",
    "MIN_BY(",
    "MAX(",
    "SUM(",
    "AVG(",
]

# Internal name of the UNION-of-sources base view for the union_sources strategy.
# It must NOT collide with the target table name: when a gold target is itself
# named ``member_universe`` (as the survivorship corpus is), reusing that literal
# name made the union base view and the final-assembly view share a name. The CTE
# conversion dedups CTE names, so the SECOND (the final assembly carrying the
# survivorship COALESCE) was dropped and the model ended at the last join step --
# the declared survivorship column was never produced. A distinct internal name
# keeps the final assembly as the terminal CTE.
_UNION_UNIVERSE_VIEW = "union_universe"


def _parse_table_ref(name: str) -> tuple[str | None, str]:
    """Split a possibly-qualified table name into (namespace, bare_name).

    Args:
        name: Table name, optionally qualified as ``namespace.table``.

    Returns:
        Tuple of (namespace, bare_name).  ``namespace`` is ``None`` when
        *name* contains no dot.

    Examples:
        >>> _parse_table_ref("my_table")
        (None, 'my_table')
        >>> _parse_table_ref("other_ns.my_table")
        ('other_ns', 'my_table')

    """
    if "." in name:
        parts = name.split(".", maxsplit=1)
        return parts[0], parts[1]
    return None, name


def _output_ordered_columns(columns: list[Any]) -> list[Any]:
    """Order columns for OUTPUT projections: spec ``position`` first, then
    case-insensitive name.

    The final projection defines the physical column order of a
    ``CREATE TABLE ... AS`` target, so the emitted order must follow the
    spec's declared positions — an alphabetical projection produces a
    semantically different table schema. Columns without a parseable
    position sort after all positioned columns, alphabetically among
    themselves (deterministic for legacy specs that never set positions).
    """

    def _key(col: Any) -> tuple[int, int, str]:
        try:
            return (0, int(col.position), col.name.lower())
        except (TypeError, ValueError):
            return (1, 0, col.name.lower())

    return sorted(columns, key=_key)


class SQLPlanGenerator:
    """Generate SQL execution plans from UMF metadata and relationships.

    Pure-data implementation: accepts UMF Pydantic models, performs no file I/O,
    and has no dependency on any specific pipeline framework.

    Args:
        template_vars: Optional template variables for substitution in SQL
            expressions.  ``{{var_name}}`` patterns in derivation expressions
            are replaced with the corresponding value.
        table_resolver: Optional callable that maps a table name to a resolved
            name (e.g. catalog-qualified path).  When ``None``, table names are
            used as-is.  Mutually exclusive with *table_renderer* (it is a
            shorthand for ``LiteralRenderer(resolver=table_resolver)``).
        table_renderer: Optional :class:`~tablespec.core.relations.TableRenderer`.
            This is the single relation-rendering seam: every site that inlines a
            relation name routes it through ``self._renderer.render(name)``.  When
            ``None``, a :class:`LiteralRenderer` wrapping *table_resolver* is used,
            preserving the historical inline-the-name behaviour byte-for-byte.
            Inject a ``DbtRefRenderer`` here to emit ``{{ ref() }}`` instead.

    """

    def __init__(
        self,
        template_vars: dict[str, str] | None = None,
        table_resolver: Callable[[str], str] | None = None,
        *,
        table_renderer: TableRenderer | None = None,
    ) -> None:
        if table_renderer is not None and table_resolver is not None:
            msg = "Pass either table_resolver or table_renderer, not both"
            raise ValueError(msg)
        self.template_vars: dict[str, str] = template_vars or {}
        self.table_resolver = table_resolver
        # The one relation-rendering seam. Defaults to the historical literal
        # behaviour so existing artifacts/goldens are unchanged.
        self._renderer: TableRenderer = (
            table_renderer
            if table_renderer is not None
            else LiteralRenderer(resolver=table_resolver)
        )
        self.logger = logging.getLogger(self.__class__.__name__)

        # Instance state reset per ``generate_for_table`` call
        self._related_umfs: dict[str, UMF] = {}
        self._pre_aggregated_columns: dict[str, list[dict[str, str | bool]]] = {}
        self._agg_view_source_columns: dict[str, str] = {}
        self._required_columns: dict[str, set[str]] = {}
        self._accumulated_columns: dict[str, str] = {}
        self._join_sequence: list[dict[str, Any]] = []
        # Maps (source_table, target_column_name) -> the pivoted column alias the
        # pivot CTE actually emits (e.g. ("diagnosis", "diagnosis_1") ->
        # "diagnosis__diagnosis_1"). The final-assembly column mapping consults
        # this so a target column derived from a pivot source references the
        # pivoted output column rather than the raw source column.
        self._pivot_column_map: dict[tuple[str, str], str] = {}
        # Target columns projected by the union_branches base view. The final
        # assembly references these as bare ``base.<col>`` (the union view
        # already applied each branch's candidate mapping).
        self._union_branch_columns: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_for_table(
        self,
        table_umf: UMF,
        related_umfs: dict[str, UMF],
        *,
        mode: Literal["views", "cte"] = "views",
    ) -> str:
        """Generate a complete SQL plan for a single target table.

        Args:
            table_umf: UMF metadata for the target table.
            related_umfs: Dict mapping table names to UMF models for all
                tables that participate in derivations (source tables).
            mode: Output format.  ``"views"`` (default) emits sequential
                ``CREATE OR REPLACE TEMPORARY VIEW`` statements.
                ``"cte"`` emits a single ``WITH ... SELECT`` statement
                using Common Table Expressions.

        Returns:
            Multi-statement SQL string (views mode) or a single CTE
            statement (cte mode).

        Raises:
            ValueError: If the table name cannot be determined.

        """
        table_name = table_umf.table_name
        if not table_name:
            msg = "table_name could not be determined from table_umf"
            raise ValueError(msg)
        scd = table_umf.metadata.scd if table_umf.metadata else None
        if scd is not None:
            return self._generate_scd_plan(table_name, table_umf, related_umfs, scd)
        sql = self._generate_table_sql(table_name, table_umf, related_umfs)
        if mode == "cte":
            return self._convert_views_to_cte(sql)
        return sql

    # ------------------------------------------------------------------
    # SCD2 staged-recompute emission
    # ------------------------------------------------------------------

    def _generate_scd_plan(
        self,
        table_name: str,
        table_umf: UMF,
        related_umfs: dict[str, UMF],
        scd: Any,
    ) -> str:
        """Emit the SCD2 staged-recompute plan (4 statements).

        1. ``CREATE TABLE IF NOT EXISTS`` — initial load: the current
           snapshot with open validity (no-op after the first run).
        2. ``CREATE OR REPLACE TABLE <target><stage_suffix>`` — the staged
           recompute: history rows pass through; current rows whose natural
           key vanished from the snapshot or whose tracked-column hash
           changed are CLOSED; unchanged current rows are carried; snapshot
           rows with new keys or changed hashes are OPENED. Staged because
           the new state reads the CURRENT target (a self-referencing CTAS
           is not reliable).
        3. Swap the stage into the target.
        4. Drop the stage.

        The snapshot query is the table's normal generated plan with the
        SCD bookkeeping columns excluded (the plan itself emits them), and
        is embedded as a nested subquery (Spark 3+ CTE-in-subquery).
        Statements are separated by ``;``. Table names are unqualified —
        the runtime resolves them via its session catalog/schema context.
        """
        bookkeeping = {scd.effective_from, scd.effective_to, scd.current_flag}
        ordered = _output_ordered_columns(table_umf.columns or [])
        spec_names = {c.name for c in ordered}
        missing_book = sorted(bookkeeping - spec_names)
        if missing_book:
            msg = (
                f"scd on {table_name}: bookkeeping column(s) {missing_book} "
                "missing from the table spec"
            )
            raise ValueError(msg)
        data_cols = [c.name for c in ordered if c.name not in bookkeeping]
        missing = [c for c in [*scd.keys, *scd.tracked] if c not in data_cols]
        if missing:
            msg = (
                f"scd on {table_name}: keys/tracked column(s) {missing} are "
                "not data columns of the table spec"
            )
            raise ValueError(msg)

        # The snapshot is generated as a plain plan (no scd block) under a
        # DISTINCT name: the cte-mode terminal CTE is named after the table,
        # and a CTE sharing the real target's name both shadows reads of the
        # CURRENT target in the stage statement and defeats downstream
        # canonicalizers' CTE merging (the name resolves to a real schema
        # table). Self-referencing candidates are remapped to the same name.
        snap_name = f"{table_name}__scd_snapshot"
        snapshot_umf = table_umf.model_copy(deep=True)
        snapshot_umf.table_name = snap_name
        snapshot_umf.columns = [
            c for c in (table_umf.columns or []) if c.name not in bookkeeping
        ]
        for col in snapshot_umf.columns:
            if col.derivation and col.derivation.candidates:
                for cand in col.derivation.candidates:
                    if cand.table == table_name:
                        cand.table = snap_name
        snapshot_umf.metadata = snapshot_umf.metadata.model_copy(update={"scd": None})
        snapshot_sql = self._convert_views_to_cte(
            self._generate_table_sql(snap_name, snapshot_umf, related_umfs)
        ).rstrip().rstrip(";")

        stage = f"{table_name}{scd.stage_suffix}"
        eff_from, eff_to, flag = (
            scd.effective_from,
            scd.effective_to,
            scd.current_flag,
        )
        hash_args = ",\n      ".join(
            f"coalesce(CAST({c} AS STRING), '{scd.null_sentinel}')"
            for c in scd.tracked
        )
        hash_expr = f"md5(concat_ws('|',\n      {hash_args}))"

        def _key_join(left: str, right: str) -> str:
            return " AND ".join(f"{left}.{k} <=> {right}.{k}" for k in scd.keys)

        first_key = scd.keys[0]
        cols = ", ".join(data_cols)
        cols_prefixed = ", ".join(f"e.{c}" for c in data_cols)
        indent_snapshot = "\n".join(f"  {ln}" for ln in snapshot_sql.splitlines())

        seed = (
            f"CREATE TABLE IF NOT EXISTS {table_name} AS\n"
            f"SELECT snap.*,\n"
            f"  current_date() AS {eff_from},\n"
            f"  CAST(NULL AS DATE) AS {eff_to},\n"
            f"  TRUE AS {flag}\n"
            f"FROM (\n{indent_snapshot}\n) snap"
        )
        stage_stmt = (
            f"CREATE OR REPLACE TABLE {stage} AS\n"
            f"WITH new_snapshot AS (\n{indent_snapshot}\n),\n"
            f"hashed AS (\n"
            f"  SELECT *, {hash_expr} AS _row_hash\n"
            f"  FROM new_snapshot\n"
            f"),\n"
            f"existing AS (SELECT * FROM {table_name}),\n"
            f"existing_current AS (\n"
            f"  SELECT *, {hash_expr} AS _row_hash\n"
            f"  FROM existing WHERE {flag} = TRUE\n"
            f"),\n"
            f"-- current rows leaving: gone from the snapshot, or tracked columns changed\n"
            f"to_close AS (\n"
            f"  SELECT e.* FROM existing_current e\n"
            f"  LEFT JOIN hashed n\n"
            f"    ON {_key_join('e', 'n')}\n"
            f"  WHERE n.{first_key} IS NULL OR e._row_hash <> n._row_hash\n"
            f"),\n"
            f"-- snapshot rows arriving: new keys, or the new value of a changed key\n"
            f"to_open AS (\n"
            f"  SELECT n.* FROM hashed n\n"
            f"  LEFT JOIN existing_current e\n"
            f"    ON {_key_join('e', 'n')}\n"
            f"  WHERE e.{first_key} IS NULL OR e._row_hash <> n._row_hash\n"
            f")\n"
            f"SELECT {cols},\n"
            f"       {eff_from}, {eff_to}, {flag}\n"
            f"FROM existing WHERE {flag} = FALSE\n"
            f"UNION ALL\n"
            f"SELECT {cols},\n"
            f"       {eff_from}, current_date() AS {eff_to}, FALSE AS {flag}\n"
            f"FROM to_close\n"
            f"UNION ALL\n"
            f"SELECT {cols_prefixed},\n"
            f"       e.{eff_from}, e.{eff_to}, e.{flag}\n"
            f"FROM existing_current e\n"
            f"LEFT ANTI JOIN to_close c\n"
            f"  ON {_key_join('e', 'c')}\n"
            f"UNION ALL\n"
            f"SELECT {cols},\n"
            f"       current_date() AS {eff_from}, CAST(NULL AS DATE) AS {eff_to}, TRUE AS {flag}\n"
            f"FROM to_open"
        )
        swap = f"CREATE OR REPLACE TABLE {table_name} AS\nSELECT * FROM {stage}"
        drop = f"DROP TABLE {stage}"
        return ";\n\n".join([seed, stage_stmt, swap, drop])

    # ------------------------------------------------------------------
    # CTE conversion
    # ------------------------------------------------------------------

    _VIEW_PATTERN = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+TEMPORARY\s+VIEW\s+(\S+)\s+AS\s*\n",
        re.IGNORECASE,
    )

    def _convert_views_to_cte(self, views_sql: str) -> str:
        """Post-process views-mode SQL into a single ``WITH ... SELECT`` statement.

        Each ``CREATE OR REPLACE TEMPORARY VIEW <name> AS`` block is
        converted into a CTE entry.  SQL comment blocks between statements
        are preserved inside the CTE body so traceability is maintained.
        """
        # Split on CREATE OR REPLACE TEMPORARY VIEW boundaries
        parts = self._VIEW_PATTERN.split(views_sql)
        # parts looks like: [preamble, name1, body1, name2, body2, ...]
        if len(parts) < 3:
            # No views found — return as-is
            return views_sql

        preamble = parts[0]
        cte_entries: list[str] = []
        last_name: str | None = None
        seen_names: set[str] = set()
        # A comment block trailing one view's statement is actually the NEXT
        # step's leading comment (views are joined with "\n\n"). Carry it forward
        # so traceability comments survive WITHOUT leaving a stray ';' mid-CTE.
        carried_comment = ""

        for i in range(1, len(parts), 2):
            name = parts[i]
            body = parts[i + 1] if i + 1 < len(parts) else ""

            select_body, trailing_comment = self._split_cte_body(body)
            if carried_comment:
                select_body = f"{carried_comment}\n{select_body}"
            carried_comment = trailing_comment

            # Avoid duplicate CTE names (diamond dependencies)
            if name in seen_names:
                continue
            seen_names.add(name)

            cte_entries.append(f"{name} AS (\n{select_body}\n)")
            last_name = name

        if not cte_entries or last_name is None:
            return views_sql

        cte_block = ",\n\n".join(cte_entries)
        result = f"{preamble.rstrip()}\nWITH\n{cte_block}\nSELECT * FROM {last_name};\n"
        return result

    @staticmethod
    def _split_cte_body(body: str) -> tuple[str, str]:
        """Split one view body into ``(select_body, trailing_comment)``.

        A views-mode body is a single ``CREATE OR REPLACE TEMPORARY VIEW ... AS``
        statement -- one SELECT terminated by ``;`` -- optionally followed by the
        *next* step's leading comment block (an artefact of joining views with
        ``"\\n\\n"``). For a valid CTE we keep the SELECT (without its terminating
        ``;``) and return the trailing comment separately so the caller can carry
        it forward as the next CTE's leading comment (preserving traceability
        without leaving a stray ``;`` mid-CTE).
        """
        text = body.strip()
        semi = text.rfind(";")
        if semi == -1:
            return text, ""
        tail = text[semi + 1 :].strip()
        # Only the LAST ';' followed solely by comments/blank is the terminator.
        if tail != "" and not all(
            line.strip() == "" or line.lstrip().startswith("--")
            for line in tail.splitlines()
        ):
            return text, ""
        return text[:semi].strip(), tail

    # ------------------------------------------------------------------
    # Internal orchestration
    # ------------------------------------------------------------------

    def _generate_table_sql(
        self, table_name: str, table_umf: UMF, related_umfs: dict[str, UMF]
    ) -> str:
        """Generate the complete SQL plan for *table_name*."""
        # Reset per-table state
        self._related_umfs = related_umfs
        self._pre_aggregated_columns = {}
        self._agg_view_source_columns = {}
        self._required_columns = self._build_required_columns_map(table_umf)
        self._accumulated_columns = {}
        self._pivot_column_map = {}
        self._union_branch_columns = set()

        sections: list[str] = []

        # Resolve relationships via the metadata-driven resolver
        resolver = RelationshipResolver(related_umfs)
        plan = resolver.resolve_plan(table_umf)

        join_sequence: list[dict[str, Any]] = plan.join_sequence
        base_table: str | None = plan.base_table
        base_table_strategy: str | None = plan.base_table_strategy
        union_sources: list[dict[str, Any]] | None = plan.union_sources

        self._join_sequence = join_sequence

        metadata = table_umf.metadata

        # Warn on declarative fields that the selected strategy does not consume
        if metadata:
            if metadata.union_base_tables and base_table_strategy != "union_branches":
                self.logger.warning(
                    f"union_base_tables set on {table_name} but base_table_strategy "
                    f"is {base_table_strategy!r} - ignored (set base_table_strategy: "
                    "union_branches to enable UNION branch generation)"
                )
            if metadata.base_table_filter and base_table_strategy in (
                "unpivot",
                "union_sources",
            ):
                self.logger.warning(
                    f"base_table_filter set on {table_name} but base_table_strategy "
                    f"{base_table_strategy!r} does not consume it - ignored"
                )

        # Header
        if base_table_strategy == "union_sources":
            header_base = f"{_UNION_UNIVERSE_VIEW} (UNION of source tables)"
        elif base_table_strategy == "unpivot":
            header_base = f"{base_table} (UNPIVOT)"
        elif base_table_strategy == "union_branches" and base_table:
            union_tables = self._get_union_branch_tables(table_umf)
            union_op = (
                "UNION" if metadata and metadata.union_type == "union" else "UNION ALL"
            )
            header_base = f"{base_table} ({union_op} with {', '.join(union_tables)})"
        else:
            header_base = base_table or ""

        sections.append(
            self._generate_header(
                table_name, table_umf, header_base, len(join_sequence)
            )
        )

        # Base view
        if base_table_strategy == "unpivot" and base_table:
            sections.append(self._generate_unpivot_base_view(table_umf, base_table))
        elif base_table_strategy == "union_sources" and union_sources:
            sections.append(
                self._generate_member_universe_view(table_umf, union_sources)
            )
            agg_sections = self._generate_pre_aggregation_views(
                table_umf, union_sources
            )
            sections.extend(agg_sections)
        elif base_table_strategy == "union_branches":
            if not base_table:
                msg = (
                    f"base_table_strategy 'union_branches' on {table_name} requires "
                    "a resolvable base_table"
                )
                raise ValueError(msg)
            sections.append(
                self._generate_union_branch_base_view(
                    table_umf, base_table, join_sequence
                )
            )
        elif base_table:
            sections.append(
                self._generate_base_view(
                    table_umf,
                    base_table,
                    self._join_source_columns(join_sequence),
                )
            )
        else:
            self.logger.info(
                f"No base table for {table_name} - generating synthetic table from derivations"
            )

        # Sequential join steps
        current_view = "disposition_base"
        for step, join_info in enumerate(join_sequence, 1):
            join_sql = self._generate_join_step(step, join_info, current_view)
            sections.append(join_sql)
            current_view = f"disposition_step_{step}"

        # Pre-aggregation view join steps
        if self._pre_aggregated_columns:
            agg_views: dict[str, list[str]] = {}
            for col_name, agg_sources in sorted(self._pre_aggregated_columns.items()):
                for agg_info in agg_sources:
                    view_name = str(agg_info["agg_view_name"])
                    if view_name not in agg_views:
                        agg_views[view_name] = []
                    if col_name not in agg_views[view_name]:
                        agg_views[view_name].append(col_name)

            step = len(join_sequence)
            for agg_view_name, col_names in sorted(agg_views.items()):
                step += 1
                agg_join_sql = self._generate_agg_view_join(
                    step, agg_view_name, col_names, current_view, table_umf
                )
                sections.append(agg_join_sql)
                current_view = f"disposition_step_{step}"

        # Final assembly
        effective_base = (
            _UNION_UNIVERSE_VIEW
            if base_table_strategy == "union_sources"
            else base_table
        )
        sections.append(
            self._generate_final_assembly(
                table_name, table_umf, current_view, effective_base
            )
        )

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Helpers: name resolution
    # ------------------------------------------------------------------

    def _resolve_table_name(self, table_name: str) -> str:
        """Render a relation name via the configured :class:`TableRenderer`.

        This is the single seam through which every external relation flows. The
        default :class:`LiteralRenderer` reproduces the historical behaviour
        (apply ``table_resolver`` if any, else identity); a ``DbtRefRenderer``
        turns the same call into a ``{{ ref() }}`` / ``{{ source() }}`` literal.
        """
        return self._renderer.render(table_name)

    @staticmethod
    def _sanitize_alias(name: str) -> str:
        """Replace dots with underscores for use in SQL identifiers."""
        return name.replace(".", "_")

    def _get_table_columns(self, table_name: str) -> list[str]:
        """Return column names for *table_name* from ``_related_umfs``."""
        # Try the name as-given first, then the bare name
        umf = self._related_umfs.get(table_name)
        if umf is None:
            _, bare = _parse_table_ref(table_name)
            umf = self._related_umfs.get(bare)
        if umf is None:
            self.logger.warning(f"Table {table_name} not found in related_umfs")
            return []
        return [col.name for col in umf.columns] if umf.columns else []

    # ------------------------------------------------------------------
    # Template variable substitution
    # ------------------------------------------------------------------

    def _substitute_template_vars(self, text: str) -> str:
        """Replace ``{{var}}`` patterns in *text* with values from ``template_vars``."""
        if not self.template_vars:
            return text
        result = text
        for var_name, var_value in self.template_vars.items():
            result = result.replace(f"{{{{{var_name}}}}}", var_value)
        return result

    # ------------------------------------------------------------------
    # Required-columns tracking
    # ------------------------------------------------------------------

    def _build_required_columns_map(self, table_umf: UMF) -> dict[str, set[str]]:
        """Map source table names to the set of their columns used by derivations."""
        required: dict[str, set[str]] = {}

        for col in table_umf.columns:
            if not col.derivation or not col.derivation.candidates:
                continue

            for cand in col.derivation.candidates:
                if not cand.table:
                    continue

                col_names: list[str] = []
                if cand.column:
                    col_names.append(cand.column)
                elif cand.expression:
                    col_names.extend(
                        self._extract_columns_from_expression(cand.expression)
                    )

                for col_name in col_names:
                    _, bare_name = _parse_table_ref(cand.table)
                    required.setdefault(bare_name, set()).add(col_name)
                    if cand.table != bare_name:
                        required.setdefault(cand.table, set()).add(col_name)

        return required

    def _extract_columns_from_expression(self, expression: str) -> list[str]:
        """Extract column references from a SQL expression.

        Parses the expression with sqlglot and returns the bare names of every
        Column node (dropping any table qualifier and the ``alias__`` join
        prefix). Falls back to a keyword-filtered identifier regex only when the
        expression does not parse. The AST path is authoritative: an all-caps
        column name (``QPA``) is a real column, not a keyword — the old regex
        dropped it and the base view omitted it, breaking downstream refs.
        """
        try:
            parsed = sqlglot.parse_one(expression, read="spark")
        except Exception:  # noqa: BLE001 - regex fallback below
            parsed = None

        columns: list[str] = []
        if parsed is not None:
            seen: set[str] = set()
            for col in parsed.find_all(exp.Column):
                name = col.name
                if "__" in name:
                    name = name.split("__")[-1]
                if name and name not in seen:
                    seen.add(name)
                    columns.append(name)
            return columns

        pattern = r"\b([a-zA-Z][a-zA-Z0-9_]*)\b"
        for match in re.findall(pattern, expression):
            if match.upper() in _SQL_KEYWORDS or len(match) == 1:
                continue
            columns.append(match.split("__")[-1] if "__" in match else match)
        return columns

    def _get_required_columns_for_table(self, table_name: str) -> set[str] | None:
        """Return the set of required columns for *table_name*, or ``None`` if unfiltered."""
        if table_name in self._required_columns:
            return self._required_columns[table_name]
        _, bare = _parse_table_ref(table_name)
        if bare in self._required_columns:
            return self._required_columns[bare]
        return None

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _generate_header(
        self, table_name: str, table_umf: UMF, base_table: str, join_count: int
    ) -> str:
        """Generate a SQL file header comment block."""
        return f"""-- ============================================================================
-- SQL Execution Plan: {table_name}
-- ============================================================================
-- Purpose: Build {table_name} dataset through sequential joins
-- Base Table: {base_table} (hub table)
-- Total Joins: {join_count}
-- Strategy: Pure SQL with temporary views for transparency
-- ============================================================================"""

    # ------------------------------------------------------------------
    # Base view generation
    # ------------------------------------------------------------------

    def _generate_base_view(
        self,
        table_umf: UMF,
        base_table: str,
        join_source_columns: list[str] | None = None,
    ) -> str:
        """Generate the base view selecting required columns from *base_table*."""
        resolved_table = self._resolve_table_name(base_table)
        base_columns = self._get_table_columns(base_table)
        metadata = table_umf.metadata

        # Filter to derivation-required columns + join key + meta columns.
        # base_join_column overrides the primary-key-derived join key so the
        # overridden key survives the required-columns filter.
        required_cols = self._get_required_columns_for_table(base_table)
        if required_cols:
            pk_col = (metadata.base_join_column if metadata else None) or (
                table_umf.primary_key[0] if table_umf.primary_key else None
            )
            selected_columns = [
                col
                for col in base_columns
                if col in required_cols or col == pk_col or col.startswith("meta_")
            ]
        else:
            selected_columns = base_columns

        # Join source columns (incl. alternative/join_via keys) must survive the
        # required-columns filter or the emitted joins reference columns the
        # base view never projected
        for col in join_source_columns or []:
            if col in base_columns and col not in selected_columns:
                selected_columns.append(col)

        for col in selected_columns:
            self._accumulated_columns[col] = "base"

        column_list = ",\n  ".join(selected_columns)

        where_clause = ""
        if metadata and metadata.base_table_filter:
            base_filter = self._substitute_template_vars(
                metadata.base_table_filter.strip()
            )
            where_clause = f"\nWHERE {base_filter}"

        return f"""-- ============================================================================
-- STEP 0: Create base view from {base_table}
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW disposition_base AS
SELECT
  {column_list}
FROM {resolved_table}{where_clause};"""

    @staticmethod
    def _join_source_columns(join_sequence: list[dict[str, Any]]) -> list[str]:
        """Base-side key columns every join in *join_sequence* references."""
        cols: list[str] = []
        for join_info in join_sequence:
            candidates = [join_info.get("source_column")]
            join_via = join_info.get("join_via")
            if join_via:
                candidates.append(join_via.get("source_key"))
            for alt in join_info.get("alternative_joins") or []:
                candidates.append(alt.get("source_column"))
            for col in candidates:
                if col and col not in cols:
                    cols.append(col)
        return cols

    # ------------------------------------------------------------------
    # union_branches base view
    # ------------------------------------------------------------------

    def _get_union_branch_tables(self, table_umf: UMF) -> list[str]:
        """Union table list for the union_branches strategy.

        ``union_base_tables`` wins; ``source_tables`` is the documented fallback.
        """
        metadata = table_umf.metadata
        if not metadata:
            return []
        return list(metadata.union_base_tables or metadata.source_tables or [])

    def _generate_union_branch_base_view(
        self,
        table_umf: UMF,
        base_table: str,
        join_sequence: list[dict[str, Any]],
    ) -> str:
        """Generate ``disposition_base`` as a UNION of per-source-table branches.

        Unlike ``union_base_tables`` handling in the pulseflow fork (which
        projects the base table's schema from every branch), each branch here
        projects the TARGET column set through that source table's own
        derivation candidates:

        - a candidate with ``union_value`` emits ``CAST(<literal> AS <type>)``
        - otherwise the branch table's lowest-priority candidate supplies the
          expression or column
        - a column with no candidate for the branch emits ``CAST(NULL AS <type>)``
          so the UNION stays column-aligned
        - the single distinct ``row_filter`` among a branch's candidates becomes
          the branch WHERE clause (conflicting filters raise)
        - with ``metadata.dedup_strategy == 'latest'`` and a candidate
          ``order_by``, each branch is deduplicated with ``ROW_NUMBER() OVER
          (PARTITION BY <target primary_key> ORDER BY <order_by>)`` keeping the
          first row

        The whole construct is ONE ``CREATE OR REPLACE TEMPORARY VIEW`` whose
        body starts ``AS\\nWITH`` -- branch CTEs are nested-scope, so CTE-mode
        conversion and name-collision safety are preserved.
        """
        metadata = table_umf.metadata
        if metadata is None:  # pragma: no cover - strategy implies metadata
            msg = "union_branches strategy requires table metadata"
            raise ValueError(msg)

        union_tables = self._get_union_branch_tables(table_umf)
        if not union_tables:
            msg = (
                f"base_table_strategy 'union_branches' on {table_umf.table_name} "
                "requires union_base_tables (or source_tables) in metadata"
            )
            raise ValueError(msg)

        branch_tables = [base_table, *union_tables]
        if len(set(branch_tables)) != len(branch_tables):
            msg = (
                f"union_branches on {table_umf.table_name}: duplicate table in "
                f"base_table + union tables: {branch_tables}"
            )
            raise ValueError(msg)

        # ---- Column set -------------------------------------------------
        branch_table_set = set(branch_tables)
        branch_cols: list[str] = []
        col_types: dict[str, str] = {}
        cands_by_table: dict[str, dict[str, list[Any]]] = {t: {} for t in branch_tables}
        for col in _output_ordered_columns(table_umf.columns):
            cands = (
                col.derivation.candidates
                if col.derivation and col.derivation.candidates
                else []
            )
            table_cands = [c for c in cands if c.table in branch_table_set]
            if not table_cands:
                continue
            branch_cols.append(col.name)
            col_types[col.name] = (col.data_type or "STRING").upper()
            for cand in table_cands:
                cands_by_table[cand.table].setdefault(col.name, []).append(cand)

        if not branch_cols:
            msg = (
                f"union_branches on {table_umf.table_name}: no derivation "
                f"candidates reference the branch tables {branch_tables}"
            )
            raise ValueError(msg)

        branch_col_set = set(branch_cols)
        table_columns = {t: self._get_table_columns(t) for t in branch_tables}
        table_col_types = {t: self._get_table_column_types(t) for t in branch_tables}

        # ---- meta_* passthrough (sorted union across branch tables) ------
        meta_types: dict[str, str] = {}
        for t in branch_tables:
            for c in table_columns[t]:
                if c.startswith("meta_") and c not in branch_col_set:
                    meta_types.setdefault(c, table_col_types[t].get(c, "STRING"))
        meta_cols = sorted(meta_types)

        # ---- join-key guarantee (later disposition steps join on these) --
        extra_types: dict[str, str] = {}
        extra_keys: list[str] = []
        for src in self._join_source_columns(join_sequence):
            if src in branch_col_set or src in meta_types or src in extra_types:
                continue
            if src not in table_columns[base_table]:
                self.logger.warning(
                    f"union_branches on {table_umf.table_name}: join source column "
                    f"'{src}' not found on base table {base_table}; projecting "
                    "NULL where absent"
                )
            for t in branch_tables:
                if src in table_col_types[t]:
                    extra_types[src] = table_col_types[t][src]
                    break
            else:
                extra_types[src] = "STRING"
            extra_keys.append(src)

        passthrough_cols = meta_cols + extra_keys
        canonical_cols = branch_cols + passthrough_cols

        # ---- per-branch filter / dedup specs ------------------------------
        def _distinct_single(
            table: str, values: set[str] | set[tuple[str, ...]], what: str
        ) -> Any:
            if len(values) > 1:
                msg = (
                    f"union_branches on {table_umf.table_name}: conflicting "
                    f"{what} values among candidates for branch table {table}: "
                    f"{sorted(values)!r}"
                )
                raise ValueError(msg)
            return next(iter(values)) if values else None

        branch_filters: dict[str, str | None] = {}
        branch_order_by: dict[str, tuple[str, ...] | None] = {}
        for t in branch_tables:
            all_cands = [c for cl in cands_by_table[t].values() for c in cl]
            filters = {c.row_filter.strip() for c in all_cands if c.row_filter}
            branch_filters[t] = _distinct_single(t, filters, "row_filter")
            orders = {tuple(c.order_by) for c in all_cands if c.order_by}
            branch_order_by[t] = _distinct_single(t, orders, "order_by")

        dedup_requested = metadata.dedup_strategy == "latest" and any(
            branch_order_by[t] for t in branch_tables
        )
        pk_cols = list(table_umf.primary_key or [])
        if dedup_requested:
            missing_pk = [c for c in pk_cols if c not in branch_col_set]
            if not pk_cols or missing_pk:
                msg = (
                    f"union_branches dedup on {table_umf.table_name} requires "
                    "every primary_key column to be branch-projected; missing: "
                    f"{missing_pk or 'primary_key itself'}"
                )
                raise ValueError(msg)

        needs_pk_semantics = metadata.union_exclude_base or metadata.union_coalesce_base
        if needs_pk_semantics:
            missing_pk = [c for c in pk_cols if c not in branch_col_set]
            if not pk_cols or missing_pk:
                msg = (
                    f"union_exclude_base/union_coalesce_base on "
                    f"{table_umf.table_name} requires every primary_key column "
                    f"to be branch-projected; missing: "
                    f"{missing_pk or 'primary_key itself'}"
                )
                raise ValueError(msg)
        if metadata.union_coalesce_base and len(union_tables) > 1:
            msg = (
                f"union_coalesce_base on {table_umf.table_name} supports exactly "
                "one union table (base-vs-union overlap semantics are pairwise); "
                f"got {union_tables}"
            )
            raise ValueError(msg)

        # ---- branch CTEs ---------------------------------------------------
        cte_blocks: list[str] = []
        terminal_cte: dict[str, str] = {}
        for t in branch_tables:
            san = self._sanitize_alias(t)
            resolved = self._resolve_table_name(t)
            exprs: list[str] = []
            for col in branch_cols:
                exprs.append(
                    f"    {self._union_branch_expr(t, col, cands_by_table, col_types)}"
                )
            for col in passthrough_cols:
                dtype = meta_types.get(col) or extra_types.get(col, "STRING")
                if col in table_col_types[t]:
                    exprs.append(f"    {col}")
                else:
                    spark_type = self._get_spark_type(dtype)
                    exprs.append(f"    CAST(NULL AS {spark_type}) AS {col}")

            where_parts: list[str] = []
            if t == base_table and metadata.base_table_filter:
                where_parts.append(
                    self._substitute_template_vars(metadata.base_table_filter.strip())
                )
            branch_filter = branch_filters[t]
            if branch_filter:
                where_parts.append(self._substitute_template_vars(branch_filter))
            if len(where_parts) == 2:
                where_clause = (
                    f"\n  WHERE ({where_parts[0]})\n    AND ({where_parts[1]})"
                )
            elif where_parts:
                where_clause = f"\n  WHERE {where_parts[0]}"
            else:
                where_clause = ""

            order_by = branch_order_by[t]
            use_dedup = dedup_requested and order_by is not None

            scratch_cols: list[str] = []
            if use_dedup and order_by is not None:
                projected = set(canonical_cols)
                for entry in order_by:
                    bare = self._order_col_bare(entry)
                    if bare in projected or bare in scratch_cols:
                        continue
                    if bare not in table_col_types[t]:
                        msg = (
                            f"union_branches dedup on {table_umf.table_name}: "
                            f"order_by column '{bare}' is neither projected nor "
                            f"present on branch table {t}"
                        )
                        raise ValueError(msg)
                    scratch_cols.append(bare)
                for col in scratch_cols:
                    exprs.append(f"    {col}")

            exprs_str = ",\n".join(exprs)
            cte_blocks.append(
                f"{san}__rows AS (\n  SELECT\n{exprs_str}\n  FROM {resolved}{where_clause}\n)"
            )

            if use_dedup and order_by is not None:
                partition = ", ".join(pk_cols)
                order_clause = ", ".join(
                    self._order_col_with_direction(entry) for entry in order_by
                )
                final_projection = ",\n".join(f"    {c}" for c in canonical_cols)
                cte_blocks.append(
                    f"{san}__ranked AS (\n  SELECT\n    *,\n"
                    f"    ROW_NUMBER() OVER (\n      PARTITION BY {partition}\n"
                    f"      ORDER BY {order_clause}\n    ) AS __rn\n"
                    f"  FROM {san}__rows\n)"
                )
                cte_blocks.append(
                    f"{san}__dedup AS (\n  SELECT\n{final_projection}\n"
                    f"  FROM {san}__ranked\n  WHERE __rn = 1\n)"
                )
                terminal_cte[t] = f"{san}__dedup"
            else:
                terminal_cte[t] = f"{san}__rows"

        # ---- union assembly -------------------------------------------------
        select_list = ",\n".join(f"  {c}" for c in canonical_cols)
        base_terminal = terminal_cte[base_table]

        if metadata.union_coalesce_base:
            union_parts = self._union_branch_coalesce_parts(
                table_umf,
                canonical_cols,
                pk_cols,
                passthrough_cols,
                base_terminal,
                terminal_cte[union_tables[0]],
                cands_by_table,
                union_tables[0],
            )
            union_op = "UNION ALL"
        else:
            union_parts = [f"SELECT\n{select_list}\nFROM {base_terminal}"]
            for t in union_tables:
                if metadata.union_exclude_base:
                    pk_match = "\n      AND ".join(f"b.{c} = t.{c}" for c in pk_cols)
                    part = (
                        f"SELECT\n{select_list}\nFROM {terminal_cte[t]} t"
                        f"\nWHERE NOT EXISTS (\n  SELECT 1 FROM {base_terminal} b"
                        f"\n  WHERE {pk_match}\n)"
                    )
                else:
                    part = f"SELECT\n{select_list}\nFROM {terminal_cte[t]}"
                union_parts.append(part)
            union_op = "UNION" if metadata.union_type == "union" else "UNION ALL"

        # ---- register + emit -------------------------------------------------
        for name in canonical_cols:
            self._accumulated_columns[name] = "base"
        self._union_branch_columns = set(branch_cols)

        ctes_str = ",\n".join(cte_blocks)
        union_str = f"\n{union_op}\n".join(union_parts)
        union_label = " + ".join(branch_tables)

        return f"""-- ============================================================================
-- STEP 0: Create base view from UNION branches: {union_label}
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW disposition_base AS
WITH {ctes_str}
{union_str};"""

    def _union_branch_expr(
        self,
        table: str,
        col_name: str,
        cands_by_table: dict[str, dict[str, list[Any]]],
        col_types: dict[str, str],
    ) -> str:
        """Projection expression for one target column in one union branch."""
        data_type = col_types[col_name]
        spark_type = self._get_spark_type(data_type)
        cands = cands_by_table[table].get(col_name)
        if not cands:
            return f"CAST(NULL AS {spark_type}) AS {col_name}"

        uv_cands = [c for c in cands if c.union_value is not None]
        if uv_cands:
            literal = self._format_default_value_literal(
                uv_cands[0].union_value, data_type
            )
            return f"CAST({literal} AS {spark_type}) AS {col_name}"

        cand = min(cands, key=lambda c: c.priority)
        if cand.expression:
            expr = self._substitute_template_vars(cand.expression.strip())
            return f"({expr}) AS {col_name}"
        source_col = cand.column or col_name
        if source_col == col_name:
            return col_name
        return f"{source_col} AS {col_name}"

    def _union_branch_coalesce_parts(
        self,
        table_umf: UMF,
        canonical_cols: list[str],
        pk_cols: list[str],
        passthrough_cols: list[str],
        base_terminal: str,
        union_terminal: str,
        cands_by_table: dict[str, dict[str, list[Any]]],
        union_table: str,
    ) -> list[str]:
        """Three-part COALESCE union: base-only, overlap (base wins), union-only.

        Overlap rows keep the base value for primary-key columns, meta/join
        passthroughs, ``union_value`` discriminators, and columns the union
        table cannot supply (NULL-cast branches); every other column fills base
        NULLs from the union branch via COALESCE.
        """
        passthrough = set(passthrough_cols)
        pk_set = set(pk_cols)

        def _base_wins(col: str) -> bool:
            if col in pk_set or col in passthrough:
                return True
            cands = cands_by_table[union_table].get(col)
            if not cands:  # NULL-cast on the union side
                return True
            return any(c.union_value is not None for c in cands)

        pk_match_b = "\n      AND ".join(f"u.{c} = b.{c}" for c in pk_cols)
        pk_match_u = "\n      AND ".join(f"b.{c} = u.{c}" for c in pk_cols)
        on_clause = " AND ".join(f"b.{c} = u.{c}" for c in pk_cols)

        base_only_list = ",\n".join(f"  b.{c}" for c in canonical_cols)
        union_only_list = ",\n".join(f"  u.{c}" for c in canonical_cols)
        overlap_list = ",\n".join(
            f"  b.{c}" if _base_wins(c) else f"  COALESCE(b.{c}, u.{c}) AS {c}"
            for c in canonical_cols
        )

        return [
            (
                f"SELECT\n{base_only_list}\nFROM {base_terminal} b"
                f"\nWHERE NOT EXISTS (\n  SELECT 1 FROM {union_terminal} u"
                f"\n  WHERE {pk_match_b}\n)"
            ),
            (
                f"SELECT\n{overlap_list}\nFROM {base_terminal} b"
                f"\nINNER JOIN {union_terminal} u\n  ON {on_clause}"
            ),
            (
                f"SELECT\n{union_only_list}\nFROM {union_terminal} u"
                f"\nWHERE NOT EXISTS (\n  SELECT 1 FROM {base_terminal} b"
                f"\n  WHERE {pk_match_u}\n)"
            ),
        ]

    def _get_table_column_types(self, table_name: str) -> dict[str, str]:
        """Map column name -> UMF data type for *table_name* (upper-cased)."""
        umf = self._related_umfs.get(table_name)
        if umf is None:
            _, bare = _parse_table_ref(table_name)
            umf = self._related_umfs.get(bare)
        if umf is None or not umf.columns:
            return {}
        return {c.name: (c.data_type or "STRING").upper() for c in umf.columns}

    @staticmethod
    def _order_col_with_direction(col: str) -> str:
        """Append DESC when no explicit direction; always pin NULLS LAST.

        DuckDB and Spark default NULL placement diverges by direction, so the
        window pick stays deterministic across backends.
        """
        stripped = col.strip()
        tokens = stripped.upper().split()
        has_dir = "ASC" in tokens or "DESC" in tokens
        has_nulls = "NULLS" in tokens
        result = stripped if has_dir else f"{stripped} DESC"
        if not has_nulls:
            result = f"{result} NULLS LAST"
        return result

    @staticmethod
    def _order_col_bare(col: str) -> str:
        """Strip a trailing ASC/DESC direction from an order_by entry."""
        parts = col.strip().rsplit(None, 1)
        if len(parts) == 2 and parts[1].upper() in ("ASC", "DESC"):
            return parts[0].strip()
        return col.strip()

    def _generate_unpivot_base_view(self, table_umf: UMF, base_table: str) -> str:
        """Generate the base view with UNPIVOT transformation."""
        resolved_table = self._resolve_table_name(base_table)
        metadata = table_umf.metadata
        if metadata is None:
            msg = "UNPIVOT strategy requires metadata to be set on the UMF"
            raise ValueError(msg)

        columns = metadata.unpivot_columns
        value_column = metadata.unpivot_value_column
        if not columns or not value_column:
            msg = (
                f"UNPIVOT strategy requires unpivot_columns and "
                f"unpivot_value_column in metadata. Got columns={columns}, "
                f"value_column={value_column}"
            )
            raise ValueError(msg)

        in_clause = ",\n    ".join(columns)

        # Track accumulated columns
        base_columns = self._get_table_columns(base_table)
        unpivot_source_cols = set(columns)
        for col in base_columns:
            if col not in unpivot_source_cols:
                self._accumulated_columns[col] = "base"
        self._accumulated_columns[value_column] = "base"
        self._accumulated_columns["source_column"] = "base"

        # Optional dedup with ROW_NUMBER.
        #
        # CORRECTNESS: dedup the WIDE row BEFORE the UNPIVOT, not after. Deduping
        # AFTER the unpivot with ``PARTITION BY <pk>`` keeps exactly ONE unpivoted
        # row per key -- collapsing q1/q2/q3 to a single arbitrary quarter instead
        # of all quarters of the latest snapshot. Picking the latest WIDE row first
        # (PARTITION BY <pk> over the order column) and unpivoting THAT preserves
        # every quarter of the winning snapshot. The deduped CTE also lists its
        # columns explicitly (no ``SELECT * EXCEPT (rn)`` / ``EXCLUDE (rn)``), which
        # is the dialect-portable form (DuckDB ``EXCLUDE`` vs Spark ``EXCEPT``) the
        # generator prefers everywhere else.
        dedup_strategy = metadata.dedup_strategy
        if dedup_strategy == "latest" and table_umf.primary_key:
            partition_cols = self._build_unpivot_dedup_partition(table_umf)
            wide_cols = ",\n    ".join(base_columns)
            return f"""-- ============================================================================
-- STEP 0: Create base view from {base_table} with UNPIVOT (dedup: latest per PK)
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW disposition_base AS
WITH ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY {partition_cols}
      ORDER BY meta_load_dt DESC
    ) AS rn
  FROM {resolved_table}
),
deduped AS (
  SELECT
    {wide_cols}
  FROM ranked
  WHERE rn = 1
)
SELECT *
FROM deduped
UNPIVOT EXCLUDE NULLS (
  {value_column} FOR source_column IN (
    {in_clause}
  )
);"""

        return f"""-- ============================================================================
-- STEP 0: Create base view from {base_table} with UNPIVOT
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW disposition_base AS
SELECT *
FROM {resolved_table}
UNPIVOT EXCLUDE NULLS (
  {value_column} FOR source_column IN (
    {in_clause}
  )
);"""

    def _build_unpivot_dedup_partition(self, table_umf: UMF) -> str:
        """Build PARTITION BY clause for UNPIVOT dedup using source column expressions."""
        pk_columns = table_umf.primary_key or []
        col_lookup = {col.name: col for col in table_umf.columns}

        partition_exprs: list[str] = []
        for pk_col in pk_columns:
            col_def = col_lookup.get(pk_col)
            if (
                not col_def
                or not col_def.derivation
                or not col_def.derivation.candidates
            ):
                partition_exprs.append(pk_col)
                continue

            candidate = col_def.derivation.candidates[0]
            if candidate.expression:
                partition_exprs.append(candidate.expression)
            elif candidate.column:
                partition_exprs.append(candidate.column)
            else:
                partition_exprs.append(pk_col)

        return ", ".join(partition_exprs)

    def _generate_member_universe_view(
        self, table_umf: UMF, union_sources: list[dict[str, Any]]
    ) -> str:
        """Generate the member universe base view from a UNION of source tables."""
        pk_col = table_umf.primary_key[0] if table_umf.primary_key else "id"

        union_parts: list[str] = []
        for source in union_sources:
            source_table = source["table"]
            join_col = source["join_column"]
            resolved = self._resolve_table_name(source_table)

            # Check for derived column expression
            derived_cols = self._get_derived_columns_for_source(source_table)
            if join_col in derived_cols:
                expr, _ = derived_cols[join_col]
                select_expr = f"({expr}) AS {pk_col}"
                key_ref = f"({expr})"
            else:
                select_expr = f"{join_col} AS {pk_col}"
                key_ref = join_col

            # The universe key becomes the target's primary key; never admit a
            # NULL key row (it would surface as a spurious NULL-PK member).
            union_parts.append(
                f"SELECT DISTINCT {select_expr} FROM {resolved} "
                f"WHERE {key_ref} IS NOT NULL"
            )

        union_sql = "\nUNION\n".join(union_parts)

        self._accumulated_columns[pk_col] = "base"

        return f"""-- ============================================================================
-- STEP 0: Create {_UNION_UNIVERSE_VIEW} from UNION of {len(union_sources)} source tables
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW {_UNION_UNIVERSE_VIEW} AS
{union_sql};

-- Create base view from {_UNION_UNIVERSE_VIEW}
CREATE OR REPLACE TEMPORARY VIEW disposition_base AS
SELECT {pk_col} FROM {_UNION_UNIVERSE_VIEW};"""

    # ------------------------------------------------------------------
    # Derived columns helpers
    # ------------------------------------------------------------------

    def _get_derived_columns_for_source(
        self, source_table: str
    ) -> dict[str, tuple[str, bool]]:
        """Get derived column definitions from a source table's UMF.

        Returns:
            Dict mapping column name to (SQL expression, needs_except).

        """
        derived_cols: dict[str, tuple[str, bool]] = {}
        umf = self._related_umfs.get(source_table)
        if umf is None:
            _, bare = _parse_table_ref(source_table)
            umf = self._related_umfs.get(bare)
        if umf is None or not umf.columns:
            return derived_cols

        for col in umf.columns:
            if not col.derivation or not col.derivation.candidates:
                continue
            for cand in col.derivation.candidates:
                if cand.expression and cand.table == source_table:
                    needs_except = col.source != "derived"
                    expr = self._substitute_template_vars(cand.expression)
                    derived_cols[col.name] = (expr, needs_except)
                    break

        return derived_cols

    def _get_derived_column_expression(
        self, table_umf: UMF, col_name: str
    ) -> str | None:
        """Get the derivation expression for a column in the target table."""
        if not table_umf.columns:
            return None

        for col in table_umf.columns:
            if col.name != col_name:
                continue
            if not col.derivation or not col.derivation.candidates:
                return None
            for cand in col.derivation.candidates:
                if cand.expression and (
                    cand.table
                    in (
                        table_umf.table_name,
                        "intermediate",
                        "member_universe",
                        _UNION_UNIVERSE_VIEW,
                    )
                ):
                    return self._substitute_template_vars(cand.expression)
        return None

    def _infer_join_column_from_umf(self, table_name: str) -> str | None:
        """Try to infer join column from UMF primary key or common patterns.

        Delegates to :func:`.relationship_resolver.infer_join_key`.
        """
        from .relationship_resolver import infer_join_key

        _, bare = _parse_table_ref(table_name)
        umf = self._related_umfs.get(table_name) or self._related_umfs.get(bare)
        if umf is None or not umf.columns:
            return None

        columns = [col.name for col in umf.columns]
        lookup_name = table_name if table_name in self._related_umfs else bare
        return infer_join_key(lookup_name, columns, {lookup_name: umf})

    # ------------------------------------------------------------------
    # Pre-aggregation views
    # ------------------------------------------------------------------

    def _generate_pre_aggregation_views(
        self, table_umf: UMF, union_sources: list[dict[str, Any]]
    ) -> list[str]:
        """Generate pre-aggregation views for columns with aggregate expressions."""
        agg_specs: dict[str, list[dict[str, Any]]] = {}

        for col in table_umf.columns:
            if not col.derivation or not col.derivation.candidates:
                continue
            for cand in col.derivation.candidates:
                if not cand.expression:
                    continue
                expr_upper = cand.expression.upper()
                agg_func = None
                if "COUNT(" in expr_upper:
                    agg_func = "COUNT"
                elif "MIN(" in expr_upper:
                    agg_func = "MIN"
                elif "MAX_BY(" in expr_upper:
                    agg_func = "MAX_BY"
                elif "MAX(" in expr_upper:
                    agg_func = "MAX"
                elif "SUM(" in expr_upper:
                    agg_func = "SUM"

                if agg_func and cand.table and cand.table != "intermediate":
                    _, source_table = _parse_table_ref(cand.table)
                    agg_specs.setdefault(source_table, []).append(
                        {
                            "col_name": col.name,
                            "function": agg_func,
                            "expression": cand.expression,
                            "source_column": cand.column or "*",
                            "join_via": cand.join_via,
                            "order_by": cand.order_by,
                            "row_filter": cand.row_filter,
                            "select_columns": cand.select_columns,
                        }
                    )

                    if col.name not in self._pre_aggregated_columns:
                        self._pre_aggregated_columns[col.name] = []
                    self._pre_aggregated_columns[col.name].append(
                        {
                            "source_table": source_table,
                            "agg_view_name": f"{source_table}_agg",
                        }
                    )

        if not agg_specs:
            return []

        sections: list[str] = []
        for source_table, specs in sorted(agg_specs.items()):
            # Find join column
            join_col = None
            source_col = None
            for source in union_sources:
                if source["table"] == source_table:
                    join_col = source["join_column"]
                    source_col = source.get("source_column")
                    break

            if not join_col:
                join_col = self._infer_join_column_from_umf(source_table)
            if not join_col:
                self.logger.warning(
                    f"Could not determine join column for {source_table} aggregation"
                )
                continue

            pk_col = table_umf.primary_key[0] if table_umf.primary_key else "id"
            resolved_table = self._resolve_table_name(source_table)
            derived_cols = self._get_derived_columns_for_source(source_table)

            # Separate window vs GROUP BY specs
            window_specs = [s for s in specs if s.get("order_by")]
            non_window_specs = [s for s in specs if not s.get("order_by")]

            if window_specs:
                # Group by row_filter
                filter_groups: dict[str, list[dict[str, Any]]] = {}
                for spec in window_specs:
                    filter_key = (spec.get("row_filter") or "").strip()
                    filter_groups.setdefault(filter_key, []).append(spec)

                for filter_idx, (_filter_key, filter_specs) in enumerate(
                    sorted(filter_groups.items())
                ):
                    view_suffix = f"_{filter_idx + 1}" if len(filter_groups) > 1 else ""
                    agg_view_name = f"{source_table}_agg{view_suffix}"

                    section = self._generate_window_aggregation_view(
                        source_table=source_table,
                        specs=filter_specs,
                        join_col=join_col,
                        pk_col=pk_col,
                        resolved_table=resolved_table,
                        derived_cols=derived_cols,
                        view_name_suffix=view_suffix,
                    )
                    if section:
                        sections.append(section)
                        if source_col:
                            self._agg_view_source_columns[agg_view_name] = source_col
                        for spec in filter_specs:
                            col_name = spec["col_name"]
                            if col_name in self._pre_aggregated_columns:
                                for entry in self._pre_aggregated_columns[col_name]:
                                    if entry["source_table"] == source_table:
                                        entry["agg_view_name"] = agg_view_name
                                        entry["is_window_function"] = True
                        for spec in filter_specs:
                            for sel_col in spec.get("select_columns") or []:
                                if sel_col not in self._pre_aggregated_columns:
                                    self._pre_aggregated_columns[sel_col] = []
                                self._pre_aggregated_columns[sel_col].append(
                                    {
                                        "source_table": source_table,
                                        "agg_view_name": agg_view_name,
                                        "is_window_function": True,
                                    }
                                )

                if not non_window_specs:
                    continue

            # GROUP BY approach. When this source ALSO produced a window view, the
            # window view already claimed the bare ``{source_table}_agg`` name, so
            # the GROUP BY view must use a DISTINCT name -- otherwise the second
            # CREATE OR REPLACE clobbers the window view and the window-derived
            # columns (e.g. latest_claim_status) vanish from the agg join.
            group_by_specs = non_window_specs if window_specs else specs
            group_view_name = (
                f"{source_table}_agg_grouped" if window_specs else f"{source_table}_agg"
            )
            # Point the GROUP BY columns' pre-aggregation bookkeeping at the
            # distinct grouped view so the agg-view join references the right view.
            if window_specs:
                for spec in group_by_specs:
                    for entry in self._pre_aggregated_columns.get(spec["col_name"], []):
                        if entry["source_table"] == source_table and not entry.get(
                            "is_window_function"
                        ):
                            entry["agg_view_name"] = group_view_name
            agg_columns: list[str] = []
            for spec in group_by_specs:
                func = spec["function"]
                src_col = spec["source_column"]
                col_name = spec["col_name"]
                expression_raw = spec.get("expression", "")
                expression = self._substitute_template_vars(expression_raw)

                if self._is_complex_aggregate_expression(expression):
                    agg_columns.append(f"  {expression} AS {col_name}")
                elif func == "COUNT":
                    agg_columns.append(f"  COUNT(*) AS {col_name}")
                else:
                    agg_columns.append(f"  {func}({src_col}) AS {col_name}")

            from_clause = resolved_table
            has_alias = False

            if derived_cols:
                all_expressions = " ".join(
                    spec.get("expression", "") for spec in group_by_specs
                )
                referenced_derived = [
                    (col_n, expr, needs_except)
                    for col_n, (expr, needs_except) in sorted(derived_cols.items())
                    if col_n in all_expressions or col_n == join_col
                ]

                if referenced_derived:
                    except_col_names = [
                        col_n for col_n, _, ne in referenced_derived if ne
                    ]
                    derived_select = ",\n    ".join(
                        f"({expr}) AS {col_n}" for col_n, expr, _ in referenced_derived
                    )
                    except_clause = (
                        f" EXCEPT ({', '.join(except_col_names)})"
                        if except_col_names
                        else ""
                    )
                    from_clause = f"""(
  SELECT
    *{except_clause},
    {derived_select}
  FROM {resolved_table}
) src"""
                    has_alias = True

                    def _qualify_column_refs(
                        col_expr: str, derived_cols_list: list[tuple[str, str, bool]]
                    ) -> str:
                        qualified = col_expr
                        for cn, _, _ in derived_cols_list:
                            qualified = qualified.replace(f" {cn} ", f" src.{cn} ")
                            qualified = qualified.replace(f"({cn})", f"(src.{cn})")
                            qualified = qualified.replace(f" {cn}=", f" src.{cn}=")
                            qualified = qualified.replace(
                                f"WHEN {cn} ", f"WHEN src.{cn} "
                            )
                        return qualified

                    agg_columns = [
                        _qualify_column_refs(c, referenced_derived) for c in agg_columns
                    ]

            agg_columns_str = ",\n".join(agg_columns)

            # Check for join_via
            join_via_spec = None
            for spec in specs:
                if spec.get("join_via"):
                    join_via_spec = spec["join_via"]
                    break

            if join_via_spec:
                lookup_table = self._resolve_table_name(join_via_spec.lookup_table)
                from_clause_with_alias = (
                    from_clause if has_alias else f"{from_clause} src"
                )
                section = f"""-- ============================================================================
-- PRE-AGGREGATION: {source_table} aggregate columns (via {join_via_spec.lookup_table} lookup)
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW {group_view_name} AS
SELECT
  lookup.{join_via_spec.lookup_key} AS {pk_col},
{agg_columns_str}
FROM {from_clause_with_alias}
INNER JOIN {lookup_table} lookup
  ON src.{join_via_spec.target_key} = lookup.{join_via_spec.target_key}
GROUP BY lookup.{join_via_spec.lookup_key};"""
            else:
                col_prefix = "src." if has_alias else ""
                section = f"""-- ============================================================================
-- PRE-AGGREGATION: {source_table} aggregate columns
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW {group_view_name} AS
SELECT
  {col_prefix}{join_col} AS {pk_col},
{agg_columns_str}
FROM {from_clause}
GROUP BY {col_prefix}{join_col};"""
            sections.append(section)

            if source_col:
                self._agg_view_source_columns[group_view_name] = source_col

        return sections

    def _generate_window_aggregation_view(
        self,
        source_table: str,
        specs: list[dict[str, Any]],
        join_col: str,
        pk_col: str,
        resolved_table: str,
        derived_cols: dict[str, tuple[str, bool]] | None = None,
        view_name_suffix: str = "",
    ) -> str:
        """Generate a window-function based aggregation view."""
        window_specs = [s for s in specs if s.get("order_by")]
        if not window_specs:
            return ""

        first_spec = window_specs[0]
        order_by_cols = first_spec["order_by"]
        row_filter_raw = first_spec.get("row_filter")
        row_filter = (
            self._substitute_template_vars(row_filter_raw) if row_filter_raw else None
        )

        # Merge select_columns from all specs
        select_columns: list[str] = []
        seen_cols: set[str] = set()
        for spec in window_specs:
            for col in spec.get("select_columns") or []:
                if col not in seen_cols:
                    select_columns.append(col)
                    seen_cols.add(col)

        # Append DESC only when the order_by entry does not already carry an
        # explicit direction, so a column listed as ``service_date`` renders
        # ``service_date DESC`` while one already listed as ``service_date DESC``
        # (or ASC) is left untouched -- never ``service_date DESC DESC``. Pin
        # ``NULLS LAST`` so NULL ordering is identical across backends (DuckDB and
        # Spark default NULL placement diverges by direction), keeping the
        # "most recent non-null" window pick deterministic.
        order_by_clause = ", ".join(
            self._order_col_with_direction(col) for col in order_by_cols
        )

        # Bare order_by column names (direction stripped): these must be PROJECTED
        # into the ``filtered`` CTE so the ``ranked`` CTE's ORDER BY can resolve
        # them. They are then DROPPED from the view's final output (via an explicit
        # final projection) so only the declared output columns survive.
        order_by_bare = [self._order_col_bare(c) for c in order_by_cols]

        output_columns: list[str] = []
        # Names the view ultimately exposes, in order, starting with the join key.
        final_output_names: list[str] = [pk_col]
        produced_cols: set[str] = {pk_col}
        for spec in specs:
            col_name = spec["col_name"]
            src_col = spec["source_column"]
            if src_col and src_col != "*":
                src_col = self._substitute_template_vars(src_col)
                output_columns.append(f"  {src_col} AS {col_name}")
                if col_name not in produced_cols:
                    final_output_names.append(col_name)
                    produced_cols.add(col_name)

        for col in select_columns:
            output_columns.append(f"  {col}")
            if col not in produced_cols:
                final_output_names.append(col)
                produced_cols.add(col)

        # Project any order_by column not already produced (and not the partition
        # key) so the window ORDER BY can reference it. These are NOT added to
        # ``final_output_names`` -- they are scratch columns dropped by the final
        # explicit projection below.
        order_by_passthrough = [
            c for c in order_by_bare if c and c not in produced_cols and c != join_col
        ]
        for col in order_by_passthrough:
            output_columns.append(f"  {col}")
            produced_cols.add(col)

        # Build FROM clause with derived columns if needed
        from_clause = resolved_table
        col_prefix = ""

        if derived_cols:
            all_cols_to_check = [join_col, *order_by_cols, *select_columns]
            referenced_derived = [
                (col_name, expr, needs_except)
                for col_name, (expr, needs_except) in sorted(derived_cols.items())
                if any(col_name in col_ref for col_ref in all_cols_to_check)
                or col_name == join_col
            ]

            if referenced_derived:
                except_col_names = [
                    col_name
                    for col_name, _, needs_except in referenced_derived
                    if needs_except
                ]
                derived_select = ",\n    ".join(
                    f"({expr}) AS {col_name}"
                    for col_name, expr, _ in referenced_derived
                )
                except_clause = (
                    f" EXCEPT ({', '.join(except_col_names)})"
                    if except_col_names
                    else ""
                )
                from_clause = f"""(
  SELECT
    *{except_clause},
    {derived_select}
  FROM {resolved_table}
) src"""
                col_prefix = "src."

        # WHERE clause
        where_clause = ""
        if row_filter:
            qualified_filter = row_filter
            if col_prefix and derived_cols:
                for col_name in derived_cols:
                    qualified_filter = qualified_filter.replace(
                        f" {col_name} ", f" {col_prefix}{col_name} "
                    )
                    qualified_filter = qualified_filter.replace(
                        f"({col_name})", f"({col_prefix}{col_name})"
                    )
            where_clause = f"\n  WHERE {qualified_filter}"

        output_cols_str = (
            ",\n".join(output_columns)
            if output_columns
            else f"  {first_spec['source_column']} AS {first_spec['col_name']}"
        )

        # Final projection: list only the declared output columns explicitly. This
        # drops both ``rn`` and the order-by scratch columns WITHOUT a star-exclude,
        # which differs between dialects (DuckDB ``* EXCLUDE (a, b)`` vs Spark
        # ``* EXCEPT (a, b)``); an explicit column list is valid on both.
        final_projection = ",\n  ".join(final_output_names)

        agg_view_name = f"{source_table}_agg{view_name_suffix}"
        return f"""-- ============================================================================
-- PRE-AGGREGATION: {agg_view_name} (window function - max row with traceability)
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW {agg_view_name} AS
WITH filtered AS (
  SELECT
    {col_prefix}{join_col} AS {pk_col},
{output_cols_str}
  FROM {from_clause}{where_clause}
),
ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY {pk_col}
      ORDER BY {order_by_clause}
    ) AS rn
  FROM filtered
)
SELECT
  {final_projection}
FROM ranked
WHERE rn = 1;"""

    # ------------------------------------------------------------------
    # Join step generation
    # ------------------------------------------------------------------

    def _generate_join_step(
        self, step: int, join_info: dict[str, Any], prev_view: str
    ) -> str:
        """Dispatch to the appropriate join strategy handler."""
        strategy = join_info.get("strategy", "direct")
        if strategy != "direct" and join_info.get("alternative_joins"):
            self.logger.warning(
                f"alternative_joins on {join_info.get('target_table')} is only "
                f"consumed by direct joins (strategy is {strategy!r}) - using "
                "the primary join path only"
            )
        if strategy == "direct":
            return self._generate_direct_join(step, join_info, prev_view)
        if strategy == "pivot":
            return self._generate_pivot_join(step, join_info, prev_view)
        if strategy in ("first", "first_record"):
            return self._generate_first_record_join(step, join_info, prev_view)
        msg = f"Unknown join strategy: {strategy}"
        raise ValueError(msg)

    def _prepare_join_context(
        self, join_info: dict[str, Any]
    ) -> tuple[str, str, str, str, str, str, str, list[str]]:
        """Extract common join setup from a join_info dict.

        Returns:
            Tuple of (target_table, source_col, target_col, join_filter,
            resolved_table, table_alias, sanitized_alias, base_column_selections).
        """
        target_table = join_info["target_table"]
        source_col = join_info["source_column"]
        target_col = join_info["target_column"]

        join_via = join_info.get("join_via")
        if join_via:
            source_col = join_via["source_key"]
            target_col = join_via["target_key"]

        join_filter: str = join_info.get("join_filter") or ""
        resolved_table = self._resolve_table_name(target_table)

        table_instance = join_info.get("table_instance")
        table_alias = table_instance if table_instance else target_table
        sanitized_alias = self._sanitize_alias(table_alias)

        base_column_selections = [
            f"base.{col}" for col in sorted(self._accumulated_columns.keys())
        ]

        return (
            target_table,
            source_col,
            target_col,
            join_filter,
            resolved_table,
            table_alias,
            sanitized_alias,
            base_column_selections,
        )

    def _generate_direct_join(
        self, step: int, join_info: dict[str, Any], prev_view: str
    ) -> str:
        """Generate SQL for a direct LEFT/INNER JOIN."""
        (
            target_table,
            source_col,
            target_col,
            join_filter,
            resolved_table,
            table_alias,
            sanitized_alias,
            base_column_selections,
        ) = self._prepare_join_context(join_info)
        cardinality = join_info["cardinality"].get("notation", "1:1")
        table_instance = join_info.get("table_instance")

        target_columns = self._get_table_columns(target_table)

        required_cols = self._get_required_columns_for_table(target_table)
        if required_cols:
            target_columns = [
                col
                for col in target_columns
                if col in required_cols or col.startswith("meta_")
            ]

        alternative_joins = join_info.get("alternative_joins") or []
        if alternative_joins:
            return self._generate_direct_union_join(
                step,
                join_info,
                prev_view,
                target_table=target_table,
                source_col=source_col,
                target_col=target_col,
                join_filter=join_filter,
                resolved_table=resolved_table,
                table_alias=table_alias,
                sanitized_alias=sanitized_alias,
                base_column_selections=base_column_selections,
                target_columns=target_columns,
                alternative_joins=alternative_joins,
            )

        target_column_selections: list[str] = []
        for col in target_columns:
            alias = f"{sanitized_alias}__{col}"
            target_column_selections.append(f"target.{col} AS {alias}")
            self._accumulated_columns[alias] = sanitized_alias

        all_selections = base_column_selections + target_column_selections
        column_list = ",\n  ".join(all_selections)

        join_type = join_info.get("join_type", "left").upper()
        join_clause = f"{join_type} JOIN"

        step_label = (
            f"{target_table} as {table_alias}" if table_instance else target_table
        )

        lookup_join = join_info.get("lookup_join")
        if lookup_join:
            # Two-hop: base -> bridge -> target. The bridge is joined on the
            # base's source_key, then the target on the bridge's target key.
            bridge_table = self._resolve_table_name(lookup_join.bridge_table)
            bridge_alias = f"{sanitized_alias}_bridge"
            on_clause = f"target.{target_col} = {bridge_alias}.{lookup_join.bridge_target_key}"
            if join_filter:
                on_clause += f" AND {self._rewrite_join_filter(join_filter, target_table)}"
            return f"""-- ============================================================================
-- STEP {step}: Join {step_label} (Two-Hop via {lookup_join.bridge_table} - {cardinality})
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW disposition_step_{step} AS
SELECT
  {column_list}
FROM {prev_view} base
{join_clause} {bridge_table} {bridge_alias}
  ON base.{lookup_join.source_key} = {bridge_alias}.{lookup_join.bridge_source_key}
{join_clause} {resolved_table} target
  ON {on_clause};"""

        source_expression = join_info.get("source_expression")
        target_expression = join_info.get("target_expression")
        left_key = (
            self._rewrite_join_filter(
                source_expression, target_table, alias="base",
                columns=list(self._accumulated_columns),
            )
            if source_expression
            else f"base.{source_col}"
        )
        right_key = (
            self._rewrite_join_filter(target_expression, target_table)
            if target_expression
            else f"target.{target_col}"
        )
        on_clause = f"{left_key} = {right_key}"
        if join_filter:
            rewritten_filter = self._rewrite_join_filter(join_filter, target_table)
            on_clause += f" AND {rewritten_filter}"

        return f"""-- ============================================================================
-- STEP {step}: Join {step_label} (Direct Join - {cardinality})
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW disposition_step_{step} AS
SELECT
  {column_list}
FROM {prev_view} base
{join_clause} {resolved_table} target
  ON {on_clause};"""

    def _generate_direct_union_join(
        self,
        step: int,
        join_info: dict[str, Any],
        prev_view: str,
        *,
        target_table: str,
        source_col: str,
        target_col: str,
        join_filter: str,
        resolved_table: str,
        table_alias: str,
        sanitized_alias: str,
        base_column_selections: list[str],
        target_columns: list[str],
        alternative_joins: list[dict[str, str]],
    ) -> str:
        """Direct join with ``alternative_joins`` as a UNION-of-joins.

        One inner-join branch per join path (primary = priority 1, alternatives
        2..n in declared order), combined with UNION and deduplicated per base
        key by branch priority, then LEFT/INNER joined back null-safely. This
        avoids emitting the OR-join form that Spark plans as a
        BroadcastNestedLoopJoin.

        ``base_keys`` scans ``disposition_base`` when every join key is
        base-sourced: scanning the (lazy) previous step view would re-evaluate
        the whole join chain per branch. Falls back to *prev_view* when a key
        is not base-sourced.

        Portability: the null-safe join-back is spelled with an explicit
        ``(a = b OR (a IS NULL AND b IS NULL))`` expansion (no ``<=>``), the
        dedup projection is an explicit column list (no ``* EXCEPT``), and no
        engine hints are emitted -- Spark-side broadcast hinting belongs at a
        renderer seam if ever needed.
        """
        cardinality = join_info["cardinality"].get("notation", "1:1")
        table_instance = join_info.get("table_instance")

        # Join branches: primary first, then alternatives in declared order
        branch_conditions: list[tuple[str, str]] = [(source_col, target_col)]
        for alt in alternative_joins:
            branch_conditions.append((alt["source_column"], alt["target_column"]))

        # Base-side keys, order-preserving dedupe
        keys: list[str] = []
        for src, _ in branch_conditions:
            if src not in keys:
                keys.append(src)

        # Scan disposition_base only when every key is base-sourced
        if all(self._accumulated_columns.get(k) == "base" for k in keys):
            keys_view = "disposition_base"
        else:
            keys_view = prev_view
            self.logger.warning(
                f"alternative_joins for {target_table}: join keys {keys} are not "
                f"all base-sourced; base_keys scans {prev_view} (may re-evaluate "
                "the join chain)"
            )

        target_aliases: list[str] = []
        for col in target_columns:
            alias = f"{sanitized_alias}__{col}"
            target_aliases.append(alias)
            self._accumulated_columns[alias] = sanitized_alias

        rewritten_filter = (
            self._rewrite_join_filter(join_filter, target_table) if join_filter else ""
        )

        match_keys = [f"__match_key_{k}" for k in keys]
        branch_selects: list[str] = []
        for priority, (src, tgt) in enumerate(branch_conditions, 1):
            key_selections = [
                f"    b.{k} AS {mk}" for k, mk in zip(keys, match_keys, strict=True)
            ]
            col_selections = [
                f"    target.{col} AS {alias}"
                for col, alias in zip(target_columns, target_aliases, strict=True)
            ]
            on_clause = f"b.{src} = target.{tgt}"
            if rewritten_filter:
                on_clause += f" AND {rewritten_filter}"
            select_body = ",\n".join(
                [
                    *key_selections,
                    *col_selections,
                    f"    {priority} AS __branch_priority",
                ]
            )
            branch_selects.append(
                f"  SELECT\n{select_body}\n  FROM base_keys b\n"
                f"  JOIN {resolved_table} target\n    ON {on_clause}"
            )

        branches_str = "\n  UNION\n".join(branch_selects)

        keys_list = ",\n    ".join(keys)
        partition = ", ".join(match_keys)
        order_tiebreak = "".join(
            f",\n        {alias} ASC NULLS LAST" for alias in sorted(target_aliases)
        )
        dedup_projection = ",\n    ".join([*match_keys, *target_aliases])

        joined_selections = [f"  m.{alias}" for alias in target_aliases]
        all_selections = [f"  {s}" for s in base_column_selections] + joined_selections
        column_list = ",\n".join(all_selections)

        null_safe_conds = "\n    AND ".join(
            f"(base.{k} = m.{mk} OR (base.{k} IS NULL AND m.{mk} IS NULL))"
            for k, mk in zip(keys, match_keys, strict=True)
        )

        step_label = (
            f"{target_table} as {table_alias}" if table_instance else target_table
        )
        join_type = join_info.get("join_type", "left").upper()
        join_clause = f"{join_type} JOIN"
        n_paths = len(branch_conditions)

        return f"""-- ============================================================================
-- STEP {step}: Join {step_label} (Direct Join - {cardinality}, {n_paths} alternative join paths)
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW disposition_step_{step} AS
WITH base_keys AS (
  SELECT DISTINCT
    {keys_list}
  FROM {keys_view}
),
matches AS (
{branches_str}
),
matches_ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY {partition}
      ORDER BY __branch_priority{order_tiebreak}
    ) AS __rn
  FROM matches
),
matches_deduped AS (
  SELECT
    {dedup_projection}
  FROM matches_ranked
  WHERE __rn = 1
)
SELECT
{column_list}
FROM {prev_view} base
{join_clause} matches_deduped m
  ON {null_safe_conds};"""

    def _generate_pivot_join(
        self, step: int, join_info: dict[str, Any], prev_view: str
    ) -> str:
        """Generate SQL for a pivot join."""
        target_table = join_info["target_table"]
        source_col = join_info["source_column"]
        target_col = join_info["target_column"]
        cardinality = join_info["cardinality"].get("notation", "1:N")
        join_filter = join_info.get("join_filter")

        resolved_table = self._resolve_table_name(target_table)

        pivot_spec = join_info.get("pivot", {})
        value_column = pivot_spec.get("value_column", "Value")
        prefix = pivot_spec.get("prefix", "Value")
        max_records = int(pivot_spec.get("max_records", 6))

        where_clause = ""
        if join_filter:
            rewritten_filter = self._rewrite_join_filter(
                join_filter, target_table, alias=target_table
            )
            where_clause = f"\n  WHERE {rewritten_filter}"

        pivot_agg_selections: list[str] = []
        pivot_column_names: list[str] = []
        for i in range(1, max_records + 1):
            target_col_name = f"{prefix}{i}"
            col_alias = f"{target_table}__{target_col_name}"
            pivot_agg_selections.append(
                f"MAX(CASE WHEN rn = {i} THEN {value_column} END) AS {col_alias}"
            )
            pivot_column_names.append(col_alias)
            self._accumulated_columns[col_alias] = target_table
            # Record so the final-assembly mapping points the target column
            # (e.g. diagnosis_1) at the pivoted output (diagnosis__diagnosis_1)
            # rather than the raw source column.
            self._pivot_column_map[(target_table, target_col_name)] = col_alias

        pivot_agg_str = ",\n  ".join(pivot_agg_selections)

        base_column_selections = [
            f"base.{col}"
            for col in sorted(self._accumulated_columns.keys())
            if col not in pivot_column_names
        ]
        pivot_column_selections = [f"pivoted.{col}" for col in pivot_column_names]

        all_selections = base_column_selections + pivot_column_selections
        column_list = ",\n  ".join(all_selections)

        filter_note = f" (Filtered: {join_filter})" if join_filter else ""

        return f"""-- ============================================================================
-- STEP {step}: Join {target_table} (Pivot Join - {cardinality}){filter_note}
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW {target_table}_pivoted AS
WITH ranked AS (
  SELECT
    {target_col},
    {value_column},
    ROW_NUMBER() OVER (PARTITION BY {target_col} ORDER BY {value_column} ASC NULLS LAST) as rn
  FROM {resolved_table}{where_clause}
)
SELECT
  {target_col},
  {pivot_agg_str}
FROM ranked
WHERE rn <= {max_records}
GROUP BY {target_col};

CREATE OR REPLACE TEMPORARY VIEW disposition_step_{step} AS
SELECT
  {column_list}
FROM {prev_view} base
LEFT JOIN {target_table}_pivoted pivoted
  ON base.{source_col} = pivoted.{target_col};"""

    def _generate_first_record_join(
        self, step: int, join_info: dict[str, Any], prev_view: str
    ) -> str:
        """Generate SQL for a first-record join (ROW_NUMBER partitioned dedup)."""
        (
            target_table,
            source_col,
            target_col,
            join_filter,
            resolved_table,
            table_alias,
            sanitized_alias,
            base_column_selections,
        ) = self._prepare_join_context(join_info)
        cardinality = join_info["cardinality"].get("notation", "1:0..N")
        table_instance = join_info.get("table_instance")

        where_clause = ""
        if join_filter:
            rewritten_filter = self._rewrite_join_filter(
                join_filter, target_table, alias=target_table
            )
            where_clause = f"\n  WHERE {rewritten_filter}"

        target_columns = self._get_table_columns(target_table)

        required_cols = self._get_required_columns_for_table(target_table)
        if required_cols:
            filtered_columns = [
                col
                for col in target_columns
                if col in required_cols or col.startswith("meta_")
            ]
        else:
            filtered_columns = target_columns

        # Determine the SEMANTIC ordering column(s): the heuristic picks a
        # type/name column (or the first non-key column) as the "first record"
        # discriminator. This alone does NOT impose a total order over the rows of
        # a partition, so when two rows of a key tie on it ROW_NUMBER() is
        # non-deterministic and DuckDB and Spark can pick different "first" rows.
        order_heuristic: list[str] = []
        if target_columns:
            type_cols = [col for col in target_columns if "type" in col.lower()]
            name_cols = [
                col
                for col in target_columns
                if any(x in col.lower() for x in ["name", "lastname", "last_name"])
            ]
            if type_cols and name_cols:
                order_heuristic = [type_cols[0], name_cols[0]]
            elif type_cols:
                order_heuristic = [type_cols[0]]
            elif name_cols:
                order_heuristic = [name_cols[0]]
            else:
                order_col = next(
                    (
                        col
                        for col in target_columns
                        if col.lower() != target_col.lower()
                    ),
                    target_col,
                )
                order_heuristic = [order_col]
        if not order_heuristic:
            order_heuristic = [target_col]

        # Append a STABLE TIEBREAK so the ranking is a total order and therefore
        # deterministic on every backend: the partition key followed by every
        # remaining target column in declared order, so no two distinct rows of a
        # key can remain tied. Each term is emitted ``col ASC NULLS LAST``: the
        # direction + null-placement are made EXPLICIT because the two backends
        # disagree on the default ASC null placement (DuckDB sorts NULLS LAST,
        # Spark NULLS FIRST), so a NULL in any tiebreak column would otherwise pick a
        # different "first" row per backend. ``ASC NULLS LAST`` is accepted verbatim
        # by both DuckDB and Spark, making the rank dialect-identical.
        ordered_seen: set[str] = set()
        order_cols: list[str] = []
        for col in [*order_heuristic, target_col, *target_columns]:
            key = col.lower()
            if key in ordered_seen:
                continue
            ordered_seen.add(key)
            order_cols.append(col)
        order_columns = ", ".join(f"{col} ASC NULLS LAST" for col in order_cols)

        # Build subquery column set
        subquery_columns = set(filtered_columns)
        subquery_columns.add(target_col)
        for order_col_item in order_cols:
            subquery_columns.add(order_col_item.strip())

        # Check for derived columns
        derived_cols = self._get_derived_columns_for_source(target_table)
        derived_in_subquery: list[tuple[str, str]] = []
        if derived_cols:
            for col_name in list(subquery_columns):
                if col_name in derived_cols:
                    expr, _ = derived_cols[col_name]
                    derived_in_subquery.append((col_name, expr))
                    subquery_columns.discard(col_name)

        subquery_column_list = ",\n    ".join(sorted(subquery_columns))

        target_column_selections: list[str] = []
        for col in filtered_columns:
            alias = f"{sanitized_alias}__{col}"
            target_column_selections.append(f"target.{col} AS {alias}")
            self._accumulated_columns[alias] = sanitized_alias

        all_selections = base_column_selections + target_column_selections
        column_list = ",\n  ".join(all_selections)

        step_label = (
            f"{target_table} as {table_alias}" if table_instance else target_table
        )
        filter_note = f" (Filtered: {join_filter})" if join_filter else ""

        # Build FROM clause with derived columns
        if derived_in_subquery:
            derived_select = ",\n    ".join(
                f"({expr}) AS {col_name}" for col_name, expr in derived_in_subquery
            )
            full_subquery_column_list = subquery_column_list
            if subquery_column_list:
                full_subquery_column_list += ",\n    " + ",\n    ".join(
                    col_name for col_name, _ in derived_in_subquery
                )
            else:
                full_subquery_column_list = ",\n    ".join(
                    col_name for col_name, _ in derived_in_subquery
                )
            inner_from = f"""(
    SELECT
      *,
      {derived_select}
    FROM {resolved_table}
  ) src"""
        else:
            full_subquery_column_list = subquery_column_list
            inner_from = resolved_table

        return f"""-- ============================================================================
-- STEP {step}: Join {step_label} (First Record - {cardinality}){filter_note}
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW {table_alias}_first AS
SELECT
  {full_subquery_column_list}
FROM (
  SELECT
    {full_subquery_column_list},
    ROW_NUMBER() OVER (PARTITION BY {target_col} ORDER BY {order_columns}) as rn
  FROM {inner_from}{where_clause}
) ranked
WHERE rn = 1;

CREATE OR REPLACE TEMPORARY VIEW disposition_step_{step} AS
SELECT
  {column_list}
FROM {prev_view} base
LEFT JOIN {table_alias}_first target
  ON base.{source_col} = target.{target_col};"""

    # ------------------------------------------------------------------
    # Aggregation view join
    # ------------------------------------------------------------------

    def _generate_agg_view_join(
        self,
        step: int,
        agg_view_name: str,
        col_names: list[str],
        prev_view: str,
        table_umf: UMF,
    ) -> str:
        """Generate SQL for joining a pre-aggregation view."""
        pk_col = table_umf.primary_key[0] if table_umf.primary_key else "id"

        base_column_selections = [
            f"base.{col}" for col in sorted(self._accumulated_columns.keys())
        ]

        agg_column_selections: list[str] = []
        for col_name in col_names:
            alias = f"{agg_view_name}__{col_name}"
            agg_column_selections.append(f"agg.{col_name} AS {alias}")
            self._accumulated_columns[alias] = agg_view_name

        all_selections = base_column_selections + agg_column_selections
        column_list = ",\n  ".join(all_selections)

        # Check if source column is derived
        source_col = self._agg_view_source_columns.get(agg_view_name)
        join_key_expr = f"base.{pk_col}"

        if source_col and source_col != pk_col:
            derived_expr = self._get_derived_column_expression(table_umf, source_col)
            if derived_expr:
                join_key_expr = derived_expr.replace(pk_col, f"base.{pk_col}")

        return f"""-- ============================================================================
-- STEP {step}: Join {agg_view_name} (Pre-aggregated Data)
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW disposition_step_{step} AS
SELECT
  {column_list}
FROM {prev_view} base
LEFT JOIN {agg_view_name} agg
  ON {join_key_expr} = agg.{pk_col};"""

    # ------------------------------------------------------------------
    # Expression rewriting
    # ------------------------------------------------------------------

    def _rewrite_join_filter(
        self,
        join_filter: str,
        target_table: str,
        alias: str = "target",
        columns: list[str] | None = None,
    ) -> str:
        """Rewrite bare column references in a join filter to use the given alias.

        Only *code* spans are rewritten: single-quoted SQL string literals are
        preserved verbatim, so a column-named token that happens to appear INSIDE
        a literal (e.g. ``region`` inside ``'no region here'``) is never qualified.
        Rewriting tokens inside a literal would silently corrupt the constant on
        BOTH backends (a correctness bug, not merely a dialect divergence). The
        rewrite is also a single left-to-right pass over the code spans (longest
        column name first within the pass) so an alias inserted for one column can
        never be re-matched as a bare token for another.
        """
        table_cols = sorted(
            set(columns if columns is not None else self._get_table_columns(target_table)),
            key=len,
            reverse=True,
        )
        if not table_cols:
            return join_filter

        col_alt = "|".join(re.escape(str(c)) for c in table_cols)
        token_re = re.compile(rf"(?<![.\w])({col_alt})(?![\w])")

        def _rewrite_code(span: str) -> str:
            return token_re.sub(rf"{alias}.\1", span)

        # Split the filter into alternating code / single-quoted-string spans. A
        # SQL string literal is ``'...'`` with an embedded quote escaped as ``''``;
        # the capture group keeps the literal so we can pass it through untouched.
        parts = re.split(r"('(?:[^']|'')*')", join_filter)
        out: list[str] = []
        for i, part in enumerate(parts):
            # Odd indices are the captured quoted literals -> preserve verbatim.
            out.append(part if i % 2 else _rewrite_code(part))
        return "".join(out)

    def _rewrite_expression_for_alias(
        self, expression: str, alias_prefix: str, table_name: str
    ) -> str:
        """Rewrite bare column references in *expression* with *alias_prefix*."""
        table_cols = set(self._get_table_columns(table_name))

        def _token_repl(m: re.Match[str]) -> str:
            tok = m.group(1)
            if tok in table_cols:
                return f"{alias_prefix}{tok}"
            return tok

        return _IDENTIFIER_RE.sub(_token_repl, expression)

    # ------------------------------------------------------------------
    # Final assembly
    # ------------------------------------------------------------------

    def _generate_final_assembly(
        self,
        table_name: str,
        table_umf: UMF,
        final_view: str,
        base_table: str | None,
    ) -> str:
        """Generate the final SELECT with column derivations and survivorship."""
        column_mappings: list[str] = []

        for col_def in _output_ordered_columns(table_umf.columns):
            col_name = col_def.name
            derivation = col_def.derivation
            data_type = (col_def.data_type or "STRING").upper()
            column_default = col_def.default

            if derivation:
                mapping = self._generate_column_mapping(
                    col_name, derivation, base_table, data_type, column_default
                )
                column_mappings.append(f"  {mapping} AS {col_name}")
            elif column_default is not None:
                spark_type = self._get_spark_type(data_type)
                default_literal = self._format_default_value_literal(column_default)
                column_mappings.append(
                    f"  CAST({default_literal} AS {spark_type}) AS {col_name}"
                )
            else:
                spark_type = self._get_spark_type(data_type)
                column_mappings.append(f"  CAST(NULL AS {spark_type}) AS {col_name}")

        # Provenance passthrough
        if self._join_sequence:
            provenance_cols = self._get_joined_provenance_columns(self._join_sequence)
            for table_alias, prov_col in provenance_cols:
                sanitized = self._sanitize_alias(table_alias)
                prefixed = f"{sanitized}__{prov_col}"
                column_mappings.append(f"  base.{prefixed} AS {prefixed}")

        column_mappings_str = ",\n".join(column_mappings)

        # Optional final-assembly filter/dedup (metadata.final_filter /
        # metadata.final_dedup). The inner select is wrapped so the WHERE can
        # reference derived column aliases (same-level WHERE cannot).
        metadata = table_umf.metadata
        final_filter = None
        dedup_select = None
        if metadata:
            if metadata.final_filter:
                final_filter = self._substitute_template_vars(
                    metadata.final_filter.strip()
                )
            dedup_select = self._final_dedup_select_clause(metadata)

        if not base_table:
            inner = f"SELECT\n{column_mappings_str}"
            header = f"""-- ============================================================================
-- FINAL ASSEMBLY: {table_name} (Synthetic Table)
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW {table_name} AS"""
        else:
            inner = f"SELECT\n{column_mappings_str}\nFROM {final_view} base"
            header = f"""-- ============================================================================
-- FINAL ASSEMBLY: {table_name} with Column Derivations
-- ============================================================================
CREATE OR REPLACE TEMPORARY VIEW {table_name} AS"""

        if final_filter is None and dedup_select is None:
            return f"{header}\n{inner};"

        outer_select = dedup_select or "SELECT *"
        where_clause = f"\nWHERE {final_filter}" if final_filter else ""
        return f"""{header}
{outer_select}
FROM (
{inner}
) _final{where_clause};"""

    @staticmethod
    def _final_dedup_select_clause(metadata: Any) -> str | None:
        """Return the outer SELECT clause for ``metadata.final_dedup``."""
        if metadata.final_dedup == "distinct":
            return "SELECT DISTINCT *"
        return None

    def _get_joined_provenance_columns(
        self, join_sequence: list[dict[str, Any]]
    ) -> list[tuple[str, str]]:
        """Collect meta_* columns from all joined tables."""
        provenance_cols: list[tuple[str, str]] = []
        for join_info in join_sequence:
            target_table = join_info["target_table"]
            table_alias = join_info.get("table_instance", target_table)
            for col in self._get_table_columns(target_table):
                if col.startswith("meta_"):
                    provenance_cols.append((table_alias, col))
        return provenance_cols

    # ------------------------------------------------------------------
    # Column mapping / derivation
    # ------------------------------------------------------------------

    def _generate_column_mapping(
        self,
        col_name: str,
        derivation: Any,
        base_table: str | None,
        data_type: str = "STRING",
        column_default: str | float | bool | None = None,
    ) -> str:
        """Generate a SQL expression for a single column derivation."""
        candidates = derivation.candidates if derivation else []
        survivorship = derivation.survivorship if derivation else None
        survivorship_default = survivorship.default_value if survivorship else None
        default_value = (
            survivorship_default if survivorship_default is not None else column_default
        )
        strategy = survivorship.strategy if survivorship else None

        derivation_strategy = derivation.strategy if derivation else None

        if derivation_strategy in ("primary_key", "base_column"):
            return f"base.{col_name}"

        # Columns projected by the union_branches base view already carry their
        # branch-specific candidate mapping under the TARGET name — reference
        # them directly (must run before the base-table candidate path, which
        # would emit the SOURCE column name).
        if col_name in self._union_branch_columns:
            return f"base.{col_name}"

        if not candidates:
            if default_value is not None:
                default_literal = self._format_default_value_literal(
                    default_value, data_type
                )
                spark_type = self._get_spark_type(data_type)
                return f"CAST({default_literal} AS {spark_type})"
            spark_type = self._get_spark_type(data_type)
            return f"CAST(NULL AS {spark_type})"

        # Single candidate
        if len(candidates) == 1:
            single_expr = self._generate_single_candidate_mapping(
                candidates[0], base_table, col_name
            )
            if default_value is not None:
                default_literal = self._format_default_value_literal(
                    default_value, data_type
                )
                return f"COALESCE({single_expr}, {default_literal})"
            return single_expr

        # max_across_sources
        if strategy == "max_across_sources":
            return self._generate_greatest_mapping(
                candidates, base_table, col_name, default_value, data_type
            )

        # Multiple candidates -> COALESCE
        return self._generate_multiple_candidate_mapping(
            candidates, base_table, col_name, default_value, data_type
        )

    def _generate_single_candidate_mapping(
        self,
        candidate: Any,
        base_table: str | None,
        target_col_name: str = "",
    ) -> str:
        """Generate SQL mapping for a single derivation candidate."""
        table = candidate.table
        source_column = candidate.column or ""
        expression_raw = candidate.expression
        expression = (
            self._substitute_template_vars(expression_raw) if expression_raw else None
        )

        # Check pre-aggregated columns
        col_to_check = (
            target_col_name
            if target_col_name in self._pre_aggregated_columns
            else source_column
        )
        if col_to_check in self._pre_aggregated_columns:
            agg_sources = self._pre_aggregated_columns[col_to_check]
            for agg_info in agg_sources:
                if table == agg_info["source_table"]:
                    agg_view_name = agg_info["agg_view_name"]
                    if self._expression_has_aggregate(expression):
                        return f"base.{agg_view_name}__{col_to_check}"
                    if expression:
                        return self._rewrite_expression_for_alias(
                            expression, f"base.{agg_view_name}__", table
                        )
                    return f"base.{agg_view_name}__{col_to_check}"

        column = source_column
        table_instance = candidate.table_instance

        if not table:
            return "NULL"

        # Pivoted source: the pivot CTE emits one numbered column per target
        # column (e.g. diagnosis_1 -> diagnosis__diagnosis_1), so a target column
        # derived from a pivot source references that pivoted output, NOT the raw
        # source column (which the pivot CTE does not project).
        pivoted_alias = self._pivot_column_map.get((table, target_col_name))
        if pivoted_alias is not None:
            return f"base.{pivoted_alias}"

        table_alias = self._sanitize_alias(table_instance if table_instance else table)

        source_expr = expression if expression else column
        if not source_expr:
            return "NULL"

        # Base table column
        if base_table and table == base_table:
            if expression:
                return self._rewrite_expression_for_alias(source_expr, "base.", table)
            return f"base.{column}"

        # Expression with alias rewriting
        if expression:
            agg_view_alias = None
            for agg_sources in self._pre_aggregated_columns.values():
                for agg_info in agg_sources:
                    if agg_info["source_table"] == table:
                        agg_view_alias = agg_info["agg_view_name"]
                        break
                if agg_view_alias:
                    break
            alias_prefix = (
                f"base.{agg_view_alias}__"
                if agg_view_alias
                else f"base.{table_alias}__"
            )
            return self._rewrite_expression_for_alias(source_expr, alias_prefix, table)

        # Simple column reference
        return f"base.{table_alias}__{column}"

    def _generate_multiple_candidate_mapping(
        self,
        candidates: list[Any],
        base_table: str | None,
        target_col_name: str = "",
        default_value: str | float | bool | None = None,
        data_type: str = "STRING",
    ) -> str:
        """Generate COALESCE mapping for multiple candidates."""
        is_string_type = data_type.upper() in (
            "STRING",
            "STRINGTYPE",
            "VARCHAR",
            "CHAR",
            "TEXT",
        )

        coalesce_parts: list[str] = []
        for candidate in sorted(
            candidates, key=lambda x: x.priority if x.priority is not None else 999
        ):
            part = self._generate_single_candidate_mapping(
                candidate, base_table, target_col_name
            )
            if part and part.strip():
                if is_string_type and part != "NULL":
                    part = f"NULLIF({part}, '')"
                coalesce_parts.append(part)

        if default_value is not None:
            coalesce_parts.append(
                self._format_default_value_literal(default_value, data_type)
            )

        if len(coalesce_parts) == 0:
            return "NULL"
        if len(coalesce_parts) == 1:
            return coalesce_parts[0]

        if len(coalesce_parts) <= 3:
            return f"COALESCE({', '.join(coalesce_parts)})"
        parts_formatted = ",\n    ".join(coalesce_parts)
        return f"COALESCE(\n    {parts_formatted}\n)"

    def _generate_greatest_mapping(
        self,
        candidates: list[Any],
        base_table: str | None,
        target_col_name: str = "",
        default_value: str | float | bool | None = None,
        data_type: str = "STRING",
    ) -> str:
        """Generate GREATEST mapping for max_across_sources strategy."""
        greatest_parts: list[str] = []
        for candidate in candidates:
            part = self._generate_single_candidate_mapping(
                candidate, base_table, target_col_name
            )
            if part and part.strip() and part != "NULL":
                greatest_parts.append(part)

        if len(greatest_parts) == 0:
            if default_value is not None:
                return self._format_default_value_literal(default_value, data_type)
            return "NULL"

        if len(greatest_parts) == 1:
            if default_value is not None:
                default_literal = self._format_default_value_literal(
                    default_value, data_type
                )
                return f"COALESCE({greatest_parts[0]}, {default_literal})"
            return greatest_parts[0]

        parts_formatted = ", ".join(greatest_parts)
        if default_value is not None:
            default_literal = self._format_default_value_literal(
                default_value, data_type
            )
            return f"COALESCE(GREATEST({parts_formatted}), {default_literal})"
        return f"GREATEST({parts_formatted})"

    # ------------------------------------------------------------------
    # Type / literal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_spark_type(data_type: str) -> str:
        """Convert a UMF data type to a SQL type string."""
        return _SPARK_TYPE_MAP.get(data_type.upper(), "STRING")

    @staticmethod
    def _format_default_value_literal(
        default_value: str | float | bool | None, data_type: str = "STRING"
    ) -> str:
        """Format a default value as a SQL literal."""
        if data_type.upper() in ("STRING", "TEXT", "CHAR", "STRINGTYPE"):
            if default_value is None or default_value == "":
                return "''"
            if isinstance(default_value, (int, float)):
                return f"'{default_value!s}'"

        if default_value is None:
            return "NULL"
        if isinstance(default_value, bool):
            return "TRUE" if default_value else "FALSE"
        if isinstance(default_value, (int, float)):
            return str(default_value)
        escaped = str(default_value).replace("'", "''")
        return f"'{escaped}'"

    # ------------------------------------------------------------------
    # Aggregate expression detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_complex_aggregate_expression(expression: str) -> bool:
        """Check if an expression is complex and should be used verbatim."""
        if not expression:
            return False
        expr_upper = expression.upper()

        if "CASE" in expr_upper:
            return True

        agg_count = sum(1 for agg in _AGGREGATE_FUNCTIONS if agg in expr_upper)
        if agg_count > 1:
            return True

        if "MAX_BY(" in expr_upper:
            return True

        format_funcs = ["DATE_FORMAT(", "CAST(", "COALESCE(", "CONCAT(", "IFNULL("]
        has_format_func = any(func in expr_upper for func in format_funcs)
        has_aggregate = any(agg in expr_upper for agg in _AGGREGATE_FUNCTIONS)
        return bool(has_format_func and has_aggregate)

    @staticmethod
    def _expression_has_aggregate(expression: str | None) -> bool:
        """Check if expression contains SQL aggregate functions."""
        if not expression:
            return False
        expr_upper = expression.upper()
        return any(agg in expr_upper for agg in _AGGREGATE_FUNCTIONS)


# ------------------------------------------------------------------
# Convenience function
# ------------------------------------------------------------------


def generate_sql_plan(
    table_umf: UMF,
    related_umfs: dict[str, UMF],
    *,
    template_vars: dict[str, str] | None = None,
    table_resolver: Callable[[str], str] | None = None,
    table_renderer: TableRenderer | None = None,
    mode: Literal["views", "cte"] = "views",
) -> str:
    """Generate a SQL execution plan for a single target table.

    This is a convenience wrapper around :class:`SQLPlanGenerator`.

    Args:
        table_umf: UMF metadata for the target table.
        related_umfs: Dict mapping table names to UMF models for source tables.
        template_vars: Optional template variable substitutions.
        table_resolver: Optional callable to resolve table names.
        table_renderer: Optional :class:`TableRenderer` seam (mutually exclusive
            with *table_resolver*); inject a ``DbtRefRenderer`` to emit refs.
        mode: Output format — ``"views"`` (default) or ``"cte"``.

    Returns:
        Multi-statement SQL string (views mode) or single CTE statement
        (cte mode).

    """
    generator = SQLPlanGenerator(
        template_vars=template_vars,
        table_resolver=table_resolver,
        table_renderer=table_renderer,
    )
    return generator.generate_for_table(table_umf, related_umfs, mode=mode)


__all__ = ["SQLPlanGenerator", "generate_sql_plan"]
