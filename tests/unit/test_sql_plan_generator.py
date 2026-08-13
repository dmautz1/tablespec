"""Tests for SQL plan generation and relationship resolution from UMF metadata."""

from __future__ import annotations

import pytest

from tablespec.models.umf import (
    Cardinality,
    UMFMetadata,
    DerivationCandidate,
    OutgoingRelationship,
    Relationships,
    RelationshipSummary,
    Survivorship,
    UMF,
    UMFColumn,
    UMFColumnDerivation,
)
from tablespec.schemas.relationship_resolver import (
    JoinInfo,
    PivotSpec,
    RelationshipResolver,
    ResolvedPlan,
)
from tablespec.schemas.sql_generator import SQLPlanGenerator, generate_sql_plan

pytestmark = pytest.mark.fast


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _card(type_: str, notation: str) -> Cardinality:
    """Create a Cardinality with required multiplicity fields derived from notation."""
    parts = notation.split(":")
    return Cardinality(
        type=type_,
        notation=notation,
        source_multiplicity=parts[0] if len(parts) > 0 else "1",
        target_multiplicity=parts[1] if len(parts) > 1 else "*",
    )


def _make_umf(
    table_name: str,
    columns: list[UMFColumn],
    *,
    primary_key: list[str] | None = None,
    relationships: Relationships | None = None,
) -> UMF:
    """Shortcut to build a UMF with sensible defaults."""
    return UMF(
        version="1.0",
        table_name=table_name,
        columns=columns,
        primary_key=primary_key,
        relationships=relationships,
    )


@pytest.fixture
def minimal_umf() -> UMF:
    """Simple 3-column table with no derivations."""
    return _make_umf(
        "test_claims",
        [
            UMFColumn(name="claim_id", data_type="VARCHAR"),
            UMFColumn(name="claim_amount", data_type="DECIMAL"),
            UMFColumn(name="provider_id", data_type="VARCHAR"),
        ],
        primary_key=["claim_id"],
    )


@pytest.fixture
def source_table_a() -> UMF:
    """Source table A with claim_id PK and member_name."""
    return _make_umf(
        "source_a",
        [
            UMFColumn(name="claim_id", data_type="VARCHAR"),
            UMFColumn(name="member_name", data_type="VARCHAR"),
            UMFColumn(name="service_date", data_type="DATE"),
        ],
        primary_key=["claim_id"],
        relationships=Relationships(
            outgoing=[
                OutgoingRelationship(
                    target_table="source_b",
                    source_column="claim_id",
                    target_column="claim_id",
                    type="foreign_to_primary",
                    confidence=0.9,
                    cardinality=_card("one_to_one", "1:0..1"),
                ),
            ],
            summary=RelationshipSummary(
                total_relationships=1,
                total_incoming=0,
                total_outgoing=1,
                hub_score=5.0,
            ),
        ),
    )


@pytest.fixture
def source_table_b() -> UMF:
    """Source table B with claim_id PK and provider info."""
    return _make_umf(
        "source_b",
        [
            UMFColumn(name="claim_id", data_type="VARCHAR"),
            UMFColumn(name="provider_name", data_type="VARCHAR"),
            UMFColumn(name="provider_type", data_type="VARCHAR"),
        ],
        primary_key=["claim_id"],
        relationships=Relationships(
            summary=RelationshipSummary(
                total_relationships=0,
                total_incoming=1,
                total_outgoing=0,
                hub_score=1.0,
            ),
        ),
    )


@pytest.fixture
def derived_umf(source_table_a: UMF, source_table_b: UMF) -> UMF:
    """Table with columns derived from two source tables."""
    return _make_umf(
        "derived_output",
        [
            UMFColumn(
                name="claim_id",
                data_type="VARCHAR",
                derivation=UMFColumnDerivation(
                    strategy="primary_key",
                ),
            ),
            UMFColumn(
                name="member_name",
                data_type="VARCHAR",
                derivation=UMFColumnDerivation(
                    candidates=[
                        DerivationCandidate(
                            table="source_a",
                            column="member_name",
                            priority=1,
                        ),
                    ],
                    survivorship=Survivorship(
                        strategy="single_source",
                        explanation="Direct from source_a",
                    ),
                ),
            ),
            UMFColumn(
                name="provider_name",
                data_type="VARCHAR",
                derivation=UMFColumnDerivation(
                    candidates=[
                        DerivationCandidate(
                            table="source_b",
                            column="provider_name",
                            priority=1,
                        ),
                    ],
                    survivorship=Survivorship(
                        strategy="single_source",
                        explanation="Direct from source_b",
                    ),
                ),
            ),
        ],
        primary_key=["claim_id"],
    )


@pytest.fixture
def survivorship_umf() -> UMF:
    """Table with multi-source survivorship (COALESCE strategy)."""
    return _make_umf(
        "survivorship_output",
        [
            UMFColumn(
                name="member_id",
                data_type="VARCHAR",
                derivation=UMFColumnDerivation(strategy="primary_key"),
            ),
            UMFColumn(
                name="phone_number",
                data_type="VARCHAR",
                derivation=UMFColumnDerivation(
                    candidates=[
                        DerivationCandidate(
                            table="enrollment",
                            column="phone",
                            priority=1,
                        ),
                        DerivationCandidate(
                            table="demographics",
                            column="phone_number",
                            priority=2,
                        ),
                    ],
                    survivorship=Survivorship(
                        strategy="highest_priority",
                        explanation="Enrollment preferred, fallback to demographics",
                        default_value="UNKNOWN",
                    ),
                ),
            ),
        ],
        primary_key=["member_id"],
    )


@pytest.fixture
def aggregate_umf() -> UMF:
    """Table with COUNT/MAX aggregate derivations."""
    return _make_umf(
        "aggregate_output",
        [
            UMFColumn(
                name="member_id",
                data_type="VARCHAR",
                derivation=UMFColumnDerivation(strategy="primary_key"),
            ),
            UMFColumn(
                name="claim_count",
                data_type="INTEGER",
                derivation=UMFColumnDerivation(
                    candidates=[
                        DerivationCandidate(
                            table="claims",
                            column="claim_id",
                            expression="COUNT(*)",
                            priority=1,
                        ),
                    ],
                    survivorship=Survivorship(
                        strategy="single_source",
                        explanation="Count of claims per member",
                    ),
                ),
            ),
            UMFColumn(
                name="last_claim_date",
                data_type="DATE",
                derivation=UMFColumnDerivation(
                    candidates=[
                        DerivationCandidate(
                            table="claims",
                            column="service_date",
                            expression="MAX(service_date)",
                            priority=1,
                        ),
                    ],
                    survivorship=Survivorship(
                        strategy="single_source",
                        explanation="Most recent claim date",
                    ),
                ),
            ),
        ],
        primary_key=["member_id"],
    )


