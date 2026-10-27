"""Migration lint: CONCURRENTLY in autocommit blocks, shared helpers (#2240).

Walks every revision's AST (``tests/migration_ast.py``, the walker of
``tests/test_check_validate_outside_alter_tx.py``). No database.

- ``CREATE INDEX CONCURRENTLY`` / ``DROP INDEX CONCURRENTLY`` (and
  ``REINDEX ... CONCURRENTLY``, ``postgresql_concurrently=``) cannot run in a
  transaction block, and ``alembic/env.py`` runs a whole upgrade in one: each
  one sits inside ``autocommit_block()``.
- A revision after f17 calls the ``db.migration_helpers`` helpers instead of
  writing its own copy: ``validate_constraint`` for VALIDATE,
  ``index_is_invalid`` for the INVALID-index probe, ``session_lock_timeout``
  for a session-level ``SET lock_timeout`` in an autocommit block. The
  revisions that already carry a copy are allow-listed by revision id; the
  lists only shrink.
- ``validate_constraint`` commits before and after its VALIDATE, so it is
  called outside an autocommit block; ``session_lock_timeout`` is entered
  inside one.
"""

from __future__ import annotations

import ast
import re

import pytest

from tests.migration_ast import (
    Site,
    call_sites,
    keyword_sites,
    literal_sites,
    migration_trees,
    revision_id,
)

_CONCURRENTLY = re.compile(r"\bINDEX\s+CONCURRENTLY\b|\bREINDEX\b.*\bCONCURRENTLY\b", re.I | re.S)
_SESSION_SET_LOCK_TIMEOUT = re.compile(r"\bSET\s+lock_timeout\b", re.I)

# CONCURRENTLY sites the walker finds across ``alembic/versions``. A new
# revision that builds or drops an index concurrently bumps it; a drop means
# the walker lost a shape.
_KNOWN_CONCURRENTLY_SITE_COUNT = 53

# Revisions that carry their own copy of a helper (written before #2240).
_LEGACY_INVALID_INDEX_PROBE = frozenset(
    {
        "a96_ctx_resource_id_unique",
        "a97_resources_entity",
        "b02_383_edges_ws_ctx_idx",
        "e26_818_summary_trgm_idx",
        "e29_619_memories_ws_ctx_idx",
        "e32_886_delivery_mode",
        "e62_1245_assign_mem_idx",
        "e65_1275_api_keys_agent",
        "e84_1619_tool_guardrails",
        "e94_1869_summary_emb_idx",
        "f04_2044_session_provenance",
        "f05_2121_rollback_window",
        "f10_2115_revisions_actor_idx",
        "f13_1952_api_keys_connector_id",
        "f14_1963_memories_ws_ctx_fk",
        "f17_2298_audit_logs_user_id_idx",
    }
)
_LEGACY_VALIDATE = frozenset(
    {
        "a97_resources_entity",
        "b03_396_edges_ws_ctx_not_null",
        "d05_523_source_paid_by",
        "d07_495_cluster_label_phase",
        "e01_546_cache_write_pricing",
        "e65_1275_api_keys_agent",
        "e67_1281_agent_ws",
        "e74_1401_mae_explore",
        "e84_1619_tool_guardrails",
        "e98_2007_mae_list_changes_since",
        "f00_2019_llm_log_supersede",
        "f12_2244_rev_writer_erasure",
        "f13_1952_api_keys_connector_id",
        "f14_1963_memories_ws_ctx_fk",
    }
)
_LEGACY_SESSION_LOCK_TIMEOUT = frozenset(
    {
        "e99_2022_supersede_cand_null",
        "f14_1963_memories_ws_ctx_fk",
        "f15_2026_json_none_as_null",
    }
)


def _concurrently_sites(tree: ast.AST) -> list[Site]:
    return literal_sites(
        tree, lambda text: bool(_CONCURRENTLY.search(text)), follow_constants=True
    ) + keyword_sites(tree, "postgresql_concurrently")


def _session_lock_timeout_copies(tree: ast.AST) -> list[Site]:
    """``SET lock_timeout`` literals (not ``SET LOCAL``), and
    ``lock_timeout_statement(..., local=False)`` calls."""
    sites = literal_sites(tree, lambda text: bool(_SESSION_SET_LOCK_TIMEOUT.search(text)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name | ast.Attribute)
            and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
            == "lock_timeout_statement"
            and any(
                kw.arg == "local" and not (isinstance(kw.value, ast.Constant) and kw.value.value)
                for kw in node.keywords
            )
        ):
            sites.append((node.lineno, False))
    return sites


_COPIES = {
    "invalid-index probe": (lambda tree: literal_sites(tree, lambda t: "indisvalid" in t)),
    "VALIDATE CONSTRAINT": (lambda tree: literal_sites(tree, lambda t: "VALIDATE CONSTRAINT" in t)),
    "session SET lock_timeout": _session_lock_timeout_copies,
}
_LEGACY = {
    "invalid-index probe": _LEGACY_INVALID_INDEX_PROBE,
    "VALIDATE CONSTRAINT": _LEGACY_VALIDATE,
    "session SET lock_timeout": _LEGACY_SESSION_LOCK_TIMEOUT,
}
_HELPER = {
    "invalid-index probe": "index_is_invalid",
    "VALIDATE CONSTRAINT": "validate_constraint",
    "session SET lock_timeout": "session_lock_timeout",
}


def _offenders(sites: dict[str, list[Site]], want_in_block: bool) -> dict[str, list[int]]:
    return {
        name: bad
        for name, found in sites.items()
        if (bad := [line for line, in_block in found if in_block != want_in_block])
    }


