"""Tests for admin context recovery endpoint (Issue #86)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.admin import ContextRecoveryRequest, recover_context


def _mock_admin_user():
    return {"user_id": "admin_user", "email": "admin@test.com"}


class TestRecoverContext:
    """Test POST /admin/contexts/recover endpoint."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def admin_user(self):
        return _mock_admin_user()

    @pytest.mark.asyncio
    async def test_invalid_context_id_raises_400(self, mock_db, admin_user):
        """Test that invalid UUID context_id returns 400."""
        request_body = ContextRecoveryRequest(context_id="not-a-uuid")

        with pytest.raises(HTTPException) as exc:
            await recover_context(request_body=request_body, user=admin_user, db=mock_db)

        assert exc.value.status_code == 400
        assert "Invalid context_id" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_no_qdrant_points_returns_error(self, mock_db, admin_user):
        """Test that zero Qdrant points returns descriptive error."""
        context_id = str(uuid4())
        request_body = ContextRecoveryRequest(context_id=context_id)

        mock_client = AsyncMock()
        mock_client.scroll.return_value = ([], None)

        with (
            patch("db.qdrant.get_qdrant_client", return_value=mock_client),
            patch("config.settings.get_settings") as mock_settings,
        ):
            mock_settings.return_value = MagicMock(
                qdrant_collection_name="kagura_memories",
                embedding_model="text-embedding-3-small",
                embedding_dimensions=512,
            )
            response = await recover_context(request_body=request_body, user=admin_user, db=mock_db)

        assert response.qdrant_points_found == 0
        assert "No Qdrant points found" in response.errors[0]

    @pytest.mark.asyncio
    async def test_dry_run_reports_without_changes(self, mock_db, admin_user):
        """Test dry_run=True returns counts without DB writes."""
        context_id = str(uuid4())
        workspace_id = str(uuid4())
        request_body = ContextRecoveryRequest(
            context_id=context_id, workspace_id=workspace_id, dry_run=True
        )

        # Mock Qdrant scroll: 2 points
        mock_point_1 = MagicMock()
        mock_point_1.id = str(uuid4())
        mock_point_1.payload = {
            "workspace_id": workspace_id,
            "user_id": "user1",
            "summary": "Memory 1",
        }
        mock_point_2 = MagicMock()
        mock_point_2.id = str(uuid4())
        mock_point_2.payload = {
            "workspace_id": workspace_id,
            "user_id": "user1",
            "summary": "Memory 2",
        }

        mock_client = AsyncMock()
        mock_client.scroll.return_value = ([mock_point_1, mock_point_2], None)

        # Mock DB: context doesn't exist, no existing memories
        mock_context_result = MagicMock()
        mock_context_result.scalar_one_or_none.return_value = None

        mock_existing_mems = MagicMock()
        mock_existing_mems.all.return_value = []

        mock_db.execute.side_effect = [mock_context_result, mock_existing_mems]

        with (
            patch("db.qdrant.get_qdrant_client", return_value=mock_client),
            patch("config.settings.get_settings") as mock_settings,
        ):
            mock_settings.return_value = MagicMock(
                qdrant_collection_name="kagura_memories",
                embedding_model="text-embedding-3-small",
                embedding_dimensions=512,
            )
            response = await recover_context(request_body=request_body, user=admin_user, db=mock_db)

        assert response.dry_run is True
        assert response.qdrant_points_found == 2
        assert response.memories_recovered == 2
        assert response.memories_already_existed == 0
        assert response.context_record_created is True
        # No db.add calls in dry_run
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()
