"""``supersede_candidate.acceptance`` on Postgres (#2427).

``reference()`` and the PATCH reply tell the caller whether they may accept a
memory's supersede suggestion, from the rule the accept path enforces: write
access to the context, both memories the caller's own (#1803/#2313) and a
candidate not written after its carrier (#2308). Recall does not evaluate it.

The parity matrix pins ``allowed`` against the edge writer's own refusal
(``ensure_supersedes_owned``) over the #2313 author matrix, and the viewer
case against ``POST /graph/edges``. #2518 adds the agent binding (a read-only
one is ``not_writable``, as the accept path's 404 says) and ``edge_exists``
(a different declared edge on the pair, the accept path's 409). #2600 adds the
web dialog's accept, ``PATCH /memory/{id}`` with ``accept_supersede_candidate``,
to the matrix: the same accept path, so the same answers.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import text, update

from api.routes.graph import CreateEdgeRequest, create_graph_edge
from api.routes.memory import patch_memory as patch_route
from auth.agent_scope import AgentScope, set_agent_scope
from models.agent import Agent, AgentContextBinding
from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from models.schemas import PatchMemoryRequest, RecallRequest
from services.edge_service import create_declared_edge, ensure_supersedes_owned
from services.identity_link_service import IdentityLinkService
from services.memory_service import MemoryService
from services.plan_seats import slot_limit
from services.supersede_acceptance import ACCEPT_CONFIDENCE, ACCEPT_WEIGHT
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
from utils.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundException,
    SupersedeEdgeConflictError,
)


async def _acceptance(db, caller: User, memory_id):
    candidate = (await MemoryService(db).reference(memory_id, caller.user_id)).supersede_candidate
    assert candidate is not None, "the suggestion itself must surface"
    return candidate.acceptance


async def _rest_accept(db, caller: User, _ctx: Context, memory_id):
    """The web dialog's accept (#2600): ``PATCH /memory/{id}``, the route handler."""
    with patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()):
        return await patch_route(
            memory_id,
            PatchMemoryRequest(accept_supersede_candidate=True),
            {"user_id": caller.user_id},
            MemoryService(db),
        )


# The two transports of the one accept path: update_memory and the PATCH route.
_ACCEPTS = {"update_memory": _accept_as, "patch": _rest_accept}


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
@pytest.mark.parametrize(
    "authors",
    ["own", "foreign_source", "foreign_target", "foreign_both", "linked_private", "linked_shared"],
)
async def test_allowed_iff_the_web_accept_goes_through(db_session, authors):
    """#2600: the same author matrix through ``PATCH /memory/{id}``: ``allowed``
    ⇔ a 200 with the edge under the carrier's author; a refusal is the 422."""
    from tests.integration.test_2308_accept_supersede_candidate_db import _edges

    private = authors == "linked_private"
    a, b, _, ctx, _ = await _seed(db_session, private=private, link=authors.startswith("linked"))
    source_author = a if authors in {"foreign_source", "foreign_both", "linked_private"} else b
    target_author = b if authors in {"own", "foreign_source"} else a
    src, dst = await _pair(db_session, ctx, source_author, target_author)

    acceptance = await _acceptance(db_session, b, src)
    try:
        await _rest_accept(db_session, b, ctx, src)
        await db_session.commit()
        accepted = True
    except HTTPException as exc:
        assert exc.status_code == 422
        await db_session.rollback()
        accepted = False

    assert acceptance.allowed is accepted
    owners = [e.user_id for e in await _edges(db_session, src, dst)]
    assert owners == ([source_author.user_id] if accepted else [])


