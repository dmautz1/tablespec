"""Apply LLM enrichment responses to UMF models (descriptions, relationships).

Companions to :func:`tablespec.authoring.apply_response.apply_validation_response`
(which covers expectations). Both appliers here are FILL-ONLY by default: they
never overwrite human-authored content -- a field is only written when it is
currently empty (``overwrite=True`` relaxes that for descriptions).

Response shapes are the strict-JSON contracts documented in
``tablespec.prompts.enrichment``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tablespec.models.umf import UMF, ForeignKey, Relationships


@dataclass
class EnrichmentApplied:
    """What :func:`apply_enrichment_response` changed on one table."""

    table_description: bool = False
    descriptions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sample_values: list[str] = field(default_factory=list)
    updated_umf: UMF | None = None

    @property
    def total(self) -> int:
        return (
            int(self.table_description)
            + len(self.descriptions)
            + len(self.notes)
            + len(self.sample_values)
        )


def _clean_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def apply_enrichment_response(
    umf: UMF, response: dict[str, Any], *, overwrite: bool = False
) -> EnrichmentApplied:
    """Apply a descriptions/notes/sample-values enrichment response to *umf*.

    Fill-only by default: table/column descriptions, notes, and sample values
    are written only when currently empty. ``overwrite=True`` lets the response
    replace existing descriptions (notes/sample_values stay fill-only -- they
    are cumulative human surfaces). Unknown column keys are ignored.
    """
    applied = EnrichmentApplied()
    by_col = response.get("columns") or {}

    columns = []
    for col in umf.columns:
        update: dict[str, Any] = {}
        enrichment = by_col.get(col.name)
        if isinstance(enrichment, dict):
            desc = str(enrichment.get("description") or "").strip()
            if desc and (overwrite or not col.description):
                update["description"] = desc
                applied.descriptions.append(col.name)
            notes = _clean_str_list(enrichment.get("notes"))
            if notes and not col.notes:
                update["notes"] = notes
                applied.notes.append(col.name)
            samples = _clean_str_list(enrichment.get("sample_values"))
            if samples and not col.sample_values:
                update["sample_values"] = samples
                applied.sample_values.append(col.name)
        columns.append(col.model_copy(update=update) if update else col)

    table_update: dict[str, Any] = {"columns": columns}
    table_desc = str(response.get("table_description") or "").strip()
    if table_desc and (overwrite or not umf.description):
        table_update["description"] = table_desc
        applied.table_description = True

    applied.updated_umf = umf.model_copy(update=table_update)
    return applied


def apply_relationships_response(
    umfs: list[UMF], response: dict[str, Any]
) -> dict[str, tuple[UMF, int]]:
    """Apply a relationships-discovery response across a UMF set.

    Each proposed relationship is upserted as a ``foreign_keys`` entry on its
    SOURCE table (the shape ``core.schema_facts.relationship_tests`` and the
    dbt emitters consume). Skipped: relationships whose source or target table
    is not in the set, whose source column does not exist, or that duplicate an
    existing FK (same column -> table.column).

    Returns ``{table_name: (updated_umf, fks_added)}`` for every input table.
    """
    by_name = {u.table_name: u for u in umfs}
    added: dict[str, list[ForeignKey]] = {name: [] for name in by_name}

    for rel in response.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        source_table = rel.get("source_table")
        target_table = rel.get("target_table")
        column = rel.get("source_column")
        target_column = rel.get("target_column")
        if not (source_table and target_table and column and target_column):
            continue
        umf = by_name.get(source_table)
        if umf is None or target_table not in by_name:
            continue
        if column not in {c.name for c in umf.columns}:
            continue
        existing = {
            (fk.column, fk.references_table, fk.references_column)
            for fk in (umf.relationships.foreign_keys if umf.relationships else None)
            or []
        } | {
            (fk.column, fk.references_table, fk.references_column)
            for fk in added[source_table]
        }
        if (column, target_table, target_column) in existing:
            continue
        confidence = rel.get("confidence")
        added[source_table].append(
            ForeignKey(
                column=column,
                references_table=target_table,
                references_column=target_column,
                confidence=float(confidence)
                if isinstance(confidence, (int, float))
                else None,
                type=str(rel.get("relationship_type") or "foreign_key"),
                detection_method="llm",
            )
        )

    out: dict[str, tuple[UMF, int]] = {}
    for name, umf in by_name.items():
        new_fks = added[name]
        if not new_fks:
            out[name] = (umf, 0)
            continue
        relationships = umf.relationships or Relationships()
        relationships = relationships.model_copy(
            update={"foreign_keys": [*(relationships.foreign_keys or []), *new_fks]}
        )
        out[name] = (
            umf.model_copy(update={"relationships": relationships}),
            len(new_fks),
        )
    return out


__all__ = [
    "EnrichmentApplied",
    "apply_enrichment_response",
    "apply_relationships_response",
]
