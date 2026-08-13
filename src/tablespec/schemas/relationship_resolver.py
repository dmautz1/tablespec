"""Relationship resolver that infers join sequence and strategies from UMF metadata.

This module computes a metadata-driven plan for SQL generation without hardcoded
table/column names. It analyzes UMF relationships and target derivations to produce
a normalized configuration consumed by schema generators.

The resolver is domain-agnostic: it uses UMF primary_key fields, hub_score metadata,
and column name matching to infer joins rather than relying on hardcoded patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from tablespec.models.umf import JoinViaSpec, UMF

logger = logging.getLogger(__name__)


@dataclass
class PivotSpec:
    """Specification for pivoting a one-to-many relationship into numbered columns."""

    key_column: str
    value_column: str
    prefix: str
    max_records: int


@dataclass
class JoinInfo:
    """Normalized join descriptor for a single source table."""

    target_table: str
    source_column: str
    target_column: str
    strategy: Literal["direct", "first_record", "pivot"]
    partition_by: list[str] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)
    pivot: PivotSpec | None = None
    cardinality: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    table_instance: str | None = None  # Unique alias for filtered joins
    join_filter: str | None = None  # SQL WHERE conditions for conditional joins
    join_via: JoinViaSpec | None = None  # Custom join keys
    join_type: Literal["left", "inner"] = "left"
    # Alternative join paths (source_column/target_column dicts, priority order)
    alternative_joins: list[dict[str, str]] = field(default_factory=list)
    # Expression join keys (SQL over source/target columns; direct joins only)
    source_expression: str | None = None
    target_expression: str | None = None


@dataclass
class ResolvedPlan:
    """Normalized plan returned by ``RelationshipResolver.resolve_plan``."""

    base_table: str | None
    join_sequence: list[dict[str, Any]]
    aliases: dict[str, dict[str, str]]
    base_table_strategy: str | None = None
    union_sources: list[dict[str, Any]] | None = None


def infer_join_key(
    table_name: str,
    columns: list[str],
    all_umfs: dict[str, UMF] | None = None,
) -> str | None:
    """Infer the join key column for a table.

    Uses the UMF primary_key field first, then falls back to column name matching.

    Args:
        table_name: Name of the table (used to look up UMF primary_key)
        columns: List of column names in the table
        all_umfs: Optional dict mapping table names to UMF models for primary key lookup.

    Returns:
        Best join key column name, or None if no candidate found.

    """
    # Priority 1: Use UMF primary_key if available
    if all_umfs and table_name in all_umfs:
        umf = all_umfs[table_name]
        if umf.primary_key:
            pk = umf.primary_key[0]
            # Verify PK column actually exists in the column list
            if pk in columns or not columns:
                return pk

    # Priority 2: Look for columns containing common key patterns
    # (case-insensitive, underscore-insensitive matching)
    for col in columns:
        col_normalized = col.replace("_", "").lower()
        if col_normalized.endswith("id") and (
            "member" in col_normalized
            or "client" in col_normalized
            or "user" in col_normalized
            or "key" in col_normalized
        ):
            return col

    # Priority 3: Any column ending with _id or Id
    for col in columns:
        if col.lower().endswith("_id") or col.endswith("Id"):
            return col

    return None


class RelationshipResolver:
    """Infer SQL plan from UMF relationships and derivation metadata.

    Pure data implementation - accepts UMF Pydantic models, performs no file I/O.
    All related UMFs are passed in at construction time via ``all_umfs``.
    """

    def __init__(self, all_umfs: dict[str, UMF]) -> None:
        """Initialize resolver with all table UMF models.

        Args:
            all_umfs: Dict mapping table names to UMF Pydantic models.

        """
        self.all_umfs = all_umfs

    # ---------- Public API ----------

    def resolve_plan(self, target_umf: UMF) -> ResolvedPlan:
        """Return a normalized plan for the target table.

        Args:
            target_umf: UMF specification (Pydantic model)

        Returns:
            ResolvedPlan with join_sequence, aliases, base_table, and optional
            base_table_strategy / union_sources.

        """
        # Check for base_table_strategy and explicit base_table in metadata
        base_table_strategy = None
        union_sources = None
        explicit_base_table = None
        base_join_column = None
        if target_umf.metadata:
            base_table_strategy = target_umf.metadata.base_table_strategy
            explicit_base_table = getattr(target_umf.metadata, "base_table", None)
            base_join_column = target_umf.metadata.base_join_column

        # Determine which source tables and instances feed target columns
        contributing_instances = self._get_contributing_instances(target_umf)
        contributing_tables = set(contributing_instances.keys())

        # Handle union_sources strategy - build member universe from source tables
        if base_table_strategy == "union_sources":
            union_sources = self._build_union_sources(target_umf)
            base_table = None  # No single base table
            # Add all union source tables to contributing_tables so joins are created
            for source in union_sources:
                contributing_tables.add(source["table"])
            logger.info(
                f"Using union_sources strategy with {len(union_sources)} source tables"
            )
        elif explicit_base_table:
            base_table = explicit_base_table
            logger.info(f"Using explicit base_table from metadata: {base_table}")
        else:
            base_table = self._infer_base_table(contributing_tables, target_umf)

        # Load relationships from base table AND all contributing tables
        if base_table:
            all_relationships = self._load_all_relationships(
                base_table, contributing_tables, base_join_column
            )
        elif base_table_strategy == "union_sources" and union_sources:
            all_relationships = self._build_union_source_relationships(union_sources)
        else:
            all_relationships = []

        # Build join candidates with table_instance awareness
        candidates_by_key: dict[tuple[str, str | None], list[JoinInfo]] = {}

        # Determine the default join key once (base_join_column overrides inference)
        default_join_key = base_join_column or self._get_default_join_key(base_table)

        # Under union_branches, tables that feed UNION branches are part of the
        # base view — they must not also become join candidates
        union_branch_tables: set[str] = set()
        if base_table_strategy == "union_branches" and target_umf.metadata:
            union_branch_tables = set(
                target_umf.metadata.union_base_tables
                or target_umf.metadata.source_tables
                or []
            )

        for rel in all_relationships:
            tgt = rel.get("target_table")
            if not tgt or tgt not in contributing_tables:
                continue
            if tgt in union_branch_tables:
                continue

            alternative_joins = rel.get("alternative_joins") or []
            if alternative_joins and base_table:
                self._validate_alternative_joins(base_table, tgt, alternative_joins)

            # Create JoinInfo for each instance of this table
            instances = contributing_instances.get(tgt, {None})
            for inst in sorted(instances, key=lambda x: (x is None, x or "")):
                key = (tgt, inst)
                candidate = JoinInfo(
                    target_table=tgt,
                    source_column=rel.get("source_column", default_join_key),
                    target_column=rel.get("target_column", default_join_key),
                    strategy="direct",  # filled below
                    cardinality=rel.get("cardinality", {}),
                    confidence=rel.get("confidence", 0.0),
                    table_instance=inst,
                    join_filter=None,
                    alternative_joins=alternative_joins,
                    source_expression=rel.get("source_expression"),
                    target_expression=rel.get("target_expression"),
                )

                if key not in candidates_by_key:
                    candidates_by_key[key] = []
                candidates_by_key[key].append(candidate)

        # Deduplicate: choose best relationship per (table, instance) pair
        join_candidates: list[JoinInfo] = []
        for key in sorted(candidates_by_key.keys(), key=lambda k: (k[0], k[1] or "")):
            candidates = candidates_by_key[key]
            if len(candidates) == 1:
                join_candidates.append(candidates[0])
            else:
                best_candidate = self._select_best_relationship(candidates, target_umf)
                join_candidates.append(best_candidate)

        # Extract and apply join_filter and join_via from derivation candidates
        self._populate_join_metadata(target_umf, join_candidates)

        # Fill join_filter gaps from foreign_keys (candidate-level filters win:
        # they are (table, table_instance)-keyed and can disambiguate multi-instance
        # joins; FK-level filters apply table-wide)
        self._populate_fk_join_filters(join_candidates, target_umf)

        # Apply join_type from foreign_keys if specified
        self._populate_join_types(join_candidates, target_umf)

        # Infer strategy/pivot specs/first_record ordering
        for j in join_candidates:
            strat = self._infer_strategy(j, target_umf)
            j.strategy = strat
            if strat == "pivot":
                j.pivot = self._infer_pivot_spec(j, target_umf)
            elif strat == "first_record":
                j.partition_by, j.order_by = self._infer_first_record_order(j)

        # Order joins deterministically: by contribution score, table name, then alias
        # Cache contribution scores to avoid recomputation
        score_cache: dict[str, int] = {}
        for j in join_candidates:
            if j.target_table not in score_cache:
                score_cache[j.target_table] = self._contribution_score(
                    j.target_table, target_umf
                )
        scored = [(score_cache[j.target_table], j) for j in join_candidates]
        scored.sort(
            key=lambda x: (
                -x[0],  # Contribution score (descending)
                x[1].target_table.lower(),  # Table name (ascending)
                x[1].table_instance or "",  # Alias/instance (ascending)
            )
        )
        join_sequence = [j for _, j in scored]

        # Build alias maps (automatic case/underscore-insensitive mapping)
        aliases = self._build_alias_maps(contributing_tables)

        return ResolvedPlan(
            base_table=base_table,
            join_sequence=[self._joininfo_to_dict(j) for j in join_sequence],
            aliases=aliases,
            base_table_strategy=base_table_strategy,
            union_sources=union_sources if union_sources else None,
        )

    # ---------- Inference helpers ----------

    def _get_default_join_key(self, base_table: str | None) -> str:
        """Get the default join key from the base table's primary key, or first column."""
        if base_table and base_table in self.all_umfs:
            umf = self.all_umfs[base_table]
            if umf.primary_key:
                return umf.primary_key[0]
        return "id"

    def _load_outgoing_relationships(self, table_name: str) -> list[dict[str, Any]]:
        """Load outgoing relationships for a table from UMF model.

        Args:
            table_name: Name of the table

        Returns:
            List of relationship dicts

        """
        if table_name not in self.all_umfs:
            return []

        umf = self.all_umfs[table_name]
        if umf.relationships and umf.relationships.outgoing:
            result: list[dict[str, Any]] = []
            for rel in umf.relationships.outgoing:
                d: dict[str, Any] = {
                    "target_table": rel.target_table,
                    "source_column": rel.source_column,
                    "target_column": rel.target_column,
                    "type": rel.type,
                    "confidence": rel.confidence,
                }
                if rel.cardinality:
                    d["cardinality"] = {
                        "notation": rel.cardinality.notation,
                        "type": rel.cardinality.type,
                        "mandatory": rel.cardinality.mandatory,
                    }
                if rel.reasoning:
                    d["reasoning"] = rel.reasoning
                if rel.alternative_joins:
                    d["alternative_joins"] = [dict(a) for a in rel.alternative_joins]
                if rel.source_expression:
                    d["source_expression"] = rel.source_expression
                if rel.target_expression:
                    d["target_expression"] = rel.target_expression
                result.append(d)
            return result
        return []

    def _load_all_relationships(
        self,
        base_table: str,
        contributing_tables: set[str],
        base_join_column_override: str | None = None,
    ) -> list[dict[str, Any]]:
        """Load relationships from base table and synthesize joins for contributing tables.

        Creates synthetic relationships for any contributing table not covered
        by existing relationships, using primary key inference.

        When ``base_join_column_override`` (metadata.base_join_column) is set it
        replaces the inferred base join key AND overwrites ``source_column`` on
        every relationship declared outgoing from the base table — the field
        asserts "every join out of the base uses this key", because it exists
        precisely when the auto-selected key is wrong and declared relationships
        carry that same wrong key.
        """
        all_rels: list[dict[str, Any]] = []
        covered_targets: set[str] = set()

        # Start with base table relationships
        base_rels = self._load_outgoing_relationships(base_table)
        if base_join_column_override:
            for rel in base_rels:
                rel["source_column"] = base_join_column_override
        all_rels.extend(base_rels)

        # Track which contributing tables are covered
        for rel in base_rels:
            tgt = rel.get("target_table")
            if tgt:
                covered_targets.add(tgt)

        # Infer the join key from the base table (override wins)
        base_cols = self._get_table_columns(base_table)
        base_join_key = base_join_column_override or self._infer_join_key(
            base_table, base_cols
        )

        # For contributing tables not covered by relationships, synthesize direct joins
        for contrib_table in sorted(contributing_tables):
            if contrib_table in covered_targets or contrib_table == base_table:
                continue

            table_cols = self._get_table_columns(contrib_table)
            join_key = self._infer_join_key(contrib_table, table_cols)

            if join_key and base_join_key:
                # Qualified table names (containing ".") are assumed pre-aggregated -> direct join
                is_qualified = "." in contrib_table
                cardinality_notation = "1:0..1" if is_qualified else "1:0..N"
                synthetic_rel = {
                    "target_table": contrib_table,
                    "source_column": base_join_key,
                    "target_column": join_key,
                    "cardinality": {
                        "notation": cardinality_notation,
                        "mandatory": False,
                    },
                    "confidence": 0.5,
                    "_synthetic": True,
                }
                all_rels.append(synthetic_rel)
                covered_targets.add(contrib_table)

        return all_rels

    def _infer_join_key(self, table_name: str, columns: list[str]) -> str | None:
        """Infer the join key column for a table.

        Delegates to the module-level :func:`infer_join_key` helper.
        """
        return infer_join_key(table_name, columns, self.all_umfs)

    def _get_table_columns(self, table_name: str) -> list[str]:
        """Get column names for a table from UMF model.

        Args:
            table_name: Name of the table

        Returns:
            List of column names, or empty list if table not found

        """
        if table_name in self.all_umfs:
            umf = self.all_umfs[table_name]
            return [col.name for col in umf.columns] if umf.columns else []
        return []

    def _get_contributing_instances(
        self, target_umf: UMF
    ) -> dict[str, set[str | None]]:
        """Get all table instances that contribute columns to the target table.

        Returns dict mapping table name to set of table_instance values.
        None indicates the default instance (no explicit table_instance).
        """
        instances: dict[str, set[str | None]] = {}

        for col in target_umf.columns:
            if col.derivation and col.derivation.candidates:
                for cand in col.derivation.candidates:
                    tbl = cand.table
                    if tbl:
                        inst = cand.table_instance
                        if tbl not in instances:
                            instances[tbl] = set()
                        instances[tbl].add(inst)

        return instances

    def _populate_join_metadata(
        self, target_umf: UMF, join_candidates: list[JoinInfo]
    ) -> None:
        """Extract join_filter and join_via from derivation candidates in a single pass.

        When present, join_via overrides the default join keys (source_column/target_column)
        with custom keys for indirect joins through lookup tables.
        """
        filters: dict[tuple[str, str | None], str] = {}
        join_via_specs: dict[tuple[str, str | None], Any] = {}

        for col in target_umf.columns:
            if col.derivation and col.derivation.candidates:
                for cand in col.derivation.candidates:
                    tbl = cand.table
                    if not tbl:
                        continue
                    inst = cand.table_instance
                    key = (tbl, inst)

                    filt = cand.join_filter
                    if filt and key not in filters:
                        filters[key] = filt
                        logger.debug(
                            f"Found join_filter for {tbl} (instance={inst}): {filt[:80]}..."
                        )

                    jv = cand.join_via
                    if jv and key not in join_via_specs:
                        join_via_specs[key] = jv
                        logger.debug(
                            f"Found join_via for {tbl} (instance={inst}): "
                            f"{jv.source_key} -> {jv.target_key}"
                        )

        for join_info in join_candidates:
            key = (join_info.target_table, join_info.table_instance)
            if key in filters:
                join_info.join_filter = filters[key]
                logger.info(
                    f"Applied join_filter to {join_info.target_table} "
                    + f"(instance={join_info.table_instance})"
                )
            if key in join_via_specs:
                jv_spec = join_via_specs[key]
                join_info.join_via = jv_spec
                logger.info(
                    f"Applied join_via to {join_info.target_table} "
                    f"(instance={join_info.table_instance}): "
                    f"{jv_spec.source_key} -> {jv_spec.target_key}"
                )

    def _populate_fk_join_filters(
        self, join_candidates: list[JoinInfo], target_umf: UMF
    ) -> None:
        """Fill join_filter gaps from ForeignKey.join_filter.

        Candidate-level filters (populated by ``_populate_join_metadata``) take
        precedence; FK-level filters apply only where no candidate filter was
        found for the joined table.
        """
        if not target_umf.relationships or not target_umf.relationships.foreign_keys:
            return

        fk_join_filters: dict[str, str] = {}
        for fk in target_umf.relationships.foreign_keys:
            if fk.join_filter:
                if fk.cross_pipeline and fk.references_pipeline:
                    qualified = f"{fk.references_pipeline}.{fk.references_table}"
                else:
                    qualified = fk.references_table
                if qualified not in fk_join_filters:
                    fk_join_filters[qualified] = fk.join_filter

        for join_info in join_candidates:
            if join_info.join_filter is not None:
                continue
            filt = fk_join_filters.get(join_info.target_table)
            if filt:
                join_info.join_filter = filt
                logger.info(
                    f"Applied foreign-key join_filter to {join_info.target_table}"
                )

    def _validate_alternative_joins(
        self,
        base_table: str,
        target_table: str,
        alternative_joins: list[dict[str, str]],
    ) -> None:
        """Validate alternative join entries reference real columns.

        Each entry must carry ``source_column`` (on the base table) and
        ``target_column`` (on the joined table). Raises ``ValueError`` on the
        first violation so misconfigured specs fail at plan time, not run time.
        """
        base_cols = set(self._get_table_columns(base_table))
        target_cols = set(self._get_table_columns(target_table))

        for idx, alt in enumerate(alternative_joins):
            src = alt.get("source_column")
            tgt = alt.get("target_column")
            if not src or not tgt:
                msg = (
                    f"alternative_joins[{idx}] for {target_table} must specify "
                    f"both source_column and target_column (got {alt!r})"
                )
                raise ValueError(msg)
            if base_cols and src not in base_cols:
                msg = (
                    f"alternative_joins[{idx}] for {target_table}: source_column "
                    f"'{src}' does not exist on base table {base_table}"
                )
                raise ValueError(msg)
            if target_cols and tgt not in target_cols:
                msg = (
                    f"alternative_joins[{idx}] for {target_table}: target_column "
                    f"'{tgt}' does not exist on {target_table}"
                )
                raise ValueError(msg)

    def _populate_join_types(
        self, join_candidates: list[JoinInfo], target_umf: UMF
    ) -> None:
        """Apply join_type from foreign_keys to matching JoinInfo entries."""
        if not target_umf.relationships or not target_umf.relationships.foreign_keys:
            return

        fk_join_types: dict[str, str] = {}
        for fk in target_umf.relationships.foreign_keys:
            if fk.join_type:
                if fk.cross_pipeline and fk.references_pipeline:
                    qualified = f"{fk.references_pipeline}.{fk.references_table}"
                else:
                    qualified = fk.references_table
                fk_join_types[qualified] = fk.join_type.lower()

        for join_info in join_candidates:
            jt = fk_join_types.get(join_info.target_table)
            if jt and jt in ("left", "inner"):
                join_info.join_type = jt  # type: ignore[assignment]
                logger.info(
                    f"Applied join_type '{jt}' to {join_info.target_table} from foreign_keys"
                )

    def _infer_base_table(self, contributing_tables: set[str], target_umf: UMF) -> str:
        """Find the hub table using hub_score from UMF metadata.

        The hub table is identified by:
        1. Reading hub_score from UMF files (highest score wins)
        2. Counting outgoing relationships as fallback
        """
        hub_scores: dict[str, float] = {}
        # Exclude qualified (cross-pipeline) tables from base table candidacy
        candidate_tables = {table for table in contributing_tables if "." not in table}

        # Also consider tables that have relationships TO contributing tables
        for table_name, umf in sorted(self.all_umfs.items()):
            if umf.relationships and umf.relationships.outgoing:
                for rel in umf.relationships.outgoing:
                    if rel.target_table in contributing_tables:
                        if "." not in table_name:
                            candidate_tables.add(table_name)
                        break

        logger.info(
            f"Evaluating {len(candidate_tables)} candidate tables for base table selection: "
            f"{sorted(candidate_tables)}"
        )

        # Priority 1: Use hub_score from UMF metadata
        for table in sorted(candidate_tables):
            if table not in self.all_umfs:
                logger.debug(f"  {table}: UMF not found in all_umfs")
                continue

            umf = self.all_umfs[table]
            hub_score = 0.0
            if umf.relationships and umf.relationships.summary:
                hub_score = umf.relationships.summary.hub_score or 0.0

            if hub_score > 0:
                hub_scores[table] = hub_score
                logger.info(f"  {table}: hub_score={hub_score}")
            else:
                logger.debug(f"  {table}: hub_score={hub_score} (skipped)")

        if hub_scores:
            selected = max(hub_scores.items(), key=lambda x: (x[1], x[0]))
            logger.info(f"Selected base table: {selected[0]} (hub_score={selected[1]})")
            return selected[0]

        # Priority 2: Count outgoing relationships
        source_counts: dict[str, int] = {}
        for table in sorted(candidate_tables):
            rels = self._load_outgoing_relationships(table)
            if rels:
                source_counts[table] = len(rels)

        if source_counts:
            return max(source_counts.items(), key=lambda x: (x[1], x[0]))[0]

        # Final fallback: first contributing table (deterministic via sort)
        return sorted(contributing_tables)[0] if contributing_tables else ""

    def _build_union_sources(self, target_umf: UMF) -> list[dict[str, Any]]:
        """Build union source configuration for union_sources strategy.

        Reads source_tables from metadata and determines join columns for each.

        Returns:
            List of dicts with table name and join column info.

        """
        union_sources: list[dict[str, Any]] = []

        if not target_umf.metadata:
            return union_sources

        source_tables = target_umf.metadata.source_tables or []
        if not source_tables:
            logger.warning(
                "union_sources strategy specified but no source_tables in metadata"
            )
            return union_sources

        # Get the primary key column from the target table
        primary_key_col = "id"
        if target_umf.primary_key:
            primary_key_col = target_umf.primary_key[0]

        for source_table in source_tables:
            join_column, source_column = self._find_source_join_column(
                target_umf, source_table, primary_key_col
            )
            union_sources.append(
                {
                    "table": source_table,
                    "join_column": join_column,
                    "source_column": source_column,
                }
            )

        logger.info(f"Built union_sources with {len(union_sources)} tables")
        return union_sources

    def _find_source_join_column(
        self, target_umf: UMF, source_table: str, primary_key_col: str
    ) -> tuple[str, str]:
        """Find the join column in a source table that maps to the target's primary key.

        Returns:
            Tuple of (target_column, source_column).

        """
        # Check relationships.outgoing for the mapping
        if target_umf.relationships and target_umf.relationships.outgoing:
            for rel in target_umf.relationships.outgoing:
                if rel.target_table == source_table:
                    return (rel.target_column, rel.source_column)

        # The union_sources universe must be unified on a CONSISTENT key: every
        # source contributes the column that corresponds to the target's primary
        # key (the universe key), NOT its own primary key. For a child/detail
        # source (e.g. claims with PK claim_id but FK member_id), inferring the
        # source's own PK (claim_id) would union claim_ids INTO the member
        # universe and make every downstream per-key pre-aggregation join on the
        # wrong key. So prefer the source column that matches the target PK.
        if source_table in self.all_umfs:
            source_cols = self._get_table_columns(source_table)
            if primary_key_col in source_cols:
                return (primary_key_col, primary_key_col)

            # Fallback: the source lacks the target PK column. Use the source's
            # own inferred key (best effort) -- the universe is then unified on
            # whatever shared key the source exposes.
            join_key = self._infer_join_key(source_table, source_cols)
            if join_key:
                return (join_key, primary_key_col)

        logger.warning(
            f"Could not find join column for {source_table}, using {primary_key_col}"
        )
        return (primary_key_col, primary_key_col)

    def _build_union_source_relationships(
        self, union_sources: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Build relationship dicts for joining union_sources tables.

        Creates a relationship for each source table that joins to the
        base universe via the primary key.
        """
        relationships: list[dict[str, Any]] = []

        for source in union_sources:
            table_name = source["table"]
            join_col = source["join_column"]
            source_col = source.get("source_column", join_col)

            rel = {
                "target_table": table_name,
                "source_column": source_col,
                "target_column": join_col,
                "cardinality": {"notation": "1:N", "type": "one_to_many"},
                "confidence": 1.0,
                "_union_source": True,
            }
            relationships.append(rel)

        return relationships

    def _infer_strategy(
        self, j: JoinInfo, target_umf: UMF
    ) -> Literal["direct", "first_record", "pivot"]:
        """Infer join strategy based on cardinality notation."""
        notation = j.cardinality.get("notation", "") if j.cardinality else ""

        if "1:N" in notation or "1:0..N" in notation:
            # Check if pivot columns are defined in the target for this table
            if self._has_pivot_columns(j.target_table, target_umf):
                return "pivot"
            return "first_record"

        # 1:1 or 1:0..1 relationships use direct join
        return "direct"

    def _has_pivot_columns(self, table_name: str, target_umf: UMF) -> bool:
        """Check if target has numbered/pivoted columns derived from this table.

        Looks for derivation candidates from the given table that have a
        ``pivot_spec`` or follow a numbered naming pattern (e.g., col1, col2, ...).
        """
        # Collect columns derived from this table
        derived_cols: list[str] = []
        for col in target_umf.columns:
            if col.derivation and col.derivation.candidates:
                for cand in col.derivation.candidates:
                    if cand.table == table_name:
                        derived_cols.append(col.name)
                        break

        if len(derived_cols) < 2:
            return False

        # Check for numbered suffix pattern (e.g., gap1, gap2, item_1, item_2)
        numbered = [c for c in derived_cols if re.search(r"\d+$", c)]
        return len(numbered) >= 2

    def _infer_pivot_spec(self, j: JoinInfo, target_umf: UMF) -> PivotSpec:
        """Infer pivot specification from target columns derived from this table."""
        target_cols = self._get_table_columns(j.target_table)
        key_col = j.target_column

        # Find value column candidates in the source table
        candidates = ["Description", "Name", "Value", "Amount", "Code", "Type"]
        value_col = next((c for c in candidates if c in target_cols), "Value")

        # Determine prefix and count from target UMF columns derived from this table
        derived_cols: list[str] = []
        for col in target_umf.columns:
            if col.derivation and col.derivation.candidates:
                for cand in col.derivation.candidates:
                    if cand.table == j.target_table:
                        derived_cols.append(col.name)
                        break

        # Find common prefix among numbered columns
        numbered = [(c, re.search(r"(\d+)$", c)) for c in derived_cols]
        numbered = [(c, m) for c, m in numbered if m]

        if numbered:
            # Extract prefix from the first numbered column
            first_col, first_match = numbered[0]
            prefix = first_col[: first_match.start()]  # type: ignore[union-attr]
            max_n = max(int(m.group(1)) for _, m in numbered)  # type: ignore[union-attr]
        else:
            prefix = "Value"
            max_n = 6

        return PivotSpec(
            key_column=key_col, value_column=value_col, prefix=prefix, max_records=max_n
        )

    def _infer_first_record_order(self, j: JoinInfo) -> tuple[list[str], list[str]]:
        """Infer partition_by and order_by for first_record strategy."""
        cols = self._get_table_columns(j.target_table)

        # Partition by the join key
        partition_col = j.target_column

        # Look for typical ordering columns (timestamps, dates, sequence numbers)
        order_candidates = []
        for col in cols:
            col_lower = col.lower()
            if any(
                kw in col_lower
                for kw in (
                    "date",
                    "time",
                    "timestamp",
                    "created",
                    "updated",
                    "sequence",
                    "order",
                )
            ):
                order_candidates.append(col)

        if order_candidates:
            return [partition_col], order_candidates[:2]

        # Fallback: order by first column
        first = cols[0] if cols else partition_col
        return [partition_col], [first] if first else []

    def _select_best_relationship(
        self, candidates: list[JoinInfo], target_umf: UMF
    ) -> JoinInfo:
        """Select the best relationship when multiple exist to the same target table."""
        if len(candidates) == 1:
            return candidates[0]

        scored_candidates = []
        for candidate in candidates:
            score = 0.0

            # Primary: confidence score (weight: 10)
            score += candidate.confidence * 10

            # Secondary: cardinality appropriateness (weight: 5)
            cardinality = candidate.cardinality.get("notation", "")
            if "1:N" in cardinality or "N:1" in cardinality:
                score += 5
            elif "1:1" in cardinality:
                score += 3
            elif "N:N" in cardinality or "M:N" in cardinality:
                score -= 2

            # Tertiary: prefer primary key columns (weight: 2)
            for _tbl_name, umf in self.all_umfs.items():
                if umf.primary_key and candidate.source_column in umf.primary_key:
                    score += 2
                    break

            # Quaternary: contribution score to target (weight: 1)
            contrib_score = self._contribution_score(candidate.target_table, target_umf)
            score += contrib_score

            scored_candidates.append((score, candidate))

        scored_candidates.sort(
            key=lambda x: (-x[0], x[1].target_table, x[1].source_column)
        )
        return scored_candidates[0][1]

    def _contribution_score(self, source_table: str, target_umf: UMF) -> int:
        """Count how many target columns are derived from the given source table."""
        score = 0
        for col in target_umf.columns:
            if col.derivation:
                for cand in col.derivation.candidates or []:
                    if cand.table == source_table:
                        score += 1
        return score

    def _build_alias_maps(
        self, contributing_tables: set[str]
    ) -> dict[str, dict[str, str]]:
        """Build case/underscore-insensitive canonicalization maps per table."""

        def norm(s: str) -> str:
            return s.replace("_", "").lower()

        aliases: dict[str, dict[str, str]] = {}
        for tbl in sorted(contributing_tables):
            cols = self._get_table_columns(tbl)
            canon = {norm(c): c for c in cols}
            aliases[tbl] = {c: canon.get(norm(c), c) for c in cols}
        return aliases

    # ---------- Utils ----------

    @staticmethod
    def _joininfo_to_dict(j: JoinInfo) -> dict[str, Any]:
        """Convert a JoinInfo dataclass to a plain dict."""
        d: dict[str, Any] = {
            "target_table": j.target_table,
            "source_column": j.source_column,
            "target_column": j.target_column,
            "strategy": j.strategy,
            "partition_by": j.partition_by,
            "order_by": j.order_by,
            "cardinality": j.cardinality,
        }
        if j.pivot:
            d["pivot"] = {
                "key_column": j.pivot.key_column,
                "value_column": j.pivot.value_column,
                "prefix": j.pivot.prefix,
                "max_records": j.pivot.max_records,
            }
        if j.table_instance:
            d["table_instance"] = j.table_instance
        if j.join_filter:
            d["join_filter"] = j.join_filter
        if j.source_expression:
            d["source_expression"] = j.source_expression
        if j.target_expression:
            d["target_expression"] = j.target_expression
        if j.join_via:
            d["join_via"] = {
                "source_key": j.join_via.source_key,
                "target_key": j.join_via.target_key,
                "lookup_key": j.join_via.lookup_key,
                "lookup_table": j.join_via.lookup_table,
            }
        if j.join_type != "left":
            d["join_type"] = j.join_type
        if j.alternative_joins:
            d["alternative_joins"] = j.alternative_joins
        return d
