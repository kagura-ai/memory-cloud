"""Integration tests for ``cli/transfer_context_creator`` (#1783).

Needs a live Postgres (``TEST_DATABASE_URL``, ``*_test`` suffixed); the
``db_session`` fixture skips otherwise. Pins the acceptance bullets: the ops
command re-points only the named user's live contexts in the named workspace
together with their memories (SQL rows and vector payloads), refuses a target
that is not the workspace owner or an admin member, writes nothing in
dry-run, leaves an audit row per moved context, and is a no-op when re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember
from models.memory import (
    EDGE_ORIGIN_DECLARED,
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
    EDGE_TYPE_CONTRADICTS,
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_NEURAL_ASSOCIATION,
    EDGE_TYPE_RELATED_TO,
    EDGE_TYPE_SUPERSEDES,
    Memory,
    NeuralMemoryEdge,
)
from utils.datetime import utcnow

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli.transfer_context_creator import (  # noqa: E402
    AUDIT_ACTION,
    _main,
    _parse,
    transfer_context_creator,
)


def _user(label: str) -> User:
    uid = f"{label}_{uuid4().hex[:8]}"
    return User(user_id=uid, email=f"{uid}@example.com", name=label)


def _workspace(owner: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="basic",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


def _context(ws: Workspace, owner: str, *, private: bool, deleted: bool = False) -> Context:
    return Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=private,
        deleted_at=utcnow() if deleted else None,
    )


def _memory(
    ctx: Context, author: str, *, deleted: bool = False, embedding_status: str = "success"
) -> Memory:
    return Memory(
        id=uuid4(),
        user_id=author,
        workspace_id=ctx.workspace_id,
        context_id=ctx.id,
        summary="s",
        content="c",
        type="note",
        client="test",
        embedding_status=embedding_status,
        deleted_at=utcnow() if deleted else None,
    )


def _edge(
    src: Memory,
    dst: Memory,
    owner: str,
    *,
    edge_type: str = EDGE_TYPE_RELATED_TO,
    origin: str = EDGE_ORIGIN_DECLARED,
    weight: float = 1.0,
) -> NeuralMemoryEdge:
    return NeuralMemoryEdge(
        user_id=owner,
        src_id=src.id,
        dst_id=dst.id,
        workspace_id=src.workspace_id,
        context_id=src.context_id,
        edge_type=edge_type,
        weight=weight,
        confidence=1.0,
        origin=origin,
    )


def _member(ws: Workspace, user: User, role: WorkspaceRole, allowed=None) -> WorkspaceMember:
    return WorkspaceMember(
        workspace_id=ws.id, user_id=user.user_id, role=role, allowed_context_ids=allowed
    )


@pytest_asyncio.fixture
async def scenario(db_session: AsyncSession):
    """``cli_admin`` owns both workspaces and created everything. ``web_user``
    is an admin member of ``ws`` only; ``suspended`` a member with no
    whitelist; ``viewer`` a viewer. ``other`` owns one context that must not
    move."""
    cli_admin, web_user, suspended, viewer, other = (
        _user("cli"),
        _user("web"),
        _user("susp"),
        _user("view"),
        _user("other"),
    )
    ws = _workspace(cli_admin.user_id)
    far_ws = _workspace(cli_admin.user_id)
    db_session.add_all([cli_admin, web_user, suspended, viewer, other, ws, far_ws])
    await db_session.flush()
    db_session.add_all(
        [
            _member(ws, web_user, WorkspaceRole.ADMIN),
            _member(ws, suspended, WorkspaceRole.MEMBER, allowed=None),
            _member(ws, viewer, WorkspaceRole.VIEWER),
        ]
    )

    private_ctx = _context(ws, cli_admin.user_id, private=True)
    shared_ctx = _context(ws, cli_admin.user_id, private=False)
    deleted_ctx = _context(ws, cli_admin.user_id, private=False, deleted=True)
    others_ctx = _context(ws, other.user_id, private=True)
    far_ctx = _context(far_ws, cli_admin.user_id, private=True)
    # A shared context --to owned all along, with a memory --from legitimately
    # authored there: no run may re-attribute it.
    pre_owned_ctx = _context(ws, web_user.user_id, private=False)
    db_session.add_all([private_ctx, shared_ctx, deleted_ctx, others_ctx, far_ctx, pre_owned_ctx])
    await db_session.flush()

    memories = {
        "private_a": _memory(private_ctx, cli_admin.user_id),
        "private_b": _memory(private_ctx, cli_admin.user_id),
        "private_tombstone": _memory(private_ctx, cli_admin.user_id, deleted=True),
        "private_by_other": _memory(private_ctx, other.user_id),
        "shared_a": _memory(shared_ctx, cli_admin.user_id),
        "far_a": _memory(far_ctx, cli_admin.user_id),
        "pre_owned_by_from": _memory(pre_owned_ctx, cli_admin.user_id),
    }
    db_session.add_all(memories.values())
    await db_session.flush()

    return {
        "cli_admin": cli_admin,
        "web_user": web_user,
        "suspended": suspended,
        "viewer": viewer,
        "other": other,
        "ws": ws,
        "far_ws": far_ws,
        "private_ctx": private_ctx,
        "shared_ctx": shared_ctx,
        "deleted_ctx": deleted_ctx,
        "others_ctx": others_ctx,
        "far_ctx": far_ctx,
        "pre_owned_ctx": pre_owned_ctx,
        "memories": memories,
    }


@pytest.fixture
def vector_store():
    """The vector-store payload update, recorded instead of performed."""
    with (
        patch(
            "cli.transfer_context_creator.resolve_collection_name",
            AsyncMock(return_value="test_collection"),
        ),
        patch(
            "cli.transfer_context_creator.update_memory_payload_in_qdrant", AsyncMock()
        ) as update_payload,
    ):
        yield update_payload


async def _created_by(db: AsyncSession, context_id) -> str | None:
    return await db.scalar(select(Context.created_by).where(Context.id == context_id))


async def _author(db: AsyncSession, memory_id) -> str | None:
    return await db.scalar(select(Memory.user_id).where(Memory.id == memory_id))


def _audit_rows_for(scenario):
    resources = [f"context:{scenario[k].id}" for k in ("private_ctx", "shared_ctx", "far_ctx")]
    return select(AuditLog).where(AuditLog.resource.in_(resources))


@pytest.mark.asyncio
async def test_dry_run_plans_contexts_with_memory_counts_and_writes_nothing(
    db_session, scenario, vector_store
):
    s = scenario
    result = await transfer_context_creator(
        db_session,
        from_user_id=s["cli_admin"].user_id,
        to_user_id=s["web_user"].user_id,
        workspace_id=s["ws"].id,
        dry_run=True,
    )

    assert set(result.transferred_ids) == {s["private_ctx"].id, s["shared_ctx"].id}
    counts = {line.context_id: line.memory_count for line in result.lines}
    # Every row by --from, the tombstone included — what the UPDATE will touch.
    assert counts == {s["private_ctx"].id: 3, s["shared_ctx"].id: 1}
    assert result.memories == 4
    # Nothing written: every row still names the CLI admin, no audit rows.
    for key in ("private_ctx", "shared_ctx", "deleted_ctx", "far_ctx"):
        assert await _created_by(db_session, s[key].id) == s["cli_admin"].user_id
    assert await _author(db_session, s["memories"]["private_a"].id) == s["cli_admin"].user_id
    assert (await db_session.execute(_audit_rows_for(s))).scalars().first() is None
    vector_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_moves_contexts_and_their_memories(db_session, scenario, vector_store):
    s, m = scenario, scenario["memories"]
    result = await transfer_context_creator(
        db_session,
        from_user_id=s["cli_admin"].user_id,
        to_user_id=s["web_user"].user_id,
        workspace_id=s["ws"].id,
        dry_run=False,
    )

    assert result.transferred == 2
    assert result.payload_failures == []
    assert await _created_by(db_session, s["private_ctx"].id) == s["web_user"].user_id
    assert await _created_by(db_session, s["shared_ctx"].id) == s["web_user"].user_id
    # Out of scope: deleted context, another creator, another workspace.
    assert await _created_by(db_session, s["deleted_ctx"].id) == s["cli_admin"].user_id
    assert await _created_by(db_session, s["others_ctx"].id) == s["other"].user_id
    assert await _created_by(db_session, s["far_ctx"].id) == s["cli_admin"].user_id

    # Memories by --from in the moved contexts follow, tombstones included;
    # another author's memory and the far workspace's stay put.
    for key in ("private_a", "private_b", "private_tombstone", "shared_a"):
        assert await _author(db_session, m[key].id) == s["web_user"].user_id
    assert await _author(db_session, m["private_by_other"].id) == s["other"].user_id
    assert await _author(db_session, m["far_a"].id) == s["cli_admin"].user_id

    # Vector payloads re-pointed for the live moved memories only.
    repointed = {call.kwargs["memory_id"] for call in vector_store.await_args_list}
    assert repointed == {m["private_a"].id, m["private_b"].id, m["shared_a"].id}
    assert all(
        call.kwargs["payload_updates"] == {"user_id": s["web_user"].user_id}
        and call.kwargs["collection_name"] == "test_collection"
        for call in vector_store.await_args_list
    )

    # One audit row per moved context, naming both identities and the count.
    rows = list((await db_session.execute(_audit_rows_for(s))).scalars().all())
    assert {r.resource for r in rows} == {
        f"context:{s['private_ctx'].id}",
        f"context:{s['shared_ctx'].id}",
    }
    assert all(
        r.action == AUDIT_ACTION
        and r.user_metadata["from_user_id"] == s["cli_admin"].user_id
        and r.user_metadata["to_user_id"] == s["web_user"].user_id
        for r in rows
    )
    assert {r.resource: r.user_metadata["memories"] for r in rows} == {
        f"context:{s['private_ctx'].id}": 3,  # tombstone counted: its row moved too
        f"context:{s['shared_ctx'].id}": 1,
    }

    # Idempotent: a second run finds nothing left to move.
    again = await transfer_context_creator(
        db_session,
        from_user_id=s["cli_admin"].user_id,
        to_user_id=s["web_user"].user_id,
        workspace_id=s["ws"].id,
        dry_run=False,
    )
    assert again.transferred == 0


@pytest.mark.asyncio
async def test_payload_failures_are_reported_after_the_commit(db_session, scenario, vector_store):
    """The database write is not rolled back by a vector-store failure — the
    memory list is right, recall is repaired by hand — but the failure is
    surfaced with the memory ids."""
    s, m = scenario, scenario["memories"]
    bad = m["private_b"].id

    async def flaky(*, memory_id, **_):
        if memory_id == bad:
            raise RuntimeError("qdrant down")

    vector_store.side_effect = flaky
    result = await transfer_context_creator(
        db_session,
        from_user_id=s["cli_admin"].user_id,
        to_user_id=s["web_user"].user_id,
        workspace_id=s["ws"].id,
        dry_run=False,
    )
    assert result.payload_failures == [bad]
    assert await _author(db_session, bad) == s["web_user"].user_id


@pytest.fixture
def cli_db(db_session):
    """Route the command's ``get_db()`` to the test session."""

    async def _get_db():
        yield db_session

    with patch("cli._oneshot.get_db", _get_db):
        yield db_session


