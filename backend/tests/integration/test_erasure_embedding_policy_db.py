"""Erasure author races and durable cleanup against real PostgreSQL/Qdrant local."""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes.admin import retry_failed_embeddings
from cli.cleanup_erased_vectors import cleanup_erased_vectors
from models.auth import User
from models.erasure import ErasureRequest
from models.memory import Memory
from models.vector_erasure import VectorErasureJob
from services.account_erasure_service import AccountErasureService
from services.content_chunk_writer import _superseded, requeue_embedding, write_content_chunks
from services.erasure_details import erasure_pseudonym
from services.memory_service import write_memory_point
from services.sleep.importance_reeval import ImportanceReevalPhase
from services.sleep.reporter import SleepBudget
from services.vector_erasure import (
    capture_vector_erasure,
    delete_subject_points,
    drain_vector_erasures,
)
from utils.content_chunks import chunk_point_id
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session):
    subject = f"erasure-policy-{uuid4()}"
    pseudonym = erasure_pseudonym(subject)
    memory = Memory(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=subject,
        summary="workspace note",
        content="long content " * 600,
        type="note",
        scope="working",
        client="test",
        embedding_status="success",
        importance=0.5,
        created_at=utcnow() - timedelta(days=45),
    )
    request = ErasureRequest(
        id=uuid4(),
        user_id=pseudonym,
        user_email_hash="a" * 64,
        initiated_by="admin",
        reason_code="user_request_via_support",
        status="complete",
        completed_at=utcnow(),
        deleted_data_summary={"postgres": {"memories_scrubbed": 1}},
    )
    db_session.add_all([memory, request])
    await db_session.commit()
    request_id, workspace_id = request.id, memory.workspace_id
    snapshot = SimpleNamespace(id=memory.id, workspace_id=workspace_id, content=memory.content)
    try:
        yield SimpleNamespace(
            memory=snapshot,
            request=SimpleNamespace(id=request_id),
            subject=subject,
            pseudonym=pseudonym,
        )
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(VectorErasureJob).where(VectorErasureJob.request_id == request_id)
        )
        await db_session.execute(delete(Memory).where(Memory.workspace_id == workspace_id))
        await db_session.execute(delete(ErasureRequest).where(ErasureRequest.id == request_id))
        await db_session.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def vectors():
    client = AsyncQdrantClient(":memory:")
    await client.create_collection(
        "kagura_memories", vectors_config={"dense": VectorParams(size=2, distance=Distance.COSINE)}
    )
    with (
        patch("db.qdrant.get_qdrant_client", return_value=client),
        patch("db.qdrant._active_store", return_value=None),
    ):
        yield client
    await client.close()


async def put(client, point_id, author, **payload):
    await client.upsert(
        "kagura_memories",
        points=[
            PointStruct(
                id=str(point_id),
                vector={"dense": [1.0, 0.0]},
                payload={"user_id": author, **payload},
            )
        ],
        wait=True,
    )


async def erase(db, world):
    await capture_vector_erasure(db, world.request.id, world.subject)
    service = AccountErasureService(db)
    await service._dequeue_surviving_embeddings(world.subject)
    await service._scrub_surviving_memories(world.subject)
    await db.commit()


async def test_suppression_survives_error_overwrite_and_admin_requires_opt_in(db_session, world):
    await erase(db_session, world)
    await db_session.execute(
        update(Memory).where(Memory.id == world.memory.id).values(embedding_error="CFG-new error")
    )
    await db_session.commit()
    assert not await requeue_embedding(db_session, world.memory.id, reason="automatic")
    result = await retry_failed_embeddings(
        user={"sub": "admin"},
        db=db_session,
        context_id=None,
        workspace_id=str(world.memory.workspace_id),
        include_erased=False,
    )
    assert result["reset_count"] == 0
    result = await retry_failed_embeddings(
        user={"sub": "admin"},
        db=db_session,
        context_id=None,
        workspace_id=str(world.memory.workspace_id),
        include_erased=True,
    )
    assert result["reset_count"] == 1
    state = (
        await db_session.execute(
            select(
                Memory.embedding_suppressed_at, Memory.embedding_erasure_restored_at, Memory.user_id
            ).where(Memory.id == world.memory.id)
        )
    ).one()
    assert state.embedding_suppressed_at is None
    assert state.embedding_erasure_restored_at is not None
    assert state.user_id == world.pseudonym


