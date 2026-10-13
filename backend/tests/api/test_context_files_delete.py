"""``DELETE /api/v1/contexts/{id}/files`` (#1960): owner-only bulk purge of a context's files.

The row selection and accounting are pinned against PostgreSQL in
``tests/integration/test_context_delete_files.py``; this file pins the route's
contract: who may call it, what it answers, and that Redis is released only
after the commit with the bytes the purge reported.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_session_auth
from db.base import get_db
from utils.exceptions import AuthorizationError, NotFoundException

OWNER = "user-owner"


@pytest.fixture
def db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.fixture
def client(db: MagicMock):
    app.dependency_overrides[require_session_auth] = lambda: {"user_id": OWNER, "email": "o@x"}

    async def fake_db():
        yield db

    app.dependency_overrides[get_db] = fake_db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(require_session_auth, None)
        app.dependency_overrides.pop(get_db, None)


def _context(ws_id):
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.workspace_id = ws_id
    ctx.is_locked = False
    return ctx


def test_a_locked_context_keeps_its_files(client, db) -> None:
    """``is_locked`` protects the context's content as it does the context itself (#85)."""
    ctx = _context(uuid4())
    ctx.is_locked = True
    with (
        patch(
            "services.permission_service.PermissionService.check_context_owner",
            AsyncMock(return_value=ctx),
        ),
        patch(
            "services.file_storage_service.FileStorageService.purge_context_files",
            AsyncMock(),
        ) as purge,
    ):
        r = client.delete(f"/api/v1/contexts/{ctx.id}/files")

    assert r.status_code == 409
    purge.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_the_owner_purges_and_redis_follows_the_commit(client, db) -> None:
    ws_id = uuid4()
    ctx = _context(ws_id)
    with (
        patch(
            "services.permission_service.PermissionService.check_context_owner",
            AsyncMock(return_value=ctx),
        ) as owner_check,
        patch(
            "services.file_storage_service.FileStorageService.purge_context_files",
            AsyncMock(return_value=(3, 4096)),
        ) as purge,
        patch("services.storage_quota_service.release_storage_bytes", AsyncMock()) as release,
    ):
        r = client.delete(f"/api/v1/contexts/{ctx.id}/files")

    assert r.status_code == 200
    assert r.json() == {"deleted": 3, "released_bytes": 4096}
    # Resolved as the context's owner, with the lock allowing cleanup (#1941).
    _, kwargs = owner_check.await_args
    assert kwargs["capacity"].name == "ALLOWED"
    purge.assert_awaited_once_with(ctx.id)
    db.commit.assert_awaited_once()
    release.assert_awaited_once_with(workspace_id=ws_id, size_bytes=4096)


def test_nothing_to_release_skips_redis(client, db) -> None:
    ctx = _context(uuid4())
    with (
        patch(
            "services.permission_service.PermissionService.check_context_owner",
            AsyncMock(return_value=ctx),
        ),
        patch(
            "services.file_storage_service.FileStorageService.purge_context_files",
            AsyncMock(return_value=(0, 0)),
        ),
        patch("services.storage_quota_service.release_storage_bytes", AsyncMock()) as release,
    ):
        r = client.delete(f"/api/v1/contexts/{ctx.id}/files")

    assert r.status_code == 200 and r.json() == {"deleted": 0, "released_bytes": 0}
    release.assert_not_awaited()


@pytest.mark.parametrize(
    ("refusal", "status"),
    [
        (AuthorizationError("Insufficient permissions"), 403),
        (NotFoundException("Context", str(uuid4())), 404),
    ],
)
def test_a_non_owner_or_unknown_context_purges_nothing(client, db, refusal, status) -> None:
    """A member who is not the context owner gets 403; a context of another
    workspace or a deleted one is 404 — the resolver answers before any row moves."""
    with (
        patch(
            "services.permission_service.PermissionService.check_context_owner",
            AsyncMock(side_effect=refusal),
        ),
        patch(
            "services.file_storage_service.FileStorageService.purge_context_files",
            AsyncMock(),
        ) as purge,
        patch("services.storage_quota_service.release_storage_bytes", AsyncMock()) as release,
    ):
        r = client.delete(f"/api/v1/contexts/{uuid4()}/files")

    assert r.status_code == status
    purge.assert_not_awaited()
    db.commit.assert_not_awaited()
    release.assert_not_awaited()
