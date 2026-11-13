"""#565 malware scan: every read path refuses a file that is not downloadable (real PostgreSQL).

On real rows with the real ``PermissionService``:

- the service gate, the REST ``download-url`` route and the MCP
  ``get_file_download_url`` tool give the same answer per ``scan_status`` and
  switch position: a URL, 409 ``SCAN-001`` (+ ``Retry-After``) /
  ``file_scan_pending``, or 409 ``SCAN-002`` / ``file_blocked``;
- a caller who cannot read the file gets the uniform 404 / ``not_found`` in
  every scan state (no verdict leaks past the ACL);
- binding off: a ``clean`` file whose object changed is put back to
  ``pending`` (fenced, committed) and refused;
- confirm makes the row ``pending`` only when scanning is on; list / confirm
  report ``scan_status``;
- a context restore over a reservation that may have replaced the bytes
  brings the row back ``pending`` (R5);
- the dedup 409 never hands out a blocked row (R10 / W3).

Blob storage is the in-memory fake; Redis quota calls are stubbed. Not executed
in the local unit run — needs the DB container.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from models.auth import Context, Workspace, WorkspaceMember
from models.file_objects import SCAN_STATUSES, FileObject
from services.file_storage_service import FileStorageService
from tests.storage._fakes import FakeBlobStorage
from utils.datetime import utcnow
from utils.exceptions import (
    DuplicateFileError,
    FileBlockedError,
    FileScanPendingError,
    NotFoundException,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

OWNER = "owner-565"
OTHER = "other-565"  # a workspace member who cannot see the private context
SCANNED_AT = datetime(2026, 11, 13, 1, 2, 3)

# What a reader gets per (scanning on, scan_status). Pinned against every
# value of the CHECK constraint below.
EXPECTED: dict[tuple[bool, str], str] = {
    (True, "unscanned"): "url",
    (True, "pending"): "pending",
    (True, "clean"): "url",
    (True, "infected"): "blocked",
    (True, "error"): "blocked",
    (False, "unscanned"): "url",
    (False, "pending"): "url",
    (False, "clean"): "url",
    (False, "infected"): "blocked",
    (False, "error"): "url",
}
CASES = sorted(EXPECTED)


def _id(case: tuple[bool, str]) -> str:
    return f"{'on' if case[0] else 'off'}-{case[1]}"


def test_every_scan_state_has_an_expected_answer() -> None:
    assert {s for _, s in EXPECTED} == set(SCAN_STATUSES)
    assert len(EXPECTED) == 2 * len(SCAN_STATUSES)


@pytest.fixture
def scanning(monkeypatch):
    """Scanning on, checksum binding off; ``scanning(False)`` turns it off."""
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_checksum_binding_enabled", False)
    monkeypatch.setattr(settings, "allowed_file_content_types", "application/pdf,text/plain")

    def _set(on: bool = True) -> None:
        monkeypatch.setattr(settings, "file_scan_enabled", on)

    _set(True)
    return _set


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession, async_engine):
    """A workspace, its owner, a member without access to the owner's private context."""
    ws_id = uuid.uuid4()
    db_session.add(Workspace(id=ws_id, name=f"scan-{ws_id}", owner_user_id=OWNER, plan_name="pro"))
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws_id, user_id=OWNER, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=ws_id, user_id=OTHER, role=WorkspaceRole.MEMBER),
        ]
    )
    ctx = Context(
        id=uuid.uuid4(), workspace_id=ws_id, name="private", created_by=OWNER, is_private=True
    )
    db_session.add(ctx)
    await db_session.flush()
    await db_session.execute(
        text(
            "UPDATE workspace_members SET allowed_context_ids = '{}' "
            "WHERE workspace_id = :w AND user_id = :u"
        ),
        {"w": str(ws_id), "u": OTHER},
    )
    await db_session.commit()
    storage = FakeBlobStorage()
    world = {
        "ws": ws_id,
        "ctx": ctx.id,
        "db": db_session,
        "storage": storage,
        "factory": async_sessionmaker(async_engine, expire_on_commit=False),
    }
    with (
        patch("services.file_storage_service.get_blob_storage", return_value=storage),
        patch(
            "services.file_storage_service.storage_quota_service.reserve_storage_bytes",
            new=AsyncMock(),
        ),
        patch(
            "services.file_storage_service.storage_quota_service.release_storage_bytes",
            new=AsyncMock(),
        ),
        patch("services.file_storage_service.kick_file_scan"),
    ):
        yield world
    await db_session.rollback()
    for table, column in (
        ("file_objects", "workspace_id"),
        ("workspace_storage_usage", "workspace_id"),
        ("contexts", "workspace_id"),
        ("workspace_members", "workspace_id"),
        ("workspaces", "id"),
    ):
        await db_session.execute(
            text(f"DELETE FROM {table} WHERE {column} = :w"),  # noqa: S608 - fixed names
            {"w": str(ws_id)},
        )
    await db_session.commit()


