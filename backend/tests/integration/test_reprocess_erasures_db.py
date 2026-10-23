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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cli import reprocess_erasures as cli
from models.auth import AuditLog, Context, User, Workspace
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
            "request_repaired": False,
            # #2287: none of the subject's audit rows carried the account id
            "audit_logs_pseudonymized": 0,
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
    assert "is not an erasure request that ran in Postgres" in capsys.readouterr().err


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


@pytest.mark.parametrize("first_is_newest", [True, False])
async def test_a_double_erasure_chain_ends_with_the_authors_value_in_either_order(
    async_engine: AsyncEngine,
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    first_is_newest: bool,
) -> None:
    """#2268 (owner decision): S2 wrote over the author's value, S1 over S2's;
    both erased before the #2235 fix, so S1's own row still holds S2's value
    in its prior. Whichever request is processed first, the walk goes past
    both erased members to the author's value: one ``erasure`` row, S2's
    value stripped from the log, the second request restores nothing. The
    author's own memory and every other row are untouched."""
    p1, p2 = _pseudonym(), _pseudonym()
    memory = _memory(world["ws"], world["ctx"], world["author"], SUBJECTS, 3)
    s2_value = {"by": "s2", "location": {"lat": 1.0, "lon": 2.0}}
    s1_request, s2_request = _request(p1, minutes=3), _request(p2, minutes=4)
    order = [s1_request, s2_request] if first_is_newest else [s2_request, s1_request]
    async with _maker(async_engine)() as session:
        session.add(memory)
        await session.flush()
        session.add_all(
            [
                _edit(memory, 2, p2, {"details": AUTHORS}),
                _edit(memory, 3, p1, {"details": s2_value}),
                s1_request,
                s2_request,
            ]
        )
        await session.commit()
    try:
        async with _maker(async_engine)() as session:
            before = await _snapshots(session, [memory.id])
        reports = []
        for request in order:
            sync = AsyncMock()
            code, _ = await _run(
                async_engine, ["--request", str(request.id), "--apply", "--yes"], sync
            )
            assert code == 0
            reports.append((capsys.readouterr().out, sync))

        async with _maker(async_engine)() as session:
            assert await _details(session, memory.id) == AUTHORS
            log = await _log(session, memory.id)
            assert log[:2] == [(2, "api", p2, {"details": AUTHORS}), (3, "api", p1, STRIPPED)]
            # exactly one erasure row, by the request that restored, naming
            # the whole run of erased members' edits
            assert [row for row in log if row[1] == "erasure"] == [
                (4, "erasure", p1, {"erased": ["details"], "restored_from": 2})
            ]
            assert len(log) == 3
            after = await _snapshots(session, [memory.id])
        for snapshot in (before, after):
            snapshot.pop("audit_logs")
        assert after == before  # the author's own memory and every other row
        restored = [
            "1 memory details restored" in out for out, _ in reports
        ]  # only the request whose subject made the newest edit restores
        assert restored == ([True, False] if first_is_newest else [False, True])
        # the author's location went to the payload, S2's never did
        (sync,) = [sync for out, sync in reports if sync.await_count]
        assert sync.await_args.kwargs["payload_updates"]["location"] == AUTHORS["location"]
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id.in_([p1, p2])))
            await session.execute(
                delete(ErasureRequest).where(ErasureRequest.id.in_([s1_request.id, s2_request.id]))
            )
            await session.commit()