@pytest.mark.asyncio
async def test_repair_payloads_converges_after_a_failed_run(
    db_session, scenario, vector_store, cli_db, capsys
):
    """A re-run finds 0 contexts to move (created_by already moved). Driven
    through ``_main`` — the plan→confirm→apply scaffold skips the write when
    the plan is empty, so repair work has to count as planned work — the
    command re-points the vector payloads of every live memory the target
    now owns in the contexts it created."""
    s, m = scenario, scenario["memories"]
    first = await transfer_context_creator(
        db_session,
        from_user_id=s["cli_admin"].user_id,
        to_user_id=s["web_user"].user_id,
        workspace_id=s["ws"].id,
        dry_run=False,
    )
    assert first.transferred == 2
    vector_store.reset_mock()

    argv = [
        "--from",
        s["cli_admin"].user_id,
        "--to",
        s["web_user"].user_id,
        "--workspace",
        str(s["ws"].id),
        "--apply",
        "--yes",
    ]
    # Plain re-run: nothing planned, nothing touched.
    assert await _main(_parse(argv)) == 0
    vector_store.assert_not_awaited()

    # A late write: a memory committed by --from after the flip (the command
    # is not fenced against concurrent writers).
    late = _memory(s["private_ctx"], s["cli_admin"].user_id)
    db_session.add(late)
    await db_session.flush()

    assert await _main(_parse([*argv, "--repair-payloads"])) == 0
    assert await _author(db_session, late.id) == s["web_user"].user_id
    repointed = {call.kwargs["memory_id"] for call in vector_store.await_args_list}
    assert repointed == {m["private_a"].id, m["private_b"].id, m["shared_a"].id, late.id}
    # The context --to owned all along is outside the sweep: --from's memory
    # there keeps its author and its payload is not touched.
    assert await _author(db_session, m["pre_owned_by_from"].id) == s["cli_admin"].user_id
    assert m["pre_owned_by_from"].id not in repointed
    out = capsys.readouterr().out
    assert "would move 1 memory row(s) still authored by" in out
    # The swept row is re-pointed too, so the plan says 4 — what the run does.
    assert "would re-point the vector payload of 4 live memor(ies)" in out
    # The prompt and the report name what moves instead of "5 item(s)" (#1872).
    assert "changed 1 memory row(s), 4 vector payload repair(s)" in out
    assert "item(s)" not in out
    sweep = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == AUDIT_ACTION,
            AuditLog.resource == f"workspace:{s['ws'].id}",
        )
    )
    assert sweep is not None and sweep.user_metadata["sweep"] is True
    assert sweep.user_metadata["memories"] == 1


