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
from services.details_provenance import details_key_digest
from utils.exceptions import QdrantError

pytestmark = pytest.mark.asyncio(loop_scope="session")

_QDRANT = "services.erasure_details.update_memory_payload_in_qdrant"

CREATION = {"at": "creation"}
AUTHORS = {"by": "author", "location": {"lat": 10.5, "lon": 20.25}}
SUBJECTS = {"by": "subject", "location": {"lat": 35.0, "lon": 139.0}}
THIRDS = {"by": "third"}
STRIPPED = {"erased": ["details"]}  # the whole-value strip (authored arm, before v0.129.0)
# #2275: a prior whose keys were all the subject's, stripped per key
_BY_LOCATION = sorted(details_key_digest(k) for k in ("by", "location"))
KEYS_STRIPPED: dict[str, Any] = {"details": {}, "erased_details_keys": _BY_LOCATION}
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
        from services.erasure_details import erasure_pseudonym

        async with _maker(async_engine)() as session:
            await session.execute(
                delete(AuditLog).where(
                    AuditLog.user_id.in_([p, erasure_pseudonym(absent_sub)]),
                    AuditLog.action.like("account_%"),
                )
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
    assert "1 revision prior(s) stripped, 0 marked scrubbed, 2 details key(s) reverted" in out
    assert f"reverted details keys  {world['request'].id}  2 key(s) on 1 memory(ies)" in out
    assert "details keys ambiguous" not in out
    async with _maker(async_engine)() as session:
        # the live value went back to the author's; the leftover prior is gone
        assert await _details(session, live.id) == AUTHORS
        assert await _log(session, live.id) == [
            (2, "api", world["author"], {"details": CREATION}),
            (3, "api", p, {"details": AUTHORS}),
            (
                4,
                "erasure",
                p,
                {
                    "details": {},
                    "erased_details_keys": _BY_LOCATION,
                    "restored_from": 3,
                    "details_restored_from": dict.fromkeys(_BY_LOCATION, 3),
                },
            ),
        ]
        assert await _log(session, leftover.id) == [
            (2, "api", p, {"details": AUTHORS}),
            (3, "api", world["fourth"], KEYS_STRIPPED),
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
            # #2275: the member's ``by`` and ``location``, none kept as ambiguous
            "details_keys_reverted": 2,
            "details_keys_ambiguous": 0,
            "request_repaired": False,
            # #2287: none of the subject's audit rows carried the account id
            "audit_logs_pseudonymized": 0,
            # #2298: the request's email hash names no audit row
            "audit_mentions_pseudonymized": 0,
            # #2345: the erasure's audit row has no IP / user agent here
            "audit_metadata_cleared": 0,
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
    Using it as a subject would write an ``erasure`` row and an audit row
    under a raw identifier: it never is. #2302: the legacy note row itself
    gets the pseudonym derived from it (what ``erased_pseudonyms`` already
    derived); no memory or revision row changes, the audit row is under the
    derived pseudonym, and the raw sub is never printed."""
    from services.erasure_details import erasure_pseudonym

    absent = world["absent"]
    derived = erasure_pseudonym(absent.user_id)
    async with _maker(async_engine)() as session:
        before = await _snapshots(session, [])

    code, sync = await _run(async_engine, ["--request", str(absent.id), "--apply", "--yes"])

    assert code == 0
    out = capsys.readouterr().out
    assert "1 completed erasure(s): would change 1, skipped 1" in out
    assert f"pseudonymized note row  {absent.id}" in out
    assert absent.user_id not in out
    sync.assert_not_awaited()
    async with _maker(async_engine)() as session:
        after = await _snapshots(session, [])
        row = after["erasure_requests"].pop(absent.id)
        before["erasure_requests"].pop(absent.id)
        assert row["user_id"] == derived
        assert row["deleted_data_summary"] == {
            "note": "user_row_already_absent",
            "pseudonymized": True,
        }
        new_audit = set(after.pop("audit_logs")) - set(before.pop("audit_logs"))
        assert after == before  # memories, revisions, every other request
        (audit,) = await _reprocess_audit_rows(session, derived)
        assert audit.id in new_audit and len(new_audit) == 1
        assert audit.user_metadata["request_note_pseudonymized"] is True
        assert audit.user_metadata["details_restored"] == 0


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
            assert log[:2] == [
                (2, "api", p2, {"details": AUTHORS}),
                (3, "api", p1, KEYS_STRIPPED),
            ]
            # exactly one erasure row, by the request that restored, naming
            # the whole run of erased members' edits
            assert [row for row in log if row[1] == "erasure"] == [
                (
                    4,
                    "erasure",
                    p1,
                    {
                        "details": {},
                        "erased_details_keys": _BY_LOCATION,
                        "restored_from": 2,
                        "details_restored_from": dict.fromkeys(_BY_LOCATION, 2),
                    },
                )
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

    # the erased member's trigger (with a text of theirs); the author's ``by``
    # carried in their edit (#2275: a key they removed would stay removed)
    live_trigger = normalize_trigger({"year": 2027, "month": 1, "day": 1, "text": "p's"})
    p = _pseudonym()
    memory = _memory(
        world["ws"], world["ctx"], world["author"], {"trigger": live_trigger, "by": "author"}, 2
    )
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
                    KEYS_STRIPPED,
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
        # #2302: the world's legacy note row is pseudonymized by --all
        before["erasure_requests"].pop(world["absent"].id)
        after["erasure_requests"].pop(world["absent"].id)
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
    # #2298: other accounts' rows naming the subject — an erased account and
    # a live one acting on the subject (``resource``), the subject acting on
    # the live one (``user_email``): that one value changes
    others: list[int]
    # rows of other accounts that stay byte-identical (a look-alike resource)
    untouched: list[int] = field(default_factory=list)
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
        # #2298: the subject acting as an admin on the live account (the row's
        # user_id is the live account's); the subject's IP / UA there go too
        AuditLog(
            user_email=email,
            user_id=live_sub,
            action="role_assign",
            resource=f"user:{live_email}",
            ip_address="198.51.100.30",
            user_agent="subject-agent",
        ),
        # a look-alike resource: never a substring match
        AuditLog(
            user_email=live_email,
            user_id=live_sub,
            action="role_assign",
            resource=f"user:{email}-x",
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
        case.others = [rows[2].id, rows[3].id, rows[4].id]
        case.untouched = [rows[5].id]
    try:
        with patch("services.account_erasure_service._audit_salt", return_value=_SALT):
            yield case
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(
                delete(AuditLog).where(
                    AuditLog.id.in_(case.own + case.others + case.untouched)
                    | (AuditLog.user_id == case.p)
                )
            )
            await session.execute(
                delete(ErasureRequest).where(ErasureRequest.id.in_(case.requests))
            )
            await session.execute(delete(User).where(User.user_id.in_(case.users)))
            await session.commit()


async def _add_request(
    engine: AsyncEngine,
    case: _AuditCase,
    *,
    legacy: bool,
    email: str | None = None,
    reason: str | None = None,
) -> ErasureRequest:
    """A request that failed after its Postgres step: ``legacy`` before
    v0.124.0 (stores the account id), else v0.124.x (the pseudonym and the
    marker naming the audit-log step, or ``reason``)."""
    from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT
    from utils.hashing import sha256_hex

    request = _request(
        case.sub if legacy else case.p, status="failed", summary={"postgres": {"users": 1}}
    )
    request.user_email_hash = sha256_hex(email or case.email)
    if not legacy:
        request.failure_reason = (
            reason or f"{FAILURE_AFTER_POSTGRES_COMMIT}audit_logs: RuntimeError"
        )
    async with _maker(engine)() as session:
        session.add(request)
        await session.commit()
    case.requests.append(request.id)
    return request


async def _audit(session: AsyncSession, ids: list[int]) -> dict:
    return await _snapshot(session, "audit_logs", "t.id = ANY(:x)", x=ids)


async def _others_before(session: AsyncSession, case: _AuditCase) -> tuple[dict, dict]:
    """The other accounts' rows as they are now and as #2298 leaves them:
    the value naming the subject pseudonymized, the rest byte-identical."""
    before = await _audit(session, case.others + case.untouched)
    about_gone, about_live, by_subject = case.others
    expected = {**before}
    for row_id in (about_gone, about_live):
        expected[row_id] = {**before[row_id], "resource": f"user:{case.email_p}"}
    # the subject was the actor there: their IP / UA go too (#2345)
    expected[by_subject] = {
        **before[by_subject],
        "user_email": case.email_p,
        "ip_address": None,
        "user_agent": None,
    }
    return before, expected


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
        _, others = await _others_before(session, case)

    code, _ = await _run(async_engine, ["--request", str(request.id)])
    assert code == 0
    plan = capsys.readouterr().out
    assert f"would pseudonymize audit rows  {request.id}  2 row(s)\n" in plan
    assert f"would pseudonymize audit mentions  {request.id}  3 row(s)\n" in plan

    code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"pseudonymized audit rows  {request.id}  2 row(s)\n" in out
    assert f"pseudonymized audit mentions  {request.id}  3 row(s)\n" in out
    assert "2 audit row(s) pseudonymized, 3 audit mention(s) pseudonymized" in out
    for text_ in (plan, out):
        assert case.sub not in text_ and case.email not in text_
        assert "198.51.100.30" not in text_

    async with _maker(async_engine)() as session:
        assert await _own_rows(session, case) == [
            (case.p, case.email_p, f"user:{case.email_p}"),
            (case.p, "admin@example.com", f"user:{case.email_p}|{case.p}"),
        ]
        assert await _audit(session, case.others + case.untouched) == others
        (audit,) = await _reprocess_audit_rows(session, case.p)
        assert audit.user_metadata["audit_logs_pseudonymized"] == 2
        assert audit.user_metadata["audit_mentions_pseudonymized"] == 3
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


async def test_a_request_the_sweep_found_interrupted_is_processed_like_one_that_ran(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2286: the sweep's ``interrupted`` marker carries the #2268 prefix, so
    the row is a finalized erasure: in scope, and the subject's audit rows
    that still carry the account id are found through the orphan map. The
    row itself is not rewritten (it stores the pseudonym and the marker); a
    second run is a no-op."""
    from models.erasure import INTERRUPTED_AFTER_POSTGRES_COMMIT

    case = audit_case
    request = await _add_request(
        async_engine, case, legacy=False, reason=INTERRUPTED_AFTER_POSTGRES_COMMIT
    )
    async with _maker(async_engine)() as session:
        _, others = await _others_before(session, case)
        row_before = await _snapshot(session, "erasure_requests", "t.id = :id", id=request.id)

    code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"pseudonymized audit rows  {request.id}  2 row(s)\n" in out
    assert f"pseudonymized audit mentions  {request.id}  3 row(s)\n" in out
    assert f"repaired  {request.id}" not in out and f"skipped  {request.id}" not in out
    # #2302: the sweep's payload re-drive is not on the row: re-driven once
    # (nothing restored here), and its zero counts recorded
    assert f"would resync payloads  {request.id}  0 memory(ies)" in out
    assert case.sub not in out and case.email not in out
    async with _maker(async_engine)() as session:
        assert await _own_rows(session, case) == [
            (case.p, case.email_p, f"user:{case.email_p}"),
            (case.p, "admin@example.com", f"user:{case.email_p}|{case.p}"),
        ]
        assert await _audit(session, case.others + case.untouched) == others
        row = await _snapshot(session, "erasure_requests", "t.id = :id", id=request.id)
        postgres = row[request.id]["deleted_data_summary"].pop("postgres")
        assert postgres.pop("details_payload_synced") == 0
        row_before[request.id]["deleted_data_summary"].pop("postgres")
        assert row == row_before
        before = await _snapshots(session, [])

    code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    assert "1 completed erasure(s): would change 0, skipped 0" in capsys.readouterr().out
    async with _maker(async_engine)() as session:
        assert await _snapshots(session, []) == before


async def test_an_interrupted_runs_missing_payload_re_drive_is_done_once(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """#2302: the sweep closed the run as interrupted but its payload re-drive
    never recorded (no ``details_payload_synced``): the memory the run's
    restore put back (its ``erasure`` revision row) is patched once and the
    counts are written; a second run changes nothing and calls no vector
    store. The dry run counts it and calls nothing."""
    from models.erasure import INTERRUPTED_AFTER_POSTGRES_COMMIT

    p = _pseudonym()
    scrub = datetime(2026, 9, 1, 12, 0, 0)
    restored = _memory(world["ws"], world["ctx"], world["author"], AUTHORS, 4)
    request = _request(
        p,
        status="failed",
        summary={"postgres": {"users": 1, "memory_scrub_at": scrub.isoformat() + "Z"}},
    )
    request.failure_reason = INTERRUPTED_AFTER_POSTGRES_COMMIT
    async with _maker(async_engine)() as session:
        session.add(restored)
        await session.flush()
        session.add_all(
            [
                _edit(restored, 3, p, {"details": AUTHORS}, scrub - timedelta(days=1)),
                _edit(
                    restored,
                    4,
                    p,
                    {"erased": ["details"], "restored_from": 3},
                    scrub + timedelta(seconds=1),
                    writer="erasure",
                ),
                request,
            ]
        )
        await session.commit()
    try:
        async with _maker(async_engine)() as session:
            before = await _snapshots(session, [])

        code, sync = await _run(async_engine, ["--request", str(request.id)])
        assert code == 0
        assert f"would resync payloads  {request.id}  1 memory(ies)" in capsys.readouterr().out
        sync.assert_not_awaited()
        async with _maker(async_engine)() as session:
            assert await _snapshots(session, []) == before

        code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
        assert code == 0
        out = capsys.readouterr().out
        assert f"payload  {request.id}  synced 1, skipped 0" in out
        assert "1 request(s) payloads resynced" in out
        sync.assert_awaited_once()
        assert sync.await_args.kwargs["memory_id"] == restored.id
        assert sync.await_args.kwargs["payload_updates"]["location"] == AUTHORS["location"]
        async with _maker(async_engine)() as session:
            after = await _snapshots(session, [])
            row = after["erasure_requests"].pop(request.id)
            old = before["erasure_requests"].pop(request.id)
            postgres = row["deleted_data_summary"]["postgres"]
            assert postgres["details_payload_synced"] == 1
            assert postgres["memory_scrub_at"] == scrub.isoformat() + "Z"
            assert (row["status"], row["failure_reason"]) == (old["status"], old["failure_reason"])
            for key in ("memories", "memory_revisions", "erasure_requests"):
                assert after[key] == before[key], key
            (audit,) = await _reprocess_audit_rows(session, p)
            assert audit.user_metadata["payloads_resynced"] == 1
            again = await _snapshots(session, [])

        code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
        assert code == 0
        assert "1 completed erasure(s): would change 0, skipped 0" in capsys.readouterr().out
        sync.assert_not_awaited()
        async with _maker(async_engine)() as session:
            assert await _snapshots(session, []) == again
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id == p))
            await session.execute(delete(ErasureRequest).where(ErasureRequest.id == request.id))
            await session.execute(delete(Memory).where(Memory.id == restored.id))
            await session.commit()


async def test_a_payload_step_failure_row_gets_the_backstop(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Review of #2302: a run that failed inside its payload step
    (``after_postgres_commit: payloads: <class>``) recorded no
    ``details_payload_*`` either, and nothing re-queued the memories it did
    not reach: the backstop patches the restored memory once and records the
    counts; the marker stays; a second run changes nothing."""
    from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT

    p = _pseudonym()
    scrub = datetime(2026, 9, 1, 12, 0, 0)
    restored = _memory(world["ws"], world["ctx"], world["author"], AUTHORS, 4)
    request = _request(
        p,
        status="failed",
        summary={"postgres": {"users": 1, "memory_scrub_at": scrub.isoformat() + "Z"}},
    )
    reason = f"{FAILURE_AFTER_POSTGRES_COMMIT}payloads: InterfaceError"
    request.failure_reason = reason
    async with _maker(async_engine)() as session:
        session.add(restored)
        await session.flush()
        session.add_all(
            [
                _edit(restored, 3, p, {"details": AUTHORS}, scrub - timedelta(days=1)),
                _edit(
                    restored,
                    4,
                    p,
                    {"erased": ["details"], "restored_from": 3},
                    scrub + timedelta(seconds=1),
                    writer="erasure",
                ),
                request,
            ]
        )
        await session.commit()
    try:
        code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
        assert code == 0
        out = capsys.readouterr().out
        assert f"would resync payloads  {request.id}  1 memory(ies)" in out
        assert "1 request(s) payloads resynced" in out
        sync.assert_awaited_once()
        assert sync.await_args.kwargs["memory_id"] == restored.id
        async with _maker(async_engine)() as session:
            row = (await _snapshot(session, "erasure_requests", "t.id = :id", id=request.id))[
                request.id
            ]
            assert row["failure_reason"] == reason
            assert row["deleted_data_summary"]["postgres"]["details_payload_synced"] == 1
            before = await _snapshots(session, [])

        code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
        assert code == 0
        assert "1 completed erasure(s): would change 0, skipped 0" in capsys.readouterr().out
        sync.assert_not_awaited()
        async with _maker(async_engine)() as session:
            assert await _snapshots(session, []) == before
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id == p))
            await session.execute(delete(ErasureRequest).where(ErasureRequest.id == request.id))
            await session.execute(delete(Memory).where(Memory.id == restored.id))
            await session.commit()


async def _add_note_row(
    engine: AsyncEngine, case: _AuditCase, *, pseudonymized: bool, self_service: bool = False
) -> ErasureRequest:
    """A request the sweep marked complete with the note: since v0.125.0
    pseudonymized and flagged, before that with the account id (and the
    request metadata)."""
    from utils.hashing import sha256_hex

    summary: dict[str, Any] = {"note": "user_row_already_absent"}
    if pseudonymized:
        summary["pseudonymized"] = True
    request = _request(case.p if pseudonymized else case.sub, summary=summary)
    request.user_email_hash = sha256_hex(case.email)
    if self_service:
        request.is_self_service = True
        request.reason_code = "self_service"
        request.initiated_by = request.user_id
    if not pseudonymized:
        request.ip_address, request.user_agent = "203.0.113.7", "agent/1"
    async with _maker(engine)() as session:
        session.add(request)
        await session.commit()
    case.requests.append(request.id)
    return request


async def test_a_flagged_note_rows_audit_residue_is_pseudonymized(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2286: a run interrupted on v0.124.x after its Postgres commit and
    before its audit-log step, swept on v0.125.0, is a flagged note row whose
    subject's audit rows still carry the account id. Its ``user_id`` is a real
    pseudonym, so the orphan lookup finds them: they are pseudonymized (and
    only they), the request stays skipped as ``never ran`` and is not
    rewritten, nothing is restored or stripped; a second run is a no-op."""
    case = audit_case
    request = await _add_note_row(async_engine, case, pseudonymized=True)
    async with _maker(async_engine)() as session:
        _, others = await _others_before(session, case)
        row_before = await _snapshot(session, "erasure_requests", "t.id = :id", id=request.id)
        tables_before = {t: await _snapshot(session, t) for t in ("memories", "memory_revisions")}

    code, _ = await _run(async_engine, ["--request", str(request.id)])
    assert code == 0
    plan = capsys.readouterr().out
    assert f"would pseudonymize audit rows  {request.id}  2 row(s)\n" in plan
    assert f"would pseudonymize audit mentions  {request.id}  3 row(s)\n" in plan
    assert f"skipped  {request.id}  never ran" in plan

    code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"pseudonymized audit rows  {request.id}  2 row(s)\n" in out
    for text_ in (plan, out):
        assert case.sub not in text_ and case.email not in text_
    sync.assert_not_awaited()
    async with _maker(async_engine)() as session:
        assert await _own_rows(session, case) == [
            (case.p, case.email_p, f"user:{case.email_p}"),
            (case.p, "admin@example.com", f"user:{case.email_p}|{case.p}"),
        ]
        assert await _audit(session, case.others + case.untouched) == others
        assert (
            await _snapshot(session, "erasure_requests", "t.id = :id", id=request.id) == row_before
        )
        assert {
            t: await _snapshot(session, t) for t in ("memories", "memory_revisions")
        } == tables_before
        (audit,) = await _reprocess_audit_rows(session, case.p)
        assert audit.user_metadata["audit_logs_pseudonymized"] == 2
        assert audit.user_metadata["audit_mentions_pseudonymized"] == 3
        assert audit.user_metadata["details_restored"] == 0
        before = await _snapshots(session, [])

    code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "1 completed erasure(s): would change 0, skipped 1" in out
    assert "audit rows" not in out
    async with _maker(async_engine)() as session:
        assert await _snapshots(session, []) == before


@pytest.mark.parametrize("self_service", [True, False])
async def test_a_legacy_note_row_is_pseudonymized_with_its_audit_residue(
    async_engine: AsyncEngine,
    audit_case: _AuditCase,
    capsys: pytest.CaptureFixture[str],
    self_service: bool,
) -> None:
    """#2302: a note row written before v0.125.0 stores the raw sub (no
    flag). The plan lists it; the apply stores the derived pseudonym, clears
    the request metadata and flags the summary — ``initiated_by`` too when
    it was the subject's own sub, an admin's kept — and the subject's
    orphaned audit rows are pseudonymized in the same pass. Status and
    completion time stay; nothing else changes; the sub and the email are
    never printed; a second run is a no-op."""
    from services.erasure_details import erased_pseudonyms

    case = audit_case
    request = await _add_note_row(
        async_engine, case, pseudonymized=False, self_service=self_service
    )
    async with _maker(async_engine)() as session:
        # #2298 runs under the derived pseudonym too: the other accounts' rows
        # naming the subject get it, the rest stays
        _, others = await _others_before(session, case)
        before = await _snapshots(session, [])
        assert case.p in await erased_pseudonyms(session)

    code, _ = await _run(async_engine, ["--request", str(request.id)])
    assert code == 0
    plan = capsys.readouterr().out
    assert f"would pseudonymize note row  {request.id}  marked complete without running" in plan
    assert f"would pseudonymize audit rows  {request.id}  2 row(s)\n" in plan
    assert f"would pseudonymize audit mentions  {request.id}  3 row(s)\n" in plan
    assert f"skipped  {request.id}  never ran" in plan

    code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"pseudonymized note row  {request.id}" in out
    assert f"pseudonymized audit rows  {request.id}  2 row(s)\n" in out
    assert "1 note row(s) pseudonymized" in out
    for text_ in (plan, out):
        assert case.sub not in text_ and case.email not in text_
    sync.assert_not_awaited()
    async with _maker(async_engine)() as session:
        after = await _snapshots(session, [])
        row = after["erasure_requests"].pop(request.id)
        old = before["erasure_requests"].pop(request.id)
        assert row["user_id"] == case.p
        assert row["initiated_by"] == (case.p if self_service else "admin-x")
        assert (row["ip_address"], row["user_agent"]) == (None, None)
        assert row["deleted_data_summary"] == {
            "note": "user_row_already_absent",
            "pseudonymized": True,
        }
        assert (row["status"], row["completed_at"]) == (old["status"], old["completed_at"])
        assert await _own_rows(session, case) == [
            (case.p, case.email_p, f"user:{case.email_p}"),
            (case.p, "admin@example.com", f"user:{case.email_p}|{case.p}"),
        ]
        assert await _audit(session, case.others + case.untouched) == others
        for key in ("memories", "memory_revisions", "erasure_requests"):
            assert after[key] == before[key], key
        (audit,) = await _reprocess_audit_rows(session, case.p)
        assert audit.user_metadata["request_note_pseudonymized"] is True
        assert audit.user_metadata["audit_logs_pseudonymized"] == 2
        assert case.sub not in repr(audit.user_metadata)
        # the stored value is what was derived before: no walk changes
        assert case.p in await erased_pseudonyms(session)
        again = await _snapshots(session, [])

    code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "1 completed erasure(s): would change 0, skipped 1" in out
    sync.assert_not_awaited()
    async with _maker(async_engine)() as session:
        assert await _snapshots(session, []) == again


async def test_a_legacy_note_row_whose_sub_is_in_use_again_keeps_the_live_rows(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2302: the sub was registered again. The past account's note row is
    still pseudonymized (that account was deleted); the audit rows carrying
    the sub may be the new account's: their ``user_id`` is kept and the
    request listed (an erased email on them is #2298's, not checked here)."""
    case = audit_case
    request = await _add_note_row(async_engine, case, pseudonymized=False)
    async with _maker(async_engine)() as session:
        session.add(User(email=f"again-{case.email}", user_id=case.sub, name="again", role="user"))
        await session.commit()
    case.users.append(case.sub)
    async with _maker(async_engine)() as session:
        live_user = await _snapshot(session, "users", "t.user_id = :u", u=case.sub)

    code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"pseudonymized note row  {request.id}" in out
    assert f"audit  {request.id}  rows kept: the account id is in use again" in out
    assert case.sub not in out
    async with _maker(async_engine)() as session:
        assert (await _request_row(session, request.id)).user_id == case.p
        own = await _audit(session, case.own)
        assert {row["user_id"] for row in own.values()} == {case.sub}
        assert await _snapshot(session, "users", "t.user_id = :u", u=case.sub) == live_user


async def test_a_legacy_note_rows_restored_memories_are_patched_once(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Review of #2302: a run interrupted after its Postgres commit on
    v0.124.x left a legacy note row (raw sub) and a restored memory whose
    point may keep the erased member's ``location``. The repair patches it
    (found by the derived pseudonym's ``erasure`` revision row) once; the
    next run, the row now flagged, changes nothing and calls no vector store."""
    from services.erasure_details import erasure_pseudonym

    sub = f"google-oauth2|legacy-{uuid.uuid4().hex[:8]}"
    derived = erasure_pseudonym(sub)
    restored = _memory(world["ws"], world["ctx"], world["author"], AUTHORS, 4)
    request = _request(sub, summary={"note": "user_row_already_absent"})
    async with _maker(async_engine)() as session:
        session.add(restored)
        await session.flush()
        session.add_all(
            [
                _edit(restored, 3, derived, {"details": AUTHORS}),
                _edit(
                    restored,
                    4,
                    derived,
                    {"erased": ["details"], "restored_from": 3},
                    AFTER,
                    writer="erasure",
                ),
                request,
            ]
        )
        await session.commit()
    try:
        code, sync = await _run(async_engine, ["--request", str(request.id)])
        assert code == 0
        plan = capsys.readouterr().out
        assert f"would pseudonymize note row  {request.id}" in plan
        assert f"would resync payloads  {request.id}  1 memory(ies)" in plan
        sync.assert_not_awaited()

        code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
        assert code == 0
        out = capsys.readouterr().out
        assert f"payload  {request.id}  synced 1," in out
        assert sub not in out and sub not in plan
        sync.assert_awaited_once()
        assert sync.await_args.kwargs["memory_id"] == restored.id
        async with _maker(async_engine)() as session:
            assert (await _request_row(session, request.id)).user_id == derived
            before = await _snapshots(session, [])

        code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
        assert code == 0
        assert "1 completed erasure(s): would change 0, skipped 1" in capsys.readouterr().out
        sync.assert_not_awaited()
        async with _maker(async_engine)() as session:
            assert await _snapshots(session, []) == before
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(AuditLog).where(AuditLog.user_id == derived))
            await session.execute(delete(ErasureRequest).where(ErasureRequest.id == request.id))
            await session.execute(delete(Memory).where(Memory.id == restored.id))
            await session.commit()


async def test_a_note_rows_derived_pseudonym_is_an_erased_member(
    async_engine: AsyncEngine, world: dict[str, Any]
) -> None:
    """#2286: a row the sweep marked complete with the note gives the
    pseudonym derived from its ``user_id``, never the stored value — on a
    legacy row that is a raw sub, here one with the pseudonym's shape."""
    from services.erasure_details import erased_pseudonyms, erasure_pseudonym

    async with _maker(async_engine)() as session:
        erased = await erased_pseudonyms(session)
    assert erasure_pseudonym(world["absent"].user_id) in erased
    assert world["absent"].user_id not in erased
    assert world["p"] in erased  # a finalized row: as stored


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
    # #2298: the email is in use again too: the rows naming it are kept
    assert f"audit mentions  {request.id}  {cli.MENTIONS_KEPT}" in out
    assert "pseudonymized audit rows" not in out and "pseudonymized audit mentions" not in out
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
        assert f"would pseudonymize audit mentions  {request.id}  3 row(s)\n" in out
        assert "dry run — pass --apply to write" in out
        async with _maker(async_engine)() as session:
            assert await _snapshots(session, []) == before


async def test_two_requests_of_one_account_pseudonymize_its_rows_once(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """Erased, re-created with the same account id under another email, and
    erased again: both requests store the pseudonym. The rows are handled
    under the older request only, in the plan as when applied — with the
    emails of both requests."""
    case = audit_case
    second_email = f"second-{uuid.uuid4().hex[:6]}@example.com"
    first = await _add_request(async_engine, case, legacy=False)
    second = await _add_request(async_engine, case, legacy=False, email=second_email)
    async with _maker(async_engine)() as session:
        later = AuditLog(
            user_email=second_email,
            user_id=case.sub,
            action="password_set",
            resource=f"user:{second_email}",
        )
        session.add(later)
        await session.flush()
        case.own.append(later.id)
        for request, at in ((first, COMPLETED_AT), (second, COMPLETED_AT + timedelta(days=1))):
            await session.execute(
                update(ErasureRequest).where(ErasureRequest.id == request.id).values(started_at=at)
            )
        await session.commit()

    code, _ = await _run(async_engine, ["--all", "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"would pseudonymize audit rows  {first.id}  3 row(s)\n" in out
    assert f"pseudonymized audit rows  {first.id}  3 row(s)\n" in out
    # #2298: the mention sweep runs once too, under the older request
    assert f"would pseudonymize audit mentions  {first.id}  3 row(s)\n" in out
    assert f"pseudonymized audit mentions  {first.id}  3 row(s)\n" in out
    assert f"audit rows  {second.id}" not in out
    assert f"audit mentions  {second.id}" not in out
    assert second_email not in out
    second_p = _salted(second_email)
    async with _maker(async_engine)() as session:
        assert await _own_rows(session, case) == [
            (case.p, case.email_p, f"user:{case.email_p}"),
            (case.p, "admin@example.com", f"user:{case.email_p}|{case.p}"),
            (case.p, second_p, f"user:{second_p}"),
        ]


async def test_request_recovers_every_email_of_the_account(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2287 review: ``--request`` for one of the account's two requests still
    recovers the email of the other — once the rows carry the pseudonym no
    later run can reach them, so an email left now would stay raw."""
    case = audit_case
    second_email = f"second-{uuid.uuid4().hex[:6]}@example.com"
    await _add_request(async_engine, case, legacy=False)
    second = await _add_request(async_engine, case, legacy=False, email=second_email)
    async with _maker(async_engine)() as session:
        later = AuditLog(
            user_email=second_email,
            user_id=case.sub,
            action="password_set",
            resource=f"user:{second_email}",
        )
        session.add(later)
        await session.commit()
        case.own.append(later.id)

    code, _ = await _run(async_engine, ["--request", str(second.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"pseudonymized audit rows  {second.id}  3 row(s)\n" in out
    assert case.email not in out and second_email not in out
    second_p = _salted(second_email)
    async with _maker(async_engine)() as session:
        assert await _own_rows(session, case) == [
            (case.p, case.email_p, f"user:{case.email_p}"),
            (case.p, "admin@example.com", f"user:{case.email_p}|{case.p}"),
            (case.p, second_p, f"user:{second_p}"),
        ]


async def test_an_account_id_registered_during_the_run_keeps_its_rows(
    async_engine: AsyncEngine, audit_case: _AuditCase
) -> None:
    """The re-run read the audit subs before the account id got a user row
    again: the UPDATE itself still skips rows whose account id has one."""
    from services.erasure_details import pseudonymize_audit_logs

    case = audit_case
    async with _maker(async_engine)() as session:
        session.add(User(email=case.email, user_id=case.sub, name="again", role="user"))
        await session.commit()
    case.users.append(case.sub)
    async with _maker(async_engine)() as session:
        before = await _audit(session, case.own)
        assert (
            await pseudonymize_audit_logs(session, case.sub, [case.email], orphaned_only=True) == 0
        )
        await session.commit()
        assert await _audit(session, case.own) == before


async def test_a_resource_the_pseudonyms_lengthen_is_cut_to_the_column(
    async_engine: AsyncEngine, audit_case: _AuditCase
) -> None:
    """A pseudonym is longer than the sub or email it replaces; a ``resource``
    near the column's length must not fail the statement (in the live
    erasure that would roll the whole Postgres step back on every retry)."""
    from services.erasure_details import pseudonymize_audit_logs

    case = audit_case
    async with _maker(async_engine)() as session:
        long_row = AuditLog(
            user_email=case.email,
            user_id=case.sub,
            action="config_update",
            resource=(f"user:{case.email}|" + "x" * 255)[:255],
        )
        session.add(long_row)
        await session.commit()
        case.own.append(long_row.id)
        assert await pseudonymize_audit_logs(session, case.sub, [case.email]) == 3
        await session.commit()
        resource = (
            await session.execute(select(AuditLog.resource).where(AuditLog.id == long_row.id))
        ).scalar_one()
    assert len(resource) == 255
    assert resource.startswith(f"user:{case.email_p}|") and case.email not in resource


async def test_a_finalized_erasure_gets_its_mentions_from_the_resources(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2298: an erasure finalized on v0.125.0 … v0.127.x pseudonymized the
    subject's own rows, not the rows naming them. Its own rows carry no
    account id any more (no orphan), so the email comes from the rows that
    name it (``_mentioned_emails``, by the request's ``user_email_hash``):
    the mentions are pseudonymized, the own rows stay as they are, nothing
    identifying is printed, and a second run is a no-op."""
    from services.erasure_details import pseudonymize_audit_logs
    from utils.hashing import sha256_hex

    case = audit_case
    async with _maker(async_engine)() as session:
        # what the live erasure did since v0.125.0 (before #2298)
        await session.execute(
            update(AuditLog).where(AuditLog.id.in_(case.own)).values(ip_address="203.0.113.5")
        )
        assert await pseudonymize_audit_logs(session, case.sub, [case.email]) == 2
        request = _request(case.p)
        request.user_email_hash = sha256_hex(case.email)
        session.add(request)
        await session.commit()
        case.requests.append(request.id)
        own_before = await _audit(session, case.own)
        _, others = await _others_before(session, case)

    code, _ = await _run(async_engine, ["--request", str(request.id)])
    assert code == 0
    plan = capsys.readouterr().out
    assert f"would pseudonymize audit mentions  {request.id}  3 row(s)\n" in plan
    assert "audit rows" not in plan

    code, sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"pseudonymized audit mentions  {request.id}  3 row(s)\n" in out
    assert "3 audit mention(s) pseudonymized" in out
    for text_ in (plan, out):
        assert case.sub not in text_ and case.email not in text_
        assert sha256_hex(case.email) not in text_ and case.p not in text_
        assert "198.51.100.30" not in text_
    sync.assert_not_awaited()
    async with _maker(async_engine)() as session:
        assert await _audit(session, case.others + case.untouched) == others
        assert await _audit(session, case.own) == own_before
        (audit,) = await _reprocess_audit_rows(session, case.p)
        assert audit.user_metadata["audit_mentions_pseudonymized"] == 3
        assert audit.user_metadata["audit_logs_pseudonymized"] == 0
        before = await _snapshots(session, [])

    code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "1 completed erasure(s): would change 0, skipped 0" in out
    assert "audit" not in out
    async with _maker(async_engine)() as session:
        assert await _snapshots(session, []) == before


async def test_a_re_registered_email_keeps_the_rows_naming_it(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2298: the subject's email belongs to a user row again (a new account
    under the same address): the rows naming it may be the new account's, so
    none is rewritten — byte-identical — and the request is listed, on every
    run. The subject's own rows (by the account id) are still pseudonymized."""
    case = audit_case
    new_sub = f"google-oauth2|again-{uuid.uuid4().hex[:8]}"
    async with _maker(async_engine)() as session:
        session.add(User(email=case.email, user_id=new_sub, name="again", role="user"))
        await session.commit()
    case.users.append(new_sub)
    request = await _add_request(async_engine, case, legacy=False)
    async with _maker(async_engine)() as session:
        others = await _audit(session, case.others + case.untouched)

    for _ in range(2):
        code, _sync = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
        assert code == 0
        out = capsys.readouterr().out
        assert f"audit mentions  {request.id}  {cli.MENTIONS_KEPT}" in out
        assert "pseudonymized audit mentions" not in out
        assert case.email not in out
        async with _maker(async_engine)() as session:
            assert await _audit(session, case.others + case.untouched) == others
    async with _maker(async_engine)() as session:
        assert [row[0] for row in await _own_rows(session, case)] == [case.p, case.p]


async def test_the_request_metadata_leaves_the_subjects_audit_rows(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2345: an erasure that ran before the fix left the IP / user agent on
    the subject's pseudonymized rows and its ``account_erasure`` row, whose
    ``initiated_by`` was the subject's sub on a self-initiated admin request.
    The re-run clears both and pseudonymizes that ``initiated_by``; another
    erasure row's admin id stays, other accounts' rows (the subject acting
    on one included) and a skipped request's rows are untouched. The plan
    counts and writes nothing; nothing identifying is printed; a second run
    is a no-op."""
    from services.erasure_details import pseudonymize_audit_logs

    case = audit_case
    raw_sub = f"google-oauth2|legacy-{uuid.uuid4().hex[:8]}"
    async with _maker(async_engine)() as session:
        # what the live erasure left before v0.128.0: own rows pseudonymized
        # (without the IP), then given back their request metadata here
        assert await pseudonymize_audit_logs(session, case.sub, [case.email]) == 2
        await session.execute(
            update(AuditLog)
            .where(AuditLog.id.in_(case.own))
            .values(ip_address="203.0.113.5", user_agent="ua")
        )
        request = _request(case.p)
        skipped = _request(raw_sub)  # finalized before v0.58.1: skipped
        session.add_all([request, skipped])
        erasure_rows = [
            AuditLog(
                user_email=case.email_p,
                user_id=case.p,
                action="account_erasure",
                resource=f"user_pseudonym:{case.p[:16]}",
                user_metadata={
                    "request_id": str(request.id),
                    "initiated_by": case.sub,
                    "is_self_service": False,
                },
                ip_address="203.0.113.9",
                user_agent="agent",
            ),
            AuditLog(
                user_email=case.email_p,
                user_id=case.p,
                action="account_erasure",
                resource=f"user_pseudonym:{case.p[:16]}",
                user_metadata={"request_id": str(request.id), "initiated_by": "admin-x"},
                ip_address="203.0.113.10",
            ),
            AuditLog(
                user_email="legacy@example.com",
                user_id=raw_sub,
                action="account_erasure",
                resource="user:legacy@example.com",
                user_metadata={"initiated_by": raw_sub},
                ip_address="203.0.113.11",
            ),
        ]
        session.add_all(erasure_rows)
        await session.commit()
        case.requests += [request.id, skipped.id]
        case.untouched.append(erasure_rows[2].id)
        mine, admins, legacy = (row.id for row in erasure_rows)
        others = await _audit(session, case.others + case.untouched)
        before = await _snapshots(session, [])

    code, _ = await _run(async_engine, ["--all"])
    assert code == 0
    plan = capsys.readouterr().out
    # two own rows + two erasure rows cleared (one of them also gets the
    # pseudonym in initiated_by): rows, not edits
    assert f"would clear audit request metadata  {request.id}  4 row(s)\n" in plan
    async with _maker(async_engine)() as session:
        assert await _snapshots(session, []) == before

    code, _ = await _run(async_engine, ["--all", "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"cleared audit request metadata  {request.id}  4 row(s)\n" in out
    assert "4 audit row(s) request metadata cleared" in out
    assert f"skipped  {skipped.id}" in out
    for text_ in (plan, out):
        for value in (case.sub, raw_sub, "203.0.113.", "agent"):
            assert value not in text_
    async with _maker(async_engine)() as session:
        rows = {
            row.id: row
            for row in (
                await session.execute(
                    select(AuditLog).where(AuditLog.id.in_([*case.own, mine, admins]))
                )
            ).scalars()
        }
        for row in rows.values():
            assert (row.ip_address, row.user_agent) == (None, None)
        assert rows[mine].user_metadata == {
            "request_id": str(request.id),
            "initiated_by": case.p,
            "is_self_service": False,
        }
        assert rows[admins].user_metadata["initiated_by"] == "admin-x"
        assert await _audit(session, case.others + case.untouched) == others
        assert legacy in case.untouched
        (audit,) = await _reprocess_audit_rows(session, case.p)
        assert audit.user_metadata["audit_metadata_cleared"] == 4
        assert audit.ip_address is None
        after = await _snapshots(session, [])

    code, _ = await _run(async_engine, ["--all", "--apply", "--yes"])
    assert code == 0
    assert "request metadata" not in capsys.readouterr().out
    async with _maker(async_engine)() as session:
        assert await _snapshots(session, []) == after


async def test_the_plan_counts_what_the_apply_changes_for_an_email_named_only(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2298 review: an email of the account's that only a ``resource`` names
    (the subject never acted under it) is recovered by ``_mentioned_emails``;
    the subject's own row naming it is rewritten by the own-row UPDATE, so
    the plan's counts are the apply's."""
    case = audit_case
    second_email = f"second-{uuid.uuid4().hex[:6]}@example.com"
    request = await _add_request(async_engine, case, legacy=False)
    await _add_request(async_engine, case, legacy=False, email=second_email)
    async with _maker(async_engine)() as session:
        named = AuditLog(
            user_email="admin@example.com",
            user_id=case.sub,
            action="role_assign",
            resource=f"user:{second_email}",
        )
        session.add(named)
        await session.commit()
        case.own.append(named.id)

    code, _ = await _run(async_engine, ["--all"])
    assert code == 0
    plan = capsys.readouterr().out
    code, _ = await _run(async_engine, ["--all", "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    for line in (
        f"audit rows  {request.id}  3 row(s)\n",
        f"audit mentions  {request.id}  3 row(s)\n",
    ):
        assert f"would pseudonymize {line}" in plan and f"pseudonymized {line}" in out
    second_p = _salted(second_email)
    async with _maker(async_engine)() as session:
        assert (await _own_rows(session, case))[2] == (
            case.p,
            "admin@example.com",
            f"user:{second_p}",
        )


async def test_a_re_registered_account_id_under_a_new_address_loses_the_old_one(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2298 review: the account id is in use again under another address.
    Its rows are kept by account id (``user_id`` stays, ``rows kept``), but
    the erased address on them is not in use: the mention sweep turns it
    into its pseudonym (``user_email`` and an exact ``user:<address>``
    resource) and clears that row's IP / user agent. A row of theirs under
    another address keeps everything."""
    case = audit_case
    new_email = f"new-{uuid.uuid4().hex[:6]}@example.com"
    async with _maker(async_engine)() as session:
        session.add(User(email=new_email, user_id=case.sub, name="again", role="user"))
        await session.execute(
            update(AuditLog)
            .where(AuditLog.id.in_(case.own))
            .values(ip_address="203.0.113.77", user_agent="old-ua")
        )
        await session.commit()
    case.users.append(case.sub)
    request = await _add_request(async_engine, case, legacy=False)
    async with _maker(async_engine)() as session:
        before = await _audit(session, case.own)

    code, _ = await _run(async_engine, ["--request", str(request.id), "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"audit  {request.id}  {cli.AUDIT_KEPT}" in out
    assert case.email not in out and new_email not in out
    async with _maker(async_engine)() as session:
        after = await _audit(session, case.own)
    with_email, with_admin = case.own
    assert after[with_email] == {
        **before[with_email],
        "user_email": case.email_p,
        "resource": f"user:{case.email_p}",
        "ip_address": None,
        "user_agent": None,
    }
    assert after[with_email]["user_id"] == case.sub
    assert after[with_admin] == before[with_admin]


async def test_an_address_two_erasures_share_is_counted_under_the_first(
    async_engine: AsyncEngine, audit_case: _AuditCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2298 review: two erased accounts held one address (both requests
    carry its hash). The rows naming it are swept under the first request
    of the run — in the plan as when applied, request by request."""
    from utils.hashing import sha256_hex

    case = audit_case
    second_p = _salted(f"google-oauth2|second-{uuid.uuid4().hex[:8]}")
    async with _maker(async_engine)() as session:
        first = _request(case.p, minutes=10)
        first.user_email_hash = sha256_hex(case.email)
        second = _request(second_p)
        second.user_email_hash = sha256_hex(case.email)
        session.add_all([first, second])
        await session.commit()
    case.requests += [first.id, second.id]

    code, _ = await _run(async_engine, ["--all"])
    assert code == 0
    plan = capsys.readouterr().out
    code, _ = await _run(async_engine, ["--all", "--apply", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    planned = [line for line in plan.splitlines() if "would pseudonymize audit mentions" in line]
    applied = [line for line in out.splitlines() if "pseudonymized audit mentions" in line]
    assert (
        planned
        and [line.replace("would pseudonymize", "pseudonymized") for line in planned] == applied
    )
    assert all(str(first.id) not in line for line in applied)


async def test_a_pre_v0129_ambiguous_key_is_printed_and_never_written(
    async_engine: AsyncEngine, world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """#2275: an erasure finalized before v0.129.0 stripped the state the
    member wrote whole; a later member's edit above it shows a key that may
    be theirs. The plan and the apply print it as ambiguous (count only);
    nothing is written, no audit row, no vector-store call."""
    p = _pseudonym()
    memory = _memory(world["ws"], world["ctx"], world["author"], {"a": 1, "s": "x"}, 3)
    request = _request(p, minutes=6)
    async with _maker(async_engine)() as session:
        session.add(memory)
        await session.flush()
        session.add_all(
            [
                _edit(memory, 2, p, {"details": {"a": 1}}),
                _edit(memory, 3, world["third"], STRIPPED),
                request,
            ]
        )
        await session.commit()
    try:
        async with _maker(async_engine)() as session:
            before = await _snapshots(session, [])
        line = (
            f"details keys ambiguous  {request.id}  1 key(s) kept (stripped history, see runbook)"
        )
        for argv in (
            ["--request", str(request.id)],
            ["--request", str(request.id), "--apply", "--yes"],
        ):
            code, sync = await _run(async_engine, argv)
            assert code == 0
            out = capsys.readouterr().out
            assert line in out
            assert "1 completed erasure(s): would change 0, skipped 0" in out
            assert p not in out and '"s"' not in out
            sync.assert_not_awaited()
        async with _maker(async_engine)() as session:
            assert await _snapshots(session, []) == before
            assert await _reprocess_audit_rows(session, p) == []
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(ErasureRequest).where(ErasureRequest.id == request.id))
            await session.execute(delete(Memory).where(Memory.id == memory.id))
            await session.commit()