@pytest.mark.parametrize(
    "raw_trigger",
    [{"from": "2027-01-01", "until": "2027-01-31"}, {"year": 2028, "month": 3}],
)
async def test_a_time_memory_with_an_unnormalized_authors_trigger_is_restored(
    async_engine: AsyncEngine,
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    raw_trigger: dict[str, Any],
) -> None:
    """#2266 review M1: the author wrote a trigger on a note (never normalized);
    the erased member made the row a time memory. The re-run restores the
    author's value with a window the CHECK accepts (normalized, or the live
    one) and the run succeeds."""
    from utils.time_trigger import normalize_trigger

    live_trigger = normalize_trigger({"year": 2027, "month": 1, "day": 1})
    p = _pseudonym()
    memory = _memory(world["ws"], world["ctx"], world["author"], {"trigger": live_trigger}, 2)
    memory.type = "time"
    request = _request(p, minutes=5)
    async with _maker(async_engine)() as session:
        session.add(memory)
        await session.flush()
        session.add_all(
            [_edit(memory, 2, p, {"details": {"trigger": raw_trigger, "by": "author"}}), request]
        )
        await session.commit()
    try:
        code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
        assert code == 0
        async with _maker(async_engine)() as session:
            row = (
                await session.execute(
                    select(Memory.details, Memory.trigger_from).where(Memory.id == memory.id)
                )
            ).one()
        assert row.details["by"] == "author"
        assert row.trigger_from == row.details["trigger"]["from"]
        assert len(row.trigger_from) == len("2027-01-01T00:00:00")
        capsys.readouterr()
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id == p))
            await session.execute(delete(ErasureRequest).where(ErasureRequest.id == request.id))
            await session.commit()


# ---------------------------------------------------------------------------
# #2268: requests that failed after the Postgres step, the scrub bound, and
# the same pseudonym in two requests
# ---------------------------------------------------------------------------

_SALT = "salt-2268"


def _salted(value: str) -> str:
    from utils.hashing import sha256_hex

    return sha256_hex(value, salt=_SALT)


async def _request_row(session: AsyncSession, request_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            select(
                ErasureRequest.status,
                ErasureRequest.user_id,
                ErasureRequest.initiated_by,
                ErasureRequest.ip_address,
                ErasureRequest.user_agent,
                ErasureRequest.failure_reason,
            ).where(ErasureRequest.id == request_id)
        )
    ).one()