@pytest.mark.asyncio
async def test_payload_step_exception_names_the_committed_write(db_session, scenario, vector_store):
    s = scenario
    with patch(
        "cli.transfer_context_creator.resolve_collection_name",
        AsyncMock(side_effect=RuntimeError("config lookup failed")),
    ):
        with pytest.raises(RuntimeError, match="database write committed.*--repair-payloads"):
            await transfer_context_creator(
                db_session,
                from_user_id=s["cli_admin"].user_id,
                to_user_id=s["web_user"].user_id,
                workspace_id=s["ws"].id,
                dry_run=False,
            )
    # The write did land.
    assert await _created_by(db_session, s["private_ctx"].id) == s["web_user"].user_id


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["suspended", "viewer", "other"])
async def test_target_that_cannot_list_every_context_is_refused(db_session, scenario, target):
    """A suspended member, a viewer or a non-member could end up owning a
    private context they cannot list: refused before anything is planned."""
    s = scenario
    with pytest.raises(ValueError, match="owner or an admin member"):
        await transfer_context_creator(
            db_session,
            from_user_id=s["cli_admin"].user_id,
            to_user_id=s[target].user_id,
            workspace_id=s["ws"].id,
        )
    assert await _created_by(db_session, s["private_ctx"].id) == s["cli_admin"].user_id


