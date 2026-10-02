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
        # No context row either (#1804 looks for a soft-deleted one).
        mock_db.execute.return_value = MagicMock(first=MagicMock(return_value=None))

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

        # Mock: 3 queries — context check, memory ID check, search config check
        mock_config_result = MagicMock()
        mock_config_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [mock_context_result, mock_existing_mems, mock_config_result]

        with (
            patch("db.qdrant.get_qdrant_client", return_value=mock_client),
            patch("config.settings.get_settings") as mock_settings,
        ):
            mock_settings.return_value = MagicMock(
                embedding_model="text-embedding-3-small",
                embedding_dimensions=512,
            )
            response = await recover_context(request_body=request_body, user=admin_user, db=mock_db)

        assert response.dry_run is True
        assert response.qdrant_points_found == 2
        assert response.memories_recovered == 2
        assert response.memories_already_existed == 0
        assert response.context_record_created is True
        assert response.search_config_restored is True
        # No db.add calls in dry_run
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()


class TestRecoverPointsToRestore:
    """#1804: a context deleted on v0.88.0+ has no points; recover says where to go."""

    @staticmethod
    async def _recover_without_points(deleted_row):
        db = AsyncMock()
        result = MagicMock()
        result.first.return_value = deleted_row
        db.execute.return_value = result
        client = AsyncMock()
        client.scroll.return_value = ([], None)
        with (
            patch("db.qdrant.get_qdrant_client", return_value=client),
            patch("config.settings.get_settings"),
        ):
            return await recover_context(
                request_body=ContextRecoveryRequest(context_id=str(uuid4())),
                user=_mock_admin_user(),
                db=db,
            )

    @pytest.mark.asyncio
    async def test_soft_deleted_context_points_to_the_restore_endpoint(self):
        response = await self._recover_without_points(deleted_row=("row",))

        assert "No Qdrant points found" in response.errors[0]
        assert "/restore" in response.errors[0]

    @pytest.mark.asyncio
    async def test_unknown_context_gets_no_restore_hint(self):
        response = await self._recover_without_points(deleted_row=None)

        assert "/restore" not in response.errors[0]


class TestRestoreContextEndpoint:
    """#1804: POST /admin/contexts/{context_id}/restore."""

    @pytest.mark.asyncio
    async def test_passes_the_admin_and_options_to_the_service(self):
        from datetime import datetime

        from api.routes.admin import ContextRestoreRequest, restore_context
        from services.context_restore import ContextRestoreResult

        context_id = uuid4()
        service_result = ContextRestoreResult(
            context_id=str(context_id),
            workspace_id=str(uuid4()),
            name="renamed",
            deleted_at=datetime(2026, 9, 30, 12, 0, 0),
            deleted_by="owner",
            dry_run=False,
            memories_restored=4,
            memories_left_deleted=1,
            renamed_from="original",
        )
        db = AsyncMock()
        with patch(
            "services.context_restore.restore_deleted_context",
            AsyncMock(return_value=service_result),
        ) as restore:
            response = await restore_context(
                context_id=context_id,
                request_body=ContextRestoreRequest(dry_run=False, new_name="renamed"),
                user=_mock_admin_user(),
                db=db,
            )

        restore.assert_awaited_once_with(
            db,
            context_id,
            dry_run=False,
            new_name="renamed",
            actor_id="admin_user",
            actor_email="admin@test.com",
        )
        assert response.memories_restored == 4
        assert response.renamed_from == "original"
        assert response.deleted_at == "2026-09-30T12:00:00Z"

    def test_dry_run_is_the_default(self):
        from api.routes.admin import ContextRestoreRequest

        assert ContextRestoreRequest().dry_run is True


class TestRestoreContextRoute:
    """#1804: the route through FastAPI — the admin gate and the error mapping."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from api.main import app

        yield TestClient(app, raise_server_exceptions=False)
        app.dependency_overrides.clear()

    @staticmethod
    def _override(user: dict) -> None:
        from api.main import app
        from auth.dependencies import get_current_user
        from db.base import get_db

        async def current_user():
            return user

        async def no_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = current_user
        app.dependency_overrides[get_db] = no_db

    def test_non_admin_gets_403(self, client):
        self._override({"user_id": "u1", "email": "member@test.com", "role": "member"})
        with patch("services.context_restore.restore_deleted_context", AsyncMock()) as restore:
            resp = client.post(f"/api/v1/admin/contexts/{uuid4()}/restore", json={})

        assert resp.status_code == 403
        restore.assert_not_awaited()

    def test_a_refusal_is_409(self, client):
        from utils.exceptions import ConflictError

        self._override({"user_id": "a1", "email": "admin@test.com", "role": "admin"})
        with patch(
            "services.context_restore.restore_deleted_context",
            AsyncMock(side_effect=ConflictError("Context is not deleted")),
        ):
            resp = client.post(f"/api/v1/admin/contexts/{uuid4()}/restore", json={})

        assert resp.status_code == 409
