"""Management reads share the workspace row; writers still exclude them (#2925).

``GET .../management``, ``GET .../management/access`` and
``POST .../management/resume/receipt`` take the workspace row ``FOR SHARE NOWAIT``
with the short retry of #2914 and the installation row ``FOR SHARE NOWAIT``. The
management controls (stop, finalize, resume preview, diff and confirm, reconnect)
keep ``FOR UPDATE NOWAIT``. Every test runs a second real PostgreSQL session (or a
second HTTP request) next to the read, so the lock modes are the database's own.
"""

import asyncio
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import DBAPIError

from models.auth import Context, IdentityLink, Workspace
from models.sync_installation import SyncInstallation
from services import sync_management
from services.workspace_locks import NOWAIT_LOCK_BACKOFF_SECONDS, lock_workspace_for_update
from tests.integration import test_sync_management_db as management
from tests.integration.sync_feed_fixtures import human
from tests.integration.sync_installation_fixtures import transfer
from utils.db_helpers import is_lock_not_available
from utils.exceptions import ConflictError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, managed = (
    management.store,
    management.worker,
    management.feeds,
    management.managed,
)

# A holder that lets go after this long is one the retry outlasts.
SHORT_HOLD_SECONDS = NOWAIT_LOCK_BACKOFF_SECONDS * 4
READS = ("status", "access", "receipt")


def receipt_body(subject):
    return {
        "operation_id": str(uuid4()),
        "preview_id": str(uuid4()),
        "diff_hash": "b" * 64,
        "original_subject": subject.store.users["owner"],
    }


async def read(subject, kind, role="owner"):
    """One management read as ``role``; ``receipt`` needs an Editor with the manage scope."""
    if kind == "status":
        return await human(subject, "GET", "management", role=role)
    if kind == "access":
        return await human(subject, "GET", "management/access", role=role)
    return await human(
        subject, "POST", "management/resume/receipt", role=role, body=receipt_body(subject)
    )


def ok(response):
    assert response.status_code == 200, response.text
    if "result" in response.json():
        assert response.json()["result"] == "not_found", response.text


def busy(response):
    assert response.status_code == 409, response.text
    assert response.json() == {"error": "RES-002"}


def pause_inside_boundary(monkeypatch):
    """Hold the first management boundary after its workspace lock until ``release``."""
    inside, release = asyncio.Event(), asyncio.Event()
    original = sync_management.load_sync_identity

    async def paused(*args, **kwargs):
        if not inside.is_set():
            inside.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(sync_management, "load_sync_identity", paused)
    return inside, release


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("status", "status"),
        ("access", "access"),
        ("status", "access"),
        ("access", "receipt"),
        ("receipt", "receipt"),
    ],
)
async def test_two_concurrent_management_reads_both_return_200(managed, monkeypatch, first, second):
    barrier = asyncio.Barrier(2)
    original = sync_management.load_sync_identity

    async def meet(*args, **kwargs):
        # Both requests hold their workspace row lock here at the same time and
        # then take the installation row; with an exclusive lock on either the
        # second answers 409 and the first times out waiting for it.
        try:
            await asyncio.wait_for(barrier.wait(), timeout=5)
        except (TimeoutError, asyncio.BrokenBarrierError):
            pass
        return await original(*args, **kwargs)

    monkeypatch.setattr(sync_management, "load_sync_identity", meet)
    one, two = await asyncio.gather(read(managed, first), read(managed, second, role="admin"))
    ok(one)
    ok(two)
    assert barrier.n_waiting == 0 and not barrier.broken


async def test_gathered_management_reads_of_one_installation_all_return_200(managed):
    responses = await asyncio.gather(
        read(managed, "status"),
        read(managed, "status", role="member"),
        read(managed, "access"),
        read(managed, "access", role="member"),
        read(managed, "receipt"),
        read(managed, "receipt", role="admin"),
    )
    for response in responses:
        ok(response)


@pytest.mark.parametrize("mode", ["share", "key_share"])
async def test_management_read_passes_a_concurrent_shared_holder(managed, mode):
    """Another reader (FOR SHARE) or an uncommitted FK insert (FOR KEY SHARE)."""
    async with managed.factory() as holder:
        for model in (Workspace, SyncInstallation):
            key = managed.store.wid if model is Workspace else managed.iid
            await holder.execute(
                select(model.id)
                .where(model.id == key)
                .with_for_update(read=True, key_share=mode == "key_share")
            )
        for kind in READS:
            ok(await read(managed, kind))
        await holder.rollback()


