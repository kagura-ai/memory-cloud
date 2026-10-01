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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember
from models.memory import Memory
from utils.datetime import utcnow

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli.transfer_context_creator import (  # noqa: E402
    AUDIT_ACTION,
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


def _memory(ctx: Context, author: str, *, deleted: bool = False) -> Memory:
    return Memory(
        id=uuid4(),
        user_id=author,
        workspace_id=ctx.workspace_id,
        context_id=ctx.id,
        summary="s",
        content="c",
        type="note",
        client="test",
        deleted_at=utcnow() if deleted else None,
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
    db_session.add_all([private_ctx, shared_ctx, deleted_ctx, others_ctx, far_ctx])
    await db_session.flush()

    memories = {
        "private_a": _memory(private_ctx, cli_admin.user_id),
        "private_b": _memory(private_ctx, cli_admin.user_id),
        "private_tombstone": _memory(private_ctx, cli_admin.user_id, deleted=True),
        "private_by_other": _memory(private_ctx, other.user_id),
        "shared_a": _memory(shared_ctx, cli_admin.user_id),
        "far_a": _memory(far_ctx, cli_admin.user_id),
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
    assert counts == {s["private_ctx"].id: 2, s["shared_ctx"].id: 1}  # live, by --from
    assert result.memories == 3
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


def test_parse_requires_both_users_and_a_workspace():
    ws = uuid4()
    args = _parse(["--from", "local:admin", "--to", "123", "--workspace", str(ws), "--apply"])
    assert (args.from_user, args.to_user, args.workspace, args.apply, args.yes) == (
        "local:admin",
        "123",
        ws,
        True,
        False,
    )
    with pytest.raises(SystemExit):
        _parse(["--from", "local:admin", "--to", "123"])  # no workspace
    with pytest.raises(SystemExit):
        _parse(["--from", "a", "--to", "b", "--workspace", str(ws), "--plan", "--apply"])