async def test_sleep_llm_barrier_cannot_update_or_reindex_erased_author(
    db_session, async_engine, world
):
    entered, resume = asyncio.Event(), asyncio.Event()
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as sleep_db:
        phase = ImportanceReevalPhase(sleep_db, AsyncMock())

        async def evaluate(*_args):
            entered.set()
            await resume.wait()
            return {world.memory.id: 0.9}

        phase._evaluate_batch = evaluate
        config = SimpleNamespace(sleep_importance_reeval_enabled=True, importance_ema_alpha=0.3)
        with patch(
            "services.sleep.importance_reeval.update_memory_payload_in_qdrant", AsyncMock()
        ) as patch_point:
            task = asyncio.create_task(
                phase.execute(config, world.subject, None, None, SleepBudget())
            )
            await asyncio.wait_for(entered.wait(), 5)
            await erase(db_session, world)
            resume.set()
            result = await asyncio.wait_for(task, 5)
            assert result.changed_memory_ids == set()
            assert result.memories_processed == 0
            patch_point.assert_not_awaited()
        from services.sleep.reindex import ReindexPhase

        with (
            patch("services.sleep.reindex.EmbeddingService"),
            patch("services.memory_service.write_memory_point", AsyncMock()) as write,
        ):
            await ReindexPhase(sleep_db).execute({world.memory.id}, world.subject)
            write.assert_not_awaited()
        await sleep_db.rollback()
    assert await _superseded(
        db_session,
        world.memory.id,
        world.memory.content,
        None,
        check_claim=False,
        expected_author=world.subject,
    )


async def test_chunk_embedding_barrier_stops_raw_author_write(
    db_session, async_engine, world, vectors
):
    entered, resume = asyncio.Event(), asyncio.Event()

    async def embed(texts, *_args, **_kwargs):
        entered.set()
        await resume.wait()
        return [[1.0, 0.0] for _ in texts]

    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as writer_db:
        memory = await writer_db.get(Memory, world.memory.id)
        with patch(
            "services.content_chunk_writer._chunk_flags", AsyncMock(return_value=(True, True))
        ):
            task = asyncio.create_task(
                write_content_chunks(
                    writer_db,
                    memory,
                    base_payload={"user_id": world.subject},
                    embed_svc=SimpleNamespace(embed_batch=embed),
                    collection_name="kagura_memories",
                    chunks_enabled=True,
                    chunks_stored=True,
                    fail_open=False,
                )
            )
            await asyncio.wait_for(entered.wait(), 5)
            await erase(db_session, world)
            resume.set()
            assert await asyncio.wait_for(task, 5) == 0
        await writer_db.rollback()
    assert (await vectors.count("kagura_memories")).count == 0


