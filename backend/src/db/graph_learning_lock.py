"""Per-user transaction lock around writes to a user's neural graph (Issue #1985).

A Hebbian learning pass strengthens edges and then prunes a node's weakest
edges. Sorting each phase cannot give the two phases one lock order: pass 1
may lock (a, b) and then prune (a, c) while pass 2 locks (a, c) and then
prunes (a, b). So every transaction that writes a user's
``neural_memory_edges`` rows takes this advisory lock first:

- the live recall pass takes it **without waiting**
  (:func:`try_lock_graph_learning`) and skips the pass when another
  transaction holds it — the pass is best-effort evidence and #983 already
  dedups a repeated query;
- the batch tasks take it **blocking** (:func:`lock_graph_learning`) so they
  never interleave with a live pass on the same rows.

The graph is per user (edge rows carry ``user_id``), so different users never
contend. The lock is transaction-scoped and releases on commit or rollback.
Key derivation matches the rest of the codebase: ``hashtextextended(:key, 0)``.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_KEY_PREFIX = "hebbian:"
_TRY_LOCK_SQL = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))")
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


def graph_learning_lock_key(user_id: str) -> str:
    """The advisory-lock key of ``user_id``'s graph."""
    return _KEY_PREFIX + user_id


async def try_lock_graph_learning(db: AsyncSession, user_id: str) -> bool:
    """Take the graph lock for this transaction if it is free; never wait.

    Args:
        db: Session whose transaction will hold the lock.
        user_id: Graph owner.

    Returns:
        True when this transaction now holds the lock, False when another does.
    """
    return bool(await db.scalar(_TRY_LOCK_SQL, {"key": graph_learning_lock_key(user_id)}))


async def lock_graph_learning(db: AsyncSession, user_id: str) -> None:
    """Take the graph lock for this transaction, waiting for a live pass to finish.

    Args:
        db: Session whose transaction will hold the lock.
        user_id: Graph owner.
    """
    await db.execute(_LOCK_SQL, {"key": graph_learning_lock_key(user_id)})
