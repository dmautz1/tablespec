"""LLM spec enrichment: OpenAI-compatible client + the enrich orchestrator.

Requires the ``[llm]`` extra (``pip install 'tablespec[llm]'``) to actually
call an endpoint; importing this package is dependency-free.
"""

from __future__ import annotations

from tablespec.llm.client import (
    DEFAULT_DATABRICKS_MODEL,
    LlmClient,
    LlmConfigError,
    extract_json,
)
from tablespec.llm.enrich import (
    ENRICH_INCLUDES,
    EnrichSummary,
    TableEnrichment,
    enrich_excel_specs,
    enrich_specs,
    enrich_umfs,
)

__all__ = [
    "DEFAULT_DATABRICKS_MODEL",
    "ENRICH_INCLUDES",
    "EnrichSummary",
    "LlmClient",
    "LlmConfigError",
    "TableEnrichment",
    "enrich_excel_specs",
    "enrich_specs",
    "enrich_umfs",
    "extract_json",
]