@pytest.mark.asyncio
async def test_workspace_owner_needs_no_member_row(db_session, scenario, vector_store):
    s = scenario
    result = await transfer_context_creator(
        db_session,
        from_user_id=s["other"].user_id,
        to_user_id=s["cli_admin"].user_id,  # owns ws, has no member row
        workspace_id=s["ws"].id,
        dry_run=True,
    )
    assert result.transferred_ids == [s["others_ctx"].id]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["from", "to"])
async def test_unknown_user_is_an_error(db_session, scenario, missing):
    s = scenario
    kwargs = {"from_user_id": s["cli_admin"].user_id, "to_user_id": s["web_user"].user_id}
    kwargs[f"{missing}_user_id"] = "nobody_here"
    with pytest.raises(ValueError, match="nobody_here"):
        await transfer_context_creator(db_session, workspace_id=s["ws"].id, **kwargs)


@pytest.mark.asyncio
async def test_unknown_workspace_and_same_user_are_errors(db_session, scenario):
    s = scenario
    with pytest.raises(ValueError, match="same user"):
        await transfer_context_creator(
            db_session,
            from_user_id=s["cli_admin"].user_id,
            to_user_id=s["cli_admin"].user_id,
            workspace_id=s["ws"].id,
        )
    with pytest.raises(ValueError, match="no workspace"):
        await transfer_context_creator(
            db_session,
            from_user_id=s["cli_admin"].user_id,
            to_user_id=s["web_user"].user_id,
            workspace_id=uuid4(),
        )


