"""Tests for admin context recovery endpoint (Issue #86)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

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

        # #2015: a content chunk point is a fragment of a memory, never
        # recovered — the scroll leaves chunk points out (#2041).
        flt = mock_client.scroll.await_args.kwargs["scroll_filter"]
        assert any(getattr(c, "is_empty", None) and c.is_empty.key == "chunk_of" for c in flt.must)

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
            files_restored=2,
            file_bytes_restored=4096,
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
        assert (response.files_restored, response.file_bytes_restored) == (2, 4096)  # #1960
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


def _point(workspace_id: str | None, summary: str = "recovered") -> MagicMock:
    point = MagicMock()
    point.id = str(uuid4())
    point.payload = {"user_id": "user1", "summary": summary}
    if workspace_id is not None:
        point.payload["workspace_id"] = workspace_id
    return point


class TestRecoverIntoTheContextsWorkspace:
    """#1963: recovered memories take the existing context's workspace.

    DB-backed: ``fk_memories_workspace_context`` refuses a memory stamped with
    any other workspace, so the route must pick the context's own.
    """

    @staticmethod
    async def _workspace_and_context(db_session) -> tuple[str, str]:
        from models.auth import Context, Workspace

        owner = f"recover-{uuid4().hex[:8]}"
        workspace = Workspace(id=uuid4(), name=f"ws-{uuid4().hex[:8]}", owner_user_id=owner)
        db_session.add(workspace)
        await db_session.flush()
        context = Context(
            id=uuid4(), workspace_id=workspace.id, name=f"ctx-{uuid4().hex[:8]}", created_by=owner
        )
        db_session.add(context)
        await db_session.flush()
        return str(workspace.id), str(context.id)

    @staticmethod
    async def _recover(db_session, points, **request):
        client = AsyncMock()
        client.scroll.return_value = (points, None)
        # The route commits; flush keeps the rows inside the fixture's
        # transaction, which rolls them back.
        db_session.commit = AsyncMock(side_effect=db_session.flush)
        with patch("db.qdrant.get_qdrant_client", return_value=client):
            return await recover_context(
                request_body=ContextRecoveryRequest(**request),
                user=_mock_admin_user(),
                db=db_session,
            )

    @staticmethod
    async def _stored(db_session, points) -> dict[str, tuple[str, str]]:
        from sqlalchemy import select

        from models.memory import Memory

        rows = await db_session.execute(
            select(Memory.id, Memory.workspace_id, Memory.embedding_status).where(
                Memory.id.in_([p.id for p in points])
            )
        )
        return {str(r.id): (str(r.workspace_id), r.embedding_status) for r in rows}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dry_run", [True, False])
    async def test_a_different_workspace_is_refused(self, db_session, dry_run):
        ws_id, ctx_id = await self._workspace_and_context(db_session)
        points = [_point(ws_id)]

        from utils.exceptions import BadRequestError

        with pytest.raises(BadRequestError) as exc:
            await self._recover(
                db_session, points, context_id=ctx_id, workspace_id=str(uuid4()), dry_run=dry_run
            )

        assert (exc.value.status_code, exc.value.error_code) == (400, "REQ-103")
        assert exc.value.message == "workspace_id does not match the context's workspace"
        assert await self._stored(db_session, points) == {}

    @pytest.mark.asyncio
    async def test_the_contexts_workspace_wins_over_the_points(self, db_session):
        ws_id, ctx_id = await self._workspace_and_context(db_session)
        # One point names the context's workspace, one a stale other one, one none.
        matching, stale, bare = _point(ws_id), _point(str(uuid4())), _point(None)
        upper = _point(ws_id.upper())  # the same workspace, spelled differently

        dry = await self._recover(db_session, [stale, matching, bare, upper], context_id=ctx_id)
        assert (dry.workspace_id, dry.context_record_created) == (ws_id, False)

        response = await self._recover(
            db_session,
            [stale, matching, bare, upper],
            context_id=ctx_id,
            workspace_id=ws_id.upper(),
            dry_run=False,
        )

        assert response.workspace_id == ws_id
        assert response.memories_recovered == 4
        # Stamped with the context's workspace; a point whose payload names
        # another workspace (or none) goes back through the embedding sweep.
        assert await self._stored(db_session, [stale, matching, bare, upper]) == {
            matching.id: (ws_id, "success"),
            stale.id: (ws_id, "pending"),
            bare.id: (ws_id, "pending"),
            upper.id: (ws_id, "success"),
        }

    @pytest.mark.asyncio
    async def test_a_missing_context_is_created_in_the_requested_workspace(self, db_session):
        ws_id, _ctx_id = await self._workspace_and_context(db_session)
        new_ctx = str(uuid4())
        point = _point(str(uuid4()))  # the request decides, not the payload

        response = await self._recover(
            db_session, [point], context_id=new_ctx, workspace_id=ws_id, dry_run=False
        )

        assert (response.workspace_id, response.context_record_created) == (ws_id, True)
        assert await self._stored(db_session, [point]) == {point.id: (ws_id, "pending")}

    @pytest.mark.asyncio
    async def test_a_context_created_meanwhile_elsewhere_is_refused(self, db_session):
        """#1963: the context is read again before the writes; one another
        recovery created in another workspace since is a 400, not an FK 500."""
        from models.auth import Context
        from utils.exceptions import BadRequestError

        ws_id, _ = await self._workspace_and_context(db_session)
        other_ws_id, _ = await self._workspace_and_context(db_session)
        new_ctx = str(uuid4())
        point = _point(ws_id)

        async def created_meanwhile(db):
            db.add(
                Context(
                    id=UUID(new_ctx),
                    workspace_id=UUID(other_ws_id),
                    name=f"ctx-{uuid4().hex[:8]}",
                    created_by="other-admin",
                )
            )
            await db.flush()

        with (
            patch("db.point_writer_lock.hold_point_writer_lock", side_effect=created_meanwhile),
            pytest.raises(BadRequestError) as exc,
        ):
            await self._recover(
                db_session, [point], context_id=new_ctx, workspace_id=ws_id, dry_run=False
            )

        assert exc.value.error_code == "REQ-103"
        assert await self._stored(db_session, [point]) == {}