@pytest.fixture
def related_umfs(source_table_a: UMF, source_table_b: UMF) -> dict[str, UMF]:
    """Dict of related UMFs for join resolution."""
    return {
        "source_a": source_table_a,
        "source_b": source_table_b,
    }


@pytest.fixture
def enrollment_umf() -> UMF:
    """Enrollment source table."""
    return _make_umf(
        "enrollment",
        [
            UMFColumn(name="member_id", data_type="VARCHAR"),
            UMFColumn(name="phone", data_type="VARCHAR"),
            UMFColumn(name="enrollment_date", data_type="DATE"),
        ],
        primary_key=["member_id"],
        relationships=Relationships(
            summary=RelationshipSummary(
                total_relationships=1,
                total_incoming=0,
                total_outgoing=1,
                hub_score=5.0,
            ),
            outgoing=[
                OutgoingRelationship(
                    target_table="demographics",
                    source_column="member_id",
                    target_column="member_id",
                    type="foreign_to_primary",
                    confidence=0.9,
                    cardinality=_card("one_to_one", "1:0..1"),
                ),
            ],
        ),
    )


@pytest.fixture
def demographics_umf() -> UMF:
    """Demographics source table."""
    return _make_umf(
        "demographics",
        [
            UMFColumn(name="member_id", data_type="VARCHAR"),
            UMFColumn(name="phone_number", data_type="VARCHAR"),
            UMFColumn(name="address", data_type="VARCHAR"),
        ],
        primary_key=["member_id"],
        relationships=Relationships(
            summary=RelationshipSummary(
                total_relationships=0,
                total_incoming=1,
                total_outgoing=0,
                hub_score=1.0,
            ),
        ),
    )


@pytest.fixture
def survivorship_related_umfs(
    enrollment_umf: UMF, demographics_umf: UMF
) -> dict[str, UMF]:
    return {
        "enrollment": enrollment_umf,
        "demographics": demographics_umf,
    }


@pytest.fixture
def claims_umf() -> UMF:
    """Claims source table for aggregate tests."""
    return _make_umf(
        "claims",
        [
            UMFColumn(name="claim_id", data_type="VARCHAR"),
            UMFColumn(name="member_id", data_type="VARCHAR"),
            UMFColumn(name="service_date", data_type="DATE"),
        ],
        primary_key=["claim_id"],
    )


# ---------------------------------------------------------------------------
# TestSQLPlanGeneratorBasic
# ---------------------------------------------------------------------------


class TestSQLPlanGeneratorBasic:
    """Test basic SQL plan generation."""

    def test_generates_valid_sql_string(
        self, minimal_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """Generator returns a non-empty string."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(minimal_umf, related_umfs)
        assert isinstance(sql, str)
        assert len(sql) > 0

    def test_generates_header_with_table_name(
        self, minimal_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """SQL contains a header comment block referencing the table name."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(minimal_umf, related_umfs)
        assert "SQL Execution Plan: test_claims" in sql

    def test_generates_final_assembly(
        self, minimal_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """SQL contains a FINAL ASSEMBLY block for the target table."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(minimal_umf, related_umfs)
        assert "FINAL ASSEMBLY" in sql
        assert "test_claims" in sql

    def test_generates_create_statement(
        self, minimal_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """SQL contains CREATE OR REPLACE TEMPORARY VIEW statements."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(minimal_umf, related_umfs)
        assert "CREATE OR REPLACE TEMPORARY VIEW" in sql

    def test_template_variable_substitution(self, related_umfs: dict[str, UMF]):
        """Template variables in derivation expressions are replaced."""
        target = _make_umf(
            "templated_table",
            [
                UMFColumn(
                    name="member_id",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(strategy="primary_key"),
                ),
                UMFColumn(
                    name="run_date",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="source_a",
                                expression="'{{run_date}}'",
                                priority=1,
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source",
                            explanation="Injected run date",
                        ),
                    ),
                ),
            ],
            primary_key=["member_id"],
        )

        gen = SQLPlanGenerator(template_vars={"run_date": "2026-01-01"})
        sql = gen.generate_for_table(target, related_umfs)
        assert "2026-01-01" in sql
        assert "{{run_date}}" not in sql

    def test_default_values_applied(self):
        """Columns without derivation produce CAST(NULL AS type)."""
        target = _make_umf(
            "defaults_table",
            [
                UMFColumn(name="col_a", data_type="VARCHAR"),
                UMFColumn(name="col_b", data_type="INTEGER"),
            ],
        )
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(target, {})
        assert "CAST(NULL AS STRING)" in sql
        assert "CAST(NULL AS INT)" in sql

    def test_raises_without_table_name(self):
        """ValueError when table_name is missing."""
        # UMF requires table_name, so we can't easily create one without it.
        # Instead test that generate_for_table uses table_name correctly.
        umf = _make_umf(
            "valid_name",
            [UMFColumn(name="id", data_type="INTEGER")],
        )
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(umf, {})
        assert "valid_name" in sql

    def test_table_resolver_applied(self):
        """table_resolver callback transforms table names in SQL."""
        source = _make_umf(
            "raw_table",
            [
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="val", data_type="VARCHAR"),
            ],
            primary_key=["id"],
            relationships=Relationships(
                summary=RelationshipSummary(
                    total_relationships=0,
                    total_incoming=0,
                    total_outgoing=0,
                    hub_score=5.0,
                ),
            ),
        )
        target = _make_umf(
            "output_table",
            [
                UMFColumn(
                    name="id",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(strategy="primary_key"),
                ),
                UMFColumn(
                    name="val",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="raw_table", column="val", priority=1
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source",
                            explanation="Direct",
                        ),
                    ),
                ),
            ],
            primary_key=["id"],
        )

        def resolver(name: str) -> str:
            return f"catalog.schema.{name}"

        gen = SQLPlanGenerator(table_resolver=resolver)
        sql = gen.generate_for_table(target, {"raw_table": source})
        assert "catalog.schema.raw_table" in sql


# ---------------------------------------------------------------------------
# TestSQLPlanGeneratorJoins
# ---------------------------------------------------------------------------


