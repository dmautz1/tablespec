"""How a UMF table's physical identity maps to dbt source / model routing.

A UMF table has no first-class ``catalog.schema`` today, so routing is policy:

  * Raw landing tables are dbt **sources** under the ``raw`` source, schema
    ``main`` for the duckdb/CI target. In prod (databricks) the source database/
    schema come from the configured ``RoutingPolicy`` (the ``raw_database`` /
    ``raw_schema``); the source identifier is always ``raw_<table>``.
  * Ingested + gold relations are dbt **models**. Their database/schema/alias are
    governed by dbt's standard target + ``models:`` config, NOT hard-coded into
    the SQL: dev resolves them in the dev schema, prod in the prod schema, via the
    same ``{{ ref() }}`` literal. The model *name* (and default alias) is the
    node id (``ingested_<t>`` / ``gold_<t>``).

This module is intentionally tiny and declarative: it carries the dev/prod
routing knobs and renders the ``source(...)`` / ``ref(...)`` Jinja literals. It
contains NO SQL-generation logic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingPolicy:
    """Routing knobs for source/model placement across dev and prod.

    Attributes:
        source_name: dbt source group name for raw landing tables.
        raw_schema: schema the raw source tables live in (CI/duckdb default
            ``main``; prod overrides via the generated ``sources.yml``/profile).
        raw_database: optional source database (prod catalog); ``None`` for duckdb.
        model_backed_raw: raw identifiers (``raw_<t>``) emitted as file-reading
            MODELS rather than declared sources (see ``tablespec.dbt.raw_models``);
            :meth:`raw_literal` renders these as ``ref()`` edges.
    """

    source_name: str = "raw"
    raw_schema: str = "main"
    raw_database: str | None = None
    model_backed_raw: frozenset[str] = frozenset()

    def source_literal(self, raw_identifier: str) -> str:
        """Render ``{{ source('<source_name>', '<raw_identifier>') }}``."""
        return f"{{{{ source('{self.source_name}', '{raw_identifier}') }}}}"

    @staticmethod
    def ref_literal(model_name: str) -> str:
        """Render ``{{ ref('<model_name>') }}`` (db/schema resolved by dbt target)."""
        return f"{{{{ ref('{model_name}') }}}}"

    def raw_literal(self, raw_identifier: str) -> str:
        """Render the edge to a raw landing relation.

        ``ref()`` when the landing is an emitted file-reading model
        (``model_backed_raw``), else the classic ``source()`` declaration.
        """
        if raw_identifier in self.model_backed_raw:
            return self.ref_literal(raw_identifier)
        return self.source_literal(raw_identifier)


__all__ = ["RoutingPolicy"]
