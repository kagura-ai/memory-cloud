"""Unit tests for the shared declared-edge write core (``services/edge_service``).

#1416: ``create_declared_edge`` is the transport-agnostic core shared by the
MCP ``create_edge`` tool and the REST ``POST /graph/edges`` endpoint. These
pin the deterministic declared-duplicate contract (#1321) and the #1403
supersede self-heal at the service boundary — independent of either transport's
wrapper (which the MCP handler tests / graph integration tests cover). The
service never commits; it returns a plain-dict ``EdgeWriteResult``.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.memory import EDGE_ORIGIN_DECLARED
from services.edge_service import create_declared_edge


def _mock_edge(
    src_id, dst_id, *, edge_type="related_to", weight=1.0, confidence=1.0, origin="declared"
):
    e = MagicMock()
    e.src_id = src_id
    e.dst_id = dst_id
    e.edge_type = edge_type
    e.weight = weight
    e.confidence = confidence
    e.origin = origin
    e.created_at = None
    e.last_updated = None
    return e


async def _run(*, existing, created=None, db=None, **overrides):
    """Invoke create_declared_edge with a mocked repository.

    ``existing`` is get_edge's return; ``created`` is create_or_update_edge's.
    Returns (result, repo, db, src_id, dst_id).
    """
    src_id = overrides.pop("source_id", uuid4())
    dst_id = overrides.pop("target_id", uuid4())
    repo = MagicMock()
    repo.get_edge = AsyncMock(return_value=existing)
    repo.create_or_update_edge = AsyncMock(return_value=created)
    if db is None:
        db = AsyncMock()
    args = {
        "user_id": "u1",
        "source_id": src_id,
        "target_id": dst_id,
        "edge_type": "related_to",
        "weight": 1.0,
        "confidence": 1.0,
        "workspace_id": str(uuid4()),
        "context_id": str(uuid4()),
        "overwrite": False,
    }
    args.update(overrides)
    with patch("repositories.neural_edge.NeuralEdgeRepository", return_value=repo):
        result = await create_declared_edge(db, **args)
    return result, repo, db, src_id, dst_id


class TestCreateDeclaredEdgeContract:
    """#1321 duplicate contract, expressed at the service layer."""

    @pytest.mark.asyncio
    async def test_no_existing_edge_creates(self):
        src, dst = uuid4(), uuid4()
        created = _mock_edge(src, dst, origin="declared")
        result, repo, *_ = await _run(existing=None, created=created, source_id=src, target_id=dst)
        assert result.operation == "created"
        assert result.previous is None
        assert result.edge is not None and result.edge["origin"] == "declared"
        kwargs = repo.create_or_update_edge.await_args.kwargs
        assert kwargs["origin"] == EDGE_ORIGIN_DECLARED
        # Fresh-insert arm still keeps the declared-type race guard.
        assert kwargs["protect_declared_link"] is True

    @pytest.mark.asyncio
    async def test_existing_hebbian_updates_with_previous(self):
        src, dst = uuid4(), uuid4()
        existing = _mock_edge(
            src, dst, edge_type="neural_association", weight=0.4, origin="hebbian"
        )
        post = _mock_edge(src, dst, edge_type="supersedes", weight=1.0, origin="declared")
        result, repo, *_ = await _run(
            existing=existing, created=post, source_id=src, target_id=dst, edge_type="related_to"
        )
        assert result.operation == "updated"
        assert result.previous == {
            "edge_type": "neural_association",
            "weight": 0.4,
            "confidence": 1.0,
            "origin": "hebbian",
        }
        repo.create_or_update_edge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_declared_identical_is_unchanged_without_write(self):
        src, dst = uuid4(), uuid4()
        existing = _mock_edge(src, dst, edge_type="related_to", weight=1.0, confidence=1.0)
        result, repo, *_ = await _run(existing=existing, source_id=src, target_id=dst)
        assert result.operation == "unchanged"
        assert result.edge["weight"] == 1.0
        repo.create_or_update_edge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_declared_conflict_no_write(self):
        src, dst = uuid4(), uuid4()
        existing = _mock_edge(src, dst, edge_type="related_to", weight=1.0)
        result, repo, *_ = await _run(existing=existing, source_id=src, target_id=dst, weight=0.9)
        assert result.operation == "conflict"
        assert result.edge is None
        assert result.existing is not None and result.existing["weight"] == 1.0
        repo.create_or_update_edge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_overwrite_bypasses_conflict(self):
        src, dst = uuid4(), uuid4()
        existing = _mock_edge(src, dst, edge_type="related_to", weight=1.0)
        post = _mock_edge(src, dst, edge_type="depends_on", weight=0.9)
        result, repo, *_ = await _run(
            existing=existing,
            created=post,
            source_id=src,
            target_id=dst,
            edge_type="depends_on",
            weight=0.9,
            overwrite=True,
        )
        assert result.operation == "updated"
        kwargs = repo.create_or_update_edge.await_args.kwargs
        assert kwargs["protect_declared_link"] is False


class TestCreateDeclaredEdgeSupersedeSelfHeal:
    """#1403: writing a supersedes edge that confirms a stored candidate clears it."""

    @staticmethod
    def _db_returning(memory):
        db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=memory)
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_supersedes_edge_clears_matching_candidate(self):
        src, dst = uuid4(), uuid4()
        memory = MagicMock()
        memory.supersede_candidate = {"memory_id": str(dst), "similarity": 0.9}
        db = self._db_returning(memory)
        created = _mock_edge(src, dst, edge_type="supersedes", origin="declared")

        result, *_ = await _run(
            existing=None,
            created=created,
            db=db,
            source_id=src,
            target_id=dst,
            edge_type="supersedes",
        )

        assert result.operation == "created"
        # Self-heal ran in the same transaction (no commit here — the caller owns it).
        assert memory.supersede_candidate is None

    @pytest.mark.asyncio
    async def test_non_supersedes_edge_does_not_touch_candidate(self):
        """A related_to edge never runs the supersede self-heal query."""
        src, dst = uuid4(), uuid4()
        db = MagicMock()
        db.execute = AsyncMock()
        created = _mock_edge(src, dst, edge_type="related_to")

        await _run(
            existing=None,
            created=created,
            db=db,
            source_id=src,
            target_id=dst,
            edge_type="related_to",
        )

        db.execute.assert_not_awaited()
