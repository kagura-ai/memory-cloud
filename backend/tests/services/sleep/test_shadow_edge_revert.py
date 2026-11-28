"""#3020: a shadow-merge undo touches only the edge dedup wrote.

Dedup's shadow merge always writes its ``supersedes`` edge with NULL metadata.
An edge on the same pair that carries metadata (a link Sleep's supersede-apply
phase wrote after the shadow edge was deleted) is newer state: the undo's
guarded statement must not match it, and the read-back reports
``BLOCKED_BY_NEWER_STATE``, on both the restore and the delete path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services.sleep.undo import ShadowEdgeRevert, revert_shadow_merge_edge

_PRIOR = {
    "edge_type": "neural_association",
    "origin": "hebbian",
    "weight": 0.4,
    "confidence": 0.9,
    "edge_metadata": None,
}


def _db(rowcount: int, current_type: str | None) -> AsyncMock:
    db = AsyncMock()
    write = MagicMock()
    write.rowcount = rowcount
    read_back = MagicMock()
    read_back.scalar_one_or_none.return_value = current_type
    db.execute = AsyncMock(side_effect=[write, read_back])
    return db


def _where(db: AsyncMock) -> str:
    stmt = db.execute.await_args_list[0].args[0]
    return str(stmt.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_edge", [_PRIOR, None], ids=["restore", "delete"])
async def test_the_guarded_statement_requires_null_metadata(prior_edge) -> None:
    db = _db(rowcount=1, current_type=None)
    outcome = await revert_shadow_merge_edge(
        db, user_id="u", winner_id=uuid4(), loser_id=uuid4(), prior_edge=prior_edge
    )
    assert outcome is ShadowEdgeRevert.RESTORED
    sql = _where(db)
    assert "neural_memory_edges.edge_type = " in sql
    assert "neural_memory_edges.metadata IS NULL" in sql
    assert "CAST(neural_memory_edges.metadata AS TEXT) = " in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_edge", [_PRIOR, None], ids=["restore", "delete"])
async def test_a_marked_link_on_the_pair_blocks_the_undo(prior_edge) -> None:
    """The statement matched nothing (the row carries metadata) and the pair
    still has a supersedes edge: newer state, not reversed, not deleted."""
    db = _db(rowcount=0, current_type="supersedes")
    outcome = await revert_shadow_merge_edge(
        db, user_id="u", winner_id=uuid4(), loser_id=uuid4(), prior_edge=prior_edge
    )
    assert outcome is ShadowEdgeRevert.BLOCKED_BY_NEWER_STATE