@pytest.mark.parametrize("kind", READS)
async def test_management_read_waits_out_a_short_exclusive_holder(managed, kind):
    """A memory save holds the workspace row FOR UPDATE until it commits."""
    async with managed.factory() as holder:
        await lock_workspace_for_update(holder, managed.store.wid)

        async def let_go():
            await asyncio.sleep(SHORT_HOLD_SECONDS)
            await holder.rollback()

        released = asyncio.create_task(let_go())
        response = await read(managed, kind)
        await released
    ok(response)


@pytest.mark.parametrize("kind", READS)
@pytest.mark.parametrize("writer", ["for_update", "epoch_update"])
async def test_management_read_past_the_retry_window_is_still_the_409(managed, kind, writer):
    """Ownership, epoch and role writers take FOR UPDATE or row UPDATEs (FOR NO KEY UPDATE)."""
    async with managed.factory() as holder:
        if writer == "for_update":
            await lock_workspace_for_update(holder, managed.store.wid)
        else:
            await holder.execute(
                update(Workspace)
                .where(Workspace.id == managed.store.wid)
                .values(ownership_epoch=Workspace.ownership_epoch + 1)
            )
        busy(await read(managed, kind))
        await holder.rollback()
    ok(await read(managed, kind))


async def test_installation_update_still_refuses_a_management_read(managed):
    """The read keeps its installation row shared: an UPDATE of that row excludes it."""
    async with managed.factory() as holder:
        await holder.execute(
            update(SyncInstallation)
            .where(SyncInstallation.id == managed.iid)
            .values(generation=SyncInstallation.generation)
        )
        busy(await read(managed, "access"))
        await holder.rollback()
    ok(await read(managed, "access"))


# ---- the Context and identity-link share locks of the access and receipt reads ----


def lock_authority_row(holder, managed, row):
    """What a human feed read takes on the Context row (FOR UPDATE until it commits),
    or an unlink on the admin's identity-link row."""
    model, condition = (
        (Context, Context.id == UUID(managed.target["context_id"]))
        if row == "context"
        else (IdentityLink, IdentityLink.user_id == managed.store.users["admin"])
    )
    return holder.execute(select(model.id).where(condition).with_for_update())


async def linked_private_target(managed):
    """A private Context the admin reaches through an identity link to its owner."""
    from tests.integration.test_sync_mode_access_db import private_target

    await private_target(managed, linked=True)


@pytest.mark.parametrize("kind", ["access", "receipt"])
@pytest.mark.parametrize("row", ["context", "link"])
async def test_management_read_waits_out_a_short_authority_row_holder(managed, kind, row):
    if row == "link":
        await linked_private_target(managed)
    async with managed.factory() as holder:
        await lock_authority_row(holder, managed, row)

        async def let_go():
            await asyncio.sleep(SHORT_HOLD_SECONDS)
            await holder.rollback()

        released = asyncio.create_task(let_go())
        response = await read(managed, kind, role="admin")
        await released
    ok(response)


@pytest.mark.parametrize("kind", ["access", "receipt"])
@pytest.mark.parametrize("row", ["context", "link"])
async def test_management_read_past_the_retry_window_on_an_authority_row_is_the_409(
    managed, kind, row
):
    if row == "link":
        await linked_private_target(managed)
    async with managed.factory() as holder:
        await lock_authority_row(holder, managed, row)
        response = await read(managed, kind, role="admin")
        busy(response)
        assert "permissions" not in response.text and "as_of" not in response.text
        await holder.rollback()
    ok(await read(managed, kind, role="admin"))


@pytest.mark.parametrize("kind", ["access", "receipt"])
async def test_management_read_waits_out_an_open_human_feed_read(managed, monkeypatch, kind):
    """A feed read holds the Context row FOR UPDATE until it commits."""
    from services import sync_feed_human
    from tests.integration.sync_feed_fixtures import committed, product

    await committed(managed, [product("one")])
    inside = asyncio.Event()
    original = sync_feed_human.current_schema

    async def slow(db, installation):
        # Called with the Context row held: keep the feed read open for a moment.
        if not inside.is_set():
            inside.set()
            await asyncio.sleep(SHORT_HOLD_SECONDS)
        return await original(db, installation)

    monkeypatch.setattr(sync_feed_human, "current_schema", slow)
    feed_read = asyncio.create_task(human(managed, "GET", "records"))
    await asyncio.wait_for(inside.wait(), timeout=10)
    response = await read(managed, kind)
    assert (await feed_read).status_code == 200, (await feed_read).text
    ok(response)


