"""Integration tests for ``cli/transfer_context_creator`` (#1783).

Needs a live Postgres (``TEST_DATABASE_URL``, ``*_test`` suffixed); the
``db_session`` fixture skips otherwise. Pins the acceptance bullets: the ops
command re-points only the named user's live contexts in the named workspace,
refuses a target that cannot see the workspace, writes nothing in dry-run,
leaves an audit row per moved context, and is a no-op when re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember, WorkspaceRole
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


@pytest_asyncio.fixture
async def scenario(db_session: AsyncSession):
    """Two workspaces. ``cli_admin`` created everything; ``web_user`` is a
    member of ``ws`` only. ``other`` owns one context that must not move."""
    cli_admin, web_user, other = _user("cli"), _user("web"), _user("other")
    ws = _workspace(cli_admin.user_id)
    far_ws = _workspace(cli_admin.user_id)
    db_session.add_all([cli_admin, web_user, other, ws, far_ws])
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=web_user.user_id, role=WorkspaceRole.MEMBER)
    )

    private_ctx = _context(ws, cli_admin.user_id, private=True)
    shared_ctx = _context(ws, cli_admin.user_id, private=False)
    deleted_ctx = _context(ws, cli_admin.user_id, private=False, deleted=True)
    others_ctx = _context(ws, other.user_id, private=True)
    far_ctx = _context(far_ws, cli_admin.user_id, private=True)
    db_session.add_all([private_ctx, shared_ctx, deleted_ctx, others_ctx, far_ctx])
    await db_session.flush()

    return {
        "cli_admin": cli_admin,
        "web_user": web_user,
        "other": other,
        "ws": ws,
        "far_ws": far_ws,
        "private_ctx": private_ctx,
        "shared_ctx": shared_ctx,
        "deleted_ctx": deleted_ctx,
        "others_ctx": others_ctx,
        "far_ctx": far_ctx,
    }


async def _created_by(db: AsyncSession, context_id) -> str | None:
    return await db.scalar(select(Context.created_by).where(Context.id == context_id))


@pytest.mark.asyncio
async def test_dry_run_plans_the_live_contexts_and_writes_nothing(db_session, scenario):
    s = scenario
    result = await transfer_context_creator(
        db_session,
        from_user_id=s["cli_admin"].user_id,
        to_user_id=s["web_user"].user_id,
        workspace_id=s["ws"].id,
        dry_run=True,
    )

    assert result.transferred == 2
    assert set(result.transferred_ids) == {s["private_ctx"].id, s["shared_ctx"].id}
    assert result.skipped == 0
    # Nothing written: every row still names the CLI admin.
    for key in ("private_ctx", "shared_ctx", "deleted_ctx", "far_ctx"):
        assert await _created_by(db_session, s[key].id) == s["cli_admin"].user_id
    assert (
        await db_session.scalar(select(AuditLog.id).where(AuditLog.action == AUDIT_ACTION)) is None
    )


@pytest.mark.asyncio
async def test_apply_moves_only_the_named_users_live_contexts_in_the_workspace(
    db_session, scenario
):
    s = scenario
    result = await transfer_context_creator(
        db_session,
        from_user_id=s["cli_admin"].user_id,
        to_user_id=s["web_user"].user_id,
        workspace_id=s["ws"].id,
        dry_run=False,
    )

    assert result.transferred == 2
    assert await _created_by(db_session, s["private_ctx"].id) == s["web_user"].user_id
    assert await _created_by(db_session, s["shared_ctx"].id) == s["web_user"].user_id
    # Out of scope: deleted, another creator, another workspace.
    assert await _created_by(db_session, s["deleted_ctx"].id) == s["cli_admin"].user_id
    assert await _created_by(db_session, s["others_ctx"].id) == s["other"].user_id
    assert await _created_by(db_session, s["far_ctx"].id) == s["cli_admin"].user_id

    # One audit row per moved context, naming both identities.
    rows = list(
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)))
        .scalars()
        .all()
    )
    assert {r.resource for r in rows} == {
        f"context:{s['private_ctx'].id}",
        f"context:{s['shared_ctx'].id}",
    }
    assert all(
        r.user_metadata["from_user_id"] == s["cli_admin"].user_id
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
    assert again.scanned == 0


@pytest.mark.asyncio
async def test_target_outside_the_workspace_is_refused_per_context(db_session, scenario):
    """A private context handed to someone who cannot see the workspace would
    be visible to nobody — the run reports it as skipped and moves nothing."""
    s = scenario
    result = await transfer_context_creator(
        db_session,
        from_user_id=s["cli_admin"].user_id,
        to_user_id=s["web_user"].user_id,
        workspace_id=s["far_ws"].id,
        dry_run=False,
    )

    assert result.scanned == 1
    assert result.transferred == 0
    assert result.lines[0].action == "skip"
    assert "not a member" in (result.lines[0].reason or "")
    assert await _created_by(db_session, s["far_ctx"].id) == s["cli_admin"].user_id


@pytest.mark.asyncio
async def test_workspace_owner_counts_as_able_to_see(db_session, scenario):
    """The owner of a workspace need not hold a member row to receive contexts."""
    s = scenario
    result = await transfer_context_creator(
        db_session,
        from_user_id=s["other"].user_id,
        to_user_id=s["cli_admin"].user_id,  # owns ws, has no member row
        workspace_id=s["ws"].id,
        dry_run=True,
    )
    assert result.transferred == 1
    assert result.transferred_ids == [s["others_ctx"].id]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["from", "to"])
async def test_unknown_user_is_an_error(db_session, scenario, missing):
    s = scenario
    kwargs = {
        "from_user_id": s["cli_admin"].user_id,
        "to_user_id": s["web_user"].user_id,
    }
    kwargs[f"{missing}_user_id"] = "nobody_here"
    with pytest.raises(ValueError, match="nobody_here"):
        await transfer_context_creator(db_session, workspace_id=s["ws"].id, **kwargs)


@pytest.mark.asyncio
async def test_same_user_is_an_error(db_session, scenario):
    s = scenario
    with pytest.raises(ValueError, match="same user"):
        await transfer_context_creator(
            db_session,
            from_user_id=s["cli_admin"].user_id,
            to_user_id=s["cli_admin"].user_id,
            workspace_id=s["ws"].id,
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
