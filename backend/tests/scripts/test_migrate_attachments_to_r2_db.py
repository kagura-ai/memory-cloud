"""The legacy attachment migration keeps the memory's context binding (#2647).

``scripts/migrate_attachments_to_r2.py`` against Postgres: an attachment of a
memory in a private context lands in ``file_objects`` bound to that context
(hidden from members without access to it), a context-less memory's stays
workspace-shared, a memory in a soft-deleted context is skipped, and
``--repair-context`` binds the rows an earlier run left NULL without touching
user uploads.

Every test starts from no attachments and no migration rows: the migration and
the repair walk the whole table, so the counts would otherwise depend on test
order.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from models.auth import Context, Workspace
from models.file_objects import FileObject
from models.memory import Attachment, Memory
from services import storage_quota_service
from services.file_storage_service import FileStorageService
from tests.storage._fakes import FakeBlobStorage
from utils.datetime import utcnow

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import migrate_attachments_to_r2 as mig  # noqa: E402

_PREFIX = "migration:attachment:"


async def _clear(db: AsyncSession) -> None:
    await db.execute(delete(FileObject).where(FileObject.created_by.like(f"{_PREFIX}%")))
    await db.execute(delete(Attachment))
    await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def _isolated(db_session: AsyncSession) -> AsyncGenerator[None, None]:
    await _clear(db_session)
    yield
    await db_session.rollback()
    await _clear(db_session)


@pytest.fixture
def fake_storage() -> FakeBlobStorage:
    return FakeBlobStorage()


@pytest.fixture
def patched(fake_storage: FakeBlobStorage):
    """The script's service writes to the fake; no Redis for the quota bump."""
    with (
        patch.object(
            mig,
            "FileStorageService",
            lambda db: FileStorageService(db, storage=fake_storage),
        ),
        patch.object(storage_quota_service, "bump_committed_storage_bytes", AsyncMock()),
    ):
        yield


async def _workspace(db: AsyncSession) -> UUID:
    owner = f"mig-{uuid4().hex[:8]}"
    ws = Workspace(id=uuid4(), name=f"mig-ws-{uuid4().hex[:8]}", owner_user_id=owner)
    db.add(ws)
    await db.flush()
    return ws.id


async def _context(db: AsyncSession, ws_id: UUID, *, deleted: bool = False) -> UUID:
    ctx = Context(
        id=uuid4(),
        workspace_id=ws_id,
        name=f"mig-ctx-{uuid4().hex[:8]}",
        created_by="mig-owner",
        is_private=True,
        deleted_at=utcnow() if deleted else None,
    )
    db.add(ctx)
    await db.flush()
    return ctx.id


