"""Enrich a spec directory with an LLM: descriptions, expectations, FKs.

The orchestrator that closes the repo's prompt -> LLM -> apply loop:

  1. Load every split-format spec under ``spec_dir``.
  2. Per table: :func:`~tablespec.prompts.enrichment.generate_enrichment_prompt`
     (descriptions/notes/sample values) and
     :func:`~tablespec.prompts.validation.generate_validation_prompt`
     (table-level GX expectations), each one LLM call.
  3. Once per set (2+ tables):
     :func:`~tablespec.prompts.enrichment.generate_set_relationships_prompt`
     for FK discovery.
  4. Apply via the fill-only appliers (``authoring.enrich``,
     ``authoring.apply_response``) and save the specs back in place.

Every applier is conservative: existing human-authored content is never
overwritten (see ``overwrite=`` for descriptions), expectations are
deduplicated, and FK proposals are dropped unless both tables and the source
column exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tablespec.authoring.apply_response import apply_validation_response
from tablespec.authoring.enrich import (
    apply_enrichment_response,
    apply_relationships_response,
)
from tablespec.llm.client import LlmClient
from tablespec.models.umf import UMF
from tablespec.prompts.enrichment import (
    generate_enrichment_prompt,
    generate_set_relationships_prompt,
)
from tablespec.prompts.validation import generate_validation_prompt

#: Enrichment passes ``enrich_specs`` can run.
ENRICH_INCLUDES: tuple[str, ...] = ("descriptions", "validations", "relationships")

_SYSTEM = (
    "You are a precise data engineer. Respond with ONLY the requested JSON -- "
    "no markdown fences, no commentary."
)


@dataclass
class TableEnrichment:
    """Per-table outcome of an enrichment run."""

    table: str
    descriptions: int = 0
    notes: int = 0
    sample_values: int = 0
    expectations_added: int = 0
    foreign_keys_added: int = 0


@dataclass
class EnrichSummary:
    """Outcome of :func:`enrich_specs` across the spec set."""

    tables: list[TableEnrichment] = field(default_factory=list)

    def __str__(self) -> str:
        lines = []
        for t in self.tables:
            lines.append(
                f"{t.table}: {t.descriptions} descriptions, {t.notes} notes, "
                f"{t.sample_values} sample-value sets, "
                f"{t.expectations_added} expectations, {t.foreign_keys_added} FKs"
            )
        return "\n".join(lines) or "(nothing enriched)"


def enrich_specs(
    spec_dir: str | Path,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    include: tuple[str, ...] = ENRICH_INCLUDES,
    overwrite: bool = False,
    save: bool = True,
    client: LlmClient | None = None,
) -> EnrichSummary:
    """Enrich every split-format spec under *spec_dir* with an LLM.

    Args:
        spec_dir: directory of split-format table specs (``<t>/table.yaml``).
        model / base_url / api_key: LLM endpoint configuration (see
            :class:`~tablespec.llm.client.LlmClient` for env-var and
            Databricks-ambient resolution).
        include: which passes to run (subset of :data:`ENRICH_INCLUDES`).
        overwrite: let LLM descriptions replace existing ones (notes, sample
            values, expectations, and FKs are always additive/fill-only).
        save: write the enriched specs back in place (split format).
        client: pre-built client (overrides model/base_url/api_key).

    Returns:
        An :class:`EnrichSummary` with per-table counts.
    """
    from tablespec.umf_loader import UMFLoader

    unknown = set(include) - set(ENRICH_INCLUDES)
    if unknown:
        msg = f"Unknown enrichment pass(es) {sorted(unknown)}; valid: {ENRICH_INCLUDES}"
        raise ValueError(msg)

    base = Path(spec_dir)
    loader = UMFLoader()
    table_dirs = sorted({p.parent for p in base.rglob("table.yaml")})
    if not table_dirs:
        msg = f"No split-format specs (table.yaml) under {base}"
        raise FileNotFoundError(msg)
    umfs: list[UMF] = [loader.load(d) for d in table_dirs]
    # Save each spec back where it was loaded from (spec dirs may be nested).
    dir_by_table = {u.table_name: d for u, d in zip(umfs, table_dirs)}

    llm = client or LlmClient(model=model, base_url=base_url, api_key=api_key)
    summary = EnrichSummary(tables=[TableEnrichment(table=u.table_name) for u in umfs])
    stats = {t.table: t for t in summary.tables}

    enriched: list[UMF] = []
    for umf in umfs:
        stat = stats[umf.table_name]
        umf_data = umf.model_dump(exclude_none=True)

        if "descriptions" in include:
            response = llm.complete_json(
                generate_enrichment_prompt(umf_data), system=_SYSTEM
            )
            applied = apply_enrichment_response(umf, response, overwrite=overwrite)
            assert applied.updated_umf is not None
            umf = applied.updated_umf
            stat.descriptions = len(applied.descriptions) + int(
                applied.table_description
            )
            stat.notes = len(applied.notes)
            stat.sample_values = len(applied.sample_values)

        if "validations" in include:
            response = llm.complete_json(
                generate_validation_prompt(umf.model_dump(exclude_none=True)),
                system=_SYSTEM,
            )
            expectations = (
                response.get("expectations", [])
                if isinstance(response, dict)
                else response
            )
            result = apply_validation_response(umf, expectations)
            umf = result.updated_umf
            stat.expectations_added = len(result.added)

        enriched.append(umf)

    if "relationships" in include and len(enriched) > 1:
        response = llm.complete_json(
            generate_set_relationships_prompt(
                [u.model_dump(exclude_none=True) for u in enriched]
            ),
            system=_SYSTEM,
        )
        applied_rels = apply_relationships_response(enriched, response)
        enriched = []
        for name, (umf, fks_added) in applied_rels.items():
            stats[name].foreign_keys_added = fks_added
            enriched.append(umf)

    if save:
        for umf in enriched:
            loader.save(umf, dir_by_table[umf.table_name])

    return summary


__all__ = ["ENRICH_INCLUDES", "EnrichSummary", "TableEnrichment", "enrich_specs"]