async def _edges(db: AsyncSession, context_id) -> dict[tuple, NeuralMemoryEdge]:
    rows = (
        (
            await db.execute(
                select(NeuralMemoryEdge)
                .where(NeuralMemoryEdge.context_id == context_id)
                # The command writes with Core statements: read the rows, not the identity map.
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return {(row.user_id, row.src_id, row.dst_id): row for row in rows}


@pytest.mark.asyncio
async def test_non_hebbian_edges_follow_the_context(db_session, scenario, vector_store):
    """#1872: ``supersedes`` / declared / sleep-discovered edges are state the
    new owner has to manage, so they move with the context; Hebbian weights
    are retrieval history and stay. A pair ``--to`` already holds an edge on
    cannot collide with ``unique_edge``."""
    s, m = scenario, scenario["memories"]
    frm, to = s["cli_admin"].user_id, s["web_user"].user_id
    a, b, t, o = m["private_a"], m["private_b"], m["private_tombstone"], m["private_by_other"]
    db_session.add_all(
        [
            _edge(a, b, frm, edge_type=EDGE_TYPE_SUPERSEDES),
            _edge(b, a, frm),  # declared related_to
            _edge(a, o, frm, origin=EDGE_ORIGIN_SEMANTIC),
            _edge(a, t, frm, edge_type=EDGE_TYPE_NEURAL_ASSOCIATION, origin=EDGE_ORIGIN_HEBBIAN),
            # --to declared a related_to on a pair --from superseded: recall
            # acts on the supersedes, so it is the one that survives.
            _edge(b, o, frm, edge_type=EDGE_TYPE_SUPERSEDES),
            _edge(b, o, to, weight=0.5),
            # Both superseded the pair: --to's row is kept, --from's is dropped.
            _edge(t, b, frm, edge_type=EDGE_TYPE_SUPERSEDES, weight=0.9),
            _edge(t, b, to, edge_type=EDGE_TYPE_SUPERSEDES, weight=0.3),
            # An ordinary declared link does not displace --to's declared one.
            _edge(t, o, frm),
            _edge(t, o, to, edge_type=EDGE_TYPE_DEPENDS_ON, weight=0.2),
            # A third user's edge in the same context is nobody's to move.
            _edge(b, t, s["other"].user_id, edge_type=EDGE_TYPE_SUPERSEDES),
            # --to only has a co-activation weight on the pair: the declared
            # edge replaces it, as a declared write through the API would.
            _edge(o, a, frm, edge_type=EDGE_TYPE_SUPERSEDES),
            _edge(o, a, to, edge_type=EDGE_TYPE_NEURAL_ASSOCIATION, origin=EDGE_ORIGIN_HEBBIAN),
            # Declared beats semantic (#1406): --to's sleep-discovered link on
            # the pair gives way to the old account's declared supersedes.
            _edge(o, b, frm, edge_type=EDGE_TYPE_SUPERSEDES),
            _edge(o, b, to, origin=EDGE_ORIGIN_SEMANTIC),
            # Semantic does not beat semantic: --to's row is kept.
            _edge(t, a, frm, origin=EDGE_ORIGIN_SEMANTIC, weight=0.9),
            _edge(t, a, to, origin=EDGE_ORIGIN_SEMANTIC, weight=0.4),
            # Outside the transfer: an edge of --from in a context --to owned all along.
            _edge(m["pre_owned_by_from"], m["pre_owned_by_from"], frm),
        ]
    )
    await db_session.flush()

    plan = await transfer_context_creator(
        db_session, from_user_id=frm, to_user_id=to, workspace_id=s["ws"].id, dry_run=True
    )
    by_ctx = {line.context_id: line.edges for line in plan.lines}
    assert (by_ctx[s["private_ctx"].id].moved, by_ctx[s["private_ctx"].id].dropped) == (6, 3)
    assert by_ctx[s["private_ctx"].id].replaced == 3
    assert by_ctx[s["shared_ctx"].id].moved == 0
    assert (plan.edges_moved, plan.edges_dropped) == (6, 3)
    # The prompt names moved and dropped edges the way the plan lines do.
    assert plan.summary() == (
        "2 context(s), 4 memory row(s), 6 edge(s), 3 duplicate edge(s) dropped"
    )
    # Dry run wrote nothing.
    assert (frm, a.id, b.id) in await _edges(db_session, s["private_ctx"].id)

    result = await transfer_context_creator(
        db_session, from_user_id=frm, to_user_id=to, workspace_id=s["ws"].id, dry_run=False
    )
    assert result.edges_moved == 6

    edges = await _edges(db_session, s["private_ctx"].id)
    assert set(edges) == {
        (to, a.id, b.id),
        (to, b.id, a.id),
        (to, a.id, o.id),
        (frm, a.id, t.id),  # Hebbian: left with the old account
        (to, b.id, o.id),
        (to, o.id, a.id),
        (to, o.id, b.id),
        (to, t.id, a.id),
        (to, t.id, b.id),
        (to, t.id, o.id),
        (s["other"].user_id, b.id, t.id),
    }
    assert edges[(to, a.id, b.id)].edge_type == EDGE_TYPE_SUPERSEDES
    assert edges[(to, b.id, a.id)].edge_type == EDGE_TYPE_RELATED_TO
    assert edges[(to, a.id, o.id)].origin == EDGE_ORIGIN_SEMANTIC
    assert edges[(frm, a.id, t.id)].origin == EDGE_ORIGIN_HEBBIAN
    # The supersedes replaced --to's related_to on the pair.
    assert edges[(to, b.id, o.id)].edge_type == EDGE_TYPE_SUPERSEDES
    assert edges[(to, b.id, o.id)].weight == 1.0
    # Same type on both sides, or an ordinary link: --to's own row survived
    # untouched and --from's duplicate is gone.
    assert edges[(to, t.id, b.id)].weight == 0.3
    assert edges[(to, t.id, o.id)].edge_type == EDGE_TYPE_DEPENDS_ON
    assert edges[(s["other"].user_id, b.id, t.id)].edge_type == EDGE_TYPE_SUPERSEDES
    # The declared edge took the place of --to's Hebbian row.
    assert edges[(to, o.id, a.id)].edge_type == EDGE_TYPE_SUPERSEDES
    assert edges[(to, o.id, a.id)].origin == EDGE_ORIGIN_DECLARED
    assert edges[(to, o.id, b.id)].edge_type == EDGE_TYPE_SUPERSEDES
    assert edges[(to, o.id, b.id)].origin == EDGE_ORIGIN_DECLARED
    assert edges[(to, t.id, a.id)].weight == 0.4
    # A context --to owned all along is out of scope.
    pre = m["pre_owned_by_from"]
    assert set(await _edges(db_session, s["pre_owned_ctx"].id)) == {(frm, pre.id, pre.id)}

    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.resource == f"context:{s['private_ctx'].id}")
    )
    assert audit.user_metadata["edges"] == 6
    assert audit.user_metadata["edges_dropped"] == 3
    assert audit.user_metadata["edges_replaced"] == 3

    # A second run, with and without the sweep, changes nothing.
    for repair in (False, True):
        again = await transfer_context_creator(
            db_session,
            from_user_id=frm,
            to_user_id=to,
            workspace_id=s["ws"].id,
            dry_run=False,
            repair_payloads=repair,
        )
        assert (again.transferred, again.edges_moved, again.repair_edges.total) == (0, 0, 0)
    assert set(await _edges(db_session, s["private_ctx"].id)) == set(edges)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("from_type", "to_type", "to_origin", "survivor"),
    [
        # Recall acts on supersedes / contradicts: never given up for another type.
        (EDGE_TYPE_SUPERSEDES, EDGE_TYPE_RELATED_TO, EDGE_ORIGIN_DECLARED, "from"),
        (EDGE_TYPE_CONTRADICTS, EDGE_TYPE_RELATED_TO, EDGE_ORIGIN_DECLARED, "from"),
        (EDGE_TYPE_SUPERSEDES, EDGE_TYPE_CONTRADICTS, EDGE_ORIGIN_DECLARED, "from"),
        # Same type on both sides: the new owner's row is the one that stays.
        (EDGE_TYPE_SUPERSEDES, EDGE_TYPE_SUPERSEDES, EDGE_ORIGIN_DECLARED, "to"),
        (EDGE_TYPE_CONTRADICTS, EDGE_TYPE_CONTRADICTS, EDGE_ORIGIN_SEMANTIC, "from"),
        # An ordinary link does not displace --to's supersedes.
        (EDGE_TYPE_RELATED_TO, EDGE_TYPE_SUPERSEDES, EDGE_ORIGIN_DECLARED, "to"),
    ],
)
async def test_edge_type_decides_a_collision_recall_depends_on(
    db_session, scenario, vector_store, from_type, to_type, to_origin, survivor
):
    """``--from`` holds a declared edge on a pair ``--to`` also holds one on.
    Shadowing and contradiction annotations follow ``edge_type``, so the
    transfer must not trade a ``supersedes`` / ``contradicts`` for another
    type; with the same type on both sides nothing is lost either way."""
    s, m = scenario, scenario["memories"]
    frm, to = s["cli_admin"].user_id, s["web_user"].user_id
    a, b = m["private_a"], m["private_b"]
    db_session.add_all(
        [
            _edge(a, b, frm, edge_type=from_type, weight=0.9),
            _edge(a, b, to, edge_type=to_type, origin=to_origin, weight=0.3),
        ]
    )
    await db_session.flush()

    result = await transfer_context_creator(
        db_session, from_user_id=frm, to_user_id=to, workspace_id=s["ws"].id, dry_run=False
    )

    edges = await _edges(db_session, s["private_ctx"].id)
    assert set(edges) == {(to, a.id, b.id)}
    row = edges[(to, a.id, b.id)]
    if survivor == "from":
        assert (row.edge_type, row.origin, row.weight) == (from_type, EDGE_ORIGIN_DECLARED, 0.9)
        assert (result.edges_moved, result.edges_dropped) == (1, 0)
    else:
        assert (row.edge_type, row.origin, row.weight) == (to_type, to_origin, 0.3)
        assert (result.edges_moved, result.edges_dropped) == (0, 1)


