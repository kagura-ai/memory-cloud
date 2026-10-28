"""#2313: explicit supersedes writes share the existing owner rule."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.tools.edge import handle_create_edge, handle_update_edge
from mcp_server.tools.memory import handle_update_memory
from models.memory import Memory, NeuralMemoryEdge
from repositories.neural_edge import NeuralEdgeRepository
from services.edge_service import create_declared_edge
from tests.integration.test_2308_accept_supersede_candidate_db import _pair, _row
from tests.integration.test_identity_link_writes_db import _seed, _shared_context
from utils.datetime import utcnow
from utils.exceptions import AuthorizationError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["service", "rest", "create", "overwrite", "unchanged", "retype", "weight"]
)
@pytest.mark.parametrize(
    "authors",
    ["own", "foreign_source", "foreign_target", "foreign_both", "linked_private", "linked_shared"],
)
async def test_supersedes_owner_boundary(db_session, path, authors):
    private = authors == "linked_private"
    a, b, ws, ctx, _ = await _seed(db_session, private=private, link=authors.startswith("linked"))
    source_author = a if authors in {"foreign_source", "foreign_both", "linked_private"} else b
    target_author = b if authors in {"own", "foreign_source"} else a
    src, dst = await _pair(db_session, ctx, source_author, target_author)
    caller, ws_id, ctx_id = b.user_id, ws.id, ctx.id
    allowed = authors in {"own", "linked_private"}
    denial_message = (
        "A supersedes edge requires both memories to be your own "
        "(linked accounts count only in a private context)."
    )
    repo = NeuralEdgeRepository(db_session)
    before_type = "supersedes" if path in {"unchanged", "weight"} else "related_to"
    if path in {"overwrite", "unchanged", "retype", "weight"}:
        await repo.create_or_update_edge(
            caller,
            src,
            dst,
            edge_type=before_type,
            weight=1.0,
            workspace_id=str(ws_id),
            context_id=str(ctx_id),
            origin="declared",
        )
        await db_session.commit()

    async def get_db():
        yield db_session

    async def invoke():
        if path == "rest":
            from fastapi import Response

            from api.routes.graph import CreateEdgeRequest, create_graph_edge

            return await create_graph_edge(
                CreateEdgeRequest(
                    context_id=ctx_id, source_id=src, target_id=dst, edge_type="supersedes"
                ),
                {"user_id": caller},
                Response(),
                db_session,
            )
        if path == "service":
            return await create_declared_edge(
                db_session,
                user_id=caller,
                source_id=src,
                target_id=dst,
                edge_type="supersedes",
                weight=1.0,
                confidence=1.0,
                workspace_id=str(ws_id),
                context_id=str(ctx_id),
                overwrite=False,
            )
        args = {"context_id": str(ctx_id), "source_id": str(src), "target_id": str(dst)}
        args.update({"weight": 0.8} if path == "weight" else {"edge_type": "supersedes"})
        if path == "overwrite":
            args["overwrite"] = True
        handler = handle_update_edge if path in {"retype", "weight"} else handle_create_edge
        with (
            patch("db.base.get_db", get_db),
            patch("mcp_server.tools.edge._log_tool_usage", AsyncMock()),
        ):
            return await handler(args, caller, ws_id)

    if allowed:
        await invoke()
        await db_session.commit()
        assert (await _row(db_session, src)).supersede_candidate is None
    else:
        if path in {"service", "rest"}:
            with pytest.raises(AuthorizationError) as exc:
                await invoke()
            assert exc.value.message == denial_message
            assert exc.value.details == {}
        else:
            response = json.loads((await invoke())[0].text)
            assert response["error"] == "permission_denied"
            assert response["message"] == denial_message
        await db_session.rollback()
        assert (await _row(db_session, src)).supersede_candidate is not None

    edge = (
        await db_session.execute(
            select(NeuralMemoryEdge).where(
                NeuralMemoryEdge.src_id == src,
                NeuralMemoryEdge.dst_id == dst,
            )
        )
    ).scalar_one_or_none()
    if allowed:
        assert edge.edge_type == "supersedes"
    elif path in {"service", "rest", "create"}:
        assert edge is None
    else:
        assert edge.edge_type == before_type
        assert edge.weight == 1.0


async def _call_mcp(db, handler, caller, workspace_id, **args):
    async def get_db():
        yield db

    with (
        patch("db.base.get_db", get_db),
        patch("mcp_server.tools.edge._log_tool_usage", AsyncMock()),
        patch("mcp_server.tools.memory._log_tool_usage", AsyncMock()),
    ):
        return json.loads((await handler(args, caller, workspace_id))[0].text)


@pytest.mark.asyncio
@pytest.mark.parametrize("authors", ["foreign_source", "foreign_target", "foreign_both"])
async def test_foreign_pair_can_be_retyped_away_from_supersedes(db_session, authors):
    a, b, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    src, dst = await _pair(
        db_session,
        ctx,
        a if authors in {"foreign_source", "foreign_both"} else b,
        a if authors in {"foreign_target", "foreign_both"} else b,
    )
    caller, ws_id, ctx_id = b.user_id, ws.id, ctx.id
    candidate = (await _row(db_session, src)).supersede_candidate.copy()
    await NeuralEdgeRepository(db_session).create_or_update_edge(
        caller,
        src,
        dst,
        edge_type="supersedes",
        weight=0.8,
        workspace_id=str(ws_id),
        context_id=str(ctx_id),
        origin="declared",
    )
    await db_session.commit()

    response = await _call_mcp(
        db_session,
        handle_update_edge,
        caller,
        ws_id,
        context_id=str(ctx_id),
        source_id=str(src),
        target_id=str(dst),
        edge_type="related_to",
    )

    assert response["status"] == "success"
    edge = (
        await db_session.execute(
            select(NeuralMemoryEdge).where(
                NeuralMemoryEdge.src_id == src, NeuralMemoryEdge.dst_id == dst
            )
        )
    ).scalar_one()
    assert edge.edge_type == "related_to"
    assert edge.weight == 0.8
    assert (await _row(db_session, src)).supersede_candidate == candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["create", "overwrite", "retype", "weight"])
@pytest.mark.parametrize("endpoint", ["source", "target"])
@pytest.mark.parametrize("unavailable", ["deleted", "other_context"])
async def test_unavailable_endpoint_has_uniform_denial_without_mutation(
    db_session, path, endpoint, unavailable
):
    a, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    src, dst = await _pair(db_session, ctx, a, a)
    caller, ws_id, ctx_id = a.user_id, ws.id, ctx.id
    candidate = (await _row(db_session, src)).supersede_candidate.copy()
    before_type = "supersedes" if path == "weight" else "related_to"
    if path != "create":
        await NeuralEdgeRepository(db_session).create_or_update_edge(
            caller,
            src,
            dst,
            edge_type=before_type,
            weight=1.0,
            workspace_id=str(ws_id),
            context_id=str(ctx_id),
            origin="declared",
        )
    if unavailable == "deleted":
        changed = {"deleted_at": utcnow()}
    else:
        other = await _shared_context(db_session, ws, a)
        changed = {"context_id": other.id}
    await db_session.execute(
        update(Memory).where(Memory.id == (src if endpoint == "source" else dst)).values(**changed)
    )
    await db_session.commit()

    args = {"context_id": str(ctx_id), "source_id": str(src), "target_id": str(dst)}
    args.update({"weight": 0.8} if path == "weight" else {"edge_type": "supersedes"})
    if path == "overwrite":
        args["overwrite"] = True
    response = await _call_mcp(
        db_session,
        handle_update_edge if path in {"retype", "weight"} else handle_create_edge,
        caller,
        ws_id,
        **args,
    )

    assert response["error"] == "permission_denied"
    assert response["message"] == (
        "A supersedes edge requires both memories to be your own "
        "(linked accounts count only in a private context)."
    )
    assert str(src) not in response["message"] and str(dst) not in response["message"]
    assert (await _row(db_session, src)).supersede_candidate == candidate
    edge = (
        await db_session.execute(
            select(NeuralMemoryEdge).where(
                NeuralMemoryEdge.src_id == src, NeuralMemoryEdge.dst_id == dst
            )
        )
    ).scalar_one_or_none()
    if path == "create":
        assert edge is None
    else:
        assert edge is not None
        assert edge.edge_type == before_type
        assert edge.weight == 1.0


@pytest.mark.asyncio
async def test_accept_after_candidate_is_deleted_reports_supersedes_denial(db_session):
    """A separate transaction deletes the target after resolution, before linking."""
    a, _, ws, ctx, _ = await _seed(db_session, private=False, link=False)
    src, dst = await _pair(db_session, ctx, a, a)
    caller, ws_id, ctx_id = a.user_id, ws.id, ctx.id
    candidate = (await _row(db_session, src)).supersede_candidate.copy()

    async def delete_then_link(db, **kwargs):
        async with AsyncSession(bind=db.bind) as other_session:
            await other_session.execute(
                update(Memory).where(Memory.id == dst).values(deleted_at=utcnow())
            )
            await other_session.commit()
        return await create_declared_edge(db, **kwargs)

    with patch("services.edge_service.create_declared_edge", delete_then_link):
        response = await _call_mcp(
            db_session,
            handle_update_memory,
            caller,
            ws_id,
            context_id=str(ctx_id),
            memory_id=str(src),
            accept_supersede_candidate=True,
        )

    assert response["error"] == "permission_denied"
    assert response["message"] == (
        "A supersedes edge requires both memories to be your own "
        "(linked accounts count only in a private context)."
    )
    assert (await _row(db_session, dst)).deleted_at is not None
    assert (await _row(db_session, src)).supersede_candidate == candidate
    assert (
        await db_session.execute(
            select(NeuralMemoryEdge).where(
                NeuralMemoryEdge.src_id == src, NeuralMemoryEdge.dst_id == dst
            )
        )
    ).scalar_one_or_none() is None