def test_every_concurrent_index_sits_in_an_autocommit_block() -> None:
    """A module constant counts where it is read: ``op.execute(_INDEX_DDL)``
    inside the block passes, the assignment at module level is not a site."""
    sites = {name: _concurrently_sites(tree) for name, tree in migration_trees().items()}
    assert _offenders(sites, want_in_block=True) == {}


def test_the_concurrently_walker_finds_the_known_sites() -> None:
    """Positive guard, so a gap in the walker cannot pass the check above."""
    sites = {name: _concurrently_sites(tree) for name, tree in migration_trees().items()}
    for name in (
        "a96_add_unique_contexts_resource_id_active.py",  # literals in the block
        "e26_818_summary_trgm_idx.py",  # a module constant
        "f10_2115_revisions_actor_idx.py",  # create and drop constants
        "f17_2298_audit_logs_user_id_idx.py",
        "f18_2395_file_cleanup.py",
    ):
        assert sites[name], name
    assert sum(len(found) for found in sites.values()) == _KNOWN_CONCURRENTLY_SITE_COUNT, {
        name: found for name, found in sites.items() if found
    }


@pytest.mark.parametrize("kind", sorted(_COPIES))
def test_new_revisions_call_the_shared_helper(kind: str) -> None:
    """Only the allow-listed revisions carry their own copy, and each of them
    still does (a stale entry would let a later revision under that id in)."""
    found = {
        revision_id(tree) or name for name, tree in migration_trees().items() if _COPIES[kind](tree)
    }
    assert found - _LEGACY[kind] == set(), (
        f"write db.migration_helpers.{_HELPER[kind]}() instead of a {kind} copy"
    )
    assert _LEGACY[kind] - found == set()


def test_validate_constraint_is_called_outside_an_autocommit_block() -> None:
    """It commits before and after the VALIDATE, and inside a block there is
    no transaction to commit. A live run refuses it (``RuntimeError``); this
    catches the direct form before any run."""
    sites = {
        name: call_sites(tree, {"validate_constraint"}) for name, tree in migration_trees().items()
    }
    assert _offenders(sites, want_in_block=False) == {}


def test_session_lock_timeout_is_entered_inside_an_autocommit_block() -> None:
    """Outside one, the session-level SET would be the migration transaction's
    own (``SET LOCAL`` there, from env.py)."""
    sites = {
        name: call_sites(tree, {"session_lock_timeout"}) for name, tree in migration_trees().items()
    }
    assert _offenders(sites, want_in_block=True) == {}


# --- the walker on hand-written revisions ---------------------------------

_GOOD = '''
"""Docstring: CREATE INDEX CONCURRENTLY and VALIDATE CONSTRAINT are prose here."""
from alembic import op
from db.migration_helpers import index_is_invalid, session_lock_timeout, validate_constraint

revision = "f99_test"
_DDL = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_t_a ON t (a)"
_DROP = _DDL.replace("CREATE", "DROP")


def upgrade():
    invalid = index_is_invalid("ix_t_a")
    with op.get_context().autocommit_block():
        if invalid:
            op.execute(_DROP)
        op.execute(_DDL)
        with session_lock_timeout("5s"):
            op.execute("UPDATE t SET a = 1")
    validate_constraint("t", "ck_t_a")
'''

_BAD = """
from alembic import op
from db.migration_helpers import lock_timeout_statement, session_lock_timeout, validate_constraint

revision = "f99_test"
_DDL = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_t_a ON t (a)"


def upgrade():
    op.execute(_DDL)
    op.create_index("ix_t_b", "t", ["b"], postgresql_concurrently=True)
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {'ix_t_c'}")
    with session_lock_timeout("5s"):
        pass
    with op.get_context().autocommit_block():
        validate_constraint("t", "ck_t_a")
        op.execute(lock_timeout_statement("5s", local=False))
        op.execute("SELECT 1 FROM pg_index WHERE NOT indisvalid")
        op.execute("ALTER TABLE t VALIDATE CONSTRAINT ck_t_b")
"""


def test_a_well_formed_revision_passes() -> None:
    tree = ast.parse(_GOOD)
    assert revision_id(tree) == "f99_test"
    concurrently = _concurrently_sites(tree)
    assert len(concurrently) == 2
    assert all(in_block for _, in_block in concurrently)
    assert all(not copies(tree) for copies in _COPIES.values())
    assert call_sites(tree, {"validate_constraint"}) == [(19, False)]
    assert call_sites(tree, {"session_lock_timeout"}) == [(17, True)]


def test_a_malformed_revision_is_reported() -> None:
    tree = ast.parse(_BAD)
    assert [in_block for _, in_block in _concurrently_sites(tree)] == [False, False, False]
    assert {kind for kind, copies in _COPIES.items() if copies(tree)} == set(_COPIES)
    assert call_sites(tree, {"validate_constraint"}) == [(16, True)]
    assert call_sites(tree, {"session_lock_timeout"}) == [(13, False)]


_ORDER_AND_FSTRING = """
from alembic import op
from db.migration_helpers import session_lock_timeout

_DDL = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_t_a ON t (a)"


def upgrade():
    op.execute(f"{_DDL}")
    with session_lock_timeout("5s"), op.get_context().autocommit_block():
        pass
    with op.get_context().autocommit_block(), session_lock_timeout("5s"):
        op.execute(f"{_DDL}")
"""


def test_with_items_enter_in_order_and_f_strings_are_read() -> None:
    """An item before ``autocommit_block()`` in the same ``with`` runs in the
    transaction; a constant read inside an f-string counts where it is read."""
    tree = ast.parse(_ORDER_AND_FSTRING)
    assert _concurrently_sites(tree) == [(9, False), (13, True)]
    assert call_sites(tree, {"session_lock_timeout"}) == [(10, False), (12, True)]
