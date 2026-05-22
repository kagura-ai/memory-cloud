"""Deprecate edge_type values that overlap with origin (Issue #741).

After #722 added the ``origin`` discriminator, three ``edge_type`` values
duplicated provenance information that should live exclusively on ``origin``:

* ``semantic_similarity`` — k-NN cold-start seeded edges (#221).
* ``declared_link``       — user-asserted edges (#215).
* ``tag_cooccurrence``    — tag-based discovery (#223).

### Pre-flight finding

The #722 origin-backfill ran ``server_default='hebbian'`` and did NOT
classify existing rows by their pre-#722 edge_type. As a result, prod
contains misclassified rows: every pre-#741 ``semantic_similarity`` /
``declared_link`` / ``tag_cooccurrence`` edge carries ``origin='hebbian'``
— wrong on the origin axis. Without correction the post-#741 merge would
silently drop the provenance discrimination.

### Phases

* **Phase 0 — backfill**: align ``origin`` (and ``edge_metadata`` for
  ``tag_cooccurrence``) to the post-#741 discriminator shape, BEFORE the
  edge_type collapse. After this phase a downstream consumer that reads
  ``origin`` or ``edge_metadata['source']`` instead of ``edge_type``
  retains the same classification semantics the pre-#741 caller had.
* **Phase 1 — edge_type merge**: collapse the 3 deprecated values into
  ``neural_association``. Information loss is zero because Phase 0
  preserved the provenance.
* **Phase 2 — CHECK constraint shrink**: drop+recreate the
  ``valid_edge_type`` CHECK to accept only the 4 surviving values.

### Reversibility

``downgrade()`` re-widens the CHECK to accept the 3 deprecated values, but
does NOT split rows back — Phase 0 + Phase 1's UPDATEs are lossy at the
edge_type column level (the original column value is gone). Callers that
need the legacy edge_type strings post-downgrade should read ``origin``
('semantic' for the former 'semantic_similarity', 'declared' for the
former 'declared_link') or ``edge_metadata['source']``.

Revision ID: e20_741_deprecate_edge_type
Revises: e19_737_drop_redundant_ix
Create Date: 2026-05-23
"""

from alembic import op


revision = "e20_741_deprecate_edge_type"
down_revision = "e19_737_drop_redundant_ix"
branch_labels = None
depends_on = None


_NEW_CHECK_SQL = "edge_type IN ('neural_association', 'related_to', 'depends_on', 'learned_from')"

_OLD_CHECK_SQL = (
    "edge_type IN ('neural_association', 'related_to', 'depends_on', "
    "'learned_from', 'semantic_similarity', 'declared_link', "
    "'tag_cooccurrence')"
)


def upgrade() -> None:
    """Backfill origin/metadata, merge edge_type, shrink CHECK."""
    # Phase 0: backfill misclassified origin (and metadata for tag_cooccurrence).
    # The #722 backfill used server_default='hebbian' and did not classify by
    # pre-#722 edge_type. Without these UPDATEs, the Phase 1 merge would
    # silently drop the provenance discrimination.
    #
    # The WHERE clause is idempotent: rows already at the target origin are
    # untouched, so re-running this migration is a no-op (assuming Phase 1
    # hasn't yet collapsed edge_type).
    op.execute(
        """
        UPDATE neural_memory_edges
           SET origin = 'semantic'
         WHERE edge_type = 'semantic_similarity'
           AND origin <> 'semantic'
        """
    )
    op.execute(
        """
        UPDATE neural_memory_edges
           SET origin = 'declared'
         WHERE edge_type = 'declared_link'
           AND origin <> 'declared'
        """
    )
    # tag_cooccurrence does not have a dedicated origin value (intentional —
    # adding EDGE_ORIGIN_TAG is out of scope per #741 issue body). Preserve
    # the discriminator via ``edge_metadata['source']`` so post-#741 readers
    # can still distinguish tag-derived edges from generic hebbian co-
    # activation traces.
    #
    # NOTE on column type: ``neural_memory_edges.metadata`` is declared
    # ``JSON`` (not ``JSONB``) in the ORM and in the live DB. The ``||``
    # merge operator is only defined for ``jsonb``, and reading
    # ``metadata->>'source'`` is also unambiguous only on jsonb. We cast
    # to jsonb for the read+merge, then back to json for storage. The
    # CASE expression handles the NULL-metadata case (current prod state
    # per pre-flight check) without relying on COALESCE-coerces-NULL,
    # which postgres refuses across json/jsonb.
    op.execute(
        """
        UPDATE neural_memory_edges
           SET metadata = (
                CASE
                  WHEN metadata IS NULL
                    THEN '{"source": "tag_cooccurrence"}'::json
                  ELSE (metadata::jsonb || '{"source": "tag_cooccurrence"}'::jsonb)::json
                END
           )
         WHERE edge_type = 'tag_cooccurrence'
           AND (
                metadata IS NULL
             OR (metadata::jsonb ->> 'source') IS DISTINCT FROM 'tag_cooccurrence'
           )
        """
    )

    # Phase 1: collapse 3 deprecated edge_type values into neural_association.
    # Phase 0 preserved the discriminating info, so this UPDATE is information-
    # lossless at the (edge_type, origin, edge_metadata) tuple level.
    op.execute(
        """
        UPDATE neural_memory_edges
           SET edge_type = 'neural_association'
         WHERE edge_type IN (
            'semantic_similarity',
            'declared_link',
            'tag_cooccurrence'
         )
        """
    )

    # Phase 2: replace the valid_edge_type CHECK constraint with the 4-value
    # version. The ORM-side CheckConstraint at models/memory.py is derived
    # from _ALL_EDGE_TYPES via f-string and already produces the new string.
    op.drop_constraint("valid_edge_type", "neural_memory_edges", type_="check")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        _NEW_CHECK_SQL,
    )


def downgrade() -> None:
    """Re-widen the CHECK to accept the 3 deprecated values.

    Note: this is NOT a true reversal. The forward UPDATEs in Phase 0 and
    Phase 1 are lossy at the edge_type column level. Callers that depended
    on the legacy edge_type strings post-downgrade must read ``origin``
    (or ``edge_metadata['source']`` for tag_cooccurrence) instead.
    """
    op.drop_constraint("valid_edge_type", "neural_memory_edges", type_="check")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        _OLD_CHECK_SQL,
    )