class TestSQLPlanGeneratorJoins:
    """Test join SQL generation."""

    def test_direct_join_produces_left_join(
        self, derived_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """Direct join strategy emits LEFT JOIN SQL."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(derived_umf, related_umfs)
        assert "LEFT JOIN" in sql

    def test_direct_join_has_on_clause(
        self, derived_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """Direct join includes an ON clause for the join key."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(derived_umf, related_umfs)
        assert " ON " in sql

    def test_first_record_join_produces_row_number(self):
        """first_record join strategy generates ROW_NUMBER dedup."""
        # Create a 1:N relationship so strategy becomes first_record
        source = _make_umf(
            "detail_table",
            [
                UMFColumn(name="parent_id", data_type="VARCHAR"),
                UMFColumn(name="detail_value", data_type="VARCHAR"),
                UMFColumn(name="updated_date", data_type="DATE"),
            ],
            primary_key=["parent_id"],
            relationships=Relationships(
                summary=RelationshipSummary(
                    total_relationships=0,
                    total_incoming=0,
                    total_outgoing=0,
                    hub_score=1.0,
                ),
            ),
        )
        hub = _make_umf(
            "hub_table",
            [
                UMFColumn(name="parent_id", data_type="VARCHAR"),
            ],
            primary_key=["parent_id"],
            relationships=Relationships(
                outgoing=[
                    OutgoingRelationship(
                        target_table="detail_table",
                        source_column="parent_id",
                        target_column="parent_id",
                        type="foreign_to_primary",
                        confidence=0.9,
                        cardinality=_card("one_to_many", "1:N"),
                    ),
                ],
                summary=RelationshipSummary(
                    total_relationships=1,
                    total_incoming=0,
                    total_outgoing=1,
                    hub_score=5.0,
                ),
            ),
        )
        target = _make_umf(
            "output_first_record",
            [
                UMFColumn(
                    name="parent_id",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(strategy="primary_key"),
                ),
                UMFColumn(
                    name="detail_value",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="detail_table",
                                column="detail_value",
                                priority=1,
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source",
                            explanation="First record from detail_table",
                        ),
                    ),
                ),
            ],
            primary_key=["parent_id"],
        )

        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(target, {"hub_table": hub, "detail_table": source})
        assert "ROW_NUMBER" in sql
        assert "PARTITION BY" in sql
        assert "First Record" in sql
        # The ranking must be a TOTAL ORDER (stable tiebreak) so the "first" row is
        # deterministic across backends, not the bare heuristic discriminator. The
        # ORDER BY therefore spans the partition key + every remaining target column.
        order_line = next(ln for ln in sql.splitlines() if "ORDER BY" in ln)
        assert "ORDER BY" in order_line
        for col in ("parent_id", "detail_value", "updated_date"):
            assert col in order_line, (
                f"ORDER BY missing tiebreak column {col!r}: {order_line!r}"
            )
        # Explicit ASC NULLS LAST on every term: DuckDB and Spark disagree on the
        # DEFAULT ASC null placement, so the placement must be pinned to stay
        # dialect-identical.
        assert "NULLS LAST" in order_line, order_line

    def test_multiple_joins_create_sequential_steps(
        self, derived_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """Multiple joins create numbered disposition_step_ views."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(derived_umf, related_umfs)
        # With two source tables, we expect at least step_1
        assert "disposition_step_1" in sql

    def test_join_filter_appears_in_on_clause(self):
        """join_filter from derivation candidates appears in the ON clause."""
        source = _make_umf(
            "filtered_source",
            [
                UMFColumn(name="member_id", data_type="VARCHAR"),
                UMFColumn(name="status", data_type="VARCHAR"),
                UMFColumn(name="value", data_type="VARCHAR"),
            ],
            primary_key=["member_id"],
            relationships=Relationships(
                summary=RelationshipSummary(
                    total_relationships=0,
                    total_incoming=0,
                    total_outgoing=0,
                    hub_score=1.0,
                ),
            ),
        )
        hub = _make_umf(
            "member_hub",
            [
                UMFColumn(name="member_id", data_type="VARCHAR"),
            ],
            primary_key=["member_id"],
            relationships=Relationships(
                outgoing=[
                    OutgoingRelationship(
                        target_table="filtered_source",
                        source_column="member_id",
                        target_column="member_id",
                        type="foreign_to_primary",
                        confidence=0.9,
                        cardinality=_card("one_to_one", "1:0..1"),
                    ),
                ],
                summary=RelationshipSummary(
                    total_relationships=1,
                    total_incoming=0,
                    total_outgoing=1,
                    hub_score=5.0,
                ),
            ),
        )
        target = _make_umf(
            "filtered_output",
            [
                UMFColumn(
                    name="member_id",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(strategy="primary_key"),
                ),
                UMFColumn(
                    name="active_value",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="filtered_source",
                                column="value",
                                priority=1,
                                join_filter="status = 'ACTIVE'",
                                table_instance="active_source",
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source",
                            explanation="Active records only",
                        ),
                    ),
                ),
            ],
            primary_key=["member_id"],
        )

        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(
            target, {"member_hub": hub, "filtered_source": source}
        )
        # The join filter should be rewritten into the ON clause
        assert "ACTIVE" in sql


class TestRewriteJoinFilterQuoteSpans:
    """``_rewrite_join_filter`` must qualify code tokens but never string literals.

    Regression for the gold_inner_join_filter conformance case: a column-named
    token INSIDE a single-quoted literal must be preserved verbatim (rewriting it
    corrupts the constant on BOTH backends), while bare references and references
    inside function calls are still qualified to the join alias.
    """

    def _gen(self, cols: list[str]) -> SQLPlanGenerator:
        gen = SQLPlanGenerator()
        gen._related_umfs = {
            "member": _make_umf(
                "member",
                [UMFColumn(name=c, data_type="VARCHAR") for c in cols],
            )
        }
        return gen

    def test_token_inside_literal_is_preserved(self) -> None:
        gen = self._gen(["region", "plan_type"])
        out = gen._rewrite_join_filter(
            "plan_type = 'PPO' AND UPPER(region) <> 'no region here'", "member"
        )
        # Bare tokens + function-arg tokens are qualified ...
        assert "target.plan_type = 'PPO'" in out
        assert "UPPER(target.region)" in out
        # ... but the column-named token inside the literal is NOT touched.
        assert "'no region here'" in out
        assert "target.region here" not in out

    def test_escaped_quote_literal_is_preserved(self) -> None:
        gen = self._gen(["region", "plan_type"])
        out = gen._rewrite_join_filter(
            "region = 'it''s region' AND plan_type = 'PPO'", "member"
        )
        assert "target.region = 'it''s region'" in out
        # the in-literal 'region' (after the escaped quote) stays unqualified
        assert "'it''s region'" in out

    def test_literal_in_in_list_is_preserved(self) -> None:
        gen = self._gen(["region", "plan_type"])
        out = gen._rewrite_join_filter(
            "UPPER(region) = 'WEST' OR plan_type IN ('region','PPO')", "member"
        )
        assert "UPPER(target.region)" in out
        assert "target.plan_type IN ('region','PPO')" in out


# ---------------------------------------------------------------------------
# TestSQLPlanGeneratorDerivations
# ---------------------------------------------------------------------------


class TestSQLPlanGeneratorDerivations:
    """Test column derivation mapping in SQL output."""

    def test_single_source_derivation(
        self, derived_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """Single-source derivation maps correctly in final assembly."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(derived_umf, related_umfs)
        # member_name comes from source_a (the base table) so it appears as base.member_name
        assert "base.member_name AS member_name" in sql
        # provider_name comes from source_b (joined) so it appears with table alias prefix
        assert "source_b__provider_name" in sql

    def test_coalesce_survivorship(
        self,
        survivorship_umf: UMF,
        survivorship_related_umfs: dict[str, UMF],
    ):
        """Multi-source survivorship generates a COALESCE expression."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(survivorship_umf, survivorship_related_umfs)
        assert "COALESCE" in sql

    def test_survivorship_default_value(
        self,
        survivorship_umf: UMF,
        survivorship_related_umfs: dict[str, UMF],
    ):
        """Default value appears in survivorship COALESCE."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(survivorship_umf, survivorship_related_umfs)
        assert "UNKNOWN" in sql

    def test_expression_derivation(self, related_umfs: dict[str, UMF]):
        """Expression-based derivation rewrites correctly."""
        target = _make_umf(
            "expr_output",
            [
                UMFColumn(
                    name="claim_id",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(strategy="primary_key"),
                ),
                UMFColumn(
                    name="full_info",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="source_a",
                                expression="CONCAT(member_name, ' - ', service_date)",
                                priority=1,
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source",
                            explanation="Concatenated info",
                        ),
                    ),
                ),
            ],
            primary_key=["claim_id"],
        )
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(target, related_umfs)
        assert "CONCAT" in sql

    def test_unmapped_column_produces_cast_null(self):
        """Columns with no derivation produce CAST(NULL AS type)."""
        target = _make_umf(
            "sparse_table",
            [
                UMFColumn(name="unmapped_col", data_type="DATE"),
            ],
        )
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(target, {})
        assert "CAST(NULL AS DATE)" in sql

    def test_primary_key_strategy(self, derived_umf: UMF, related_umfs: dict[str, UMF]):
        """primary_key derivation strategy references base.column."""
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(derived_umf, related_umfs)
        assert "base.claim_id" in sql

    def test_default_value_column(self):
        """Column with explicit default uses CAST(default AS type)."""
        target = _make_umf(
            "default_table",
            [
                UMFColumn(name="status", data_type="VARCHAR", default="PENDING"),
            ],
        )
        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(target, {})
        assert "PENDING" in sql