async def test_a_legacy_failed_after_commit_request_is_repaired_and_reprocessed(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A request that failed after its Postgres step before v0.124.0 stores the
    raw sub while the subject's rows carry the pseudonym. ``--apply`` stores
    the derived pseudonym (request metadata cleared, the marker added), then
    restores and strips by it; one audit row; the account id is never
    printed. A ``failed`` request without a Postgres step is not listed and
    stays byte-identical. A second run changes nothing."""
    from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT
    from services.erasure_details import erased_pseudonyms

    tag = uuid.uuid4().hex[:8]
    raw, untouched_raw = f"google-oauth2|legacy-{tag}", f"google-oauth2|early-{tag}"
    p = _salted(raw)
    live = _memory(world["ws"], world["ctx"], world["author"], SUBJECTS, 3)
    leftover = _memory(world["ws"], world["ctx"], world["author"], {"by": "fourth"}, 3)
    legacy = _request(raw, status="failed", summary={"postgres": {"users": 1}})
    legacy.initiated_by, legacy.is_self_service, legacy.reason_code = raw, True, "self_service"
    legacy.ip_address, legacy.user_agent, legacy.failure_reason = "203.0.113.5", "ua", "redis down"
    early = _request(untouched_raw, status="failed", summary={"qdrant": {"kagura_memories": 0}})
    early.failure_reason = "vector store down"
    async with _maker(async_engine)() as session:
        session.add_all([live, leftover])
        await session.flush()
        session.add_all(
            [
                _edit(live, 2, world["author"], {"details": CREATION}),
                _edit(live, 3, p, {"details": AUTHORS}),
                _edit(leftover, 2, p, {"details": AUTHORS}),
                _edit(leftover, 3, world["fourth"], {"details": SUBJECTS}),
                legacy,
                early,
            ]
        )
        await session.commit()
    try:
        with patch("services.account_erasure_service._audit_salt", return_value=_SALT):
            async with _maker(async_engine)() as session:
                # the live erasure of another member walks past this subject
                assert p in await erased_pseudonyms(session)
                assert _salted(untouched_raw) not in await erased_pseudonyms(session)
                before = await _snapshots(session, [live.id, leftover.id])

            code, _ = await _run(async_engine, ["--all"])
            assert code == 0
            plan = capsys.readouterr().out
            assert f"would repair  {legacy.id}" in plan
            assert str(early.id) not in plan
            assert raw not in plan and untouched_raw not in plan

            code, sync = await _run(async_engine, ["--request", str(legacy.id), "--apply", "--yes"])
            assert code == 0
            out = capsys.readouterr().out
            assert f"repaired  {legacy.id}" in out and raw not in out
            assert "1 request row(s) repaired" in out

            async with _maker(async_engine)() as session:
                row = await _request_row(session, legacy.id)
                assert (row.status, row.user_id, row.initiated_by) == ("failed", p, p)
                assert (row.ip_address, row.user_agent) == (None, None)
                # the old reason is replaced: an exception text can name the account
                assert row.failure_reason == FAILURE_AFTER_POSTGRES_COMMIT + cli.REPAIRED_REASON
                assert await _details(session, live.id) == AUTHORS
                assert (await _log(session, leftover.id))[1] == (
                    3,
                    "api",
                    world["fourth"],
                    STRIPPED,
                )
                (audit,) = await _reprocess_audit_rows(session, p)
                assert audit.user_metadata["request_repaired"] is True
                assert audit.user_metadata["details_restored"] == 1
                assert raw not in repr(audit.user_metadata)
                after = await _snapshots(session, [live.id, leftover.id])
            sync.assert_awaited_once()
            # every other row, the early failed request included, byte-identical
            before.pop("audit_logs"), after.pop("audit_logs")
            repaired_before = before["erasure_requests"].pop(legacy.id)
            repaired_after = after["erasure_requests"].pop(legacy.id)
            assert repaired_before != repaired_after
            assert after == before

            code, sync = await _run(async_engine, ["--request", str(legacy.id), "--apply", "--yes"])
            assert code == 0
            out = capsys.readouterr().out
            assert "1 completed erasure(s): would change 0, skipped 0" in out
            sync.assert_not_awaited()
            async with _maker(async_engine)() as session:
                assert len(await _reprocess_audit_rows(session, p)) == 1
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id == p))
            await session.execute(
                delete(ErasureRequest).where(ErasureRequest.id.in_([legacy.id, early.id]))
            )
            await session.execute(delete(Memory).where(Memory.id.in_([live.id, leftover.id])))
            await session.commit()


@pytest.mark.parametrize("with_scrub_time", [True, False])
async def test_the_scrub_time_bounds_the_rerun_on_authored_memories(
    async_engine: AsyncEngine,
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    with_scrub_time: bool,
) -> None:
    """A surviving member edited the subject's (scrubbed) memory between the
    erasure's memory lock and its completion. With ``memory_scrub_at`` in the
    summary that row is left alone; without it (a request from before
    v0.124.0) the completion time is the bound and the row is stripped and
    marked."""
    scrubbed_at = datetime(2026, 9, 1, 12, 0, 0)  # naive UTC
    p = _pseudonym()
    own = _memory(world["ws"], world["ctx"], p, {"by": "third"}, 2)
    summary: dict[str, Any] = {"postgres": {"users": 1}}
    if with_scrub_time:
        summary["postgres"]["memory_scrub_at"] = scrubbed_at.isoformat() + "Z"
    request = _request(p, summary=summary)
    request.completed_at = (scrubbed_at + timedelta(hours=1)).replace(tzinfo=UTC)
    async with _maker(async_engine)() as session:
        session.add(own)
        await session.flush()
        session.add_all(
            [
                _edit(
                    own, 2, world["third"], {"details": None}, scrubbed_at + timedelta(minutes=30)
                ),
                request,
            ]
        )
        await session.commit()
    try:
        async with _maker(async_engine)() as session:
            before = await _snapshots(session, [own.id])
        code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
        assert code == 0
        capsys.readouterr()
        async with _maker(async_engine)() as session:
            (row,) = await _log(session, own.id)
            after = await _snapshots(session, [own.id])
        if with_scrub_time:
            assert row[3] == {"details": None}  # written after the lock: kept
        else:
            assert row[3] == SCRUBBED  # within the completion bound
        before.pop("audit_logs"), after.pop("audit_logs")
        assert after == before
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id == p))
            await session.execute(delete(ErasureRequest).where(ErasureRequest.id == request.id))
            await session.execute(delete(Memory).where(Memory.id == own.id))
            await session.commit()


async def test_the_same_pseudonym_in_two_requests_is_handled_once(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The account was erased, re-created with the same sub, and erased
    again: both requests store the same pseudonym. Request 1 (older) reaches
    only rows of the subject's memories written by its bound, request 2 the
    rows up to its own; a row written after both is left alone. The restore
    runs under request 1, request 2 restores nothing. Two audit rows; a
    second run changes nothing; neither request sees its subject as another
    erased member."""
    from services import erasure_details

    p = _pseudonym()
    t1 = datetime(2026, 7, 10, 12, 0, 0)
    t2 = datetime(2026, 9, 10, 12, 0, 0)
    own = _memory(world["ws"], world["ctx"], p, None, 4)
    live = _memory(world["ws"], world["ctx"], world["author"], SUBJECTS, 2)
    first = _request(p, summary={"postgres": {"memory_scrub_at": t1.isoformat() + "Z"}})
    second = _request(p, summary={"postgres": {"memory_scrub_at": t2.isoformat() + "Z"}})
    first.completed_at = t1.replace(tzinfo=UTC) + timedelta(minutes=1)
    second.completed_at = t2.replace(tzinfo=UTC) + timedelta(minutes=1)
    async with _maker(async_engine)() as session:
        session.add_all([own, live])
        await session.flush()
        session.add_all(
            [
                # period 1: a leftover prior holding the subject's value
                _edit(own, 2, world["third"], {"details": SUBJECTS}, t1 - timedelta(days=1)),
                # period 2
                _edit(own, 3, world["third"], {"details": THIRDS}, t1 + timedelta(days=1)),
                # after both erasures: a surviving member's own history
                _edit(own, 4, world["fourth"], {"details": THIRDS}, t2 + timedelta(days=1)),
                _edit(live, 2, p, {"details": AUTHORS}, t1 - timedelta(days=2)),
                first,
                second,
            ]
        )
        await session.commit()
    try:
        async with _maker(async_engine)() as session:
            # the world's own request changes its two memories in an --all run
            changed = [own.id, live.id, world["live"].id, world["leftover"].id]
            before = await _snapshots(session, changed)
        calls = []
        real = erasure_details.erase_subject_details

        async def spy(*args: Any, **kwargs: Any) -> Any:
            calls.append((args[1], kwargs["erased_actors"]))
            return await real(*args, **kwargs)

        with patch.object(cli, "erase_subject_details", spy):
            code, _ = await _run(async_engine, ["--all", "--apply", "--yes"])
        assert code == 0
        capsys.readouterr()
        async with _maker(async_engine)() as session:
            applied = {
                audit.user_metadata["request_id"]: [
                    audit.user_metadata[key]
                    for key in (
                        "details_restored",
                        "details_restored_unavailable",
                        "details_stripped",
                        "scrub_marked",
                    )
                ]
                for audit in await _reprocess_audit_rows(session, p)
            }
        # what each request changed when applied (oldest first): request 1
        # restores and handles the period-1 row, request 2 the period-2 row
        assert applied == {str(first.id): [1, 0, 1, 1], str(second.id): [0, 0, 1, 1]}
        mine = [erased for subject, erased in calls if subject == p]
        assert len(mine) == 4 and all(p not in erased for erased in mine)  # plan + apply
        async with _maker(async_engine)() as session:
            assert await _details(session, live.id) == AUTHORS
            assert [row[3] for row in await _log(session, own.id)] == [
                SCRUBBED,
                SCRUBBED,
                {"details": THIRDS},
            ]
            assert len(await _reprocess_audit_rows(session, p)) == 2
            after = await _snapshots(session, changed)
        before.pop("audit_logs"), after.pop("audit_logs")
        assert after == before

        code, sync = await _run(async_engine, ["--all", "--apply", "--yes"])
        assert code == 0
        out = capsys.readouterr().out
        assert str(first.id) not in out and str(second.id) not in out
        sync.assert_not_awaited()
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id == p))
            await session.execute(
                delete(ErasureRequest).where(ErasureRequest.id.in_([first.id, second.id]))
            )
            await session.execute(delete(Memory).where(Memory.id.in_([own.id, live.id])))
            await session.commit()


