"""The web dialog's supersede accept through ``PATCH /api/v1/memory/{id}`` (#2600), on Postgres.

``accept_supersede_candidate`` on the PATCH route runs the accept of
``update_memory(accept_supersede_candidate=true)`` (#2308): the ``supersedes``
edge is kept under the carrier's author, the account supersede detection looks
the pair's edge up under. A linked account accepting in a private context
(#1784) therefore leaves the edge where a re-embed finds it, and the
suggestion is not stored again. ``POST /graph/edges``, the dialog's writer
before, stays the generic edge writer and keeps the edge under the caller.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException, Response

from api.routes.graph import CreateEdgeRequest, create_graph_edge
from api.routes.memory import patch_memory as patch_route
from models.auth import Context, User
from models.memory import EDGE_TYPE_SUPERSEDES
from models.schemas import PatchMemoryRequest
from repositories.neural_edge import NeuralEdgeRepository
from services.memory_service import MemoryService, _detect_supersede_candidate
from services.supersede_dismissal import dismissed_entry
from tests.integration.test_2308_accept_supersede_candidate_db import (
    _edges,
    _pair,
    _row,
    _store,
)
from tests.integration.test_identity_link_writes_db import _seed


async def _patch(db, caller: User, memory_id: UUID, **body):
    """The dialog's request, through the route handler as the web calls it."""
    with patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()):
        return await patch_route(
            memory_id,
            PatchMemoryRequest.model_validate(body),
            {"user_id": caller.user_id},
            MemoryService(db),
        )


async def _detection_sees_the_edge(db, ctx: Context, author: User, carrier: UUID, target: UUID):
    """The lookup detection gates its write on: the pair's edge under the author."""
    edge = await NeuralEdgeRepository(db).get_edge(
        author.user_id,
        carrier,
        target,
        workspace_id=str(ctx.workspace_id),
        context_id=str(ctx.id),
    )
    return edge is not None and edge.edge_type == EDGE_TYPE_SUPERSEDES


async def _redetect(db, ctx: Context, carrier: UUID, target: UUID):
    """A re-embed's detection pass meeting the same neighbour again."""
    await _detect_supersede_candidate(
        db,
        memory=await _row(db, carrier),
        raw_top={"id": str(target), "score": 0.93, "payload": {}},
        edge_repo=NeuralEdgeRepository(db),
        config=SimpleNamespace(supersede_verify_mode="off"),
        workspace_id_str=str(ctx.workspace_id),
        context_id_str=str(ctx.id),
    )
    return (await _row(db, carrier)).supersede_candidate


@pytest.mark.asyncio
async def test_a_detection_pass_stores_the_pair_while_no_edge_answers_it(db_session):
    """The control for the re-embed assertions below: with no edge, it writes."""
    admin, _, _, ctx, _ = await _seed(db_session, private=True, link=True)
    carrier, target = await _pair(db_session, ctx, admin, admin)
    await _store(db_session, carrier, None)

    stored = await _redetect(db_session, ctx, carrier, target)

    assert stored is not None and stored["memory_id"] == str(target)


@pytest.mark.asyncio
async def test_a_linked_callers_web_accept_keeps_the_edge_under_the_author(db_session):
    """The issue's case: admin's memory in admin's private context, accepted
    from the web by oauth, the linked account."""
    admin, oauth, _, ctx, _ = await _seed(db_session, private=True, link=True)
    carrier, target = await _pair(db_session, ctx, admin, admin)
    before = await _row(db_session, carrier)
    updated_at, version = before.updated_at, before.version

    reply = await _patch(db_session, oauth, carrier, accept_supersede_candidate=True)
    await db_session.commit()

    edges = await _edges(db_session, carrier, target)
    assert [(e.edge_type, e.user_id) for e in edges] == [(EDGE_TYPE_SUPERSEDES, admin.user_id)]
    after = await _row(db_session, carrier)
    assert after.supersede_candidate is None
    assert after.version == version and after.updated_at == updated_at
    # The reply is the memory as it now stands: no suggestion, the new link.
    assert reply.supersede_candidate is None
    assert [link.memory_id for link in reply.outgoing_links] == [target]
    # The next re-embed finds the edge under the author and stores nothing.
    assert await _detection_sees_the_edge(db_session, ctx, admin, carrier, target)
    assert await _redetect(db_session, ctx, carrier, target) is None


@pytest.mark.asyncio
async def test_the_edge_route_keeps_its_edge_under_the_caller(db_session):
    """Contrast: ``POST /graph/edges`` is the generic writer, under the caller,
    so detection's lookup under the author misses it (only the store's final
    owner-agnostic guard, #2069, then keeps the suggestion from coming back)."""
    admin, oauth, _, ctx, _ = await _seed(db_session, private=True, link=True)
    carrier, target = await _pair(db_session, ctx, admin, admin)

    await create_graph_edge(
        CreateEdgeRequest(
            context_id=ctx.id, source_id=carrier, target_id=target, edge_type="supersedes"
        ),
        {"user_id": oauth.user_id},
        Response(),
        db_session,
    )
    await db_session.commit()

    edges = await _edges(db_session, carrier, target)
    assert [(e.edge_type, e.user_id) for e in edges] == [(EDGE_TYPE_SUPERSEDES, oauth.user_id)]
    assert not await _detection_sees_the_edge(db_session, ctx, admin, carrier, target)


@pytest.mark.asyncio
async def test_a_linked_account_does_not_count_in_a_shared_context(db_session):
    """Outside a private context the link set does not widen the owner rule:
    422, nothing written."""
    admin, oauth, _, ctx, _ = await _seed(db_session, private=False, link=True)
    carrier, target = await _pair(db_session, ctx, admin, admin)

    with pytest.raises(HTTPException) as exc:
        await _patch(db_session, oauth, carrier, accept_supersede_candidate=True)
    await db_session.rollback()

    assert exc.value.status_code == 422
    assert "on your own memory" in str(exc.value.detail)
    assert await _edges(db_session, carrier, target) == []
    assert (await _row(db_session, carrier)).supersede_candidate is not None


@pytest.mark.asyncio
async def test_an_accept_without_a_live_suggestion_is_a_422(db_session):
    admin, _, _, ctx, _ = await _seed(db_session, private=True, link=True)
    carrier, target = await _pair(db_session, ctx, admin, admin)
    await _store(db_session, carrier, None)

    with pytest.raises(HTTPException) as exc:
        await _patch(db_session, admin, carrier, accept_supersede_candidate=True)
    await db_session.rollback()

    assert exc.value.status_code == 422
    assert await _edges(db_session, carrier, target) == []


@pytest.mark.asyncio
async def test_a_linked_callers_web_dismissal_tombstones_the_pair(db_session):
    """The rejection half through the same route: the pair stays unlinked and
    the re-embed does not bring it back."""
    admin, oauth, _, ctx, _ = await _seed(db_session, private=True, link=True)
    carrier, target = await _pair(db_session, ctx, admin, admin)
    updated_at = (await _row(db_session, carrier)).updated_at

    reply = await _patch(db_session, oauth, carrier, dismiss_supersede_candidate=True)
    await db_session.commit()

    after = await _row(db_session, carrier)
    entry = dismissed_entry(after.supersede_candidate)
    assert entry is not None and entry["memory_id"] == str(target)
    assert after.updated_at == updated_at
    assert reply.supersede_candidate is None
    assert await _edges(db_session, carrier, target) == []
    assert dismissed_entry(await _redetect(db_session, ctx, carrier, target)) is not None