async def _attachment(
    db: AsyncSession,
    ws_id: UUID,
    ctx_id: UUID | None,
    data: bytes = b"legacy-bytes",
    att_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    """A memory (in ``ctx_id`` or context-less) with one attachment."""
    memory = Memory(
        id=uuid4(),
        user_id="mig-owner",
        workspace_id=ws_id,
        context_id=ctx_id,
        summary="attachment holder",
        content="content",
        type="note",
        client="test",
    )
    db.add(memory)
    await db.flush()
    att = Attachment(
        id=att_id or uuid4(),
        memory_id=memory.id,
        filename="report.txt",
        content_type="text/plain",
        size_bytes=len(data),
        data=data,
    )
    db.add(att)
    await db.flush()
    return memory.id, att.id


def _legacy_key(ws_id: UUID, att_id: UUID) -> str:
    return f"{ws_id}/legacy/{att_id}/report.txt"


async def _file_rows(db: AsyncSession, ws_id: UUID) -> list[FileObject]:
    db.expire_all()
    result = await db.execute(select(FileObject).where(FileObject.workspace_id == ws_id))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Forward path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_context_attachment_is_bound_to_its_context(
    db_session, patched, fake_storage
):
    ws = await _workspace(db_session)
    ctx = await _context(db_session, ws)
    _, att = await _attachment(db_session, ws, ctx)
    await db_session.commit()

    counts = await mig.migrate_all(db_session, dry_run=False, batch_size=10)
    print("migrate_all private context:", counts)

    assert counts == {"migrated": 1, "skipped": 0, "dry_run": 0, "failed": 0}
    rows = await _file_rows(db_session, ws)
    assert len(rows) == 1
    file = rows[0]
    assert file.context_id == ctx
    assert file.created_by == f"{_PREFIX}{att}"
    assert file.storage_key == _legacy_key(ws, att)
    assert fake_storage.objects[_legacy_key(ws, att)][0] == b"legacy-bytes"

    # The done-when: a member without access to the context does not see it.
    service = FileStorageService(db_session, storage=fake_storage)
    assert await service.list_files(workspace_id=ws, accessible_context_ids=[]) == []
    visible = await service.list_files(workspace_id=ws, accessible_context_ids=[ctx])
    assert [f.id for f in visible] == [file.id]


@pytest.mark.asyncio
async def test_contextless_memory_attachment_stays_workspace_shared(
    db_session, patched, fake_storage
):
    ws = await _workspace(db_session)
    _, att = await _attachment(db_session, ws, None)
    await db_session.commit()

    counts = await mig.migrate_all(db_session, dry_run=False, batch_size=10)

    assert counts == {"migrated": 1, "skipped": 0, "dry_run": 0, "failed": 0}
    rows = await _file_rows(db_session, ws)
    assert [(r.context_id, r.created_by) for r in rows] == [(None, f"{_PREFIX}{att}")]
    service = FileStorageService(db_session, storage=fake_storage)
    visible = await service.list_files(workspace_id=ws, accessible_context_ids=[])
    assert [f.id for f in visible] == [rows[0].id]


@pytest.mark.asyncio
async def test_memory_in_a_soft_deleted_context_is_skipped(db_session, patched, fake_storage):
    ws = await _workspace(db_session)
    gone = await _context(db_session, ws, deleted=True)
    await _attachment(db_session, ws, gone)
    await db_session.commit()

    dry = await mig.migrate_all(db_session, dry_run=True, batch_size=10)
    assert dry == {"migrated": 0, "skipped": 1, "dry_run": 0, "failed": 0}

    counts = await mig.migrate_all(db_session, dry_run=False, batch_size=10)

    assert counts == {"migrated": 0, "skipped": 1, "dry_run": 0, "failed": 0}
    assert await _file_rows(db_session, ws) == []
    assert fake_storage.objects == {}


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db_session, patched, fake_storage):
    ws = await _workspace(db_session)
    ctx = await _context(db_session, ws)
    await _attachment(db_session, ws, ctx)
    await _attachment(db_session, ws, None)
    await db_session.commit()

    counts = await mig.migrate_all(db_session, dry_run=True, batch_size=10)

    assert counts == {"migrated": 0, "skipped": 0, "dry_run": 2, "failed": 0}
    assert await _file_rows(db_session, ws) == []
    assert fake_storage.objects == {}


@pytest.mark.asyncio
async def test_a_failed_row_does_not_abort_the_rest_of_the_page(db_session, patched, fake_storage):
    """A row that fails rolls the session back; the rows after it in the same
    page still migrate (they must not depend on ORM state the rollback expired)."""
    ws_gone = await _workspace(db_session)
    ws = await _workspace(db_session)
    # Page order is by attachment id: the failing row comes first.
    failing_id, second_id = sorted([uuid4(), uuid4()])
    await _attachment(db_session, ws_gone, None, att_id=failing_id)
    await _attachment(db_session, ws, None, att_id=second_id)
    workspace = (
        await db_session.execute(select(Workspace).where(Workspace.id == ws_gone))
    ).scalar_one()
    workspace.deleted_at = utcnow()  # migrate_attachment refuses a deleted workspace
    await db_session.commit()

    counts = await mig.migrate_all(db_session, dry_run=False, batch_size=10)

    assert counts == {"migrated": 1, "skipped": 0, "dry_run": 0, "failed": 1}
    assert [r.created_by for r in await _file_rows(db_session, ws)] == [f"{_PREFIX}{second_id}"]
    assert await _file_rows(db_session, ws_gone) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["soft", "hard"])
async def test_context_deleted_after_the_page_read_is_skipped(db_session, fake_storage, how):
    """The page read the context live; it is deleted before the row migrates.

    ``migrate_attachment`` locks and re-checks the context before the R2 write,
    so neither bytes without a registry row (hard delete: the FK check would
    fail at flush) nor a live row in a deleted context (soft delete) result;
    the script counts the row ``skipped``, not ``failed``.
    """
    ws = await _workspace(db_session)
    ctx = await _context(db_session, ws)
    await _attachment(db_session, ws, ctx)
    await db_session.commit()

    class _ContextGoesFirst(FileStorageService):
        async def migrate_attachment(self, **kwargs) -> FileObject:
            if how == "soft":
                await self.db.execute(
                    update(Context).where(Context.id == ctx).values(deleted_at=utcnow())
                )
            else:
                await self.db.execute(delete(Context).where(Context.id == ctx))
            return await super().migrate_attachment(**kwargs)

    with (
        patch.object(
            mig, "FileStorageService", lambda db: _ContextGoesFirst(db, storage=fake_storage)
        ),
        patch.object(storage_quota_service, "bump_committed_storage_bytes", AsyncMock()),
    ):
        counts = await mig.migrate_all(db_session, dry_run=False, batch_size=10)

    assert counts == {"migrated": 0, "skipped": 1, "dry_run": 0, "failed": 0}
    assert await _file_rows(db_session, ws) == []
    assert fake_storage.objects == {}