async def test_a_legacy_row_whose_pseudonym_no_row_carries_is_left_alone(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """#2268 review: with another salt than the erasure's (or no surviving
    row) the derived pseudonym matches nothing; the account id is not
    overwritten with it. A reason that only looks like the marker under LIKE
    (``_`` is a wildcard) does not make the row count as marked."""
    from services.erasure_details import erased_pseudonyms

    raw = f"google-oauth2|nosalt-{uuid.uuid4().hex[:8]}"
    # the erasure says it pseudonymized rows, yet none carries the pseudonym
    legacy = _request(raw, status="failed", summary={"postgres": {"memories_scrubbed": 2}})
    legacy.failure_reason = "afterXpostgresXcommit: lookalike"
    async with _maker(async_engine)() as session:
        session.add(legacy)
        await session.commit()
    try:
        with patch("services.account_erasure_service._audit_salt", return_value=_SALT):
            async with _maker(async_engine)() as session:
                before = await _snapshots(session, [])
                assert _salted(raw) in await erased_pseudonyms(session)  # unrepaired
            code, sync = await _run(async_engine, ["--request", str(legacy.id), "--apply", "--yes"])
        assert code == 0
        out = capsys.readouterr().out
        assert f"skipped  {legacy.id}  {cli.SKIPPED_NO_ROWS}" in out
        assert raw not in out
        sync.assert_not_awaited()
        async with _maker(async_engine)() as session:
            assert await _snapshots(session, []) == before
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(ErasureRequest).where(ErasureRequest.id == legacy.id))
            await session.commit()