async def _file(world: dict[str, Any], scan_status: str, *, data: bytes = b"%PDF-1") -> FileObject:
    """A committed uploaded row in the private context, its object in the fake storage."""
    sha = uuid.uuid4().hex * 2
    key = f"{world['ws']}/{sha[:2]}/{sha}"
    await world["storage"].write_object(key, data, "application/pdf", "etag-1")
    world["storage"].last_modified[key] = SCANNED_AT
    row = FileObject(
        id=uuid.uuid4(),
        workspace_id=world["ws"],
        context_id=world["ctx"],
        sha256=sha,
        size_bytes=len(data),
        content_type="application/pdf",
        filename="a.pdf",
        storage_backend="r2",
        storage_key=key,
        status="uploaded",
        created_by=OWNER,
        uploaded_at=utcnow(),
        scan_status=scan_status,
        scan_etag="etag-1" if scan_status == "clean" else None,
        scan_last_modified=SCANNED_AT if scan_status == "clean" else None,
        scanned_at=SCANNED_AT if scan_status in ("clean", "infected", "error") else None,
        scan_next_attempt_at=utcnow() + timedelta(minutes=5) if scan_status == "pending" else None,
    )
    world["db"].add(row)
    await world["db"].commit()
    return row


async def _reload(world: dict[str, Any], file_id: uuid.UUID) -> FileObject:
    async with world["factory"]() as s:
        return (await s.execute(select(FileObject).where(FileObject.id == file_id))).scalar_one()


# --- the three read paths -----------------------------------------------------------------


async def _via_service(world: dict[str, Any], file_id: uuid.UUID, actor: str) -> str:
    async with world["factory"]() as s:
        try:
            await FileStorageService(s).get_presigned_download(
                workspace_id=world["ws"], file_id=file_id, actor_user_id=actor
            )
        except FileScanPendingError:
            return "pending"
        except FileBlockedError:
            return "blocked"
        except NotFoundException:
            return "not_found"
        return "url"


@contextlib.asynccontextmanager
async def _rest(world: dict[str, Any], actor: str) -> AsyncIterator[Any]:
    import httpx

    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db

    opened: list[AsyncSession] = []

    async def _db() -> AsyncIterator[AsyncSession]:
        s = world["factory"]()
        opened.append(s)
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise

    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: {
        "user_id": actor,
        "auth_type": "session",
    }
    app.dependency_overrides[get_db] = _db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        for s in opened:
            await s.close()


async def _via_rest(world: dict[str, Any], file_id: uuid.UUID, actor: str) -> str:
    async with _rest(world, actor) as client:
        r = await client.get(
            f"/api/v1/files/{file_id}/download-url", params={"workspace_id": str(world["ws"])}
        )
    if r.status_code == 200:
        assert r.json()["download_url"].startswith("https://test.local/get/")
        return "url"
    if r.status_code == 404:
        return "not_found"
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["details"]["file_id"] == str(file_id)
    if body["error"] == "SCAN-001":
        assert r.headers["Retry-After"] == "30"
        return "pending"
    assert body["error"] == "SCAN-002"
    assert "Retry-After" not in r.headers
    return "blocked"


async def _via_mcp(world: dict[str, Any], file_id: uuid.UUID, actor: str) -> str:
    from mcp_server.tools.files import handle_get_file_download_url

    async def _db() -> AsyncIterator[AsyncSession]:
        async with world["factory"]() as s:
            yield s
            await s.commit()

    with patch("db.base.get_db", new=_db):
        out = await handle_get_file_download_url(
            {"file_id": str(file_id), "workspace_id": str(world["ws"])}, actor, None
        )
    body = json.loads(out[0].text)
    if "download_url" in body:
        return "url"
    return {"file_scan_pending": "pending", "file_blocked": "blocked", "not_found": "not_found"}[
        body["error"]
    ]