@pytest.mark.asyncio
async def test_an_automated_supersedes_does_not_delete_a_declared_edge(
    db_session, scenario, vector_store
):
    """``--from``'s sleep-discovered ``supersedes`` meets a link ``--to``
    declared by hand on the same pair: the declared row stays."""
    s, m = scenario, scenario["memories"]
    frm, to = s["cli_admin"].user_id, s["web_user"].user_id
    a, b = m["private_a"], m["private_b"]
    db_session.add_all(
        [
            _edge(a, b, frm, edge_type=EDGE_TYPE_SUPERSEDES, origin=EDGE_ORIGIN_SEMANTIC),
            _edge(a, b, to, edge_type=EDGE_TYPE_CONTRADICTS, weight=0.3),
        ]
    )
    await db_session.flush()

    result = await transfer_context_creator(
        db_session, from_user_id=frm, to_user_id=to, workspace_id=s["ws"].id, dry_run=False
    )

    edges = await _edges(db_session, s["private_ctx"].id)
    assert set(edges) == {(to, a.id, b.id)}
    row = edges[(to, a.id, b.id)]
    assert (row.edge_type, row.origin, row.weight) == (
        EDGE_TYPE_CONTRADICTS,
        EDGE_ORIGIN_DECLARED,
        0.3,
    )
    assert (result.edges_moved, result.edges_dropped) == (0, 1)