async def test_a_repair_another_run_already_made_writes_nothing(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """#2268 review: two runs race on one legacy row; the second one's repair
    matches no row (``user_id`` changed), so it is not counted, and with
    nothing else to do no audit row is written."""
    raw = f"google-oauth2|race-{uuid.uuid4().hex[:8]}"
    p = _salted(raw)
    done = _request(p, status="failed", summary={"postgres": {"users": 1}})
    async with _maker(async_engine)() as session:
        session.add(done)
        await session.commit()
    try:
        async with _maker(async_engine)() as session:
            change = await cli._reprocess_one(
                session, done.id, p, None, None, False, frozenset(), account_id=raw
            )
        assert (change.repaired, change.changes) == (False, 0)
        async with _maker(async_engine)() as session:
            assert await _reprocess_audit_rows(session, p) == []
            assert (await _request_row(session, done.id)).user_id == p
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(ErasureRequest).where(ErasureRequest.id == done.id))
            await session.commit()


async def test_a_legacy_row_whose_erasure_wrote_no_pseudonym_is_still_repaired(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """#2268 review: nothing of the subject survived (the summary counts no
    pseudonymized memory or revision row), so no row can carry the pseudonym.
    The account id must not stay on the row: it is repaired anyway."""
    raw = f"google-oauth2|empty-{uuid.uuid4().hex[:8]}"
    p = _salted(raw)
    legacy = _request(
        raw,
        status="failed",
        summary={"postgres": {"memories_scrubbed": 0, "memory_revisions_pseudonymized": 0}},
    )
    async with _maker(async_engine)() as session:
        session.add(legacy)
        await session.commit()
    try:
        with patch("services.account_erasure_service._audit_salt", return_value=_SALT):
            code, _ = await _run(async_engine, ["--request", str(legacy.id), "--apply", "--yes"])
        assert code == 0
        out = capsys.readouterr().out
        assert f"repaired  {legacy.id}" in out and raw not in out
        async with _maker(async_engine)() as session:
            assert (await _request_row(session, legacy.id)).user_id == p
            assert len(await _reprocess_audit_rows(session, p)) == 1
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id == p))
            await session.execute(delete(ErasureRequest).where(ErasureRequest.id == legacy.id))
            await session.commit()


