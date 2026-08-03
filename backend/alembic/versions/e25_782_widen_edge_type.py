"""Widen the valid_edge_type CHECK to add continues_from + references_file (#782).

The connector worker emits two producer-asserted structural relation
types that did not exist in the post-#741 four-value ``edge_type`` set:

* ``continues_from``  — chronological/narrative successor between chat memories.
* ``references_file`` — structural reference from a chat memory to a file overview.

Both live on the ``edge_type`` (relation) axis, coherent with the #741/#722
two-axis model; their provenance is carried on the ``origin`` axis
(``origin='declared'`` for the worker create_edge path). Adding them lets the
worker drop its SDK-boundary ``_EDGE_TYPE_WORKAROUND_MAP`` — which translated
``references_file -> declared_link``, a value #741 removed, so those edges are
currently rejected in prod.

### No data migration

This is a pure CHECK-constraint widening: existing rows already satisfy the
wider constraint, so no UPDATE is needed on upgrade. (Contrast #741, whose
Phase 0/1 backfill was required because it *narrowed* the set and relocated
provenance.)

### Reversibility (downgrade caveats)

``downgrade()`` narrows the CHECK back to the four post-#741 values. Postgres
validates existing rows when (re)creating a CHECK constraint, so any row written
with the new types after this migration would block the narrowing. ``downgrade``
therefore first remaps ``continues_from`` / ``references_file`` to ``related_to``
(the closest surviving relation; ``declared_link`` no longer exists). This is
lossy at TWO axes — symmetric with #741's lossy collapse — and operators should
be explicit about both before downgrading:

* **edge_type axis**: the original ``continues_from`` / ``references_file``
  distinction is gone. Re-upgrading does NOT split the collapsed ``related_to``
  rows back out — the producer must re-ingest to recover the canonical types
  via UPSERT (3-col uniqueness; edge_type is max-weight UPSERT metadata).
* **directionality axis**: ``continues_from`` and ``references_file`` are
  inherently directional (per ``services/graph_service.py`` class docstring),
  while ``related_to`` is undirected by convention (see
  ``services/sleep/edge_discovery.py::DIRECTED_EDGE_TYPES``). After downgrade,
  any reader that distinguishes direction by ``edge_type`` will treat the
  relabeled rows as undirected.

``origin`` is untouched on downgrade.

**Concurrency contract**: the downgrade UPDATE acquires ``ROW EXCLUSIVE``;
the subsequent ``DROP CONSTRAINT`` / ``CREATE CONSTRAINT`` requires
``ACCESS EXCLUSIVE``. A writer that commits a new ``continues_from`` /
``references_file`` row between the UPDATE and the DROP would survive the
remap and then violate the recreated narrower CHECK, aborting the migration.
This migration is therefore intended for **offline (maintenance-window)
downgrade**; same property as ``e20_741``'s downgrade. Operators running an
online downgrade should ``LOCK TABLE neural_memory_edges IN ACCESS EXCLUSIVE
MODE`` ahead of the UPDATE inside the same alembic transaction.

Revision ID: e25_782_widen_edge_type
Revises: e24_668_drop_user_plans
Create Date: 2026-05-28
"""

from alembic import op

revision = "e25_782_widen_edge_type"
down_revision = "e24_668_drop_user_plans"
branch_labels = None
depends_on = None


# Upgrade target: the post-#741 four values PLUS the two #782 additions, in the
# exact registration order of ``_ALL_EDGE_TYPES`` in models/memory.py. Must stay
# byte-identical to that f-string output (single quotes, ', ' separator) — pinned
# by test_valid_edge_type_check_constraint_matches_migration_literal.
_NEW_CHECK_SQL = (
    "edge_type IN ('neural_association', 'related_to', 'depends_on', "
    "'learned_from', 'continues_from', 'references_file')"
)

# Downgrade target: the post-#741 four-value set (== e20_741's _NEW_CHECK_SQL).
_OLD_CHECK_SQL = "edge_type IN ('neural_association', 'related_to', 'depends_on', 'learned_from')"


def upgrade() -> None:
    """Widen the valid_edge_type CHECK to accept the two #782 relation types."""
    op.drop_constraint("valid_edge_type", "neural_memory_edges", type_="check")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        _NEW_CHECK_SQL,
    )


def downgrade() -> None:
    """Narrow the CHECK back to the post-#741 four values.

    Remaps the two #782 relation types to ``related_to`` first so existing rows
    do not violate the narrowed constraint. Lossy at the edge_type column level
    (the original continues_from / references_file distinction is gone); origin
    is preserved.
    """
    op.execute(
        """
        UPDATE neural_memory_edges
           SET edge_type = 'related_to'
         WHERE edge_type IN ('continues_from', 'references_file')
        """
    )
    op.drop_constraint("valid_edge_type", "neural_memory_edges", type_="check")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        _OLD_CHECK_SQL,
    )
