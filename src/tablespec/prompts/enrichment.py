"""Prompts for LLM spec enrichment: descriptions/notes/samples + relationships.

Companions to the existing prompt builders that return STRICT-JSON asks so the
responses are machine-appliable (``tablespec.authoring.enrich``):

* :func:`generate_enrichment_prompt` -- per-table descriptions, business notes,
  and sample values (``documentation.py`` asks for prose; this is its
  structured sibling).
* :func:`generate_set_relationships_prompt` -- FK discovery over an in-memory
  UMF set (``relationship.py`` reads legacy ``*.umf.yaml`` files from disk;
  this one works on loaded models, e.g. split-format spec dirs).

Like every module in ``tablespec.prompts``, these build prompt STRINGS only --
no LLM is called here (see ``tablespec.llm`` for the client).
"""

from __future__ import annotations

import json
from typing import Any

#: Column ``source`` values that are pipeline-synthesized -- excluded from
#: enrichment (an LLM has nothing to say about provenance plumbing).
_NON_DATA_SOURCES: frozenset[str] = frozenset({"filename", "metadata", "derived"})


def _data_columns(umf_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        col
        for col in umf_data.get("columns") or []
        if col.get("source", "data") not in _NON_DATA_SOURCES
    ]


def _column_summary(col: dict[str, Any]) -> dict[str, Any]:
    """The column facts worth showing the LLM (omit empty fields)."""
    keep = ("name", "data_type", "length", "format", "description", "sample_values")
    summary = {k: col[k] for k in keep if col.get(k)}
    nullable = col.get("nullable")
    if isinstance(nullable, dict):
        summary["nullable"] = nullable
    return summary


def generate_enrichment_prompt(umf_data: dict[str, Any]) -> str:
    """Prompt for table + column descriptions, notes, and sample values.

    The response contract (strict JSON, no prose):

    .. code-block:: json

        {
          "table_description": "...",
          "columns": {
            "<column_name>": {
              "description": "...",
              "notes": ["..."],
              "sample_values": ["..."]
            }
          }
        }
    """
    table = umf_data.get("table_name", "unknown")
    cols = [_column_summary(c) for c in _data_columns(umf_data)]
    existing_desc = umf_data.get("description") or "(none)"

    return f"""You are a data engineer documenting a table schema.

Table: {table}
Existing table description: {existing_desc}

Columns (existing metadata shown where present):
{json.dumps(cols, indent=2)}

Write concise, factual documentation for this table and its columns:
- "description": one or two sentences on what the column holds and how it is
  used. Infer from the name, type, format, and sample values; do not invent
  specifics you cannot support.
- "notes": zero or more short business-rule or data-quality observations
  (e.g. expected code sets, casing, known caveats). Omit when you have none.
- "sample_values": 3-5 PLAUSIBLE example values consistent with the type and
  format. These are illustrative documentation, NOT real data -- keep them
  obviously generic (no realistic names, SSNs, or identifiers that could be
  mistaken for real records). Skip when existing sample_values are shown.

Respond with ONLY a JSON object in exactly this shape (no markdown fences, no
commentary):
{{
  "table_description": "...",
  "columns": {{
    "<column_name>": {{
      "description": "...",
      "notes": ["..."],
      "sample_values": ["..."]
    }}
  }}
}}
Include every column listed above, keyed by its exact name."""


def generate_set_relationships_prompt(umfs_data: list[dict[str, Any]]) -> str:
    """Prompt for FK discovery across an in-memory UMF set.

    Response contract matches the flat shape ``prompts.relationship`` documents
    (and ``authoring.enrich.apply_relationships_response`` consumes):

    .. code-block:: json

        {
          "relationships": [
            {
              "source_table": "...", "source_column": "...",
              "target_table": "...", "target_column": "...",
              "relationship_type": "foreign_key",
              "confidence": 0.9,
              "reasoning": "..."
            }
          ]
        }
    """
    tables = [
        {
            "table_name": u.get("table_name"),
            "primary_key": u.get("primary_key") or [],
            "columns": [_column_summary(c) for c in _data_columns(u)],
        }
        for u in umfs_data
    ]

    return f"""You are a data engineer identifying foreign-key relationships in a schema.

Tables:
{json.dumps(tables, indent=2)}

Identify foreign-key relationships BETWEEN these tables only:
- A relationship means a column in one table references a key column of
  another table (matching names/types like customer_id -> customers.id are
  the usual signal).
- Only propose targets that look like identifying columns (primary keys or
  unique identifiers) of the target table.
- Set "confidence" between 0 and 1; do not force relationships that are not
  evident. An empty list is a valid answer.

Respond with ONLY a JSON object in exactly this shape (no markdown fences, no
commentary):
{{
  "relationships": [
    {{
      "source_table": "...",
      "source_column": "...",
      "target_table": "...",
      "target_column": "...",
      "relationship_type": "foreign_key",
      "confidence": 0.0,
      "reasoning": "..."
    }}
  ]
}}"""


__all__ = ["generate_enrichment_prompt", "generate_set_relationships_prompt"]