@pytest.mark.asyncio
async def test_repair_sweeps_edges_left_on_the_old_account(
    db_session, scenario, vector_store, cli_db, capsys
):
    """A transfer made before #1872, or an edge declared by ``--from`` after
    the flip, leaves non-Hebbian edges behind: the sweep moves them, counts
    them as planned work and records them on its audit row."""
    s, m = scenario, scenario["memories"]
    frm, to = s["cli_admin"].user_id, s["web_user"].user_id
    await transfer_context_creator(
        db_session, from_user_id=frm, to_user_id=to, workspace_id=s["ws"].id, dry_run=False
    )
    db_session.add_all(
        [
            _edge(m["private_a"], m["private_b"], frm, edge_type=EDGE_TYPE_SUPERSEDES),
            _edge(
                m["private_b"],
                m["private_a"],
                frm,
                edge_type=EDGE_TYPE_NEURAL_ASSOCIATION,
                origin=EDGE_ORIGIN_HEBBIAN,
            ),
        ]
    )
    await db_session.flush()

    argv = ["--from", frm, "--to", to, "--workspace", str(s["ws"].id), "--repair-payloads"]
    assert await _main(_parse(argv)) == 0
    out = capsys.readouterr().out
    assert "would move 1 edge(s) still held by" in out
    assert "dry run" in out
    assert (frm, m["private_a"].id, m["private_b"].id) in await _edges(
        db_session, s["private_ctx"].id
    )

    assert await _main(_parse([*argv, "--apply", "--yes"])) == 0
    out = capsys.readouterr().out
    assert "changed 1 edge(s), 3 vector payload repair(s)" in out
    assert set(await _edges(db_session, s["private_ctx"].id)) == {
        (to, m["private_a"].id, m["private_b"].id),
        (frm, m["private_b"].id, m["private_a"].id),
    }
    sweep = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == AUDIT_ACTION, AuditLog.resource == f"workspace:{s['ws'].id}"
        )
    )
    assert sweep.user_metadata["edges"] == 1 and sweep.user_metadata["memories"] == 0


