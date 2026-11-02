"""``supersede_candidate.acceptance`` on Postgres (#2427).

``reference()`` and the PATCH reply tell the caller whether they may accept a
memory's supersede suggestion, from the rule the accept path enforces: write
access to the context, both memories the caller's own (#1803/#2313) and a
candidate not written after its carrier (#2308). Recall does not evaluate it.

The parity matrix pins ``allowed`` against the edge writer's own refusal
(``ensure_supersedes_owned``) over the #2313 author matrix, and the viewer
case against ``POST /graph/edges``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import Response
from sqlalchemy import update

from api.routes.graph import CreateEdgeRequest, create_graph_edge
from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from models.schemas import RecallRequest
from services.edge_service import ensure_supersedes_owned
from services.memory_service import MemoryService
from services.plan_seats import slot_limit
from services.supersede_dismissal import build_tombstone
from tests.integration.test_2308_accept_supersede_candidate_db import (
    _accept_as,
    _candidate,
    _pair,
    _row,
    _store,
)
from tests.integration.test_identity_link_writes_db import _seed
from tests.integration.test_identity_links_db import _user
from utils.datetime import utcnow
from utils.exceptions import AuthorizationError, NotFoundException


async def _acceptance(db, caller: User, memory_id):
    candidate = (await MemoryService(db).reference(memory_id, caller.user_id)).supersede_candidate
    assert candidate is not None, "the suggestion itself must surface"
    return candidate.acceptance


async def _viewer(db, workspace: Workspace) -> User:
    viewer = await _user(db, "viewer")
    db.add(
        WorkspaceMember(
            workspace_id=workspace.id, user_id=viewer.user_id, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    return viewer


@pytest.mark.asyncio
async def test_owner_of_both_may_accept(db_session):
    a, _, _, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, _ = await _pair(db_session, ctx, a, a)

    acceptance = await _acceptance(db_session, a, carrier)

    assert acceptance is not None
    assert acceptance.allowed is True
    assert acceptance.reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["source", "target"])
async def test_a_foreign_end_is_not_owner(db_session, foreign):
    """Shared context, caller B: one end by A. The reason never says which."""
    a, b, _, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, _ = await _pair(
        db_session, ctx, a if foreign == "source" else b, b if foreign == "source" else a
    )

    acceptance = await _acceptance(db_session, b, carrier)

    assert acceptance.model_dump(exclude_none=True) == {"allowed": False, "reason": "not_owner"}


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False], ids=["private", "shared"])
async def test_a_linked_account_counts_only_in_a_private_context(db_session, private):
    admin, oauth, _, ctx, _ = await _seed(db_session, private=private, link=True)
    carrier, _ = await _pair(db_session, ctx, admin, admin)

    acceptance = await _acceptance(db_session, oauth, carrier)

    if private:
        assert acceptance.allowed is True and acceptance.reason is None
    else:
        assert acceptance.allowed is False and acceptance.reason == "not_owner"


@pytest.mark.asyncio
async def test_a_viewer_is_not_writable_and_the_edge_route_agrees(db_session):
    a, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    viewer = await _viewer(db_session, ws)
    carrier, target = await _pair(db_session, ctx, a, a)

    acceptance = await _acceptance(db_session, viewer, carrier)

    assert acceptance.model_dump(exclude_none=True) == {
        "allowed": False,
        "reason": "not_writable",
    }
    with pytest.raises(NotFoundException):
        await create_graph_edge(
            CreateEdgeRequest(
                context_id=ctx.id, source_id=carrier, target_id=target, edge_type="supersedes"
            ),
            {"user_id": viewer.user_id},
            Response(),
            db_session,
        )


@pytest.mark.asyncio
async def test_a_context_without_a_writable_slot_is_not_writable(db_session):
    """#1943: the oldest contexts keep their slots; a write beyond them is refused."""
    a, _, ws, _, _ = await _seed(db_session, private=False, link=False)
    ws.plan_name = "free"
    await db_session.flush()
    for i in range(slot_limit(ws) + 1):
        db_session.add(
            Context(
                id=uuid4(),
                workspace_id=ws.id,
                name=f"older_{i}_{uuid4().hex[:6]}",
                created_by=a.user_id,
                is_private=False,
                created_at=utcnow() - timedelta(days=30 - i),
            )
        )
    suspended = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"beyond_{uuid4().hex[:6]}",
        created_by=a.user_id,
        is_private=False,
    )
    db_session.add(suspended)
    await db_session.commit()
    carrier, _ = await _pair(db_session, suspended, a, a)

    acceptance = await _acceptance(db_session, a, carrier)

    assert acceptance.allowed is False
    assert acceptance.reason == "not_writable"


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["deleted", "dismissed"])
async def test_a_stale_candidate_is_not_surfaced(db_session, stale):
    a, _, _, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, ctx, a, a)
    if stale == "deleted":
        await db_session.execute(
            update(Memory).where(Memory.id == target).values(deleted_at=utcnow())
        )
        await db_session.commit()
    else:
        await _store(db_session, carrier, build_tombstone(_candidate(target)))

    response = await MemoryService(db_session).reference(carrier, a.user_id)

    assert response.supersede_candidate is None