async def test_summary_embedding_barrier_stops_raw_author_write(
    db_session, async_engine, world, vectors
):
    entered, resume = asyncio.Event(), asyncio.Event()

    async def embed(*_args, **_kwargs):
        entered.set()
        await resume.wait()
        return [1.0, 0.0]

    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as writer_db:
        memory = await writer_db.get(Memory, world.memory.id)
        task = asyncio.create_task(
            write_memory_point(
                writer_db,
                memory,
                collection_name="kagura_memories",
                embedding_service=SimpleNamespace(embed=embed),
                user_id=world.subject,
                workspace_id=str(memory.workspace_id),
                context_id=None,
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        await erase(db_session, world)
        resume.set()
        assert await asyncio.wait_for(task, 5) is None
        await writer_db.rollback()
    assert (await vectors.count("kagura_memories")).count == 0


async def test_cleanup_outage_survives_completed_request_and_deleted_row(
    db_session, world, vectors
):
    await put(vectors, world.memory.id, world.subject)
    await put(
        vectors, chunk_point_id(world.memory.id, 0), world.subject, chunk_of=str(world.memory.id)
    )
    await capture_vector_erasure(db_session, world.request.id, world.subject)
    await db_session.execute(delete(Memory).where(Memory.id == world.memory.id))
    await db_session.commit()
    with patch(
        "services.vector_erasure.erase_point_author", AsyncMock(side_effect=RuntimeError("offline"))
    ):
        result = await drain_vector_erasures(db_session)
        assert result["pending"] == 1
    job = (await db_session.execute(select(VectorErasureJob))).scalar_one()
    assert job.completed_at is None
    assert world.subject not in repr(job.__dict__)
    assert (await db_session.get(ErasureRequest, world.request.id)).status == "complete"
    assert (await drain_vector_erasures(db_session))["completed"] == 1
    assert (await vectors.count("kagura_memories")).count == 0
    assert await drain_vector_erasures(db_session) == {"completed": 0, "pending": 0}


async def test_shared_resource_kept_and_raw_author_patched(db_session, world, vectors):
    point_id = uuid4()
    await db_session.execute(
        update(Memory).where(Memory.id == world.memory.id).values(summary_embedding_id=point_id)
    )
    other = Memory(
        id=uuid4(),
        workspace_id=world.memory.workspace_id,
        user_id="other-member",
        summary="shared doc",
        content="shared content",
        type="note",
        scope="working",
        client="test",
        summary_embedding_id=point_id,
        embedding_status="success",
    )
    db_session.add(other)
    await db_session.commit()
    await put(vectors, point_id, world.subject)
    untouched = uuid4()
    await put(vectors, untouched, "other-member")
    await delete_subject_points(db_session, world.subject)
    point = (await vectors.retrieve("kagura_memories", [str(point_id)], with_payload=True))[0]
    assert point.payload["user_id"] == world.pseudonym
    await erase(db_session, world)
    await drain_vector_erasures(db_session)
    assert len(await vectors.retrieve("kagura_memories", [str(point_id), str(untouched)])) == 2


async def test_old_scope_never_deletes_reregistered_account_points(db_session, world, vectors):
    await erase(db_session, world)
    new_id = uuid4()
    fresh = Memory(
        id=new_id,
        workspace_id=world.memory.workspace_id,
        user_id=world.subject,
        summary="new account",
        content="new content",
        type="note",
        scope="working",
        client="test",
        embedding_status="success",
    )
    db_session.add(fresh)
    await db_session.commit()
    await put(vectors, world.memory.id, world.subject)
    await put(vectors, new_id, world.subject)
    await drain_vector_erasures(db_session)
    assert not await vectors.retrieve("kagura_memories", [str(world.memory.id)])
    assert len(await vectors.retrieve("kagura_memories", [str(new_id)])) == 1


async def test_historical_cleanup_plan_apply_idempotent_and_restore_respected(
    db_session, world, vectors
):
    await db_session.execute(
        update(Memory)
        .where(Memory.id == world.memory.id)
        .values(user_id=world.pseudonym, embedding_status="success")
    )
    await db_session.commit()
    await put(vectors, world.memory.id, world.pseudonym)
    assert (await cleanup_erased_vectors(db_session)).survivors == 1
    assert (
        await db_session.execute(
            select(Memory.embedding_status).where(Memory.id == world.memory.id)
        )
    ).scalar_one() == "success"
    await cleanup_erased_vectors(db_session, dry_run=False)
    assert not await vectors.retrieve("kagura_memories", [str(world.memory.id)])
    assert (await cleanup_erased_vectors(db_session)).changes == 0
    await retry_failed_embeddings(
        user={"sub": "admin"},
        db=db_session,
        context_id=None,
        workspace_id=str(world.memory.workspace_id),
        include_erased=True,
    )
    await put(vectors, world.memory.id, world.pseudonym)
    assert (await cleanup_erased_vectors(db_session)).changes == 0
    assert len(await vectors.retrieve("kagura_memories", [str(world.memory.id)])) == 1


async def test_write_after_completed_cleanup_reopens_scope_and_removes_raw_summary(
    db_session, async_engine, world, vectors
):
    entered, resume = asyncio.Event(), asyncio.Event()
    from db.qdrant import add_memory_to_qdrant as actual_write

    async def delayed_write(**kwargs):
        entered.set()
        await resume.wait()
        # Keep real Qdrant operations: vector schema needs a sparse BM25 slot
        # only when the summary tokenizer produced terms.
        kwargs["context_id"] = str(uuid4())
        kwargs["sparse_indices"] = None
        kwargs["sparse_values"] = None
        await actual_write(**kwargs)

    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as writer_db:
        memory = await writer_db.get(Memory, world.memory.id)
        with patch("services.memory_service.add_memory_to_qdrant", delayed_write):
            task = asyncio.create_task(
                write_memory_point(
                    writer_db,
                    memory,
                    collection_name="kagura_memories",
                    embedding_service=SimpleNamespace(embed=AsyncMock(return_value=[1.0, 0.0])),
                    user_id=world.subject,
                    workspace_id=str(memory.workspace_id),
                    context_id=None,
                )
            )
            await asyncio.wait_for(entered.wait(), 5)
            await erase(db_session, world)
            assert (await drain_vector_erasures(db_session))["completed"] == 1
            resume.set()
            assert await asyncio.wait_for(task, 5) is None
        await writer_db.rollback()
    assert not await vectors.retrieve("kagura_memories", [str(world.memory.id)])
    state = (
        await db_session.execute(
            select(Memory.embedding_status, Memory.embedding_suppressed_at).where(
                Memory.id == world.memory.id
            )
        )
    ).one()
    assert state.embedding_status == "failed"
    assert state.embedding_suppressed_at is not None


async def test_completed_scope_reverification_closes_crash_after_raw_upsert(
    db_session, world, vectors
):
    await erase(db_session, world)
    assert (await drain_vector_erasures(db_session))["completed"] == 1
    # Fault injection: the stale process wrote, then died before it could
    # reopen its scope or reconcile. Its only durable evidence is completed.
    await put(vectors, world.memory.id, world.subject)
    assert (await drain_vector_erasures(db_session))["completed"] == 0
    assert (await drain_vector_erasures(db_session, verify_completed=True))["completed"] == 1
    assert not await vectors.retrieve("kagura_memories", [str(world.memory.id)])


async def test_new_shared_holder_commit_serializes_cleanup_snapshot(
    db_session, async_engine, world, vectors
):
    from db.point_writer_lock import hold_point_writer_lock

    point_id = uuid4()
    await db_session.execute(
        update(Memory).where(Memory.id == world.memory.id).values(summary_embedding_id=point_id)
    )
    await db_session.commit()
    await erase(db_session, world)
    await put(vectors, point_id, world.subject)
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as ingest:
        await hold_point_writer_lock(ingest)
        ingest.add(
            Memory(
                id=uuid4(),
                workspace_id=world.memory.workspace_id,
                user_id="new-holder",
                summary="shared",
                content="shared content",
                client="test",
                type="note",
                scope="working",
                embedding_status="success",
                summary_embedding_id=point_id,
            )
        )
        await ingest.flush()
        task = asyncio.create_task(drain_vector_erasures(db_session))
        await asyncio.sleep(0.05)
        assert not task.done()
        await ingest.commit()
        assert (await asyncio.wait_for(task, 5))["completed"] == 1
    point = (await vectors.retrieve("kagura_memories", [str(point_id)]))[0]
    assert point.payload["user_id"] == world.pseudonym


async def test_historical_cli_preserves_new_account_summary_and_multiple_chunks(
    db_session, world, vectors
):
    await erase(db_session, world)
    new_id = uuid4()
    user = User(
        user_id=world.subject,
        email=f"new-{uuid4()}@example.com",
        role="user",
        auth_method="oauth",
        auth_provider="google",
    )
    db_session.add(user)
    db_session.add(
        Memory(
            id=new_id,
            workspace_id=world.memory.workspace_id,
            user_id=world.subject,
            summary="new account",
            content="new account content",
            client="test",
            type="note",
            scope="working",
            embedding_status="success",
        )
    )
    await db_session.commit()
    try:
        await put(vectors, world.memory.id, world.subject)
        await put(vectors, new_id, world.subject)
        chunks = [chunk_point_id(new_id, index) for index in range(3)]
        for chunk in chunks:
            await put(vectors, chunk, world.subject, chunk_of=str(new_id))
        plan = await cleanup_erased_vectors(db_session)
        assert plan.scopes == 0
        assert plan.pending == 1
        await cleanup_erased_vectors(db_session, dry_run=False)
        assert not await vectors.retrieve("kagura_memories", [str(world.memory.id)])
        assert len(await vectors.retrieve("kagura_memories", [str(new_id), *map(str, chunks)])) == 4
        assert (await cleanup_erased_vectors(db_session)).changes == 0
        assert not list(
            (
                await db_session.execute(
                    select(VectorErasureJob.id).where(VectorErasureJob.memory_id == new_id)
                )
            ).scalars()
        )
        # Defense in depth: even a previously captured bad scope cannot
        # delete the live new account's chunks.
        db_session.add(
            VectorErasureJob(
                request_id=world.request.id,
                memory_id=new_id,
                point_id=new_id,
                subject_pseudonym=world.pseudonym,
                captured_before_erasure=False,
            )
        )
        await db_session.commit()
        await drain_vector_erasures(db_session)
        assert len(await vectors.retrieve("kagura_memories", [str(new_id), *map(str, chunks)])) == 4
    finally:
        await db_session.execute(delete(User).where(User.user_id == world.subject))
        await db_session.commit()


@pytest.mark.parametrize("legacy_raw", ["legacy-oauth-sub", "f" * 64])
async def test_historical_cli_repairs_legacy_interrupted_note(
    db_session, world, vectors, legacy_raw
):
    pseudonym = erasure_pseudonym(legacy_raw)
    await db_session.execute(
        update(ErasureRequest)
        .where(ErasureRequest.id == world.request.id)
        .values(user_id=legacy_raw, deleted_data_summary={"note": "user_row_already_absent"})
    )
    await db_session.execute(
        update(Memory).where(Memory.id == world.memory.id).values(user_id=pseudonym)
    )
    await db_session.commit()
    await put(vectors, world.memory.id, pseudonym)
    assert (await cleanup_erased_vectors(db_session)).survivors == 1
    await cleanup_erased_vectors(db_session, dry_run=False)
    assert not await vectors.retrieve("kagura_memories", [str(world.memory.id)])
    assert (await cleanup_erased_vectors(db_session)).changes == 0


async def test_historical_cli_does_not_treat_live_hex_subject_as_pseudonym(
    db_session, world, vectors
):
    subject = "e" * 64
    db_session.add(
        User(
            user_id=subject,
            email=f"hex-{uuid4()}@example.com",
            role="user",
            auth_method="oauth",
            auth_provider="google",
        )
    )
    await db_session.execute(
        update(ErasureRequest).where(ErasureRequest.id == world.request.id).values(user_id=subject)
    )
    await db_session.execute(
        update(Memory).where(Memory.id == world.memory.id).values(user_id=subject)
    )
    await db_session.commit()
    try:
        await put(vectors, world.memory.id, subject)
        assert (await cleanup_erased_vectors(db_session)).changes == 0
        await cleanup_erased_vectors(db_session, dry_run=False)
        assert len(await vectors.retrieve("kagura_memories", [str(world.memory.id)])) == 1
    finally:
        await db_session.execute(delete(User).where(User.user_id == subject))
        await db_session.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def scoped_world(db_session, world):
    from models.auth import Context, Workspace, WorkspaceMember, WorkspaceRole
    from tests.integration._admin_helpers import make_context, make_user, make_workspace

    owner = make_user()
    workspace = make_workspace(owner_user_id=owner.user_id)
    db_session.add_all([owner, workspace])
    await db_session.flush()
    source = make_context(workspace_id=workspace.id, created_by=owner.user_id)
    target = make_context(workspace_id=workspace.id, created_by=owner.user_id)
    db_session.add_all([source, target])
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=owner.user_id, role=WorkspaceRole.OWNER)
    )
    db_session.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=world.subject,
            role=WorkspaceRole.MEMBER,
            allowed_context_ids=[source.id, target.id],
        )
    )
    await db_session.execute(
        update(Memory)
        .where(Memory.id == world.memory.id)
        .values(workspace_id=workspace.id, context_id=source.id)
    )
    await db_session.commit()
    result = SimpleNamespace(
        owner=owner.user_id, workspace_id=workspace.id, source_id=source.id, target_id=target.id
    )
    try:
        yield result
    finally:
        await db_session.rollback()
        await db_session.execute(delete(Memory).where(Memory.workspace_id == result.workspace_id))
        await db_session.execute(delete(Context).where(Context.workspace_id == result.workspace_id))
        await db_session.execute(delete(Workspace).where(Workspace.id == result.workspace_id))
        await db_session.execute(
            delete(User).where(User.user_id.in_([result.owner, world.subject]))
        )
        await db_session.commit()


