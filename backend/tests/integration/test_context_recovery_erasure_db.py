"""Recovery preserves the UUID/cohort erasure boundary against real PostgreSQL."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from api.routes.admin import ContextRecoveryRequest, recover_context
from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR, MAX_EMBEDDING_RETRIES
from models.auth import Context, User, Workspace
from models.erasure import ErasureRequest
from models.memory import Memory
from models.vector_erasure import VectorErasureJob
from services.context_restore import restore_deleted_context
from services.erasure_details import erasure_pseudonym
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def recovery_world(db_session):
    subject = f"recovery-erased-{uuid4()}"
    pseudonym = erasure_pseudonym(subject)
    workspace = Workspace(id=uuid4(), name="Recovery privacy", owner_user_id="surviving-owner")
    db_session.add(workspace)
    await db_session.flush()
    context = Context(
        id=uuid4(), workspace_id=workspace.id, name="recovery-privacy", created_by="surviving-owner"
    )
    request = ErasureRequest(
        id=uuid4(),
        user_id=pseudonym,
        user_email_hash="a" * 64,
        initiated_by="admin",
        reason_code="user_request_via_support",
        status="complete",
        completed_at=utcnow(),
        deleted_data_summary={
            "postgres": {"memories_scrubbed": 1, "memory_scrub_at": utcnow().isoformat()}
        },
    )
    db_session.add_all([context, request])
    await db_session.commit()
    world = SimpleNamespace(
        subject=subject,
        pseudonym=pseudonym,
        workspace_id=workspace.id,
        context_id=context.id,
        request_id=request.id,
    )
    try:
        yield world
    finally:
        await db_session.rollback()
        await db_session.execute(delete(Memory).where(Memory.workspace_id == world.workspace_id))
        await db_session.execute(delete(Context).where(Context.workspace_id == world.workspace_id))
        await db_session.execute(delete(Workspace).where(Workspace.id == world.workspace_id))
        await db_session.execute(
            delete(ErasureRequest).where(ErasureRequest.id == world.request_id)
        )
        await db_session.execute(delete(User).where(User.user_id == world.subject))
        await db_session.commit()


def point(world, *, author=None, created_at=None):
    payload = {
        "user_id": author or world.subject,
        "workspace_id": str(world.workspace_id),
        "summary": "surviving workspace note",
    }
    if created_at is not None:
        payload["created_at"] = created_at.isoformat()
    return SimpleNamespace(id=str(uuid4()), payload=payload)


async def recover(db, world, points, *, dry_run=False):
    client = AsyncMock()
    client.scroll.return_value = (points, None)
    with patch("db.qdrant.get_qdrant_client", return_value=client):
        result = await recover_context(
            request_body=ContextRecoveryRequest(context_id=str(world.context_id), dry_run=dry_run),
            user={"user_id": "admin"},
            db=db,
        )
    client.delete.assert_not_awaited()
    return result


@pytest.mark.parametrize("reregistered", [False, True])
async def test_captured_old_uuid_stays_erased_even_when_subject_reregisters(
    db_session, recovery_world, reregistered
):
    w = recovery_world
    p = point(w, created_at=utcnow() - timedelta(days=10))
    from uuid import UUID

    memory_id = UUID(p.id)
    if reregistered:
        db_session.add(
            User(user_id=w.subject, email=f"{uuid4()}@example.test", created_at=utcnow())
        )
    db_session.add(
        VectorErasureJob(
            request_id=w.request_id,
            memory_id=memory_id,
            point_id=memory_id,
            subject_pseudonym=w.pseudonym,
            captured_before_erasure=True,
            completed_at=utcnow(),
        )
    )
    await db_session.commit()
    result = await recover(db_session, w, [p])
    row = await db_session.get(Memory, memory_id)
    assert result.memories_recovered == 1 and result.errors == []
    assert row.user_id == w.pseudonym
    assert row.embedding_suppressed_at is not None
    assert row.embedding_erasure_restored_at is None
    assert (row.embedding_status, row.embedding_error, row.embedding_retry_count) == (
        "failed",
        ERASED_AUTHOR_EMBEDDING_ERROR,
        MAX_EMBEDDING_RETRIES,
    )
    assert row.details is None
    again = await recover(db_session, w, [p])
    assert (again.memories_recovered, again.memories_already_existed) == (0, 1)


async def test_new_uuid_from_new_account_cohort_is_preserved(db_session, recovery_world):
    w = recovery_world
    registered = utcnow() - timedelta(days=1)
    db_session.add(User(user_id=w.subject, email=f"{uuid4()}@example.test", created_at=registered))
    await db_session.commit()
    p = point(w, created_at=registered + timedelta(hours=1))
    result = await recover(db_session, w, [p])
    from uuid import UUID

    row = await db_session.get(Memory, UUID(p.id))
    assert result.memories_recovered == 1 and result.errors == []
    assert row.user_id == w.subject
    assert row.embedding_status == "success"
    assert row.embedding_suppressed_at is None


async def test_unknown_old_timestamp_is_not_attributed_to_reregistered_account(
    db_session, recovery_world
):
    w = recovery_world
    db_session.add(User(user_id=w.subject, email=f"{uuid4()}@example.test"))
    await db_session.commit()
    p = point(w)
    plan = await recover(db_session, w, [p], dry_run=True)
    result = await recover(db_session, w, [p])
    assert (plan.memories_recovered, plan.errors) == (result.memories_recovered, result.errors)
    assert result.memories_recovered == 0
    assert len(result.errors) == 1 and w.subject not in result.errors[0]
    assert not (
        await db_session.execute(select(Memory.id).where(Memory.context_id == w.context_id))
    ).all()


async def test_legacy_old_snapshot_stays_erased_without_a_uuid_scope(db_session, recovery_world):
    w = recovery_world
    p = point(w, created_at=utcnow() - timedelta(days=10))
    result = await recover(db_session, w, [p])
    from uuid import UUID

    row = await db_session.get(Memory, UUID(p.id))
    assert result.memories_recovered == 1
    assert row.user_id == w.pseudonym and row.embedding_suppressed_at is not None


async def test_other_member_is_unchanged(db_session, recovery_world):
    w = recovery_world
    p = point(w, author="other-surviving-member")
    result = await recover(db_session, w, [p])
    from uuid import UUID

    row = await db_session.get(Memory, UUID(p.id))
    assert result.memories_recovered == 1
    assert row.user_id == "other-surviving-member" and row.embedding_status == "success"
    assert row.embedding_suppressed_at is None


async def test_soft_restore_keeps_suppression_when_error_marker_was_overwritten(
    db_session, recovery_world
):
    w = recovery_world
    deleted = utcnow() - timedelta(hours=1)
    ctx = await db_session.get(Context, w.context_id)
    ctx.deleted_at, ctx.deleted_by = deleted, "surviving-owner"
    memory = Memory(
        id=uuid4(),
        user_id=w.pseudonym,
        workspace_id=w.workspace_id,
        context_id=w.context_id,
        summary="retained note",
        content="retained workspace content",
        type="note",
        client="test",
        deleted_at=deleted,
        deleted_by="surviving-owner",
        embedding_status="failed",
        embedding_suppressed_at=deleted,
        embedding_error="later error replaced marker",
    )
    db_session.add(memory)
    await db_session.commit()
    memory_id = memory.id
    result = await restore_deleted_context(
        db_session, w.context_id, dry_run=False, actor_id="admin"
    )
    db_session.expire_all()
    restored = await db_session.get(Memory, memory_id)
    assert result.memories_restored == 1
    assert restored.deleted_at is None
    assert restored.embedding_suppressed_at is not None
    assert restored.embedding_status == "failed"
    assert restored.embedding_error == ERASED_AUTHOR_EMBEDDING_ERROR


@pytest.mark.parametrize("age", ["old", "new", "unknown"])
async def test_pseudonym_snapshot_respects_first_erasure_boundary(db_session, recovery_world, age):
    w = recovery_world
    created = utcnow() + (timedelta(days=-1) if age == "old" else timedelta(days=1))
    p = point(w, author=w.pseudonym, created_at=None if age == "unknown" else created)
    result = await recover(db_session, w, [p])
    from uuid import UUID

    row = await db_session.get(Memory, UUID(p.id))
    if age == "unknown":
        assert result.memories_recovered == 0 and row is None
        assert len(result.errors) == 1
    else:
        assert result.memories_recovered == 1 and result.errors == []
        assert row.user_id == w.pseudonym
        assert (row.embedding_suppressed_at is not None) == (age == "old")
        assert row.embedding_status == ("failed" if age == "old" else "success")


@pytest.mark.parametrize("author_kind", ["raw", "pseudonym"])
@pytest.mark.parametrize("raw_subject", ["ordinary", "hex"])
async def test_postgres_commit_is_authoritative_before_finalize(
    db_session, recovery_world, author_kind, raw_subject
):
    w = recovery_world
    if raw_subject == "hex":
        w.subject = "b" * 64
        w.pseudonym = erasure_pseudonym(w.subject)
    request = await db_session.get(ErasureRequest, w.request_id)
    request.user_id, request.status = w.subject, "in_progress"
    await db_session.commit()
    p = point(
        w,
        author=w.subject if author_kind == "raw" else w.pseudonym,
        created_at=utcnow() - timedelta(days=1),
    )
    result = await recover(db_session, w, [p])
    from uuid import UUID

    row = await db_session.get(Memory, UUID(p.id))
    assert result.memories_recovered == 1 and result.errors == []
    assert row.user_id == w.pseudonym
    assert row.embedding_suppressed_at is not None and row.embedding_status == "failed"


async def test_recovery_holds_live_user_until_its_rows_commit(
    db_session, async_engine, recovery_world
):
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from services.context_restore import recovered_erasure_overrides

    w = recovery_world
    registered = utcnow() - timedelta(days=1)
    db_session.add(User(user_id=w.subject, email=f"{uuid4()}@example.test", created_at=registered))
    await db_session.commit()
    p = point(w, created_at=registered + timedelta(hours=1))
    reached, release = asyncio.Event(), asyncio.Event()

    async def pause_after_user_lock(*args, **kwargs):
        result = await recovered_erasure_overrides(*args, **kwargs)
        reached.set()
        await release.wait()
        return result

    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as recovery_db, maker() as deleting_db, maker() as observer:
        pid = await deleting_db.scalar(text("SELECT pg_backend_pid()"))
        with patch(
            "services.context_restore.recovered_erasure_overrides",
            side_effect=pause_after_user_lock,
        ):
            recovery_task = asyncio.create_task(recover(recovery_db, w, [p]))
            deletion_task = None
            try:
                await asyncio.wait_for(reached.wait(), timeout=10)
                deletion_task = asyncio.create_task(
                    deleting_db.execute(delete(User).where(User.user_id == w.subject))
                )
                async with asyncio.timeout(10):
                    while not await observer.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": pid}
                    ):
                        await asyncio.sleep(0.01)
                assert not deletion_task.done()
                release.set()
                result = await asyncio.wait_for(recovery_task, timeout=10)
                assert result.memories_recovered == 1
                await asyncio.wait_for(deletion_task, timeout=10)
            finally:
                release.set()
                for task in (recovery_task, deletion_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(t for t in (recovery_task, deletion_task) if t is not None),
                    return_exceptions=True,
                )
                await deleting_db.rollback()