async def test_a_legacy_row_is_bounded_an_hour_after_its_start(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """#2268 review: no scrub or completion time on a legacy failed request;
    ``started_at`` + 1h (the sweep's crash cutoff) bounds the strip on the
    subject's memories, so a surviving member's later history stays."""
    raw = f"google-oauth2|bound-{uuid.uuid4().hex[:8]}"
    p = _salted(raw)
    started = datetime(2026, 9, 1, 12, 0, 0)  # naive UTC
    own = _memory(world["ws"], world["ctx"], p, None, 3)
    legacy = _request(raw, status="failed", summary={"postgres": {"memories_scrubbed": 1}})
    legacy.started_at = started.replace(tzinfo=UTC)
    async with _maker(async_engine)() as session:
        session.add(own)
        await session.flush()
        session.add_all(
            [
                _edit(own, 2, world["third"], {"details": SUBJECTS}, started - timedelta(days=1)),
                _edit(own, 3, world["fourth"], {"details": THIRDS}, started + timedelta(hours=2)),
                legacy,
            ]
        )
        await session.commit()
    try:
        async with _maker(async_engine)() as session:
            before = await _snapshots(session, [own.id])
        with patch("services.account_erasure_service._audit_salt", return_value=_SALT):
            code, _ = await _run(async_engine, ["--request", str(legacy.id), "--apply", "--yes"])
        assert code == 0
        capsys.readouterr()
        async with _maker(async_engine)() as session:
            assert [row[3] for row in await _log(session, own.id)] == [
                SCRUBBED,  # before the erasure: the subject's value, stripped
                {"details": THIRDS},  # after the bound: left alone
            ]
            after = await _snapshots(session, [own.id])
        for snapshot in (before, after):
            snapshot.pop("audit_logs")
            snapshot["erasure_requests"].pop(legacy.id)
        assert after == before
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id == p))
            await session.execute(delete(ErasureRequest).where(ErasureRequest.id == legacy.id))
            await session.execute(delete(Memory).where(Memory.id == own.id))
            await session.commit()


# ---------------------------------------------------------------------------
# #2287: audit rows an erasure's failed audit-log step left behind
# ---------------------------------------------------------------------------


@dataclass
class _AuditCase:
    """A subject ``sub`` (email ``email``) whose own audit rows still carry the
    account id, next to rows of other accounts that must stay byte-identical."""

    sub: str
    email: str
    p: str
    email_p: str
    own: list[int]  # the subject's rows: (subject's email), (an admin's email)
    others: list[int]  # an erased other account, a live account
    requests: list[uuid.UUID] = field(default_factory=list)
    users: list[str] = field(default_factory=list)


@pytest_asyncio.fixture(loop_scope="session")
async def audit_case(async_engine: AsyncEngine) -> AsyncIterator[_AuditCase]:
    tag = uuid.uuid4().hex[:8]
    sub, email = f"google-oauth2|audit-{tag}", f"subject-{tag}@example.com"
    other_sub, other_email = f"google-oauth2|gone-{tag}", f"gone-{tag}@example.com"
    live_sub, live_email = f"google-oauth2|live-{tag}", f"live-{tag}@example.com"
    rows = [
        AuditLog(user_email=email, user_id=sub, action="role_assign", resource=f"user:{email}"),
        # the subject acted, the row carries someone else's email: kept
        AuditLog(
            user_email="admin@example.com",
            user_id=sub,
            action="member_add",
            resource=f"user:{email}|{sub}",
        ),
        # another account with no user row (never erased by a request here)
        AuditLog(
            user_email=other_email,
            user_id=other_sub,
            action="role_assign",
            resource=f"user:{email}",
        ),
        # a live account acting on the subject
        AuditLog(
            user_email=live_email,
            user_id=live_sub,
            action="role_assign",
            resource=f"user:{email}",
        ),
    ]
    case = _AuditCase(
        sub=sub,
        email=email,
        p=_salted(sub),
        email_p=_salted(email),
        own=[],
        others=[],
        users=[live_sub],
    )
    async with _maker(async_engine)() as session:
        session.add(User(email=live_email, user_id=live_sub, name="live", role="user"))
        session.add_all(rows)
        await session.commit()
        case.own = [rows[0].id, rows[1].id]
        case.others = [rows[2].id, rows[3].id]
    try:
        with patch("services.account_erasure_service._audit_salt", return_value=_SALT):
            yield case
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(
                delete(AuditLog).where(
                    AuditLog.id.in_(case.own + case.others) | (AuditLog.user_id == case.p)
                )
            )
            await session.execute(
                delete(ErasureRequest).where(ErasureRequest.id.in_(case.requests))
            )
            await session.execute(delete(User).where(User.user_id.in_(case.users)))
            await session.commit()