async def test_context_merge_refreshes_stale_source_after_erasure(
    db_session, async_engine, world, scoped_world
):
    from services.context_service import ContextService

    entered, resume = asyncio.Event(), asyncio.Event()
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as merge_db:
        # Deliberately populate an old raw-author ORM object first.
        stale = await merge_db.get(Memory, world.memory.id)
        assert stale.user_id == world.subject
        actual = merge_db.execute
        paused = False

        async def execute(statement, *args, **kwargs):
            nonlocal paused
            entities = [
                column.get("entity") for column in getattr(statement, "column_descriptions", [])
            ]
            if not paused and Memory in entities:
                paused = True
                entered.set()
                await resume.wait()
            return await actual(statement, *args, **kwargs)

        merge_db.execute = execute
        task = asyncio.create_task(
            ContextService(merge_db).merge_contexts(
                scoped_world.owner, scoped_world.source_id, scoped_world.target_id
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        # Source context belongs to another member, so erasure must rely on
        # the memory author fence rather than a subject-created context lock.
        await asyncio.wait_for(erase(db_session, world), 5)
        resume.set()
        await asyncio.wait_for(task, 5)
    clones = list(
        (
            await db_session.execute(
                select(Memory).where(Memory.context_id == scoped_world.target_id)
            )
        ).scalars()
    )
    assert len(clones) == 1
    assert clones[0].user_id == world.pseudonym
    assert clones[0].embedding_suppressed_at is not None
    assert clones[0].embedding_status == "failed"


async def test_remember_user_gate_orders_insert_before_erasure_capture(
    db_session, async_engine, world, scoped_world
):
    from models.schemas import RememberRequest
    from services.erasure_write_guard import hold_remember_author
    from services.memory_service import MemoryService
    from utils.exceptions import NotFoundException

    db_session.add(
        User(
            user_id=world.subject,
            email=f"remember-{uuid4()}@example.com",
            role="user",
            auth_method="oauth",
            auth_provider="google",
        )
    )
    await db_session.commit()
    entered, resume = asyncio.Event(), asyncio.Event()

    async def quota(*_args, **_kwargs):
        entered.set()
        await resume.wait()
        return True, None

    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as writer_db, factory() as erasure_db:
        service = MemoryService(writer_db)
        service._ensure_capacity = AsyncMock()
        service._finish_remember = AsyncMock(
            side_effect=lambda prepared: SimpleNamespace(memory_id=prepared.memory.id)
        )

        async def erase_after_lock():
            await erasure_db.execute(
                select(User.id).where(User.user_id == world.subject).with_for_update()
            )
            await erase(erasure_db, world)
            await erasure_db.execute(delete(User).where(User.user_id == world.subject))
            await erasure_db.commit()

        with patch("services.quota_service.QuotaService.check_memory_quota", quota):
            writer = asyncio.create_task(
                service.remember(
                    RememberRequest(
                        summary="new memory while erasure starts",
                        content="workspace content",
                        type="note",
                    ),
                    world.subject,
                    current_context_id=scoped_world.source_id,
                    _skip_daily_quota=True,
                    dedupe="off",
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
            except TimeoutError:
                if writer.done():
                    await writer
                raise
            erasing = asyncio.create_task(erase_after_lock())
            await asyncio.sleep(0.05)
            assert not erasing.done()
            resume.set()
            written = await asyncio.wait_for(writer, 5)
            await asyncio.wait_for(erasing, 5)
        row = (
            await db_session.execute(
                select(Memory.user_id, Memory.embedding_suppressed_at).where(
                    Memory.id == written.memory_id
                )
            )
        ).one()
        assert row.user_id == world.pseudonym
        assert row.embedding_suppressed_at is not None
        assert (
            await db_session.scalar(
                select(VectorErasureJob.id).where(VectorErasureJob.memory_id == written.memory_id)
            )
            is not None
        )
        with pytest.raises(NotFoundException):
            await hold_remember_author(writer_db, world.subject)
        await writer_db.rollback()
        # Internal non-user and explicit pseudonym writers retain their API.
        await hold_remember_author(writer_db, "internal-indexer")
        await hold_remember_author(writer_db, world.pseudonym)
        await writer_db.rollback()


async def test_historical_cutoff_keeps_new_pseudonym_indexer_rows_and_repeat_erasure(
    db_session, world, vectors
):
    await db_session.execute(
        update(Memory).where(Memory.id == world.memory.id).values(user_id=world.pseudonym)
    )
    first_cutoff = utcnow() - timedelta(days=30)
    await db_session.execute(
        update(ErasureRequest)
        .where(ErasureRequest.id == world.request.id)
        .values(deleted_data_summary={"postgres": {"memory_scrub_at": first_cutoff.isoformat()}})
    )
    newer = Memory(
        id=uuid4(),
        workspace_id=world.memory.workspace_id,
        user_id=world.pseudonym,
        summary="new resource ingest after erasure",
        content="new workspace document",
        type="note",
        scope="working",
        client="resource_indexer",
        embedding_status="success",
        created_at=utcnow() - timedelta(days=10),
    )
    db_session.add(newer)
    later_id = uuid4()
    db_session.add(
        ErasureRequest(
            id=later_id,
            user_id=world.pseudonym,
            user_email_hash="b" * 64,
            initiated_by="admin",
            reason_code="user_request_via_support",
            status="complete",
            completed_at=utcnow(),
            deleted_data_summary={"postgres": {"memory_scrub_at": utcnow().isoformat()}},
        )
    )
    await db_session.commit()
    newer_id = newer.id
    try:
        await put(vectors, world.memory.id, world.pseudonym)
        await put(vectors, newer_id, world.pseudonym)
        assert (await cleanup_erased_vectors(db_session)).survivors == 1
        await cleanup_erased_vectors(db_session, dry_run=False)
        assert len(await vectors.retrieve("kagura_memories", [str(newer_id)])) == 1
        assert not await vectors.retrieve("kagura_memories", [str(world.memory.id)])
        assert (await cleanup_erased_vectors(db_session)).changes == 0
        state = (
            await db_session.execute(
                select(Memory.embedding_status, Memory.embedding_suppressed_at).where(
                    Memory.id == newer_id
                )
            )
        ).one()
        assert state.embedding_status == "success" and state.embedding_suppressed_at is None
    finally:
        await db_session.execute(delete(ErasureRequest).where(ErasureRequest.id == later_id))
        await db_session.commit()


async def test_shared_patch_transport_failure_preserves_first_pass_retry_contract(
    db_session, world, vectors
):
    from utils.exceptions import QdrantError

    point_id = uuid4()
    await db_session.execute(
        update(Memory).where(Memory.id == world.memory.id).values(summary_embedding_id=point_id)
    )
    db_session.add(
        Memory(
            id=uuid4(),
            workspace_id=world.memory.workspace_id,
            user_id="other-member",
            summary="holder",
            content="shared",
            type="note",
            scope="working",
            client="test",
            summary_embedding_id=point_id,
            embedding_status="success",
        )
    )
    await db_session.commit()
    await put(vectors, world.memory.id, world.subject)
    await put(vectors, point_id, world.subject)
    with patch.object(vectors, "set_payload", AsyncMock(side_effect=TimeoutError("transport"))):
        with pytest.raises(QdrantError, match="TimeoutError"):
            await delete_subject_points(db_session, world.subject)
    assert not await vectors.retrieve("kagura_memories", [str(world.memory.id)])
    assert len(await vectors.retrieve("kagura_memories", [str(point_id)])) == 1
    # The same first pass is retryable; no PG erasure happened on the error.
    await delete_subject_points(db_session, world.subject)
    assert (await vectors.retrieve("kagura_memories", [str(point_id)]))[0].payload[
        "user_id"
    ] == world.pseudonym


async def test_erased_stats_preserve_private_visibility_after_error_overwrite(
    db_session, world, scoped_world
):
    from models.auth import Context
    from services.workspace_service import WorkspaceService
    from tests.integration._admin_helpers import make_context

    private = make_context(workspace_id=scoped_world.workspace_id, created_by=scoped_world.owner)
    private.is_private = True
    db_session.add(private)
    await db_session.flush()
    common = {
        "workspace_id": scoped_world.workspace_id,
        "user_id": world.pseudonym,
        "summary": "scrubbed",
        "content": "shared content",
        "type": "note",
        "client": "test",
        "embedding_status": "failed",
        "embedding_retry_count": 3,
        "embedding_suppressed_at": utcnow(),
        "embedding_error": "CFG-overwritten",
    }
    private_id = private.id
    db_session.add(Memory(id=uuid4(), context_id=private_id, **common))
    db_session.add(Memory(id=uuid4(), context_id=scoped_world.source_id, **common))
    db_session.add(
        Memory(
            id=uuid4(),
            context_id=scoped_world.source_id,
            workspace_id=scoped_world.workspace_id,
            user_id=scoped_world.owner,
            summary="provider failed",
            content="content",
            type="note",
            client="test",
            embedding_status="failed",
            embedding_retry_count=3,
        )
    )
    await db_session.commit()
    contexts = list(
        (
            await db_session.execute(
                select(Context).where(Context.workspace_id == scoped_world.workspace_id)
            )
        ).scalars()
    )
    service = WorkspaceService(db_session)
    erased = await service.get_erased_memory_stats(world.subject, contexts)
    failed = await service.get_unsearchable_memory_stats(world.subject, contexts)
    assert erased == {str(scoped_world.source_id): 1}
    assert failed == {str(scoped_world.source_id): (1, 1)}
    assert str(private_id) not in erased and str(private_id) not in failed
    owner_view = await service.get_erased_memory_stats(
        scoped_world.owner, contexts, is_workspace_owner=True
    )
    assert owner_view == {str(scoped_world.source_id): 1, str(private_id): 1}


@pytest.mark.parametrize(
    ("shape", "cross_workspace", "holder_policy"),
    [
        ("recovery_equal", False, "active"),
        ("recovery_equal", True, "active"),
        ("normal_source", False, "active"),
        ("normal_source", True, "active"),
        ("normal_holder", False, "active"),
        ("normal_holder", True, "active"),
        ("resource_distinct", False, "same_subject"),
        ("resource_distinct", False, "suppressed_other"),
        ("resource_distinct", False, "restored_other"),
        ("unshared", False, "unshared"),
    ],
)
async def test_shared_point_identity_and_active_holder_policy(
    db_session, world, vectors, shape, cross_workspace, holder_policy
):
    """Every holder uses its physical summary UUID, including recovered rows."""
    source_id = world.memory.id
    point_id, holder_id = uuid4(), uuid4()
    if shape == "recovery_equal":
        point_id, source_summary, holder_summary = source_id, source_id, source_id
    elif shape == "normal_source":
        point_id, source_summary, holder_summary = source_id, None, source_id
    elif shape == "normal_holder":
        source_summary, holder_id, holder_summary = point_id, point_id, None
    elif shape == "unshared":
        point_id, source_summary, holder_summary = source_id, None, None
    else:
        source_summary = holder_summary = point_id
    holder_author = world.subject if holder_policy == "same_subject" else "other-member"
    holder_suppressed = (
        utcnow().replace(tzinfo=UTC) if holder_policy == "suppressed_other" else None
    )
    holder_restored = utcnow().replace(tzinfo=UTC) if holder_policy == "restored_other" else None
    if holder_policy == "restored_other":
        holder_author = erasure_pseudonym(holder_author)
    holder_workspace = uuid4() if cross_workspace else world.memory.workspace_id
    untouched_point = uuid4()
    await db_session.execute(
        update(Memory).where(Memory.id == source_id).values(summary_embedding_id=source_summary)
    )
    db_session.add(
        Memory(
            id=holder_id,
            summary_embedding_id=holder_summary,
            workspace_id=holder_workspace,
            user_id=holder_author,
            summary="another member's workspace content",
            content="shared resource",
            type="note",
            client="test",
            embedding_status="success",
            embedding_suppressed_at=holder_suppressed,
            embedding_erasure_restored_at=holder_restored,
        )
    )
    await db_session.commit()
    try:
        await put(vectors, untouched_point, "untouched-member")
        await put(vectors, point_id, world.subject)
        await delete_subject_points(db_session, world.subject)
        first = await vectors.retrieve("kagura_memories", ids=[str(point_id)])
        first_author = first[0].payload["user_id"] if first else None
        await erase(db_session, world)
        # A stale run may finish after the erasure committed.
        await put(vectors, point_id, world.subject)
        result = await drain_vector_erasures(db_session, request_id=world.request.id)
        late = await vectors.retrieve("kagura_memories", ids=[str(point_id)])
        expected_retained = holder_policy in {"active", "restored_other"}
        assert (len(first), len(late)) == (int(expected_retained), int(expected_retained))
        if expected_retained:
            assert first_author == late[0].payload["user_id"] == world.pseudonym
        assert result["pending"] == 0
        untouched = await vectors.retrieve("kagura_memories", ids=[str(untouched_point)])
        assert untouched[0].payload["user_id"] == "untouched-member"
        current_holder = (
            await db_session.execute(
                select(
                    Memory.user_id,
                    Memory.embedding_suppressed_at,
                    Memory.embedding_erasure_restored_at,
                    Memory.workspace_id,
                ).where(Memory.id == holder_id)
            )
        ).one()
        assert current_holder.workspace_id == holder_workspace
        if holder_policy == "same_subject":
            assert current_holder.user_id == world.pseudonym
            assert current_holder.embedding_suppressed_at is not None
        else:
            assert current_holder.user_id == holder_author
            assert current_holder.embedding_suppressed_at == holder_suppressed
            assert current_holder.embedding_erasure_restored_at == holder_restored
    finally:
        await db_session.rollback()
        await db_session.execute(delete(Memory).where(Memory.id == holder_id))
        await db_session.commit()