@pytest.mark.asyncio
async def test_an_already_migrated_row_releases_its_context_lock(
    db_session, async_engine: AsyncEngine, fake_storage
):
    """The existing-row branch returns without committing; the script ends the
    row's transaction so a re-run does not hold every context's row lock (and
    block a context hard delete) until the run ends."""
    ws = await _workspace(db_session)
    first_ctx = await _context(db_session, ws)
    second_ctx = await _context(db_session, ws)
    first_id, second_id = sorted([uuid4(), uuid4()])
    await _attachment(db_session, ws, first_ctx, data=b"one", att_id=first_id)
    await _attachment(db_session, ws, second_ctx, data=b"two", att_id=second_id)
    db_session.add(_legacy_file(ws, first_id))  # migrated before
    await db_session.commit()

    lock_free: list[bool] = []

    class _ProbesFirstContext(FileStorageService):
        async def migrate_attachment(self, **kwargs) -> FileObject:
            if kwargs["attachment_id"] == second_id:
                async with async_engine.connect() as other:
                    await other.execute(text("SET LOCAL lock_timeout = '2s'"))
                    try:
                        await other.execute(
                            select(Context.id).where(Context.id == first_ctx).with_for_update()
                        )
                        lock_free.append(True)
                    except DBAPIError:
                        lock_free.append(False)
                    await other.rollback()
            return await super().migrate_attachment(**kwargs)

    with (
        patch.object(
            mig, "FileStorageService", lambda db: _ProbesFirstContext(db, storage=fake_storage)
        ),
        patch.object(storage_quota_service, "bump_committed_storage_bytes", AsyncMock()),
    ):
        counts = await mig.migrate_all(db_session, dry_run=False, batch_size=10)

    assert counts == {"migrated": 2, "skipped": 0, "dry_run": 0, "failed": 0}
    assert lock_free == [True]


@pytest.mark.asyncio
async def test_pages_of_one_row_migrate_every_attachment_once(db_session, patched, fake_storage):
    ws = await _workspace(db_session)
    ctx = await _context(db_session, ws)
    for data in (b"one", b"two", b"three"):  # distinct bytes: one live row per sha256
        await _attachment(db_session, ws, ctx, data=data)
    await db_session.commit()

    dry = await mig.migrate_all(db_session, dry_run=True, batch_size=1)
    counts = await mig.migrate_all(db_session, dry_run=False, batch_size=1)

    assert dry == {"migrated": 0, "skipped": 0, "dry_run": 3, "failed": 0}
    assert counts == {"migrated": 3, "skipped": 0, "dry_run": 0, "failed": 0}
    rows = await _file_rows(db_session, ws)
    assert len(rows) == 3
    assert {r.context_id for r in rows} == {ctx}


# ---------------------------------------------------------------------------
# --repair-context
# ---------------------------------------------------------------------------


def _legacy_file(ws_id: UUID, att_id: UUID, *, created_by: str | None = None) -> FileObject:
    """A row the pre-#2647 migration wrote: legacy key, NULL context."""
    return FileObject(
        id=uuid4(),
        workspace_id=ws_id,
        context_id=None,
        sha256=uuid4().hex + uuid4().hex,  # unique per workspace among live rows
        size_bytes=12,
        content_type="text/plain",
        filename="report.txt",
        storage_backend="r2",
        storage_key=_legacy_key(ws_id, att_id),
        status="uploaded",
        created_by=created_by or f"{_PREFIX}{att_id}",
        uploaded_at=utcnow(),
    )


@pytest_asyncio.fixture
async def legacy_rows(db_session: AsyncSession) -> dict[str, UUID]:
    """Rows (i)-(v) of the brief, all with a NULL ``context_id``."""
    ws = await _workspace(db_session)
    ctx = await _context(db_session, ws)
    gone_ctx = await _context(db_session, ws, deleted=True)
    _, att_bound = await _attachment(db_session, ws, ctx)  # (i)
    _, att_shared = await _attachment(db_session, ws, None)  # (ii)
    att_orphan = uuid4()  # (iii): the attachment is gone
    _, att_gone_ctx = await _attachment(db_session, ws, gone_ctx)  # (iv)
    rows = {
        "bound": _legacy_file(ws, att_bound),
        "shared": _legacy_file(ws, att_shared),
        "orphan": _legacy_file(ws, att_orphan),
        "gone_ctx": _legacy_file(ws, att_gone_ctx),
        # (v) a user upload: NULL context, a user id in created_by, and an
        # attachment id in the key that the repair would otherwise resolve.
        "user": _legacy_file(ws, att_bound, created_by="u_someone"),
    }
    rows["user"].storage_key = f"{ws}/ab/{uuid4().hex}"
    db_session.add_all(rows.values())
    await db_session.commit()
    ids = {name: row.id for name, row in rows.items()}
    return {**ids, "ws": ws, "ctx": ctx, "gone_ctx_id": gone_ctx}