async def _add_request(
    engine: AsyncEngine, case: _AuditCase, *, legacy: bool, email: str | None = None
) -> ErasureRequest:
    """A request that failed after its Postgres step: ``legacy`` before
    v0.124.0 (stores the account id), else v0.124.x (the pseudonym and the
    marker naming the audit-log step)."""
    from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT
    from utils.hashing import sha256_hex

    request = _request(
        case.sub if legacy else case.p, status="failed", summary={"postgres": {"users": 1}}
    )
    request.user_email_hash = sha256_hex(email or case.email)
    if not legacy:
        request.failure_reason = f"{FAILURE_AFTER_POSTGRES_COMMIT}audit_logs: RuntimeError"
    async with _maker(engine)() as session:
        session.add(request)
        await session.commit()
    case.requests.append(request.id)
    return request


async def _audit(session: AsyncSession, ids: list[int]) -> dict:
    return await _snapshot(session, "audit_logs", "t.id = ANY(:x)", x=ids)


async def _own_rows(session: AsyncSession, case: _AuditCase) -> list[tuple[str, str, str]]:
    rows = (
        await session.execute(
            select(AuditLog.user_id, AuditLog.user_email, AuditLog.resource)
            .where(AuditLog.id.in_(case.own))
            .order_by(AuditLog.id)
        )
    ).all()
    return [tuple(row) for row in rows]


@pytest.mark.parametrize("legacy", [True, False])
async def test_audit_rows_left_by_a_failed_audit_step_are_pseudonymized(
    async_engine: AsyncEngine,
    audit_case: _AuditCase,
    capsys: pytest.CaptureFixture[str],
    legacy: bool,
) -> None:
    """The subject's rows get the pseudonyms (the email only where it is the
    subject's); every other account's rows — erased or live, acting on the
    subject or not — are byte-identical; one audit row counts them; the
    account id and the email are never printed; a second run is a no-op."""
    case = audit_case
    request = await _add_request(async_engine, case, legacy=legacy)
    async with _maker(async_engine)() as session:
        others = await _audit(session, case.others)

    code, _ = await _run(async_engine, ["--request", str(request.id)])
    assert code == 0
    plan = capsys.readouterr().out
    assert f"would pseudonymize audit rows  {request.id}  2 row(s)\n" in plan

    code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"pseudonymized audit rows  {request.id}  2 row(s)\n" in out
    assert "2 audit row(s) pseudonymized" in out
    for text_ in (plan, out):
        assert case.sub not in text_ and case.email not in text_

    async with _maker(async_engine)() as session:
        assert await _own_rows(session, case) == [
            (case.p, case.email_p, f"user:{case.email_p}"),
            (case.p, "admin@example.com", f"user:{case.email_p}|{case.p}"),
        ]
        assert await _audit(session, case.others) == others
        (audit,) = await _reprocess_audit_rows(session, case.p)
        assert audit.user_metadata["audit_logs_pseudonymized"] == 2
        assert audit.user_metadata["request_repaired"] is legacy
        assert case.sub not in repr(audit.user_metadata)
        row = await _request_row(session, request.id)
        assert row.user_id == case.p
        before = await _snapshots(session, [])

    code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "1 completed erasure(s): would change 0, skipped 0" in out
    assert "audit" not in out
    sync.assert_not_awaited()
    async with _maker(async_engine)() as session:
        assert await _snapshots(session, []) == before