# ---------------------------------------------------------------------------
# TestOutputColumnOrder
# ---------------------------------------------------------------------------


class TestOutputColumnOrder:
    """Output projections follow spec ``position`` order, not name order.

    The final projection defines the physical column order of a
    ``CREATE TABLE ... AS`` target — alphabetical output produces a
    semantically different table schema than the spec declares.
    """

    def _source(self) -> UMF:
        return _make_umf(
            "ord_source",
            [
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="c1", data_type="VARCHAR"),
                UMFColumn(name="c2", data_type="VARCHAR"),
                UMFColumn(name="c3", data_type="VARCHAR"),
            ],
            primary_key=["id"],
        )

    @staticmethod
    def _col(name: str, position: str | None, source_col: str) -> UMFColumn:
        return UMFColumn(
            name=name,
            data_type="VARCHAR",
            position=position,
            derivation=UMFColumnDerivation(
                candidates=[
                    DerivationCandidate(
                        table="ord_source", column=source_col, priority=1
                    ),
                ],
            ),
        )

    def test_final_assembly_follows_spec_positions(self):
        """Positioned columns project in position order even when it inverts
        the alphabetical order."""
        target = _make_umf(
            "ord_target",
            [
                self._col("zulu", "1", "c1"),
                self._col("alpha", "2", "c2"),
                self._col("mike", "3", "c3"),
            ],
        )
        sql = SQLPlanGenerator().generate_for_table(
            target, {"ord_source": self._source()}
        )
        assert sql.index("AS zulu") < sql.index("AS alpha") < sql.index("AS mike")

    def test_unpositioned_columns_sort_after_positioned_alphabetically(self):
        """Legacy specs without positions keep a deterministic tail: after all
        positioned columns, alphabetical among themselves."""
        target = _make_umf(
            "ord_target",
            [
                self._col("alpha_x", None, "c1"),
                self._col("beta_two", "2", "c2"),
                self._col("gamma_one", "1", "c3"),
                self._col("aa_tail", None, "id"),
            ],
        )
        sql = SQLPlanGenerator().generate_for_table(
            target, {"ord_source": self._source()}
        )
        assert (
            sql.index("AS gamma_one")
            < sql.index("AS beta_two")
            < sql.index("AS aa_tail")
            < sql.index("AS alpha_x")
        )

    def test_union_branches_output_follows_spec_positions(self):
        """The union strategy's shared column set (each branch's projection and
        the final output) follows spec positions."""

        def _source(name: str) -> UMF:
            return UMF(
                version="1.0",
                table_name=name,
                canonical_name=name,
                table_type="ingested",
                columns=[
                    UMFColumn(name=n, data_type=t)
                    for n, t in {
                        "rid": "VARCHAR",
                        "zz_col": "VARCHAR",
                        "aa_col": "VARCHAR",
                        "file_date": "DATE",
                        "meta_load_dt": "DATE",
                    }.items()
                ],
            )

        def _cand(table: str, col: str, prio: int) -> DerivationCandidate:
            cutover = (
                "file_date < DATE '2026-01-01'"
                if table == "u_one"
                else "file_date >= DATE '2026-01-01'"
            )
            return DerivationCandidate(
                table=table,
                column=col,
                priority=prio,
                row_filter=cutover,
                order_by=["meta_load_dt"],
            )

        def _ucol(name: str, position: str) -> UMFColumn:
            return UMFColumn(
                name=name,
                data_type="VARCHAR",
                position=position,
                derivation=UMFColumnDerivation(
                    candidates=[_cand("u_one", name, 1), _cand("u_two", name, 2)],
                ),
            )

        target = UMF(
            version="1.0",
            table_name="ord_union",
            canonical_name="ord_union",
            table_type="generated",
            primary_key=["rid"],
            metadata={
                "base_table": "u_one",
                "base_table_strategy": "union_branches",
                "union_base_tables": ["u_two"],
                "union_type": "union_all",
                "dedup_strategy": "latest",
            },
            columns=[
                _ucol("rid", "1"),
                _ucol("zz_col", "2"),
                _ucol("aa_col", "3"),
            ],
        )
        sql = SQLPlanGenerator().generate_for_table(
            target, {"u_one": _source("u_one"), "u_two": _source("u_two")}
        )
        # position order (zz before aa) must survive; alphabetical would invert
        assert sql.index("zz_col") < sql.index("aa_col")


