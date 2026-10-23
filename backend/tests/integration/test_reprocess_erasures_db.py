"""#2243: ``cli.reprocess_erasures`` over erasures that ran before #2235.

Against a real Postgres, through the command's ``_main`` (``get_db`` points
at the test engine; the vector store is stubbed). The seeded state is what
an erasure on v0.113.0 … v0.120.x left: the erased member's own revision
rows carry the stored pseudonym and no session, the authored memory is
scrubbed and its log stripped, but

- ``live``: the member's value is still the live ``details`` of a memory
  another member authored (no #2244 restore yet);
- ``leftover``: the prior of the next details edit after the member's still
  holds their value (the #2235 correlation bug).

Next to them, rows the command must leave alone:

- ``own``: the member's own (scrubbed) memory, with details edits other
  members made AFTER the erasure completed (review S1);
- ``rolled``: a session the member rolled back, then another member's edit
  over the value the rollback put back (review B3);
- a second tenant with the same shapes under a pseudonym nobody erased.

Every seeded row goes with its workspace; the requests and audit rows are
deleted by id. Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cli import reprocess_erasures as cli
from models.auth import AuditLog, Context, Workspace
from models.erasure import ErasureRequest
from models.memory import Memory, MemoryRevision
from utils.exceptions import QdrantError

pytestmark = pytest.mark.asyncio(loop_scope="session")

_QDRANT = "services.erasure_details.update_memory_payload_in_qdrant"

CREATION = {"at": "creation"}
AUTHORS = {"by": "author", "location": {"lat": 10.5, "lon": 20.25}}
SUBJECTS = {"by": "subject", "location": {"lat": 35.0, "lon": 139.0}}
THIRDS = {"by": "third"}
STRIPPED = {"erased": ["details"]}
SCRUBBED = {"erased": ["details"], "scrubbed": ["details"]}

COMPLETED_AT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
BEFORE = datetime(2026, 7, 1, 9, 0, 0)  # naive UTC, as memory_revisions.at
AFTER = datetime(2026, 8, 2, 9, 0, 0)


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _pseudonym() -> str:
    return uuid.uuid4().hex * 2  # 64 hex characters, as the salted sha256


def _memory(ws: uuid.UUID, ctx: uuid.UUID, owner: str, details: Any, version: int) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=owner,
        workspace_id=ws,
        context_id=ctx,
        summary="a memory",
        content="body",
        type="note",
        client="test",
        details=details,
        version=version,
        embedding_status="success",
    )


def _edit(
    memory: Memory,
    version: int,
    actor: str,
    prior: dict[str, Any],
    at: datetime = BEFORE,
    *,
    writer: str = "api",
    undone_by: int | None = None,
) -> MemoryRevision:
    return MemoryRevision(
        memory_id=memory.id,
        context_id=memory.context_id,
        version=version,
        kind="updated",
        writer=writer,
        actor_user_id=actor,
        prior=prior,
        at=at,
        undone_by=undone_by,
    )


def _request(
    user_id: str,
    *,
    status: str = "complete",
    minutes: int = 0,
    summary: dict[str, Any] | None = None,
) -> ErasureRequest:
    """A request row; a ``complete`` one as ``_finalize`` leaves it unless
    ``summary`` says otherwise."""
    if summary is None and status == "complete":
        summary = {"postgres": {"memories_scrubbed": 0}}
    return ErasureRequest(
        id=uuid.uuid4(),
        user_id=user_id,
        user_email_hash="0" * 64,
        initiated_by="admin-x",
        is_self_service=False,
        reason_code="other",
        status=status,
        completed_at=COMPLETED_AT + timedelta(minutes=minutes) if status == "complete" else None,
        deleted_data_summary=summary,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """The pre-#2235 state described in the module docstring, committed."""
    tag = uuid.uuid4().hex[:8]
    author, third, fourth = f"author-{tag}", f"third-{tag}", f"fourth-{tag}"
    p = _pseudonym()
    ws, ctx = uuid.uuid4(), uuid.uuid4()
    twin_ws, twin_ctx = uuid.uuid4(), uuid.uuid4()
    twin_p = _pseudonym()  # look-alike actor, never erased
    # a raw sub an IdP issued as 64 hex, whose request the sweep marked
    # complete without running (the user row was already gone): never a subject
    absent_sub = _pseudonym()

    live = _memory(ws, ctx, author, SUBJECTS, 3)
    leftover = _memory(ws, ctx, author, {"by": "fourth"}, 3)
    own = _memory(ws, ctx, p, {"by": "fourth-after"}, 4)
    rolled = _memory(ws, ctx, author, {"by": "fourth"}, 4)
    twin_live = _memory(twin_ws, twin_ctx, author, SUBJECTS, 3)
    twin_leftover = _memory(twin_ws, twin_ctx, author, THIRDS, 3)
    absent_edited = _memory(ws, ctx, author, SUBJECTS, 3)
    memories = [live, leftover, own, rolled, twin_live, twin_leftover, absent_edited]
    edits = [
        _edit(live, 2, author, {"details": CREATION}),
        _edit(live, 3, p, {"details": AUTHORS}),
        _edit(leftover, 2, p, {"details": AUTHORS}),
        _edit(leftover, 3, fourth, {"details": SUBJECTS}),  # the #2235 leftover
        _edit(own, 2, p, SCRUBBED),
        # after the erasure: the scrub had NULLed details; two members wrote
        _edit(own, 3, third, {"details": None}, AFTER),
        _edit(own, 4, fourth, {"details": THIRDS}, AFTER),
        _edit(rolled, 2, third, {"details": AUTHORS}, undone_by=1),
        _edit(rolled, 3, p, {"details": THIRDS}, writer="rollback"),
        _edit(rolled, 4, fourth, {"details": AUTHORS}),
        _edit(twin_live, 2, author, {"details": CREATION}),
        _edit(twin_live, 3, twin_p, {"details": AUTHORS}),
        _edit(twin_leftover, 2, twin_p, {"details": AUTHORS}),
        _edit(twin_leftover, 3, fourth, {"details": SUBJECTS}),
        _edit(absent_edited, 2, author, {"details": CREATION}),
        _edit(absent_edited, 3, absent_sub, {"details": AUTHORS}),
    ]
    request = _request(p, minutes=1)
    raw_sub = _request(f"google-oauth2|{tag}")  # before v0.58.1: never pseudonymized
    nothing = _request(_pseudonym(), minutes=2)  # its pseudonym has no rows
    absent = _request(absent_sub, summary={"note": "user_row_already_absent"})
    ignored = [_request(twin_p, status="failed"), _request(_pseudonym(), status="cancelled")]
    requests = [request, raw_sub, nothing, absent, *ignored]
    erasure_audit = AuditLog(
        user_email="e" * 64,
        user_id=p,
        action="account_erasure",
        resource=f"user_pseudonym:{p[:16]}",
        user_metadata={"request_id": str(request.id)},
    )
    async with _maker(async_engine)() as session:
        for w, c in ((ws, ctx), (twin_ws, twin_ctx)):
            session.add(Workspace(id=w, name=f"ws-2243-{w.hex[:8]}", owner_user_id=author))
            await session.flush()
            session.add(Context(id=c, workspace_id=w, name="2243", created_by=author))
        await session.flush()
        session.add_all(memories)
        await session.flush()
        session.add_all(edits)
        session.add_all(requests)
        session.add(erasure_audit)
        await session.commit()
    try:
        yield {
            "ws": ws,
            "ctx": ctx,
            "p": p,
            "author": author,
            "third": third,
            "fourth": fourth,
            "request": request,
            "raw_sub": raw_sub,
            "absent": absent,
            "nothing": nothing,
            "live": live,
            "leftover": leftover,
            "own": own,
            "rolled": rolled,
        }
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(
                delete(AuditLog).where(AuditLog.user_id == p, AuditLog.action.like("account_%"))
            )
            await session.execute(
                delete(ErasureRequest).where(ErasureRequest.id.in_([r.id for r in requests]))
            )
            await session.execute(delete(Workspace).where(Workspace.id.in_([ws, twin_ws])))
            await session.commit()