@pytest.mark.parametrize("legacy", [True, False])
async def test_a_re_registered_account_id_keeps_its_audit_rows(
    async_engine: AsyncEngine,
    audit_case: _AuditCase,
    capsys: pytest.CaptureFixture[str],
    legacy: bool,
) -> None:
    """The account id belongs to a user again: its audit rows may be the new
    account's, so none is rewritten; the request is listed. A legacy row is
    still repaired (the request row must not keep the account id)."""
    case = audit_case
    async with _maker(async_engine)() as session:
        session.add(User(email=case.email, user_id=case.sub, name="again", role="user"))
        await session.commit()
    case.users.append(case.sub)
    request = await _add_request(async_engine, case, legacy=legacy)
    async with _maker(async_engine)() as session:
        audit_before = await _snapshot(session, "audit_logs", "t.user_id <> :p", p=case.p)

    code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"audit  {request.id}  {cli.AUDIT_KEPT}" in out
    assert "pseudonymized audit rows" not in out
    assert case.sub not in out and case.email not in out
    async with _maker(async_engine)() as session:
        assert (await _snapshot(session, "audit_logs", "t.user_id <> :p", p=case.p)) == audit_before
        assert (await _request_row(session, request.id)).user_id == case.p
        if legacy:
            (audit,) = await _reprocess_audit_rows(session, case.p)
            assert audit.user_metadata["audit_logs_pseudonymized"] == 0


async def test_the_email_is_left_when_it_cannot_be_recovered(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """The request's email hash matches none of the rows' emails (the email
    changed before the request): the account id is replaced, ``user_email``
    and the email in ``resource`` stay, and the report says so."""
    case = audit_case
    request = await _add_request(async_engine, case, legacy=False, email="new@example.com")

    code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"pseudonymized audit rows  {request.id}  2 row(s), email not recovered" in out
    async with _maker(async_engine)() as session:
        assert await _own_rows(session, case) == [
            (case.p, case.email, f"user:{case.email}"),
            (case.p, "admin@example.com", f"user:{case.email}|{case.p}"),
        ]


async def test_the_dry_run_counts_audit_rows_and_writes_nothing(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    case = audit_case
    request = await _add_request(async_engine, case, legacy=True)
    async with _maker(async_engine)() as session:
        before = await _snapshots(session, [])

    for argv in (["--all"], ["--request", str(request.id)]):
        code, sync = await _run(async_engine, argv)
        assert code == 0
        sync.assert_not_awaited()
        out = capsys.readouterr().out
        assert f"would pseudonymize audit rows  {request.id}  2 row(s)\n" in out
        assert "dry run — pass --apply to write" in out
        async with _maker(async_engine)() as session:
            assert await _snapshots(session, []) == before


async def test_two_requests_of_one_account_pseudonymize_its_rows_once(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """Erased, re-created with the same account id, erased again: both
    requests store the pseudonym. The rows are counted under the older one
    only, in the plan as when applied."""
    case = audit_case
    first = await _add_request(async_engine, case, legacy=False)
    second = await _add_request(async_engine, case, legacy=False)
    async with _maker(async_engine)() as session:
        await session.execute(
            update(ErasureRequest)
            .where(ErasureRequest.id == second.id)
            .values(started_at=COMPLETED_AT + timedelta(days=1))
        )
        await session.execute(
            update(ErasureRequest)
            .where(ErasureRequest.id == first.id)
            .values(started_at=COMPLETED_AT)
        )
        await session.commit()

    code, _ = await _run(async_engine, ["--all", "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"would pseudonymize audit rows  {first.id}  2 row(s)\n" in out
    assert f"pseudonymized audit rows  {first.id}  2 row(s)\n" in out
    assert f"audit rows  {second.id}" not in out
    async with _maker(async_engine)() as session:
        assert [row[0] for row in await _own_rows(session, case)] == [case.p, case.p]
