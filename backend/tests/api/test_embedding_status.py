"""Tests for embedding queue status endpoints (Issue #93)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.workspace import get_embedding_status


class TestGetEmbeddingStatus:
    """Test GET /workspace/embedding-status endpoint."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def mock_user(self):
        return {
            "user_id": "test_user_123",
            "current_workspace_id": str(uuid4()),
        }

    @staticmethod
    def owner_lookup(owner_user_id):
        """#1496 added an owner lookup ahead of the status query.

        The endpoint narrows to accessible contexts for anyone who is not the
        workspace owner, so it has to ask who the owner is first. These tests
        mock by CALL ORDER, so every side_effect list needs this in front.
        """
        result = MagicMock()
        result.scalar_one_or_none.return_value = owner_user_id
        return result

    @pytest.mark.asyncio
    async def test_returns_counts_by_status(self, mock_db, mock_user):
        """Test that endpoint returns correct status counts."""
        # Mock GROUP BY result: 2 statuses
        mock_status_result = MagicMock()
        mock_status_result.all.return_value = [
            ("success", 100),
            ("pending", 5),
            ("failed", 0),
        ]
        mock_db.execute.side_effect = [
            self.owner_lookup(mock_user["user_id"]),
            mock_status_result,
        ]

        response = await get_embedding_status(user=mock_user, db=mock_db, context_id=None)

        assert response.total == 105
        assert response.by_status["success"] == 100
        assert response.by_status["pending"] == 5
        assert response.failed_memories == []

    @pytest.mark.asyncio
    async def test_no_workspace_raises_400(self, mock_db):
        """Test that missing workspace_id returns 400."""
        user = {"user_id": "test_user_123"}

        with pytest.raises(HTTPException) as exc:
            await get_embedding_status(user=user, db=mock_db, context_id=None)

        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_context_id_raises_400(self, mock_db, mock_user):
        """Test that invalid UUID context_id returns 400."""
        with pytest.raises(HTTPException) as exc:
            await get_embedding_status(user=mock_user, db=mock_db, context_id="not-a-uuid")

        assert exc.value.status_code == 400
        assert "Invalid context_id" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_includes_failed_details(self, mock_db, mock_user):
        """Test that failed memories include details."""
        mem_id = uuid4()
        mock_mem = MagicMock()
        mock_mem.id = mem_id
        mock_mem.summary = "Test memory summary"
        mock_mem.embedding_error = "Model not available"
        mock_mem.created_at = datetime(2026, 4, 1, 10, 0, 0)
        mock_mem.updated_at = datetime(2026, 4, 1, 12, 0, 0)

        # First call: GROUP BY status counts
        mock_status_result = MagicMock()
        mock_status_result.all.return_value = [("failed", 1)]

        # Second call: failed memory details
        mock_failed_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_mem]
        mock_failed_result.scalars.return_value = mock_scalars

        mock_db.execute.side_effect = [
            self.owner_lookup(mock_user["user_id"]),
            mock_status_result,
            mock_failed_result,
        ]

        response = await get_embedding_status(user=mock_user, db=mock_db, context_id=None)

        assert response.total == 1
        assert len(response.failed_memories) == 1
        assert response.failed_memories[0].id == str(mem_id)
        assert response.failed_memories[0].embedding_error == "Model not available"


class TestPrivateContextsAreNotLeaked:
    """#1496: this endpoint returns failed memories WITH their summaries.

    Its query was scoped by workspace_id and deleted_at only — no
    accessible-context filter, while every other stats path in the module has
    one — so any workspace MEMBER could read the opening 200 characters of
    another member's PRIVATE context memories by asking for the embedding
    queue.

    Asserted on the SQL the endpoint actually emits, not on its source. The db
    is mocked, so the rows are whatever the mock returns; what matters is
    whether the restriction was compiled INTO the statement, and that is
    exactly what these read.
    """

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @staticmethod
    def _owner_lookup(owner_user_id):
        result = MagicMock()
        result.scalar_one_or_none.return_value = owner_user_id
        return result

    @staticmethod
    async def _run(mock_db, *, caller: str, owner: str) -> list[str]:
        """Drive the endpoint and return the compiled SQL of every statement."""
        status_result = MagicMock()
        status_result.all.return_value = [("failed", 0)]
        mock_db.execute.side_effect = [
            TestPrivateContextsAreNotLeaked._owner_lookup(owner),
            status_result,
        ]
        user = {"user_id": caller, "current_workspace_id": str(uuid4())}
        await get_embedding_status(user=user, db=mock_db, context_id=None)
        return [
            str(call.args[0].compile(compile_kwargs={"literal_binds": False}))
            for call in mock_db.execute.await_args_list
        ]

    @pytest.mark.asyncio
    async def test_a_member_only_sees_contexts_they_may_read(self, mock_db):
        sql = await self._run(mock_db, caller="member_1", owner="someone_else")
        status_sql = sql[1]
        assert "contexts" in status_sql.lower(), (
            "the status query does not reference contexts at all — the "
            "accessible-context restriction is missing and private memories "
            "from other members are counted and returned (#1496)"
        )
        assert "is_private" in status_sql, "the private-context rule is not applied"

    @pytest.mark.asyncio
    async def test_the_owner_is_not_narrowed(self, mock_db):
        """An owner sees everything; narrowing them would hide their own data."""
        sql = await self._run(mock_db, caller="owner_1", owner="owner_1")
        assert "is_private" not in sql[1], (
            "the owner's query was narrowed — an owner must see every context in their workspace"
        )

    @pytest.mark.asyncio
    async def test_the_owner_is_looked_up_before_the_counts(self, mock_db):
        """Order matters: the narrowing decision needs the answer first."""
        sql = await self._run(mock_db, caller="member_1", owner="someone_else")
        assert "workspaces" in sql[0].lower()
