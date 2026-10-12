"""#1943: the lists tell the owner which members and contexts the plan pauses.

``GET /workspaces/{id}/members`` carries ``seat_paused`` per member and
``GET /contexts`` carries ``writes_paused`` per context, both read from the
``plan_suspension`` helpers once per request; a plan with room for everyone
yields all-``False``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from utils.datetime import utcnow


def _generic_db(workspace) -> MagicMock:
    """A session whose every query comes back empty and whose ``get`` is the workspace."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.one_or_none.return_value = None
    result.scalar.return_value = None
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    result.first.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=workspace)
    return db


class TestMembersList:
    @pytest.mark.asyncio
    async def test_seat_paused_is_the_rows_position_past_the_plans_seats(self) -> None:
        """``list_members`` is ordered by the seat ranking; the first
        ``seat_limit`` rows hold the seats, the rest are paused — no second query."""
        from api.routes import workspaces as ws_routes

        ws_id = uuid4()
        rows_in = [
            SimpleNamespace(
                id=i, user_id=f"u{i}", role=role, joined_at=utcnow(), allowed_context_ids=None
            )
            for i, role in ((1, "owner"), (2, "member"), (3, "member"))
        ]
        service = MagicMock(list_members=AsyncMock(return_value=rows_in))
        db = _generic_db(SimpleNamespace(id=ws_id, plan_name="free", effective_max_members=2))
        with (
            patch.object(ws_routes, "authorize_workspace_management", AsyncMock()),
            patch.object(ws_routes, "WorkspaceService", return_value=service),
        ):
            response = await ws_routes.list_members(
                workspace_id=ws_id, user={"user_id": "o"}, db=db
            )
        rows = response if isinstance(response, list) else response.members
        assert [r.user_id for r in rows] == ["u1", "u2", "u3"]  # the service's order, kept
        assert [r.seat_paused for r in rows] == [False, False, True]

    @pytest.mark.asyncio
    async def test_no_workspace_row_pauses_nobody(self) -> None:
        from api.routes import workspaces as ws_routes

        member = SimpleNamespace(
            id=7, user_id="u2", role="member", joined_at=utcnow(), allowed_context_ids=None
        )
        service = MagicMock(list_members=AsyncMock(return_value=[member]))
        db = _generic_db(None)
        with (
            patch.object(ws_routes, "authorize_workspace_management", AsyncMock()),
            patch.object(ws_routes, "WorkspaceService", return_value=service),
        ):
            response = await ws_routes.list_members(
                workspace_id=uuid4(), user={"user_id": "o"}, db=db
            )
        rows = response if isinstance(response, list) else response.members
        assert rows[0].seat_paused is False


class TestContextsList:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("paused", [True, False])
    async def test_writes_paused_follows_the_helper(self, paused: bool) -> None:
        from api.routes import contexts as ctx_routes

        ws_id = uuid4()
        ctx = SimpleNamespace(
            id=uuid4(),
            name="ctx",
            display_name=None,
            description=None,
            summary=None,
            usage_guide=None,
            is_default=False,
            is_private=False,
            is_public=False,
            resource_id=None,
            is_locked=False,
            sleep_mode="skip",
            created_by="u",
            created_at=utcnow(),
            updated_at=None,
            workspace_id=ws_id,
        )
        service = MagicMock(list_contexts=AsyncMock(return_value=[ctx]))
        db = _generic_db(SimpleNamespace(id=ws_id, plan_name="free"))
        with patch.object(
            ctx_routes,
            "writes_paused_context_ids",
            AsyncMock(return_value={ctx.id} if paused else set()),
        ) as helper:
            response = await ctx_routes.list_contexts(
                user={"user_id": "u", "current_workspace_id": str(ws_id)}, service=service, db=db
            )
        helper.assert_awaited_once()
        assert response.contexts[0].id == ctx.id
        assert response.contexts[0].writes_paused is paused

    @pytest.mark.asyncio
    async def test_no_current_workspace_means_no_lookup(self) -> None:
        from api.routes import contexts as ctx_routes

        service = MagicMock(list_contexts=AsyncMock(return_value=[]))
        with patch.object(ctx_routes, "writes_paused_context_ids", AsyncMock()) as helper:
            response = await ctx_routes.list_contexts(
                user={"user_id": "u"}, service=service, db=_generic_db(None)
            )
        helper.assert_not_awaited()
        assert response.contexts == []