READERS = {"service": _via_service, "rest": _via_rest, "mcp": _via_mcp}


@pytest.mark.parametrize("reader", sorted(READERS))
@pytest.mark.parametrize("case", CASES, ids=_id)
async def test_each_read_path_follows_the_gate(world, scanning, reader, case):
    on, state = case
    scanning(on)
    row = await _file(world, state)
    assert await READERS[reader](world, row.id, OWNER) == EXPECTED[case]
    # Asking does not change a stable row.
    assert (await _reload(world, row.id)).scan_status == state


@pytest.mark.parametrize("reader", sorted(READERS))
@pytest.mark.parametrize("state", sorted(SCAN_STATUSES))
async def test_a_caller_without_access_gets_404_in_every_state(world, scanning, reader, state):
    row = await _file(world, state)
    assert await READERS[reader](world, row.id, OTHER) == "not_found"


# --- binding off: the scanned version only ---------------------------------------------------


@pytest.mark.parametrize("reader", sorted(READERS))
async def test_a_changed_clean_object_is_requeued_and_refused(world, scanning, reader):
    row = await _file(world, "clean")
    # Same ETag and size, other bytes: only LastModified shows the rewrite.
    world["storage"].last_modified[row.storage_key] = SCANNED_AT + timedelta(seconds=1)
    before = utcnow()

    assert await READERS[reader](world, row.id, OWNER) == "pending"

    after = await _reload(world, row.id)
    assert after.scan_status == "pending"
    assert after.scan_attempts == 0
    assert after.scanned_at is None
    s = get_settings()
    window = timedelta(seconds=s.presign_put_ttl_seconds + s.file_scan_put_expiry_margin_seconds)
    assert after.scan_next_attempt_at >= before + window
    # The next reader is held by the pending row itself.
    assert await READERS[reader](world, row.id, OWNER) == "pending"


async def test_a_missing_object_is_requeued_not_served(world, scanning):
    row = await _file(world, "clean")
    await world["storage"].delete_object(row.storage_key)
    assert await _via_rest(world, row.id, OWNER) == "pending"
    assert (await _reload(world, row.id)).scan_status == "pending"


async def test_binding_on_serves_a_clean_file_without_a_compare(world, scanning, monkeypatch):
    monkeypatch.setattr(get_settings(), "storage_checksum_binding_enabled", True)
    row = await _file(world, "clean")
    world["storage"].last_modified[row.storage_key] = SCANNED_AT + timedelta(seconds=1)
    assert await _via_rest(world, row.id, OWNER) == "url"
    assert (await _reload(world, row.id)).scan_status == "clean"


# --- writers and metadata -----------------------------------------------------------------


async def _reserve_and_put(world: dict[str, Any], data: bytes, *, context_id=None) -> uuid.UUID:
    sha = hashlib.sha256(data).hexdigest()
    async with world["factory"]() as s:
        result = await FileStorageService(s).reserve_upload(
            workspace_id=world["ws"],
            created_by=OWNER,
            filename="a.pdf",
            content_type="application/pdf",
            size_bytes=len(data),
            sha256=sha,
            context_id=context_id,
        )
    await world["storage"].write_object(
        f"{world['ws']}/{sha[:2]}/{sha}", data, "application/pdf", sha
    )
    return result.file_id


@pytest.mark.parametrize("on", [True, False], ids=["on", "off"])
async def test_confirm_sets_pending_only_when_scanning_is_on(world, scanning, on):
    scanning(on)
    data = uuid.uuid4().bytes
    file_id = await _reserve_and_put(world, data)
    async with _rest(world, OWNER) as client:
        r = await client.post(
            f"/api/v1/files/{file_id}/confirm",
            params={"workspace_id": str(world["ws"])},
            json={"sha256": hashlib.sha256(data).hexdigest()},
        )
    assert r.status_code == 200, r.text
    assert r.json()["scan_status"] == ("pending" if on else "unscanned")
    row = await _reload(world, file_id)
    assert row.scan_status == ("pending" if on else "unscanned")
    if on:
        # Binding off: due only after the reservation's PUT URL expired.
        assert row.scan_next_attempt_at > row.uploaded_at
        assert await _via_rest(world, file_id, OWNER) == "pending"
    else:
        assert row.scan_next_attempt_at is None
        assert await _via_rest(world, file_id, OWNER) == "url"