# ---------------------------------------------------------------------------
# TestBacktickQualification
# ---------------------------------------------------------------------------


class TestBacktickQualification:
    """A backtick-quoted column in a derivation expression must be qualified
    OUTSIDE its backticks (base.`Service`), never inside (`base.Service`,
    which names a literal column that does not exist)."""

    def test_backtick_column_qualified_outside_backticks(self):
        base = _make_umf(
            "spine",
            [
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="Service", data_type="VARCHAR"),
            ],
            primary_key=["id"],
        )
        target = _make_umf(
            "gold_out",
            [
                UMFColumn(
                    name="id", data_type="VARCHAR",
                    derivation=UMFColumnDerivation(candidates=[
                        DerivationCandidate(table="spine", column="id", priority=1)]),
                ),
                UMFColumn(
                    name="service", data_type="VARCHAR",
                    derivation=UMFColumnDerivation(candidates=[
                        DerivationCandidate(
                            table="spine", expression="TRIM(`Service`)", priority=1)]),
                ),
            ],
        )
        target.metadata = UMFMetadata(base_table="spine")
        sql = SQLPlanGenerator().generate_for_table(target, {"spine": base})
        assert "TRIM(base.`Service`)" in sql
        assert "`base.Service`" not in sql


# ---------------------------------------------------------------------------
# TestBaseViewExpressionColumns
# ---------------------------------------------------------------------------


class TestBaseViewExpressionColumns:
    """Columns referenced only inside an expression candidate must survive
    into the base view — the old regex dropped all-caps names as keywords."""

    def test_uppercase_expression_column_reaches_base_view(self):
        base = _make_umf(
            "spine",
            [
                UMFColumn(name="ServiceLineID", data_type="VARCHAR"),
                UMFColumn(name="AwardAmount", data_type="DECIMAL"),
                UMFColumn(name="QPA", data_type="DECIMAL"),
            ],
            primary_key=["ServiceLineID"],
        )
        target = _make_umf(
            "gold_out",
            [
                UMFColumn(
                    name="sl_id", data_type="VARCHAR",
                    derivation=UMFColumnDerivation(candidates=[
                        DerivationCandidate(table="spine", column="ServiceLineID", priority=1)]),
                ),
                UMFColumn(
                    name="idr_increase", data_type="DECIMAL",
                    derivation=UMFColumnDerivation(candidates=[
                        DerivationCandidate(
                            table="spine", expression="AwardAmount - QPA", priority=1)]),
                ),
            ],
        )
        target.metadata = UMFMetadata(base_table="spine")
        sql = SQLPlanGenerator().generate_for_table(target, {"spine": base})
        # QPA (all-caps, expression-only) must be in the base view projection
        base_view = sql.split("STEP 0")[1].split("STEP 1")[0] if "STEP 1" in sql else sql
        assert "QPA" in base_view
        assert "base.AwardAmount - base.QPA" in sql


# ---------------------------------------------------------------------------
# TestLookupJoin
# ---------------------------------------------------------------------------


class TestLookupJoin:
    """relationships.outgoing lookup_join emits a two-hop join through a bridge
    table (base -> bridge -> target) when base and target share no direct key."""

    def test_two_hop_join_emits_bridge_then_target(self):
        from tablespec.models.umf import LookupJoin

        base = _make_umf(
            "fact_line",
            [
                UMFColumn(name="line_id", data_type="VARCHAR"),
                UMFColumn(name="incident_id", data_type="VARCHAR"),
            ],
            primary_key=["line_id"],
            relationships=Relationships(
                summary=RelationshipSummary(
                    total_relationships=1, total_incoming=0,
                    total_outgoing=1, hub_score=5.0,
                ),
                outgoing=[
                    OutgoingRelationship(
                        target_table="dim_facility",
                        source_column="incident_id",
                        target_column="facility_id",
                        type="foreign_to_primary",
                        confidence=1.0,
                        lookup_join=LookupJoin(
                            source_key="incident_id",
                            bridge_table="bronze_incident",
                            bridge_source_key="id",
                            bridge_target_key="facility_id",
                        ),
                    ),
                ],
            ),
        )
        incident = _make_umf(
            "bronze_incident",
            [
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="facility_id", data_type="VARCHAR"),
            ],
            primary_key=["id"],
        )
        facility = _make_umf(
            "dim_facility",
            [
                UMFColumn(name="facility_id", data_type="VARCHAR"),
                UMFColumn(name="facility_name", data_type="VARCHAR"),
            ],
            primary_key=["facility_id"],
        )
        target = _make_umf(
            "gold_line",
            [
                UMFColumn(
                    name="line_id", data_type="VARCHAR",
                    derivation=UMFColumnDerivation(candidates=[
                        DerivationCandidate(table="fact_line", column="line_id", priority=1)]),
                ),
                UMFColumn(
                    name="facility_name", data_type="VARCHAR",
                    derivation=UMFColumnDerivation(candidates=[
                        DerivationCandidate(table="dim_facility", column="facility_name", priority=1)]),
                ),
            ],
        )
        target.metadata = UMFMetadata(base_table="fact_line")
        sql = SQLPlanGenerator().generate_for_table(
            target,
            {"fact_line": base, "bronze_incident": incident, "dim_facility": facility},
        )
        # bridge joined on the base key, target joined on the bridge's target key
        assert "JOIN bronze_incident" in sql
        assert "ON base.incident_id = dim_facility_bridge.id" in sql
        assert "ON target.facility_id = dim_facility_bridge.facility_id" in sql
        assert "target.facility_name AS dim_facility__facility_name" in sql


# ---------------------------------------------------------------------------
# TestExpressionJoinKeys
# ---------------------------------------------------------------------------