async def _contexts(db: AsyncSession, file_ids: dict[str, UUID]) -> dict[str, UUID | None]:
    db.expire_all()
    names = ["bound", "shared", "orphan", "gone_ctx", "user"]
    result = await db.execute(
        select(FileObject.id, FileObject.context_id).where(
            FileObject.id.in_([file_ids[n] for n in names])
        )
    )
    by_id = dict(result.tuples().all())
    return {n: by_id[file_ids[n]] for n in names}


@pytest.mark.asyncio
async def test_repair_dry_run_counts_and_changes_nothing(db_session, legacy_rows):
    counts = await mig.repair_context_bindings(db_session, dry_run=True, batch_size=2)
    print("repair dry run:", counts)

    assert counts == {
        "bound": 2,
        "shared": 1,
        "orphan": 1,
        "context_deleted": 1,
        "unparsable": 0,
    }
    assert set((await _contexts(db_session, legacy_rows)).values()) == {None}


@pytest.mark.asyncio
async def test_repair_binds_migrated_rows_and_leaves_the_rest(db_session, legacy_rows):
    counts = await mig.repair_context_bindings(db_session, dry_run=False, batch_size=2)
    print("repair apply:", counts)

    assert counts == {
        "bound": 2,
        "shared": 1,
        "orphan": 1,
        "context_deleted": 1,
        "unparsable": 0,
    }
    assert await _contexts(db_session, legacy_rows) == {
        "bound": legacy_rows["ctx"],
        "shared": None,
        "orphan": None,
        "gone_ctx": legacy_rows["gone_ctx_id"],
        "user": None,  # a user upload is never touched
    }

    again = await mig.repair_context_bindings(db_session, dry_run=False, batch_size=2)
    print("repair second apply:", again)
    assert again["bound"] == 0
    assert again["context_deleted"] == 0


@pytest.mark.asyncio
async def test_run_picks_the_mode(db_session, legacy_rows):
    """``run`` opens one session and runs the repair only with the flag."""

    async def _one_session():
        yield db_session

    with patch.object(mig, "get_db", _one_session):
        repair = await mig.run(dry_run=True, batch_size=10, repair_context=True)
        migrate = await mig.run(dry_run=True, batch_size=10)

    assert repair["bound"] == 2 and repair["shared"] == 1
    # (i) and (ii) would migrate; (iv)'s context is soft-deleted.
    assert migrate == {"migrated": 0, "skipped": 1, "dry_run": 2, "failed": 0}


@pytest.mark.asyncio
async def test_repair_binds_a_page_in_file_id_order(db_session, async_engine: AsyncEngine):
    """The page's UPDATEs take file row locks in id order, the order
    ``_purge_live_rows`` locks in (backend rule: multi-row writes lock rows in
    id order); storage-key order could cross a concurrent workspace purge."""
    ws = await _workspace(db_session)
    ctx = await _context(db_session, ws)
    atts = sorted([(await _attachment(db_session, ws, ctx, data=bytes([i])))[1] for i in range(3)])
    file_ids = sorted(uuid4() for _ in range(3))
    # Legacy keys sort by attachment id: pair them with descending file ids so
    # key order is the reverse of id order.
    for att, file_id in zip(atts, reversed(file_ids), strict=True):
        row = _legacy_file(ws, att)
        row.id = file_id
        db_session.add(row)
    await db_session.commit()

    wanted = {str(f) for f in file_ids}
    updated: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.lstrip().upper().startswith("UPDATE FILE_OBJECTS"):
            updated.extend(str(p) for p in parameters if str(p) in wanted)

    event.listen(async_engine.sync_engine, "before_cursor_execute", _record)
    try:
        counts = await mig.repair_context_bindings(db_session, dry_run=False, batch_size=10)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", _record)

    assert counts["bound"] == 3
    assert updated == [str(f) for f in file_ids]


@pytest.mark.asyncio
async def test_repair_skips_an_unparsable_created_by(db_session):
    ws = await _workspace(db_session)
    row = _legacy_file(ws, uuid4(), created_by=f"{_PREFIX}not-a-uuid")
    db_session.add(row)
    await db_session.commit()
    row_id = row.id

    counts = await mig.repair_context_bindings(db_session, dry_run=False, batch_size=10)

    assert counts["unparsable"] == 1 and counts["bound"] == 0
    db_session.expire_all()
    assert (
        await db_session.scalar(select(FileObject.context_id).where(FileObject.id == row_id))
    ) is None
