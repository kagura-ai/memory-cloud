"""#2092: a re-embed of unchanged text runs no supersede detection.

``process_pending_embedding`` stamps ``memories.embedded_text_digest`` (the
digest of the summary + content it embedded) on its success write, and tells
``_create_knn_seed_edges`` the run is a mechanical re-embed when the row
already carried the digest of the text it just embedded (a content-chunk
backfill, an embedding-model switch, a repair re-queue). A first embed (NULL)
and an edit (a different digest) still detect.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services.supersede_verify import embedded_text_digest

_CLAIM_STAMP = datetime(2026, 1, 2, 3, 4, 5, 678901)


def _compiled(stmt: Any) -> str:
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).replace("memories.", "")


def _claimed(memory_id: UUID) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=memory_id)
    return result


def _memory(memory_id: UUID, digest: str | None) -> MagicMock:
    memory = MagicMock()
    memory.id = memory_id
    memory.user_id = "u1"
    memory.summary = "s"
    memory.content = "c"
    memory.embedded_text_digest = digest
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    return memory


def _row(memory: MagicMock) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=memory)
    return result


def _scalar(value: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _rowcount(n: int) -> MagicMock:
    result = MagicMock()
    result.rowcount = n
    # The failure write RETURNs the retry count.
    result.scalar_one_or_none = MagicMock(return_value=None if n == 0 else 1)
    return result


def _db(memory_id: UUID, results: list[Any], on_success=None) -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.captured = []
    queue = list(results)

    async def _execute(stmt, *a, **k):
        db.captured.append(stmt)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        if on_success is not None and "'success'" in _compiled(stmt):
            on_success()
        return item

    db.execute = AsyncMock(side_effect=_execute)
    db.memory_id = memory_id
    return db


async def _run(db: MagicMock, embed_svc: MagicMock) -> tuple[AsyncMock, AsyncMock, MagicMock]:
    from services import memory_service

    async def _aiter():
        yield db

    with (
        patch("db.base.get_db", return_value=_aiter()),
        patch("utils.datetime.utcnow", return_value=_CLAIM_STAMP),
        patch("services.embedding_service.EmbeddingService", MagicMock()),
        patch(
            "services.context_routing.resolve_context_routing",
            AsyncMock(return_value=("kagura_memories", embed_svc)),
        ),
        patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()),
        patch.object(memory_service, "build_memory_point", return_value=({}, [], [])),
        patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()) as knn,
        patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()) as tags,
        patch.object(memory_service, "logger") as log,
    ):
        await memory_service.process_pending_embedding(db.memory_id)
    return knn, tags, log


def _embed_svc(fail: bool = False) -> MagicMock:
    svc = MagicMock()
    if fail:
        svc.embed = AsyncMock(side_effect=RuntimeError("provider down"))
    else:
        svc.embed = AsyncMock(return_value=[0.1])
    svc.model = "m"
    return svc


def _success_run(memory: MagicMock, on_success=None) -> MagicMock:
    # claim, SELECT memory, chunk-flag SELECT (off), success UPDATE
    return _db(
        memory.id,
        [_claimed(memory.id), _row(memory), _scalar(False), _rowcount(1)],
        on_success=on_success,
    )


def _success_sql(db: MagicMock) -> str:
    sqls = [_compiled(s) for s in db.captured]
    matches = [s for s in sqls if "'success'" in s.split("WHERE", 1)[0]]
    assert len(matches) == 1
    return matches[0]


def test_embedded_text_digest_is_md5_over_summary_sep_content():
    import hashlib

    assert embedded_text_digest("s", "c") == hashlib.md5(b"s\x1fc").hexdigest()
    assert embedded_text_digest("ab", "c") != embedded_text_digest("a", "bc")
    ja = embedded_text_digest("記憶の要約", "本文です")
    assert ja == embedded_text_digest("記憶の要約", "本文です")
    assert ja == hashlib.md5("記憶の要約\x1f本文です".encode()).hexdigest()
    assert ja != embedded_text_digest("kioku no youyaku", "honbun desu")
    assert len(ja) == 32


@pytest.mark.asyncio
async def test_first_embed_runs_detection_and_stamps_the_digest():
    memory = _memory(uuid4(), None)
    db = _success_run(memory)

    knn, tags, log = await _run(db, _embed_svc())

    knn.assert_awaited_once()
    assert knn.await_args.kwargs["mechanical_reembed"] is False
    tags.assert_awaited_once()
    sql = _success_sql(db)
    assert f"embedded_text_digest='{embedded_text_digest('s', 'c')}'" in sql
    completed = [c for c in log.info.call_args_list if c.args[0] == "embedding_completed"]
    assert completed[0].kwargs["mechanical_reembed"] is False


@pytest.mark.asyncio
async def test_unchanged_text_reembed_skips_detection():
    memory = _memory(uuid4(), embedded_text_digest("s", "c"))
    db = _success_run(memory)

    knn, tags, log = await _run(db, _embed_svc())

    knn.assert_awaited_once()
    assert knn.await_args.kwargs["mechanical_reembed"] is True
    tags.assert_awaited_once()
    completed = [c for c in log.info.call_args_list if c.args[0] == "embedding_completed"]
    assert completed[0].kwargs["mechanical_reembed"] is True


@pytest.mark.asyncio
async def test_edited_text_reembed_runs_detection():
    memory = _memory(uuid4(), "0" * 32)
    db = _success_run(memory)

    knn, _, _ = await _run(db, _embed_svc())

    assert knn.await_args.kwargs["mechanical_reembed"] is False
    assert f"embedded_text_digest='{embedded_text_digest('s', 'c')}'" in _success_sql(db)


@pytest.mark.asyncio
async def test_mechanical_is_decided_before_the_success_write():
    """The success UPDATE is ORM-enabled; its session sync may write the new
    digest onto the loaded object. The decision must not read it after."""
    memory = _memory(uuid4(), None)

    def _sync() -> None:
        memory.embedded_text_digest = embedded_text_digest("s", "c")

    db = _success_run(memory, on_success=_sync)

    knn, _, _ = await _run(db, _embed_svc())

    assert memory.embedded_text_digest == embedded_text_digest("s", "c")
    assert knn.await_args.kwargs["mechanical_reembed"] is False


@pytest.mark.asyncio
async def test_failure_write_does_not_stamp_the_digest():
    memory = _memory(uuid4(), None)
    # claim, SELECT memory, failure UPDATE
    db = _db(memory.id, [_claimed(memory.id), _row(memory), _rowcount(1)])

    knn, _, _ = await _run(db, _embed_svc(fail=True))

    knn.assert_not_awaited()
    failure_sql = _compiled(db.captured[2])
    assert "'failed'" in failure_sql
    assert "embedded_text_digest" not in failure_sql