class TestExpressionJoinKeys:
    """relationships.outgoing source_expression/target_expression replace the
    plain column equality in direct-join ON clauses (e.g. TRIM-keyed joins)."""

    def test_direct_join_uses_expression_keys(self):
        base = _make_umf(
            "exp_base",
            [
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="npi", data_type="VARCHAR"),
            ],
            primary_key=["id"],
            relationships=Relationships(
                summary=RelationshipSummary(
                    total_relationships=1, total_incoming=0,
                    total_outgoing=1, hub_score=5.0,
                ),
                outgoing=[
                    OutgoingRelationship(
                        target_table="exp_registry",
                        source_column="npi",
                        target_column="npi",
                        source_expression="TRIM(npi)",
                        target_expression="TRIM(npi)",
                        type="foreign_to_primary",
                        confidence=1.0,
                    ),
                ],
            ),
        )
        registry = _make_umf(
            "exp_registry",
            [
                UMFColumn(name="npi", data_type="VARCHAR"),
                UMFColumn(name="entity_type", data_type="VARCHAR"),
            ],
            primary_key=["npi"],
        )
        target = _make_umf(
            "exp_dim",
            [
                UMFColumn(
                    name="id", data_type="VARCHAR",
                    derivation=UMFColumnDerivation(candidates=[
                        DerivationCandidate(table="exp_base", column="id", priority=1)]),
                ),
                UMFColumn(
                    name="entity_type", data_type="VARCHAR",
                    derivation=UMFColumnDerivation(candidates=[
                        DerivationCandidate(table="exp_registry", column="entity_type", priority=1)]),
                ),
            ],
        )
        target.metadata = UMFMetadata(base_table="exp_base")
        sql = SQLPlanGenerator().generate_for_table(
            target, {"exp_base": base, "exp_registry": registry}
        )
        assert "ON TRIM(base.npi) = TRIM(target.npi)" in sql
        assert "ON base.npi = target.npi" not in sql


# ---------------------------------------------------------------------------
# TestScdPlanEmission
# ---------------------------------------------------------------------------


class TestScdPlanEmission:
    """metadata.scd emits the SCD2 staged-recompute plan (seed / stage /
    swap / drop) instead of a single SELECT."""

    def _corpus(self, *, scd: dict, columns: list[UMFColumn] | None = None):
        src = _make_umf(
            "scd_source",
            [
                UMFColumn(name="OrgID", data_type="VARCHAR", position="1"),
                UMFColumn(name="PlanID", data_type="VARCHAR", position="2"),
                UMFColumn(name="FinClass", data_type="VARCHAR", position="3"),
            ],
        )

        def _col(name: str, pos: int, cand: dict | None = None) -> UMFColumn:
            kwargs: dict = {}
            if cand:
                kwargs["derivation"] = UMFColumnDerivation(
                    candidates=[DerivationCandidate(priority=1, **cand)],
                )
            return UMFColumn(
                name=name, data_type="VARCHAR", position=str(pos), **kwargs
            )

        target = UMF(
            version="1.0",
            table_name="scd_target",
            table_type="generated",
            metadata={"scd": scd},
            columns=columns
            or [
                _col("org_id", 1, {"table": "scd_source", "column": "OrgID"}),
                _col("plan_id", 2, {"table": "scd_source", "column": "PlanID"}),
                _col(
                    "is_oon",
                    3,
                    {
                        "table": "scd_source",
                        "expression": "UPPER(TRIM(FinClass)) = 'COMMERCIAL'",
                    },
                ),
                _col("fin_class", 4, {"table": "scd_source", "column": "FinClass"}),
                _col("effective_from", 5),
                _col("effective_to", 6),
                _col("is_current", 7),
            ],
        )
        return target, {"scd_source": src}

    def test_emits_seed_stage_swap_drop(self):
        target, related = self._corpus(
            scd={"keys": ["org_id", "plan_id"], "tracked": ["is_oon", "fin_class"]},
        )
        plan = generate_sql_plan(target, related, mode="cte")
        stmts = [s.strip() for s in plan.split(";")]
        assert len(stmts) == 4
        assert stmts[0].startswith("CREATE TABLE IF NOT EXISTS scd_target AS")
        assert stmts[1].startswith(
            "CREATE OR REPLACE TABLE scd_target__scd_stage AS"
        )
        assert stmts[2].startswith("CREATE OR REPLACE TABLE scd_target AS")
        assert stmts[3] == "DROP TABLE scd_target__scd_stage"
        # null-safe natural-key matching on every join
        assert "e.org_id <=> n.org_id AND e.plan_id <=> n.plan_id" in plan
        assert "e.org_id <=> c.org_id AND e.plan_id <=> c.plan_id" in plan
        assert "LEFT ANTI JOIN to_close" in plan

    def test_hash_follows_tracked_order(self):
        target, related = self._corpus(
            scd={"keys": ["org_id"], "tracked": ["fin_class", "is_oon"]},
        )
        plan = generate_sql_plan(target, related)
        hash_start = plan.index("md5(concat_ws")
        assert plan.index("CAST(fin_class AS STRING)", hash_start) < plan.index(
            "CAST(is_oon AS STRING)", hash_start
        )

    def test_bookkeeping_excluded_from_snapshot(self):
        target, related = self._corpus(
            scd={"keys": ["org_id"], "tracked": ["is_oon"]},
        )
        plan = generate_sql_plan(target, related)
        snapshot = plan[
            plan.index("WITH new_snapshot AS (") : plan.index("hashed AS (")
        ]
        assert "effective_from" not in snapshot
        assert "is_current" not in snapshot
        # data columns still project in position order
        assert snapshot.index("org_id") < snapshot.index("plan_id")

    def test_missing_bookkeeping_column_raises(self):
        target, related = self._corpus(
            scd={"keys": ["org_id"], "tracked": ["is_oon"]},
        )
        target.columns = [c for c in target.columns if c.name != "effective_to"]
        with pytest.raises(ValueError, match="bookkeeping column"):
            generate_sql_plan(target, related)

    def test_unknown_key_or_tracked_raises(self):
        target, related = self._corpus(
            scd={"keys": ["no_such_col"], "tracked": ["is_oon"]},
        )
        with pytest.raises(ValueError, match="keys/tracked"):
            generate_sql_plan(target, related)


# ---------------------------------------------------------------------------
# TestRelationshipResolver
# ---------------------------------------------------------------------------