@pytest.mark.asyncio
async def test_repair_runs_after_the_from_user_row_is_gone(
    db_session, scenario, vector_store, cli_db
):
    """The sweep needs the audit rows and the ``user_id`` string, not the
    ``users`` row: it still converges once the retired account was removed.
    ``--to`` is always required."""
    s = scenario
    retired = _user("retired")
    db_session.add(retired)
    await db_session.flush()
    ctx = _context(s["ws"], retired.user_id, private=True)
    db_session.add(ctx)
    await db_session.flush()
    first_memory = _memory(ctx, retired.user_id)
    db_session.add(first_memory)
    await db_session.flush()
    frm, to = retired.user_id, s["web_user"].user_id

    first = await transfer_context_creator(
        db_session, from_user_id=frm, to_user_id=to, workspace_id=s["ws"].id, dry_run=False
    )
    assert first.transferred_ids == [ctx.id]

    await db_session.execute(delete(User).where(User.user_id == frm))
    late = _memory(ctx, frm)
    db_session.add(late)
    await db_session.flush()
    # ...and an edge it declared after the flip: swept with the memory.
    db_session.add(_edge(late, first_memory, frm, edge_type=EDGE_TYPE_SUPERSEDES))
    await db_session.flush()

    argv = ["--from", frm, "--to", to, "--workspace", str(s["ws"].id), "--apply", "--yes"]
    # Without the sweep the --from row is still required: a typo must not
    # read as "nothing to transfer".
    assert await _main(_parse(argv)) == 1
    assert await _main(_parse([*argv, "--repair-payloads"])) == 0
    assert await _author(db_session, late.id) == to
    assert set(await _edges(db_session, ctx.id)) == {(to, late.id, first_memory.id)}

    with pytest.raises(ValueError, match="nobody_here"):
        await transfer_context_creator(
            db_session,
            from_user_id=frm,
            to_user_id="nobody_here",
            workspace_id=s["ws"].id,
            repair_payloads=True,
        )


@pytest.mark.asyncio
async def test_missing_point_of_an_unembedded_memory_is_not_a_failure(
    db_session, scenario, vector_store, cli_db, capsys
):
    """A memory whose embedding is pending or failed has no vector point, so
    the payload update raises on a store that rejects unknown ids. That is
    not a failure — the later embed writes the payload from the row — and it
    must not keep the command from exiting 0. A ``success`` memory's failure
    still does."""
    s, m = scenario, scenario["memories"]
    frm, to = s["cli_admin"].user_id, s["web_user"].user_id
    unembedded = _memory(s["private_ctx"], frm, embedding_status="failed")
    db_session.add(unembedded)
    await db_session.flush()
    raising = {unembedded.id}

    async def missing_point(*, memory_id, **_):
        if memory_id in raising:
            raise RuntimeError("no point with that id")

    vector_store.side_effect = missing_point
    argv = ["--from", frm, "--to", to, "--workspace", str(s["ws"].id), "--apply", "--yes"]
    assert await _main(_parse(argv)) == 0
    captured = capsys.readouterr()
    assert "NOT updated" not in captured.err
    assert "skipped 1 memor(ies) not embedded yet" in captured.out
    assert str(unembedded.id) in captured.out

    # The same raise for an embedded memory is a real failure: reported, exit 1.
    raising.add(m["private_a"].id)
    assert await _main(_parse([*argv, "--repair-payloads"])) == 1
    captured = capsys.readouterr()
    assert "vector payload NOT updated for 1 memor(ies)" in captured.err
    assert str(m["private_a"].id) in captured.err
    assert str(unembedded.id) not in captured.err


def test_help_covers_plan_mode_for_repair_payloads(capsys):
    with pytest.raises(SystemExit):
        _parse(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "planned without --apply, written with it" in out
    assert "with --apply:" not in out


def test_parse_requires_both_users_and_a_workspace():
    ws = uuid4()
    args = _parse(["--from", "local:admin", "--to", "123", "--workspace", str(ws), "--apply"])
    assert (
        args.from_user,
        args.to_user,
        args.workspace,
        args.apply,
        args.yes,
        args.repair_payloads,
    ) == ("local:admin", "123", ws, True, False, False)
    assert _parse(
        ["--from", "a", "--to", "b", "--workspace", str(ws), "--apply", "--repair-payloads"]
    ).repair_payloads
    with pytest.raises(SystemExit):
        _parse(["--from", "local:admin", "--to", "123"])  # no workspace
    with pytest.raises(SystemExit):
        _parse(["--from", "a", "--to", "b", "--workspace", str(ws), "--plan", "--apply"])