@pytest.mark.asyncio
async def test_a_newer_candidate_is_reported_and_still_refused_on_accept(db_session):
    a, _, _, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, ctx, a, a)
    await db_session.execute(
        update(Memory).where(Memory.id == target).values(created_at=utcnow() + timedelta(days=1))
    )
    await db_session.commit()

    acceptance = await _acceptance(db_session, a, carrier)

    assert acceptance.model_dump(exclude_none=True) == {
        "allowed": False,
        "reason": "newer_candidate",
    }
    with pytest.raises(ValueError, match="written after it"):
        await _accept_as(db_session, a, ctx, carrier)
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authors",
    ["own", "foreign_source", "foreign_target", "foreign_both", "linked_private", "linked_shared"],
)
async def test_allowed_iff_the_edge_writer_accepts_the_pair(db_session, authors):
    """The #2313 author matrix: ``allowed`` ⇔ ``ensure_supersedes_owned`` passes."""
    private = authors == "linked_private"
    a, b, ws, ctx, _ = await _seed(db_session, private=private, link=authors.startswith("linked"))
    source_author = a if authors in {"foreign_source", "foreign_both", "linked_private"} else b
    target_author = b if authors in {"own", "foreign_source"} else a
    src, dst = await _pair(db_session, ctx, source_author, target_author)

    acceptance = await _acceptance(db_session, b, src)
    try:
        await ensure_supersedes_owned(
            db_session,
            user_id=b.user_id,
            source_id=src,
            target_id=dst,
            workspace_id=str(ws.id),
            context_id=str(ctx.id),
        )
        writer_accepts = True
    except AuthorizationError:
        writer_accepts = False

    assert acceptance.allowed is writer_accepts
    assert writer_accepts is (authors in {"own", "linked_private"})
    if not acceptance.allowed:
        assert acceptance.reason == "not_owner"


@pytest.mark.asyncio
async def test_patch_reply_and_mcp_reference_carry_it_recall_does_not(db_session):
    a, b, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, _ = await _pair(db_session, ctx, b, a)
    service = MemoryService(db_session)

    referenced = (await service.reference(carrier, b.user_id)).supersede_candidate
    patched = (
        await service._patch_build_response(await _row(db_session, carrier), b.user_id)
    ).supersede_candidate
    assert referenced is not None and patched is not None
    assert patched.acceptance == referenced.acceptance
    assert referenced.acceptance.reason == "not_owner"

    async def get_db():
        yield db_session

    from mcp_server.tools.memory import handle_reference

    with (
        patch("db.base.get_db", get_db),
        patch("mcp_server.tools.memory._log_tool_usage", AsyncMock()),
    ):
        reply = json.loads(
            (
                await handle_reference(
                    {"context_id": str(ctx.id), "memory_id": str(carrier)}, b.user_id, ws.id
                )
            )[0].text
        )
    assert reply["memory"]["supersede_candidate"]["acceptance"] == {
        "allowed": False,
        "reason": "not_owner",
    }

    recall_service = MemoryService(db_session)
    recall_service.search_service.hybrid_search = AsyncMock(
        return_value=[{"id": str(carrier), "score": 0.9}]
    )
    recalled = await recall_service.recall(
        RecallRequest(query="a summary", k=5),
        user_id=b.user_id,
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    row = next(r for r in recalled.results if r.memory_id == carrier)
    assert row.supersede_candidate is not None
    assert row.supersede_candidate.acceptance is None
    assert "acceptance" not in row.model_dump(exclude_none=True)["supersede_candidate"]


@pytest.mark.asyncio
async def test_a_failing_evaluation_keeps_the_suggestion_without_acceptance(db_session):
    """Fail-open like the resolver: the client behaves as before."""
    a, _, _, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, ctx, a, a)

    with patch(
        "services.supersede_acceptance.evaluate", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        response = await MemoryService(db_session).reference(carrier, a.user_id)

    assert response.supersede_candidate is not None
    assert response.supersede_candidate.memory_id == target
    assert response.supersede_candidate.acceptance is None