async def _run(
    engine: AsyncEngine, argv: list[str], sync: AsyncMock | None = None
) -> tuple[int, AsyncMock]:
    """``python -m src.cli.reprocess_erasures <argv>`` against the test DB."""
    sync = sync or AsyncMock()

    async def get_db() -> AsyncIterator[AsyncSession]:
        async with _maker(engine)() as session:
            yield session

    with (
        patch("cli._oneshot.get_db", get_db),
        patch.object(cli, "configure_logging", lambda level: None),
        patch(_QDRANT, sync),
    ):
        return await cli._main(cli._parse(argv)), sync


async def _log(session: AsyncSession, memory_id: uuid.UUID) -> list[tuple[Any, ...]]:
    rows = (
        await session.execute(
            select(
                MemoryRevision.version,
                MemoryRevision.writer,
                MemoryRevision.actor_user_id,
                MemoryRevision.prior,
            )
            .where(MemoryRevision.memory_id == memory_id)
            .order_by(MemoryRevision.version, MemoryRevision.id)
        )
    ).all()
    return [tuple(r) for r in rows]


async def _details(session: AsyncSession, memory_id: uuid.UUID) -> Any:
    return (
        await session.execute(select(Memory.details).where(Memory.id == memory_id))
    ).scalar_one()


async def _snapshot(session: AsyncSession, table: str, where: str = "true", **params: Any) -> dict:
    """Byte-level ``to_jsonb`` of every row of ``table`` (every tenant)."""
    rows = (
        await session.execute(
            text(f"SELECT t.id, to_jsonb(t) FROM {table} AS t WHERE {where}"),  # noqa: S608 - fixed names
            params,
        )
    ).all()
    return {r[0]: r[1] for r in rows}


