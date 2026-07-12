"""Widen valid_edge_type CHECK with supersedes + contradicts (#1208).

Fact-succession relations — the non-destructive update path the
kagura-memory-eval program found missing (update-by-removal was the only
mechanism preferring a current fact over a stale one, and it carries the
worst failure semantics). Direction convention (load-bearing for recall
shadowing): ``src`` = the SUPERSEDING (newer) memory, ``dst`` = the
SUPERSEDED (older) memory. ``contradicts`` never hides either side.

Also seeds the ``sleep_dedup_supersede_enabled`` neural_config row
(default ``false`` — dedup keeps its update-by-removal behavior unless an
operator opts into shadow-mode merges, which record a supersedes edge and
leave the loser row alive-but-shadowed).

### No data migration

Pure CHECK widening — existing rows satisfy the wider constraint.

### Index note

The recall shadow query filters ``edge_type='supersedes' AND dst_id IN
(<result ids>)`` scoped by user/workspace/context; the existing
``idx_edges_user_dst`` / ``idx_edges_ws_ctx_dst`` composite indexes cover
it (the candidate set is one recall's top-k×fetch_factor ids), so no new
index is added.

### Reversibility (downgrade caveats)

Mirrors e25_782: downgrade remaps ``supersedes``/``contradicts`` to
``related_to`` (lossy — succession direction and contradiction semantics
are gone; re-upgrading does not split them back out), then narrows the
CHECK. Offline-downgrade recommended, same concurrency contract as e25.

NOTE (merge coordination): this branch forked from main at e54, in parallel
with e55 (#1207) and e56 (#1209). Both merged first, so this revision was
re-chained onto e56 (the then-current head) at merge time, per the plan in
epic #1214.

Revision ID: e57_1208_supersedes_contradicts
Revises: e56_1209_merge_retention
"""

from alembic import op

# revision identifiers
revision = "e57_1208_supersedes_contradicts"
down_revision = "e56_1209_merge_retention"
branch_labels = None
depends_on = None

_NEW_CHECK_SQL = (
    "edge_type IN ('neural_association', 'related_to', 'depends_on', "
    "'learned_from', 'continues_from', 'references_file', "
    "'supersedes', 'contradicts')"
)

# Downgrade target: the post-#782 six-value set (== e25_782's _NEW_CHECK_SQL).
_OLD_CHECK_SQL = (
    "edge_type IN ('neural_association', 'related_to', 'depends_on', "
    "'learned_from', 'continues_from', 'references_file')"
)


def upgrade() -> None:
    op.drop_constraint("valid_edge_type", "neural_memory_edges", type_="check")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        _NEW_CHECK_SQL,
    )
    op.execute("""
        INSERT INTO neural_config (key, value, value_type, category, description, min_value, max_value) VALUES
            ('sleep_dedup_supersede_enabled', 'false', 'bool', 'sleep', 'Dedup merges record a supersedes edge and shadow the loser instead of soft-deleting it (#1208); off = update-by-removal (pre-#1208 behavior)', NULL, NULL)
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute(
        "UPDATE neural_memory_edges SET edge_type = 'related_to' "
        "WHERE edge_type IN ('supersedes', 'contradicts')"
    )
    op.drop_constraint("valid_edge_type", "neural_memory_edges", type_="check")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        _OLD_CHECK_SQL,
    )
    op.execute("DELETE FROM neural_config WHERE key = 'sleep_dedup_supersede_enabled';")
