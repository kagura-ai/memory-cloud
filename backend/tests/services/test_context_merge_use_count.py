"""Regression: merge_contexts must not copy memories via dropped columns.

v0.34.0 review finding (#1046 fallout): ``ContextService.merge_contexts``
constructed each copied memory as ``Memory(..., use_count=0, ...)``, but #1046
dropped ``use_count`` from the ``Memory`` ORM model. The copy loop therefore
raised ``TypeError: 'use_count' is an invalid keyword argument for Memory`` for
any merge of a context that actually holds memories.

It shipped because every existing ``merge_contexts`` test mocks the service or
exercises only the empty-source early-return — the memory-copy loop had zero
coverage. This test drives the REAL copy loop with a REAL ``Memory`` model (DB,
Qdrant and permission checks mocked), so a future column drift in the copy
field-set fails here instead of in production.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.memory import Memory
from services.context_service import ContextService
from utils.datetime import utcnow


def _source_memory(
    user_id: str,
    source_context_id,
    *,
    embedding_status: str = "success",
    embedding_retry_count: int = 0,
    source_type: str = "manual",
    delivery_mode: str = "on_recall",
) -> Memory:
    """A real (transient) Memory the copy loop will read its fields from."""
    return Memory(
        id=uuid4(),
        user_id=user_id,
        workspace_id=uuid4(),
        context_id=source_context_id,
        summary="copied summary",
        context_summary="why it matters",
        content="full content",
        context={"context_id": str(source_context_id)},
        details=None,
        type="note",
        importance=0.6,
        confidence=0.9,
        tags=["a", "b"],
        scope="persistent",
        long_term=True,
        promoted_at=None,
        client="test",
        client_version="1",
        source="manual",
        source_type=source_type,
        source_uri="connector://slack/C123",
        delivery_mode=delivery_mode,
        created_at=utcnow(),
        embedding_status=embedding_status,
        embedding_retry_count=embedding_retry_count,
    )


async def _upsert_all(**kwargs) -> set[str]:
    """Stand-in for Qdrant: every requested point lands."""
    return set(kwargs["memory_id_mapping"].values())


async def _upsert_none(**kwargs) -> set[str]:
    """Stand-in for Qdrant losing the write — nothing lands."""
    return set()


@pytest.mark.asyncio
async def test_merge_contexts_copies_memory_without_dropped_use_count():
    user_id = "user-merge"
    source_id = uuid4()
    target_id = uuid4()
    workspace_id = uuid4()

    source_ctx = MagicMock(id=source_id, workspace_id=workspace_id, is_locked=False)
    target_ctx = MagicMock(id=target_id, workspace_id=workspace_id, is_locked=False)

    mock_db = AsyncMock()
    # First execute() = embedding-config query (none → both use settings defaults,
    # so the same-model check passes); second = source-memory query.
    cfg_result = MagicMock()
    cfg_result.scalars.return_value.all.return_value = []
    mem_result = MagicMock()
    mem_result.scalars.return_value.all.return_value = [_source_memory(user_id, source_id)]
    mock_db.execute.side_effect = [cfg_result, mem_result]
    mock_db.add = MagicMock()  # synchronous in SQLAlchemy

    service = ContextService(mock_db)

    with (
        patch.object(
            service, "get_context", new_callable=AsyncMock, side_effect=[source_ctx, target_ctx]
        ),
        patch("services.permission_service.PermissionService") as PermSvc,
        patch("db.qdrant.copy_context_points", new_callable=AsyncMock, side_effect=_upsert_all),
        patch("db.qdrant.get_collection_name", return_value="collection"),
    ):
        PermSvc.return_value.check_context_owner = AsyncMock()
        result = await service.merge_contexts(user_id, source_id, target_id)

    assert result["merged"] == 1
    # The copy loop built and added exactly one new Memory (no TypeError on the
    # dropped column), retargeted to the destination context with stats reset.
    mock_db.add.assert_called_once()
    new_mem = mock_db.add.call_args.args[0]
    assert isinstance(new_mem, Memory)
    assert new_mem.context_id == target_id
    assert new_mem.access_count == 0
    assert not hasattr(new_mem, "use_count")


# ---------------------------------------------------------------------------
# #1497: a merge must not lose the memories it declines to index.
#
# merge_contexts selected only `embedding_status == "success"` rows, copied
# those, and then — with delete_source=True — soft-deleted the source anyway.
# Anything pending or failed was neither copied nor kept: it stayed parented to
# a context the user had just removed, and the caller was told a "merged" count
# that silently omitted it.
#
# Live, not theoretical. The deployment in #1496 had a context with 0 successful
# and 16 failed memories; merging it would have copied nothing, reported 0, and
# deleted the context holding all 16. #1496 established that `failed` is
# RECOVERABLE — the sweep re-embeds those rows once a credential exists — so
# discarding them threw away data that was coming back on its own.
# ---------------------------------------------------------------------------


def _merge_harness(memories, *, upsert=None, live_count=None):
    """Drive the real copy loop over `memories`. Returns (result, added_rows)."""
    user_id = "user-merge"
    source_id = uuid4()
    target_id = uuid4()
    workspace_id = uuid4()

    source_ctx = MagicMock(
        id=source_id, workspace_id=workspace_id, is_locked=False, is_default=False
    )
    target_ctx = MagicMock(
        id=target_id, workspace_id=workspace_id, is_locked=False, is_default=False
    )

    mock_db = AsyncMock()
    cfg_result = MagicMock()
    cfg_result.scalars.return_value.all.return_value = []
    mem_result = MagicMock()
    mem_result.scalars.return_value.all.return_value = [
        m(user_id, source_id) if callable(m) else m for m in memories
    ]
    # #1497: with delete_source the guard issues a third statement — an
    # independent count of live rows still in the source.
    live_result = MagicMock()
    live_result.scalar_one.return_value = (
        live_count if live_count is not None else len(mem_result.scalars.return_value.all())
    )
    mock_db.execute.side_effect = [cfg_result, mem_result, live_result]
    mock_db.add = MagicMock()

    service = ContextService(mock_db)
    return service, mock_db, source_ctx, target_ctx, source_id, target_id, user_id


async def _run_merge(memories, *, upsert=None, delete_source=False, live_count=None):
    (service, mock_db, source_ctx, target_ctx, source_id, target_id, user_id) = _merge_harness(
        memories, live_count=live_count
    )
    delete_called = AsyncMock()
    with (
        patch.object(
            service, "get_context", new_callable=AsyncMock, side_effect=[source_ctx, target_ctx]
        ),
        patch("services.permission_service.PermissionService") as PermSvc,
        patch(
            "db.qdrant.copy_context_points",
            new_callable=AsyncMock,
            side_effect=upsert or _upsert_all,
        ),
        patch("db.qdrant.get_collection_name", return_value="collection"),
        patch.object(service, "delete_context", delete_called),
    ):
        PermSvc.return_value.check_context_owner = AsyncMock()
        result = await service.merge_contexts(
            user_id, source_id, target_id, delete_source=delete_source
        )
    rows = [c.args[0] for c in mock_db.add.call_args_list]
    return result, rows, delete_called, target_id


@pytest.mark.asyncio
async def test_a_failed_memory_is_carried_over_not_dropped():
    """The bug, at its smallest."""
    result, rows, _, target_id = await _run_merge(
        [lambda u, c: _source_memory(u, c, embedding_status="failed")]
    )
    assert len(rows) == 1, "the failed memory was not copied — it would be lost"
    assert rows[0].context_id == target_id
    assert result["merged"] == 1


@pytest.mark.asyncio
async def test_a_source_of_only_failed_memories_still_transfers():
    """The worst case, and the one production was actually in.

    This used to hit an early return that copied nothing and honoured
    delete_source anyway.
    """
    result, rows, delete_called, _ = await _run_merge(
        [(lambda u, c: _source_memory(u, c, embedding_status="failed")) for _ in range(16)],
        delete_source=True,
    )
    assert len(rows) == 16
    assert result["merged"] == 16
    delete_called.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unembedded_copy_lands_as_pending_so_the_sweep_finds_it():
    """Carrying the source status verbatim would be worse than useless.

    A failed row that exhausted its retry budget, copied as-is, would be born
    terminal — the #1496 sweep only claims rows below MAX_EMBEDDING_RETRIES.
    A new row in a new context is a new failure episode.
    """
    _, rows, _, _ = await _run_merge(
        [lambda u, c: _source_memory(u, c, embedding_status="failed", embedding_retry_count=3)]
    )
    assert rows[0].embedding_status == "pending"
    assert rows[0].embedding_retry_count == 0
    assert rows[0].embedding_error is None


@pytest.mark.asyncio
async def test_provenance_survives_the_copy():
    """source_type defaults to "manual", and "manual" is TRUSTED.

    A connector-origin row is excluded from the pinned/bootstrap lane precisely
    because it is not manual (OWASP LLM01/LLM03). Letting the column default
    apply would promote connector content into a lane built to keep it out —
    and merging into a trusted context clears the other half of that gate at the
    same moment.
    """
    _, rows, _, _ = await _run_merge([lambda u, c: _source_memory(u, c, source_type="connector")])
    assert rows[0].source_type == "connector", (
        "connector provenance was lost; the copy is now trusted content"
    )
    assert rows[0].source_uri == "connector://slack/C123"


@pytest.mark.asyncio
async def test_delivery_mode_survives_the_copy():
    """An always-delivered memory must not quietly become on-recall."""
    _, rows, _, _ = await _run_merge([lambda u, c: _source_memory(u, c, delivery_mode="always")])
    assert rows[0].delivery_mode == "always"


@pytest.mark.asyncio
async def test_a_row_whose_vector_did_not_land_is_not_marked_success():
    """Otherwise the merge manufactures a fresh #1496.

    A row claiming `success` with no vector is invisible to search AND invisible
    to the sweep, which only ever claims pending/processing/failed. Nothing
    would ever repair it.
    """
    _, rows, _, _ = await _run_merge(
        [lambda u, c: _source_memory(u, c, embedding_status="success")],
        upsert=_upsert_none,
    )
    assert rows[0].embedding_status == "pending"


@pytest.mark.asyncio
async def test_the_count_names_what_is_not_searchable_yet():
    """`merged` used to count Qdrant points, so a source full of unembedded
    memories reported 0 while the UI had just shown their real number."""
    result, _, _, _ = await _run_merge(
        [
            lambda u, c: _source_memory(u, c, embedding_status="success"),
            lambda u, c: _source_memory(u, c, embedding_status="failed"),
        ]
    )
    assert result["merged"] == 2
    assert result["pending_embedding"] == 1


@pytest.mark.asyncio
async def test_the_usage_stats_reset_as_one_group():
    """access_count >= reference_count is a documented invariant; resetting one
    while carrying the other would violate it."""
    _, rows, _, _ = await _run_merge([_source_memory])
    assert rows[0].access_count == 0
    assert rows[0].reference_count == 0


@pytest.mark.asyncio
async def test_the_selection_does_not_filter_on_embedding_status():
    """The bug itself, which every test above is blind to.

    Those tests hand the copy loop a list of memories through a mocked
    `db.execute`, so the WHERE clause never runs — restoring the original
    `embedding_status == "success"` predicate leaves all of them green while
    the data loss is fully back. Verified by mutation, which is how this gap
    was found.

    So assert on the SQL actually emitted: the source-memory query must scope by
    context and liveness, and must NOT mention embedding_status.
    """
    (service, mock_db, source_ctx, target_ctx, source_id, target_id, user_id) = _merge_harness(
        [_source_memory]
    )
    with (
        patch.object(
            service, "get_context", new_callable=AsyncMock, side_effect=[source_ctx, target_ctx]
        ),
        patch("services.permission_service.PermissionService") as PermSvc,
        patch("db.qdrant.copy_context_points", new_callable=AsyncMock, side_effect=_upsert_all),
        patch("db.qdrant.get_collection_name", return_value="collection"),
    ):
        PermSvc.return_value.check_context_owner = AsyncMock()
        await service.merge_contexts(user_id, source_id, target_id)

    # execute() call 2 is the source-memory query (1 is the embedding config).
    # Only the WHERE clause matters — embedding_status appears in the SELECT
    # column list either way, since the copy loop reads it.
    sql = str(mock_db.execute.await_args_list[1].args[0])
    where = sql.split("WHERE", 1)[1] if "WHERE" in sql else ""
    assert where, "the source-memory query has no WHERE clause at all"
    assert "embedding_status" not in where, (
        "merge_contexts is selecting source memories by embedding_status again; "
        "unembedded memories will be left behind when the source is deleted "
        "(#1497)"
    )
    assert "context_id" in where and "deleted_at" in where, (
        "the selection lost its context/liveness scoping"
    )


@pytest.mark.asyncio
async def test_the_delete_guard_counts_live_rows_independently():
    """The guard must not measure the copy against its own selection.

    Reviewed finding on #1499: the first version compared
    `len(rows_by_new_id) != len(source_memories)`. Narrow the SELECT and both
    sides shrink together, so it passes while rows are left behind — a
    tautology dressed as a safety check, guarding against precisely the
    regression it could not see.

    Here the source holds 16 live rows but only 1 was selected. The merge must
    refuse to delete.
    """
    from utils.exceptions import ValidationError

    with pytest.raises(ValidationError, match="refusing to delete"):
        await _run_merge([_source_memory], delete_source=True, live_count=16)


@pytest.mark.asyncio
async def test_a_complete_transfer_still_deletes_the_source():
    """The guard must not become a blanket refusal — delete_source has to keep
    working for the case it was designed for."""
    _, rows, delete_called, _ = await _run_merge(
        [_source_memory, _source_memory], delete_source=True, live_count=2
    )
    assert len(rows) == 2
    delete_called.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unembedded_row_counts_as_transferred():
    """A row copied as `pending` HAS moved; only its index is deferred.

    Gating on vectors instead of rows would refuse deletes for merges that lost
    nothing at all.
    """
    _, _, delete_called, _ = await _run_merge(
        [lambda u, c: _source_memory(u, c, embedding_status="failed")],
        delete_source=True,
        live_count=1,
    )
    delete_called.assert_awaited_once()
