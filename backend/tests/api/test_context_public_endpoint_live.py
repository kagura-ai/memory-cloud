"""``ContextResponse.public_mcp_endpoint_live`` (#2995).

The settings tab shows a note on a context that backs a live public MCP
endpoint (a ``public_mcp_endpoints`` row with ``revoked_at IS NULL``). The
flag is display only: no public id, no text. Both read routes set it from
one lookup; it defaults to false.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from api.main import app
from api.routes.contexts import ContextResponse, get_context_service
from auth.dependencies import require_session_auth
from db.base import get_db
from services.context_service import ContextService


def _context(*, is_public: bool = True):
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.workspace_id = uuid4()
    ctx.name = "shop"
    ctx.display_name = None
    ctx.description = None
    ctx.summary = None
    ctx.usage_guide = None
    ctx.is_default = False
    ctx.is_private = False
    ctx.is_public = is_public
    ctx.resource_id = "shop" if is_public else None
    ctx.is_locked = False
    ctx.sleep_mode = "skip"
    ctx.created_by = None
    ctx.created_at = datetime(2026, 11, 1, tzinfo=UTC)
    ctx.updated_at = None
    return ctx


def _override(*, detail=None, listed=None) -> None:
    app.dependency_overrides[require_session_auth] = lambda: {
        "user_id": "owner_user",
        "email": "owner@example.com",
        "role": "user",
        "current_workspace_id": uuid4(),
    }
    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    empty.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(return_value=empty)
    db.get = AsyncMock(return_value=None)
    app.dependency_overrides[get_db] = lambda: db
    service = AsyncMock(spec=ContextService)
    if detail is not None:
        service.get_context = AsyncMock(return_value=detail)
    if listed is not None:
        service.list_contexts = AsyncMock(return_value=listed)
    app.dependency_overrides[get_context_service] = lambda: service


def _cleanup() -> None:
    for key in (require_session_auth, get_db, get_context_service):
        app.dependency_overrides.pop(key, None)


def test_the_flag_defaults_to_false() -> None:
    response = ContextResponse(
        id=uuid4(), name="x", is_default=False, created_at=datetime(2026, 11, 1, tzinfo=UTC)
    )
    assert response.model_dump()["public_mcp_endpoint_live"] is False


def test_detail_sets_the_flag_from_the_lookup() -> None:
    live, plain = _context(), _context(is_public=False)
    for ctx, expected in ((live, True), (plain, False)):
        lookup = AsyncMock(return_value={live.id})
        _override(detail=ctx)
        try:
            with (
                patch("services.sync_managed_guard.live_public_endpoint_context_ids", lookup),
                TestClient(app, raise_server_exceptions=False) as client,
            ):
                response = client.get(f"/api/v1/contexts/{ctx.id}")
        finally:
            _cleanup()
        assert response.status_code == 200, response.text
        assert response.json()["public_mcp_endpoint_live"] is expected
        assert lookup.await_args.args[1] == [ctx.id]


def test_list_sets_the_flag_per_context_from_one_lookup() -> None:
    live, plain = _context(), _context(is_public=False)
    lookup = AsyncMock(return_value={live.id})
    _override(listed=[live, plain])
    try:
        with (
            patch("services.sync_managed_guard.live_public_endpoint_context_ids", lookup),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/api/v1/contexts")
    finally:
        _cleanup()
    assert response.status_code == 200, response.text
    flags = {c["id"]: c["public_mcp_endpoint_live"] for c in response.json()["contexts"]}
    assert flags == {str(live.id): True, str(plain.id): False}
    assert lookup.await_count == 1
    assert set(lookup.await_args.args[1]) == {live.id, plain.id}