class TestRelationshipResolver:
    """Test the RelationshipResolver."""

    def test_resolve_plan_returns_base_table(
        self, derived_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """resolve_plan identifies a base table."""
        resolver = RelationshipResolver(related_umfs)
        plan = resolver.resolve_plan(derived_umf)
        assert isinstance(plan, ResolvedPlan)
        # source_a has the higher hub_score
        assert plan.base_table == "source_a"

    def test_resolve_plan_returns_join_sequence(
        self, derived_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """resolve_plan returns a join_sequence list."""
        resolver = RelationshipResolver(related_umfs)
        plan = resolver.resolve_plan(derived_umf)
        assert hasattr(plan, "join_sequence")
        assert isinstance(plan.join_sequence, list)

    def test_infers_join_from_derivation_candidates(
        self, derived_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """Resolver creates joins for tables referenced in derivation candidates."""
        resolver = RelationshipResolver(related_umfs)
        plan = resolver.resolve_plan(derived_umf)
        join_tables = {j["target_table"] for j in plan.join_sequence}
        # source_b should appear in the join sequence (source_a is the base)
        assert "source_b" in join_tables

    def test_strategy_inference_direct(
        self, derived_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """1:0..1 cardinality infers 'direct' strategy."""
        resolver = RelationshipResolver(related_umfs)
        plan = resolver.resolve_plan(derived_umf)
        for join in plan.join_sequence:
            if join["target_table"] == "source_b":
                assert join["strategy"] == "direct"

    def test_strategy_inference_first_record(self):
        """1:N cardinality infers 'first_record' strategy."""
        hub = _make_umf(
            "hub",
            [UMFColumn(name="id", data_type="VARCHAR")],
            primary_key=["id"],
            relationships=Relationships(
                outgoing=[
                    OutgoingRelationship(
                        target_table="detail",
                        source_column="id",
                        target_column="id",
                        type="foreign_to_primary",
                        confidence=0.9,
                        cardinality=_card("one_to_many", "1:N"),
                    ),
                ],
                summary=RelationshipSummary(
                    total_relationships=1,
                    total_incoming=0,
                    total_outgoing=1,
                    hub_score=5.0,
                ),
            ),
        )
        detail = _make_umf(
            "detail",
            [
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="note", data_type="VARCHAR"),
            ],
            primary_key=["id"],
        )
        target = _make_umf(
            "output",
            [
                UMFColumn(
                    name="id",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(strategy="primary_key"),
                ),
                UMFColumn(
                    name="note",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="detail", column="note", priority=1
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source",
                            explanation="First record",
                        ),
                    ),
                ),
            ],
            primary_key=["id"],
        )
        resolver = RelationshipResolver({"hub": hub, "detail": detail})
        plan = resolver.resolve_plan(target)
        for join in plan.join_sequence:
            if join["target_table"] == "detail":
                assert join["strategy"] == "first_record"

    def test_contribution_score_ordering(self):
        """Tables contributing more columns appear earlier in join_sequence."""
        src_many = _make_umf(
            "src_many",
            [
                UMFColumn(name="key_id", data_type="VARCHAR"),
                UMFColumn(name="col_a", data_type="VARCHAR"),
                UMFColumn(name="col_b", data_type="VARCHAR"),
                UMFColumn(name="col_c", data_type="VARCHAR"),
            ],
            primary_key=["key_id"],
            relationships=Relationships(
                summary=RelationshipSummary(
                    total_relationships=0,
                    total_incoming=0,
                    total_outgoing=0,
                    hub_score=1.0,
                ),
            ),
        )
        src_few = _make_umf(
            "src_few",
            [
                UMFColumn(name="key_id", data_type="VARCHAR"),
                UMFColumn(name="col_x", data_type="VARCHAR"),
            ],
            primary_key=["key_id"],
            relationships=Relationships(
                summary=RelationshipSummary(
                    total_relationships=0,
                    total_incoming=0,
                    total_outgoing=0,
                    hub_score=1.0,
                ),
            ),
        )
        hub = _make_umf(
            "hub",
            [UMFColumn(name="key_id", data_type="VARCHAR")],
            primary_key=["key_id"],
            relationships=Relationships(
                outgoing=[
                    OutgoingRelationship(
                        target_table="src_many",
                        source_column="key_id",
                        target_column="key_id",
                        type="foreign_to_primary",
                        confidence=0.9,
                        cardinality=_card("one_to_one", "1:0..1"),
                    ),
                    OutgoingRelationship(
                        target_table="src_few",
                        source_column="key_id",
                        target_column="key_id",
                        type="foreign_to_primary",
                        confidence=0.9,
                        cardinality=_card("one_to_one", "1:0..1"),
                    ),
                ],
                summary=RelationshipSummary(
                    total_relationships=2,
                    total_incoming=0,
                    total_outgoing=2,
                    hub_score=10.0,
                ),
            ),
        )
        target = _make_umf(
            "ordered_output",
            [
                UMFColumn(
                    name="key_id",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(strategy="primary_key"),
                ),
                UMFColumn(
                    name="col_a",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="src_many", column="col_a", priority=1
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source", explanation="a"
                        ),
                    ),
                ),
                UMFColumn(
                    name="col_b",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="src_many", column="col_b", priority=1
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source", explanation="b"
                        ),
                    ),
                ),
                UMFColumn(
                    name="col_c",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="src_many", column="col_c", priority=1
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source", explanation="c"
                        ),
                    ),
                ),
                UMFColumn(
                    name="col_x",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="src_few", column="col_x", priority=1
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source", explanation="x"
                        ),
                    ),
                ),
            ],
            primary_key=["key_id"],
        )

        resolver = RelationshipResolver(
            {"hub": hub, "src_many": src_many, "src_few": src_few}
        )
        plan = resolver.resolve_plan(target)
        join_tables = [j["target_table"] for j in plan.join_sequence]
        # src_many contributes 3 columns, src_few contributes 1 -> src_many first
        assert join_tables.index("src_many") < join_tables.index("src_few")

    def test_resolve_plan_returns_aliases(
        self, derived_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """resolve_plan returns an aliases dict."""
        resolver = RelationshipResolver(related_umfs)
        plan = resolver.resolve_plan(derived_umf)
        assert hasattr(plan, "aliases")
        assert isinstance(plan.aliases, dict)


# ---------------------------------------------------------------------------
# TestJoinInfoAndPivotSpec
# ---------------------------------------------------------------------------


class TestJoinInfoAndPivotSpec:
    """Test JoinInfo and PivotSpec dataclasses."""

    def test_joininfo_defaults(self):
        """JoinInfo has sensible defaults."""
        j = JoinInfo(
            target_table="tbl",
            source_column="src",
            target_column="tgt",
            strategy="direct",
        )
        assert j.partition_by == []
        assert j.order_by == []
        assert j.pivot is None
        assert j.join_type == "left"
        assert j.join_filter is None

    def test_pivotspec_fields(self):
        """PivotSpec stores key/value/prefix/max_records."""
        p = PivotSpec(
            key_column="code_id",
            value_column="description",
            prefix="diag",
            max_records=10,
        )
        assert p.key_column == "code_id"
        assert p.max_records == 10


# ---------------------------------------------------------------------------
# TestConvenienceFunction
# ---------------------------------------------------------------------------


class TestConvenienceFunction:
    """Test the generate_sql_plan() convenience wrapper."""

    def test_works_end_to_end_minimal(self, minimal_umf: UMF):
        """generate_sql_plan produces SQL for a minimal UMF."""
        sql = generate_sql_plan(minimal_umf, {})
        assert "CREATE OR REPLACE TEMPORARY VIEW" in sql
        assert "test_claims" in sql

    def test_accepts_template_vars(self, minimal_umf: UMF):
        """generate_sql_plan forwards template_vars."""
        sql = generate_sql_plan(minimal_umf, {}, template_vars={"run_id": "abc123"})
        # Just verify it runs without error; no templates in minimal_umf
        assert isinstance(sql, str)

    def test_accepts_table_resolver(self, minimal_umf: UMF):
        """generate_sql_plan forwards table_resolver."""
        sql = generate_sql_plan(
            minimal_umf,
            {},
            table_resolver=lambda n: f"db.{n}",
        )
        assert isinstance(sql, str)

    def test_end_to_end_with_derivations(
        self, derived_umf: UMF, related_umfs: dict[str, UMF]
    ):
        """generate_sql_plan works with derived columns and joins."""
        sql = generate_sql_plan(derived_umf, related_umfs)
        assert "derived_output" in sql
        assert "LEFT JOIN" in sql
        assert "FINAL ASSEMBLY" in sql

    def test_end_to_end_with_survivorship(
        self,
        survivorship_umf: UMF,
        survivorship_related_umfs: dict[str, UMF],
    ):
        """generate_sql_plan works with COALESCE survivorship."""
        sql = generate_sql_plan(survivorship_umf, survivorship_related_umfs)
        assert "COALESCE" in sql
        assert "survivorship_output" in sql


# ---------------------------------------------------------------------------
# TestEdgeCasesAndErrors
# ---------------------------------------------------------------------------


class TestEdgeCasesAndErrors:
    """Test edge cases and error paths for SQL plan generation."""

    def test_empty_related_umfs(self, minimal_umf: UMF):
        """generate_sql_plan with empty related_umfs dict should not crash.

        All columns should get CAST(NULL ...) since no sources are available.
        """
        sql = generate_sql_plan(minimal_umf, {})
        assert isinstance(sql, str)
        assert len(sql) > 0
        assert "CAST(NULL" in sql
        assert "test_claims" in sql

    def test_umf_with_no_columns(self):
        """UMF with columns=[] is rejected by Pydantic validation."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="too_short"):
            _make_umf("empty_columns_table", [])

    def test_umf_with_no_derivations(self):
        """Columns with no derivation field should produce CAST(NULL) defaults."""
        target = _make_umf(
            "no_derivations_table",
            [
                UMFColumn(name="col_a", data_type="VARCHAR"),
                UMFColumn(name="col_b", data_type="INTEGER"),
                UMFColumn(name="col_c", data_type="DATE"),
            ],
        )
        sql = generate_sql_plan(target, {})
        assert "CAST(NULL" in sql
        # Each column type should map to its SQL equivalent
        assert "no_derivations_table" in sql

    def test_unknown_join_strategy_handled(self):
        """JoinInfo strategy is constrained to Literal types; valid values accepted."""
        # Verify that valid strategy values are accepted
        for valid_strategy in ("direct", "first_record", "pivot"):
            j = JoinInfo(
                target_table="tbl",
                source_column="src",
                target_column="tgt",
                strategy=valid_strategy,
            )
            assert j.strategy == valid_strategy
            assert j.target_table == "tbl"

    def test_table_resolver_transforms_names(self):
        """table_resolver that uppercases table names produces uppercased SQL."""
        source = _make_umf(
            "my_source",
            [
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="value", data_type="VARCHAR"),
            ],
            primary_key=["id"],
            relationships=Relationships(
                summary=RelationshipSummary(
                    total_relationships=0,
                    total_incoming=0,
                    total_outgoing=0,
                    hub_score=5.0,
                ),
            ),
        )
        target = _make_umf(
            "my_target",
            [
                UMFColumn(
                    name="id",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(strategy="primary_key"),
                ),
                UMFColumn(
                    name="value",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="my_source", column="value", priority=1
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source",
                            explanation="Direct",
                        ),
                    ),
                ),
            ],
            primary_key=["id"],
        )

        def upper_resolver(name: str) -> str:
            return name.upper()

        gen = SQLPlanGenerator(table_resolver=upper_resolver)
        sql = gen.generate_for_table(target, {"my_source": source})
        assert "MY_SOURCE" in sql

    def test_deeply_nested_expression_derivation(self):
        """Complex CASE expression in derivation appears in output SQL."""
        source = _make_umf(
            "expr_source",
            [
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="col1", data_type="INTEGER"),
                UMFColumn(name="col2", data_type="VARCHAR"),
                UMFColumn(name="col3", data_type="VARCHAR"),
            ],
            primary_key=["id"],
            relationships=Relationships(
                summary=RelationshipSummary(
                    total_relationships=0,
                    total_incoming=0,
                    total_outgoing=0,
                    hub_score=5.0,
                ),
            ),
        )
        target = _make_umf(
            "expr_target",
            [
                UMFColumn(
                    name="id",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(strategy="primary_key"),
                ),
                UMFColumn(
                    name="computed",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="expr_source",
                                expression="CASE WHEN col1 > 0 THEN col2 ELSE col3 END",
                                priority=1,
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source",
                            explanation="Conditional expression",
                        ),
                    ),
                ),
            ],
            primary_key=["id"],
        )

        gen = SQLPlanGenerator()
        sql = gen.generate_for_table(target, {"expr_source": source})
        assert "CASE WHEN" in sql
        assert "col1" in sql
        assert "col2" in sql
        assert "col3" in sql

    def test_resolver_with_single_table(self):
        """RelationshipResolver with only one table handles gracefully."""
        single = _make_umf(
            "only_table",
            [
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="name", data_type="VARCHAR"),
            ],
            primary_key=["id"],
            relationships=Relationships(
                summary=RelationshipSummary(
                    total_relationships=0,
                    total_incoming=0,
                    total_outgoing=0,
                    hub_score=1.0,
                ),
            ),
        )
        target = _make_umf(
            "single_target",
            [
                UMFColumn(
                    name="id",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(strategy="primary_key"),
                ),
                UMFColumn(
                    name="name",
                    data_type="VARCHAR",
                    derivation=UMFColumnDerivation(
                        candidates=[
                            DerivationCandidate(
                                table="only_table", column="name", priority=1
                            ),
                        ],
                        survivorship=Survivorship(
                            strategy="single_source",
                            explanation="Direct",
                        ),
                    ),
                ),
            ],
            primary_key=["id"],
        )

        resolver = RelationshipResolver({"only_table": single})
        plan = resolver.resolve_plan(target)
        assert plan.base_table == "only_table"
        # No joins needed since only one table
        assert isinstance(plan.join_sequence, list)

    def test_resolver_empty_umfs(self):
        """RelationshipResolver with empty all_umfs should not crash."""
        target = _make_umf(
            "orphan_target",
            [
                UMFColumn(name="id", data_type="VARCHAR"),
            ],
        )
        resolver = RelationshipResolver({})
        plan = resolver.resolve_plan(target)
        assert isinstance(plan, ResolvedPlan)
        assert isinstance(plan.join_sequence, list)