async def test_list_reports_scan_status(world, scanning):
    rows = [await _file(world, state) for state in ("pending", "infected", "clean")]
    async with _rest(world, OWNER) as client:
        r = await client.get("/api/v1/files", params={"workspace_id": str(world["ws"])})
    assert r.status_code == 200, r.text
    listed = {f["id"]: f["scan_status"] for f in r.json()}
    assert {str(row.id): row.scan_status for row in rows}.items() <= listed.items()


# --- restore (R5) and dedup (R10 / W3) -------------------------------------------------------


async def test_restore_over_a_cancelled_reservation_brings_a_clean_row_back_pending(
    world, scanning
):
    db: AsyncSession = world["db"]
    data = uuid.uuid4().bytes
    file_id = await _reserve_and_put(world, data, context_id=world["ctx"])
    async with world["factory"]() as s:
        await FileStorageService(s).confirm_upload(
            workspace_id=world["ws"],
            file_id=file_id,
            sha256=hashlib.sha256(data).hexdigest(),
            actor_user_id=OWNER,
        )
    # The scan said clean.
    await db.execute(
        text(
            "UPDATE file_objects SET scan_status = 'clean', scan_etag = 'e', "
            "scanned_at = now() WHERE id = :f"
        ),
        {"f": str(file_id)},
    )
    # The context is deleted: its files go with its timestamp.
    ctx = (await db.execute(select(Context).where(Context.id == world["ctx"]))).scalar_one()
    ctx.deleted_at = utcnow()
    await FileStorageService(db).purge_live_context_files([ctx.id], deleted_at=ctx.deleted_at)
    await db.commit()
    # Meanwhile a second reservation of the same content (same key) is made and
    # cancelled; its PUT may have replaced the bytes.
    second = await _reserve_and_put(world, data)
    async with world["factory"]() as s:
        await FileStorageService(s).delete_file(
            workspace_id=world["ws"], file_id=second, actor_user_id=OWNER
        )
    # Restore.
    ctx = (await db.execute(select(Context).where(Context.id == world["ctx"]))).scalar_one()
    outcome = await FileStorageService(db).restore_context_files(ctx)
    ctx.deleted_at = None
    await db.commit()

    assert outcome.restored == 1
    row = await _reload(world, file_id)
    assert row.deleted_at is None
    assert row.scan_status == "pending"
    assert row.scan_attempts == 0
    assert await _via_rest(world, file_id, OWNER) == "pending"


@pytest.mark.parametrize("state", ["infected", "error"])
async def test_dedup_never_hands_out_a_blocked_row(world, scanning, state):
    data = uuid.uuid4().bytes
    file_id = await _reserve_and_put(world, data)
    await world["db"].execute(
        text(
            "UPDATE file_objects SET status = 'uploaded', expires_at = NULL, "
            "uploaded_at = now(), scan_status = :s WHERE id = :f"
        ),
        {"s": state, "f": str(file_id)},
    )
    await world["db"].commit()

    with pytest.raises(DuplicateFileError) as exc_info:
        await _reserve_and_put(world, data)
    assert exc_info.value.existing is None
    assert exc_info.value.message.endswith("was blocked by the malware scan")

    async with _rest(world, OWNER) as client:
        r = await client.post(
            "/api/v1/files/reserve",
            json={
                "workspace_id": str(world["ws"]),
                "filename": "a.pdf",
                "content_type": "application/pdf",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        )
    assert r.status_code == 409
    assert "existing_file" not in r.json()


async def test_dedup_hands_out_a_pending_row(world, scanning):
    data = uuid.uuid4().bytes
    file_id = await _reserve_and_put(world, data)
    await world["db"].execute(
        text(
            "UPDATE file_objects SET status = 'uploaded', expires_at = NULL, "
            "uploaded_at = now(), scan_status = 'pending' WHERE id = :f"
        ),
        {"f": str(file_id)},
    )
    await world["db"].commit()
    with pytest.raises(DuplicateFileError) as exc_info:
        await _reserve_and_put(world, data)
    assert exc_info.value.existing is not None
    assert exc_info.value.existing.id == file_id
