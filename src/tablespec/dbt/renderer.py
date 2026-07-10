"""The dbt implementation of the core :class:`TableRenderer` seam.

``DbtRefRenderer`` turns a physical relation name the SQL generator wants to
inline into a STATIC dbt Jinja literal (``{{ ref(...) }}`` for a model,
``{{ source(...) }}`` for a raw landing table) using the planner's
:class:`~tablespec.dbt.registry.NodeRegistry`.

Two corrected-design invariants live here:

  * **Semantic, not string-rewriting.** The generator hands us the *name of a
    relation*; we map the name -> node -> ref. We never inspect or substitute SQL
    aliases (``m``, ``base``, ``src``).
  * **Fail closed.** An unknown relation raises :class:`UnknownRelationError`. It
    only becomes a ``source('external', ...)`` when the UMF explicitly marked it
    external (an external SOURCE node exists in the registry for it).
"""

from __future__ import annotations

from tablespec.core.ir import NodeRole
from tablespec.dbt.registry import NodeRegistry
from tablespec.dbt.routing import RoutingPolicy


class UnknownRelationError(LookupError):
    """Raised when a relation name resolves to no node and is not external."""


class DbtRefRenderer:
    """Render relation names as static ``{{ ref() }}`` / ``{{ source() }}`` literals.

    Implements :class:`tablespec.core.relations.TableRenderer`. Injected into
    ``SQLPlanGenerator`` so the gold model body carries dbt edges visible to the
    parser at parse time.
    """

    def __init__(
        self,
        registry: NodeRegistry,
        routing: RoutingPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._routing = routing or RoutingPolicy()

    def render(self, physical_name: str) -> str:
        resolved = self._registry.resolve(physical_name)
        if resolved is None:
            msg = (
                f"Unknown relation {physical_name!r}: it maps to no UMF table and "
                f"is not marked external. Refusing to emit a phantom "
                f"source('external', ...) edge (fail closed). Add the table to the "
                f"UMF set or mark the reference external explicitly."
            )
            raise UnknownRelationError(msg)

        if resolved.role is NodeRole.SOURCE:
            if resolved.external:
                # Explicitly external relation -> a dedicated 'external' source
                # group (NOT the local 'raw' landing source).
                return f"{{{{ source('external', '{resolved.node_id}') }}}}"
            # raw_<t> landing table -> the local raw source, or a ref() to the
            # emitted file-reading landing model when it is model-backed.
            return self._routing.raw_literal(resolved.node_id)
        # ingested_<t> / gold_<t> -> a dbt model ref.
        return self._routing.ref_literal(resolved.node_id)


__all__ = ["DbtRefRenderer", "UnknownRelationError"]