async def _bind(db, owner: User, ws: Workspace, ctx: Context, *, mode: str, write_policy: str):
    """An agent of ``owner``'s bound to ``ctx``, as the request's credential would set it."""
    agent = Agent(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"agent-{uuid4().hex[:6]}",
        owner_user_id=owner.user_id,
        status="active",
        enforcement_mode=mode,
    )
    db.add(agent)
    await db.flush()
    db.add(
        AgentContextBinding(
            id=uuid4(),
            agent_id=agent.id,
            context_id=ctx.id,
            created_by=owner.user_id,
            can_read=True,
            write_policy=write_policy,
        )
    )
    await db.commit()
    return AgentScope(agent_id=agent.id, enforcement_mode=mode, workspace_id=ws.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "write_policy", "allowed"),
    [
        ("enforce", "deny", False),
        ("enforce", "direct", True),
        # The shadow ramp lets the write proceed, so the accept goes through.
        ("shadow", "deny", True),
    ],
    ids=["read_only", "writable", "shadow_read_only"],
)
async def test_an_agent_binding_decides_writable_as_the_accept_path_does(
    db_session, mode, write_policy, allowed
):
    """#2518: a read-only binding is ``not_writable`` before the click, and the
    accept path refuses it (the uniform 404). The probe is a read: it writes
    no deny-audit row, whatever the binding says."""
    a, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, _ = await _pair(db_session, ctx, a, a)
    scope = await _bind(db_session, a, ws, ctx, mode=mode, write_policy=write_policy)

    emit = AsyncMock()
    set_agent_scope(scope)
    try:
        with patch("services.memory_access_event_writer.emit_memory_access_event", emit):
            acceptance = await _acceptance(db_session, a, carrier)
        # The reference read itself is audited for an agent; no deny decision is.
        decisions = [call.kwargs.get("policy_decision") for call in emit.await_args_list]
        assert not {"binding_denied", "would_deny"} & set(decisions), decisions
        assert acceptance.allowed is allowed
        assert acceptance.reason == (None if allowed else "not_writable")

        if allowed:
            with patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()):
                await _accept_as(db_session, a, ctx, carrier)
            await db_session.commit()
            assert (await _row(db_session, carrier)).supersede_candidate is None
        else:
            with pytest.raises(NotFoundException):
                await _accept_as(db_session, a, ctx, carrier)
            await db_session.rollback()
            assert (await _row(db_session, carrier)).supersede_candidate is not None
    finally:
        set_agent_scope(None)


async def _declare(db, owner: User, ws: Workspace, ctx: Context, src, dst, **values) -> None:
    await create_declared_edge(
        db,
        user_id=owner.user_id,
        source_id=src,
        target_id=dst,
        workspace_id=str(ws.id),
        context_id=str(ctx.id),
        overwrite=False,
        **values,
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        {"edge_type": "related_to", "weight": 1.0, "confidence": 1.0},
        {"edge_type": "supersedes", "weight": 0.5, "confidence": 1.0},
    ],
    ids=["other_type", "other_weight"],
)
@pytest.mark.parametrize("transport", sorted(_ACCEPTS))
async def test_a_different_declared_edge_is_edge_exists_and_the_accept_conflicts(
    db_session, values, transport
):
    """#2518: the 409 the click would meet is reported before it."""
    a, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, ctx, a, a)
    await _declare(db_session, a, ws, ctx, carrier, target, **values)
    # Declaring a supersedes edge clears the suggestion it answers; put it back.
    if values["edge_type"] == "supersedes":
        await _store(db_session, carrier, _candidate(target))

    acceptance = await _acceptance(db_session, a, carrier)

    assert acceptance.model_dump(exclude_none=True) == {"allowed": False, "reason": "edge_exists"}
    with pytest.raises(SupersedeEdgeConflictError):
        await _ACCEPTS[transport](db_session, a, ctx, carrier)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_the_same_declared_supersedes_edge_is_still_allowed(db_session):
    """The accept path's own values are ``unchanged``, not a conflict."""
    a, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, ctx, a, a)
    await _declare(
        db_session,
        a,
        ws,
        ctx,
        carrier,
        target,
        edge_type="supersedes",
        weight=ACCEPT_WEIGHT,
        confidence=ACCEPT_CONFIDENCE,
    )
    await _store(db_session, carrier, _candidate(target))

    acceptance = await _acceptance(db_session, a, carrier)

    assert acceptance.allowed is True and acceptance.reason is None


async def _post_supersedes(db, caller: User, ctx: Context, carrier, target) -> None:
    """The web dialog's accept: ``POST /graph/edges`` as the caller."""
    await create_graph_edge(
        CreateEdgeRequest(
            context_id=ctx.id, source_id=carrier, target_id=target, edge_type="supersedes"
        ),
        {"user_id": caller.user_id},
        Response(),
        db,
    )


