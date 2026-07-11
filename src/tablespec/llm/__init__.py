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
    enrich_specs,
)

__all__ = [
    "DEFAULT_DATABRICKS_MODEL",
    "ENRICH_INCLUDES",
    "EnrichSummary",
    "LlmClient",
    "LlmConfigError",
    "TableEnrichment",
    "enrich_specs",
    "extract_json",
]