# ---- writers the shared read fences (one per writer class) ----


@pytest.mark.parametrize("kind", READS)
async def test_ownership_transfer_cannot_pass_an_open_management_read(managed, monkeypatch, kind):
    inside, release = pause_inside_boundary(monkeypatch)
    reader = asyncio.create_task(read(managed, kind))
    try:
        await asyncio.wait_for(inside.wait(), timeout=10)
        with pytest.raises(ConflictError, match="Workspace is being modified"):
            await transfer(managed.store, "next")
    finally:
        release.set()
    ok(await reader)
    async with managed.factory() as db:
        row = await db.get(Workspace, managed.store.wid)
        assert row.owner_user_id == managed.store.users["owner"]


async def test_role_change_waits_for_an_open_management_read(managed, monkeypatch):
    from services.workspace_service import WorkspaceService

    inside, release = pause_inside_boundary(monkeypatch)
    reader = asyncio.create_task(read(managed, "access"))

    async def demote():
        async with managed.factory() as db:
            await WorkspaceService(db).update_member_role(
                managed.store.wid,
                managed.store.users["admin"],
                "member",
                actor_user_id=managed.store.users["owner"],
            )
            await db.commit()

    try:
        await asyncio.wait_for(inside.wait(), timeout=10)
        change = asyncio.create_task(demote())
        await asyncio.sleep(0.3)
        assert not change.done()  # blocked on the reader's shared workspace row lock
    finally:
        release.set()
    ok(await reader)
    await asyncio.wait_for(change, timeout=10)


@pytest.mark.parametrize("writer", ["epoch_bump", "soft_delete", "hard_delete"])
async def test_epoch_bump_and_workspace_deletion_cannot_pass_an_open_management_read(
    managed, monkeypatch, writer
):
    inside, release = pause_inside_boundary(monkeypatch)
    reader = asyncio.create_task(read(managed, "status"))
    refused = False
    try:
        await asyncio.wait_for(inside.wait(), timeout=10)
        async with managed.factory() as other:
            await other.execute(text("SET LOCAL lock_timeout = '200ms'"))
            try:
                if writer == "epoch_bump":
                    await other.execute(
                        update(Workspace)
                        .where(Workspace.id == managed.store.wid)
                        .values(ownership_epoch=Workspace.ownership_epoch + 1)
                    )
                elif writer == "soft_delete":
                    # The first lock WorkspaceService.delete_workspace takes.
                    await lock_workspace_for_update(other, managed.store.wid)
                else:
                    await other.execute(delete(Workspace).where(Workspace.id == managed.store.wid))
            except DBAPIError as exc:
                assert is_lock_not_available(exc)
                refused = True
            except ConflictError:
                # lock_workspace_for_update reports the lock timeout as the 409.
                refused = True
            await other.rollback()
    finally:
        release.set()
    assert refused, "the writer passed an open shared management read"
    ok(await reader)


@pytest.mark.parametrize("action", ["stop", "finalize"])
async def test_sync_transition_cannot_pass_an_open_management_read(managed, monkeypatch, action):
    if action == "finalize":
        await management.stop(managed)
    inside, release = pause_inside_boundary(monkeypatch)
    reader = asyncio.create_task(read(managed, "status"))
    try:
        await asyncio.wait_for(inside.wait(), timeout=10)
        busy(
            await human(
                managed, "POST", "management/" + action, body={"operation_id": str(uuid4())}
            )
        )
    finally:
        release.set()
    ok(await reader)


# ---- write callers keep their exclusive lock ----


def workspace_locks_emitted(managed):
    """Record every row-locking statement on ``workspaces`` the engine runs."""
    seen: list[str] = []
    engine = managed.factory.kw["bind"].sync_engine

    def record(_conn, _cursor, statement, *_args):
        flat = " ".join(statement.split())
        if "FROM workspaces" in flat and " FOR " in flat:
            seen.append(flat[flat.rindex(" FOR ") + 1 :])

    event.listen(engine, "before_cursor_execute", record)
    return seen, lambda: event.remove(engine, "before_cursor_execute", record)