_OTHER = {"edge_type": "related_to", "weight": 0.5, "confidence": 1.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("behind", [False, True], ids=["alone", "behind_the_authors_clean_row"])
async def test_the_linked_callers_own_edge_is_edge_exists_in_a_private_context(db_session, behind):
    """#2549: the carrier is admin's, the caller oauth (linked). oauth's own
    different declared edge on the pair is the one the web dialog's write (under
    the caller) meets: reported before the click, not only as its 409. Behind
    admin's own accept-valued row too, which a single strongest-row read
    returns in its place."""
    admin, oauth, ws, ctx, _ = await _seed(db_session, private=True, link=True)
    carrier, target = await _pair(db_session, ctx, admin, admin)
    if behind:
        await _declare(
            db_session,
            admin,
            ws,
            ctx,
            carrier,
            target,
            edge_type="supersedes",
            weight=ACCEPT_WEIGHT,
            confidence=ACCEPT_CONFIDENCE,
        )
    await _declare(db_session, oauth, ws, ctx, carrier, target, **_OTHER)
    # Declaring a supersedes edge clears the suggestion it answers; put it back.
    await _store(db_session, carrier, _candidate(target))

    acceptance = await _acceptance(db_session, oauth, carrier)

    assert acceptance.model_dump(exclude_none=True) == {"allowed": False, "reason": "edge_exists"}
    with pytest.raises(ConflictError):
        await _post_supersedes(db_session, oauth, ctx, carrier, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", sorted(_ACCEPTS))
async def test_the_authors_edge_is_edge_exists_for_a_linked_caller(db_session, transport):
    """#2549: admin's different declared edge is what the accept path (under
    the carrier's author) meets when oauth accepts — from the web too (#2600)."""
    admin, oauth, ws, ctx, _ = await _seed(db_session, private=True, link=True)
    carrier, target = await _pair(db_session, ctx, admin, admin)
    await _declare(db_session, admin, ws, ctx, carrier, target, **_OTHER)

    acceptance = await _acceptance(db_session, oauth, carrier)

    assert acceptance.model_dump(exclude_none=True) == {"allowed": False, "reason": "edge_exists"}
    with pytest.raises(SupersedeEdgeConflictError):
        await _ACCEPTS[transport](db_session, oauth, ctx, carrier)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_a_shared_context_reads_only_the_authors_edge(db_session):
    """#2549: outside a private context a linked account's edge is its own,
    not the author's: the accept (under the author) writes past it."""
    admin, oauth, ws, ctx, _ = await _seed(db_session, private=False, link=True)
    carrier, target = await _pair(db_session, ctx, admin, admin)
    await _declare(db_session, oauth, ws, ctx, carrier, target, **_OTHER)

    acceptance = await _acceptance(db_session, admin, carrier)

    assert acceptance.allowed is True and acceptance.reason is None
    with patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()):
        await _accept_as(db_session, admin, ctx, carrier)
    await db_session.commit()
    assert (await _row(db_session, carrier)).supersede_candidate is None


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["author", "linked"])
@pytest.mark.parametrize("writer", ["accept", "patch", "post"])
async def test_another_linked_accounts_edge_is_allowed_in_a_private_context(
    db_session, caller, writer
):
    """#2549: the probe reads the carrier author's row and the caller's row,
    the two accounts an accept writes under. A different declared edge kept by
    a third linked account is met by neither write: ``allowed``, and both the
    accept (under the author; from MCP or, #2600, the web) and ``POST
    /graph/edges`` (under the caller) go through."""
    admin, oauth, ws, ctx, _ = await _seed(db_session, private=True, link=True)
    third = await _user(db_session, "github")
    await IdentityLinkService(db_session).link(admin.user_id, third.user_id)
    await db_session.commit()
    carrier, target = await _pair(db_session, ctx, admin, admin)
    accepter, holder = (admin, oauth) if caller == "author" else (oauth, third)
    await _declare(db_session, holder, ws, ctx, carrier, target, **_OTHER)

    acceptance = await _acceptance(db_session, accepter, carrier)

    assert acceptance.allowed is True and acceptance.reason is None
    if writer == "accept":
        with patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()):
            await _accept_as(db_session, accepter, ctx, carrier)
    elif writer == "patch":
        await _rest_accept(db_session, accepter, ctx, carrier)
    else:
        await _post_supersedes(db_session, accepter, ctx, carrier, target)
    await db_session.commit()
    assert (await _row(db_session, carrier)).supersede_candidate is None


@pytest.mark.asyncio
async def test_ownership_wins_over_edge_exists(db_session):
    """Precedence ``not_owner`` > ``edge_exists``: the accept path refuses in that order."""
    a, b, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, ctx, b, a)
    await _declare(
        db_session, b, ws, ctx, carrier, target, edge_type="related_to", weight=1.0, confidence=1.0
    )

    acceptance = await _acceptance(db_session, b, carrier)

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
    """Fail-open like the resolver: the client behaves as before, and a failed
    query rolls back only its savepoint, so the session stays usable."""
    a, _, _, ctx, _ = await _seed(db_session, private=False, link=False)
    carrier, target = await _pair(db_session, ctx, a, a)

    async def failing_query(db, **_kwargs):
        await db.execute(text("SELECT 1 / 0"))

    with patch("services.supersede_acceptance.pair_ownership", failing_query):
        response = await MemoryService(db_session).reference(carrier, a.user_id)

    assert response.supersede_candidate is not None
    assert response.supersede_candidate.memory_id == target
    assert response.supersede_candidate.acceptance is None
    assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1
    await db_session.commit()