async def _snapshots(session: AsyncSession, excluded: list[uuid.UUID]) -> dict[str, dict]:
    return {
        "memories": await _snapshot(session, "memories", "NOT (t.id = ANY(:x))", x=excluded),
        "memory_revisions": await _snapshot(
            session, "memory_revisions", "NOT (t.memory_id = ANY(:x))", x=excluded
        ),
        "audit_logs": await _snapshot(session, "audit_logs"),
        "erasure_requests": await _snapshot(session, "erasure_requests"),
    }


async def _reprocess_audit_rows(session: AsyncSession, p: str) -> list[AuditLog]:
    return list(
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "account_erasure_reprocess", AuditLog.user_id == p
                )
            )
        ).scalars()
    )


async def test_apply_restores_and_strips_only_the_erased_members_values(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    p, live, leftover = world["p"], world["live"], world["leftover"]
    async with _maker(async_engine)() as session:
        before = await _snapshots(session, [live.id, leftover.id])

    code, sync = await _run(
        async_engine, ["--request", str(world["request"].id), "--apply", "--yes"]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert str(world["request"].id) in out
    assert "changed 1 erasure request(s): 1 memory details restored" in out
    assert "1 revision prior(s) stripped, 0 marked scrubbed" in out
    async with _maker(async_engine)() as session:
        # the live value went back to the author's; the leftover prior is gone
        assert await _details(session, live.id) == AUTHORS
        assert await _log(session, live.id) == [
            (2, "api", world["author"], {"details": CREATION}),
            (3, "api", p, {"details": AUTHORS}),
            (4, "erasure", p, {"erased": ["details"], "restored_from": 3}),
        ]
        assert await _log(session, leftover.id) == [
            (2, "api", p, {"details": AUTHORS}),
            (3, "api", world["fourth"], STRIPPED),
        ]
        assert await _details(session, leftover.id) == {"by": "fourth"}
        # every other row — the S1 rows on the scrubbed memory, the B3 chain
        # and the second tenant — byte-identical
        after = await _snapshots(session, [live.id, leftover.id])
        new_audit = set(after.pop("audit_logs")) - set(before.pop("audit_logs"))
        assert after == before
        (audit,) = await _reprocess_audit_rows(session, p)
        assert audit.id in new_audit and len(new_audit) == 1
        assert (audit.user_email, audit.resource) == ("e" * 64, f"user_pseudonym:{p[:16]}")
        assert audit.user_metadata == {
            "request_id": str(world["request"].id),
            "details_restored": 1,
            "details_restored_unavailable": 0,
            "details_stripped": 1,
            "scrub_marked": 0,
            # #2266: the payload outcome is known only after the commit
            "details_embedding_requeued": 0,
            "tool": "cli.reprocess_erasures",
        }
    sync.assert_awaited_once()
    assert sync.await_args.kwargs["memory_id"] == live.id
    assert sync.await_args.kwargs["payload_updates"]["location"] == {"lat": 10.5, "lon": 20.25}
    assert (
        f"payload  {world['request'].id}  synced 1, skipped 0 "
        "(tombstone 0, shared 0, edited since 0), re-queued for embedding 0"
    ) in out


async def test_a_second_run_changes_nothing(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--request", str(world["request"].id), "--apply", "--yes"]
    assert (await _run(async_engine, argv))[0] == 0
    capsys.readouterr()
    async with _maker(async_engine)() as session:
        before = await _snapshots(session, [])
        revisions = (await session.execute(select(func.count(MemoryRevision.id)))).scalar_one()

    code, sync = await _run(async_engine, argv)

    assert code == 0
    out = capsys.readouterr().out
    # the plan finds nothing, so the apply pass never runs
    assert "1 completed erasure(s): would change 0, skipped 0" in out
    assert "changed 1" not in out and "dry run" not in out
    sync.assert_not_awaited()
    async with _maker(async_engine)() as session:
        assert await _snapshots(session, []) == before
        assert (
            await session.execute(select(func.count(MemoryRevision.id)))
        ).scalar_one() == revisions
        assert len(await _reprocess_audit_rows(session, world["p"])) == 1


async def test_the_dry_run_writes_nothing_and_calls_no_vector_store(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    async with _maker(async_engine)() as session:
        before = await _snapshots(session, [])

    for argv in (["--all"], ["--all", "--dry-run"], ["--request", str(world["request"].id)]):
        code, sync = await _run(async_engine, argv)
        assert code == 0
        sync.assert_not_awaited()
        out = capsys.readouterr().out
        line = next(line for line in out.splitlines() if line.startswith(str(world["request"].id)))
        # restored, unavailable, stripped, scrub_marked
        assert line.split()[2:] == ["1", "0", "1", "0"]
        assert "dry run — pass --apply to write" in out
        async with _maker(async_engine)() as session:
            assert await _snapshots(session, []) == before


async def test_all_skips_a_raw_sub_and_lists_only_requests_with_changes(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = await _run(async_engine, ["--all"])

    assert code == 0
    out = capsys.readouterr().out
    assert f"skipped  {world['raw_sub'].id}  stores the account id: finalized before" in out
    assert f"skipped  {world['absent'].id}  never ran: marked complete" in out
    assert str(world["nothing"].id) not in out  # no rows: 0 changes, not listed
    # the raw sub itself is never printed
    assert world["raw_sub"].user_id not in out
    assert world["absent"].user_id not in out


async def test_a_request_the_sweep_never_ran_is_never_a_subject(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Review S2: its ``user_id`` is a raw sub that has the pseudonym's shape.
    Using it would write an ``erasure`` row and an audit row under a raw
    identifier on an account no erasure processed: skipped, nothing written."""
    async with _maker(async_engine)() as session:
        before = await _snapshots(session, [])

    code, sync = await _run(
        async_engine, ["--request", str(world["absent"].id), "--apply", "--yes"]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "1 completed erasure(s): would change 0, skipped 1" in out
    assert world["absent"].user_id not in out
    sync.assert_not_awaited()
    async with _maker(async_engine)() as session:
        assert await _snapshots(session, []) == before


async def test_request_narrows_and_an_unknown_request_is_an_error(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = await _run(async_engine, ["--request", str(world["raw_sub"].id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "1 completed erasure(s): would change 0, skipped 1" in out
    assert "skipped  " in out

    code, _ = await _run(async_engine, ["--request", str(uuid.uuid4())])
    assert code == 1
    assert "is not a completed erasure request" in capsys.readouterr().err


async def test_a_vector_store_failure_requeues_and_the_request_commits(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """#2266: the payload is patched after the request's commit; a failure
    re-queues the memory for embedding, and the run succeeds."""
    p, live = world["p"], world["live"]
    code, sync = await _run(
        async_engine,
        ["--request", str(world["request"].id), "--apply", "--yes"],
        AsyncMock(side_effect=QdrantError("vector store down")),
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "re-queued for embedding 1" in out
    sync.assert_awaited_once()
    async with _maker(async_engine)() as session:
        assert await _details(session, live.id) == AUTHORS
        status = (
            await session.execute(select(Memory.embedding_status).where(Memory.id == live.id))
        ).scalar_one()
        assert status == "pending"
        assert len(await _reprocess_audit_rows(session, p)) == 1


async def test_a_value_another_erased_member_wrote_never_comes_back(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding: S2 wrote over the author's value, S1 over S2's; both
    erased before the #2235 fix, so S1's own row still holds S2's value in its
    prior. Re-processing S1 alone must not make S2's
    value live (it becomes NULL); S2's re-run then puts the author's back and
    strips S2's value from the log."""
    p1, p2 = _pseudonym(), _pseudonym()
    memory = _memory(world["ws"], world["ctx"], world["author"], SUBJECTS, 3)
    s2_value = {"by": "s2", "location": {"lat": 1.0, "lon": 2.0}}
    first, second = _request(p1, minutes=3), _request(p2, minutes=4)
    async with _maker(async_engine)() as session:
        session.add(memory)
        await session.flush()
        session.add_all(
            [
                _edit(memory, 2, p2, {"details": AUTHORS}),
                _edit(memory, 3, p1, {"details": s2_value}),
                first,
                second,
            ]
        )
        await session.commit()
    try:
        sync = AsyncMock()
        code, _ = await _run(async_engine, ["--request", str(first.id), "--apply", "--yes"], sync)
        assert code == 0
        async with _maker(async_engine)() as session:
            assert await _details(session, memory.id) is None
            assert (await _log(session, memory.id))[-1] == (
                4,
                "erasure",
                p1,
                {"erased": ["details"], "restored_from": 3},
            )
        # the cleared location went to the payload, S2's never did
        assert sync.await_args.kwargs["delete_keys"] == ["location"]

        code, _ = await _run(async_engine, ["--request", str(second.id), "--apply", "--yes"])
        assert code == 0
        async with _maker(async_engine)() as session:
            assert await _details(session, memory.id) == AUTHORS
            assert await _log(session, memory.id) == [
                (2, "api", p2, {"details": AUTHORS}),
                (3, "api", p1, STRIPPED),
                (4, "erasure", p1, {"erased": ["details"], "restored_from": 3}),
                (5, "erasure", p2, {"erased": ["details"], "restored_from": 2}),
            ]
        capsys.readouterr()
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id.in_([p1, p2])))
            await session.execute(
                delete(ErasureRequest).where(ErasureRequest.id.in_([first.id, second.id]))
            )
            await session.commit()