@pytest.mark.parametrize("action", ["stop", "finalize", "preview", "diff"])
async def test_management_write_keeps_its_exclusive_workspace_lock(managed, action):
    async with managed.factory() as holder:
        await holder.execute(
            select(Workspace.id).where(Workspace.id == managed.store.wid).with_for_update(read=True)
        )
        seen, stop_recording = workspace_locks_emitted(managed)
        try:
            if action in ("stop", "finalize"):
                response = await human(
                    managed, "POST", "management/" + action, body={"operation_id": str(uuid4())}
                )
            elif action == "preview":
                response = await human(managed, "POST", "management/resume/preview")
            else:
                response = await human(
                    managed, "GET", f"management/resume/diff?preview_id={uuid4()}"
                )
        finally:
            stop_recording()
        # One exclusive NOWAIT attempt and no retry: no shared lock, no second try.
        assert seen == ["FOR UPDATE NOWAIT"], seen
        busy(response)
        await holder.rollback()
    stopped = await human(managed, "POST", "management/stop", body={"operation_id": str(uuid4())})
    assert stopped.status_code == 200, stopped.text


# ---- the paid write guard ----


async def test_shared_workspace_lock_under_the_paid_guard_takes_one_attempt(managed):
    """A savepoint release fires before_commit, which the paid guard refuses: no retry there."""
    commits = []

    async with managed.factory() as db:
        listener = commits.append
        event.listen(db.sync_session, "before_commit", listener)
        try:
            db.info["paid_integration_write"] = True
            workspace = await sync_management.lock_management_workspace(
                db, managed.store.wid, shared=True
            )
            assert workspace.id == managed.store.wid
            assert commits == []  # no savepoint was released
            async with managed.factory() as writer:
                await writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with pytest.raises(ConflictError):  # the shared lock is held
                    await lock_workspace_for_update(writer, managed.store.wid)
                await writer.rollback()
        finally:
            db.info.pop("paid_integration_write", None)
            await db.rollback()
            event.remove(db.sync_session, "before_commit", listener)

    async with managed.factory() as holder:
        await lock_workspace_for_update(holder, managed.store.wid)
        async with managed.factory() as db:
            event.listen(db.sync_session, "before_commit", commits.append)
            db.info["paid_integration_write"] = True
            await db.execute(text("SELECT 1"))  # open the connection outside the timing
            started = time.monotonic()
            with pytest.raises(ConflictError, match="Workspace is being modified"):
                await sync_management.lock_management_workspace(db, managed.store.wid, shared=True)
            # The proof is that no savepoint was released; the bound is loose: the
            # retries sleep about 0.75 s in all.
            assert commits == []
            assert time.monotonic() - started < 0.5
            db.info.pop("paid_integration_write", None)
            await db.rollback()
        await holder.rollback()


async def test_authority_share_lock_under_the_paid_guard_takes_one_attempt(managed):
    commits = []
    context_id = UUID(managed.target["context_id"])
    async with managed.factory() as holder:
        await lock_authority_row(holder, managed, "context")
        async with managed.factory() as db:
            event.listen(db.sync_session, "before_commit", commits.append)
            db.info["paid_integration_write"] = True
            with pytest.raises(ConflictError):
                await sync_management.share_locked(db, Context, Context.id == context_id)
            assert commits == []  # one attempt, no savepoint
            db.info.pop("paid_integration_write", None)
            await db.rollback()
        await holder.rollback()


async def test_shared_workspace_lock_outside_the_paid_guard_retries(managed):
    async with managed.factory() as holder:
        await lock_workspace_for_update(holder, managed.store.wid)

        async def let_go():
            await asyncio.sleep(SHORT_HOLD_SECONDS)
            await holder.rollback()

        released = asyncio.create_task(let_go())
        async with managed.factory() as db:
            workspace = await sync_management.lock_management_workspace(
                db, managed.store.wid, shared=True
            )
            assert workspace.id == managed.store.wid
            await db.rollback()
        await released


def test_management_read_callers_are_classified():
    """Reads pass ``shared``; every other caller keeps the exclusive default."""
    import ast
    import inspect

    from services import sync_resume, sync_resume_receipt

    def shared(module, name):
        tree = ast.parse(inspect.getsource(getattr(module, name)).lstrip())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "management_boundary"
        ]
        assert len(calls) == 1, name
        return [ast.unparse(k.value) for k in calls[0].keywords if k.arg == "shared"]

    assert shared(sync_management, "read_mode_access") == ["True"]
    assert shared(sync_management, "control_mode") == ["operation_id is None"]
    assert shared(sync_resume_receipt, "read_resume_receipt") == ["True"]
    for name in ("resume_preview", "resume_diff", "confirm_resume", "reconnect"):
        assert shared(sync_resume, name) == [], name
