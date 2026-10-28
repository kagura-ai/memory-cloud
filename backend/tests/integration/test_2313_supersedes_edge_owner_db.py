"""#2313: explicit supersedes writes share the existing owner rule."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from mcp_server.tools.edge import handle_create_edge, handle_update_edge
from models.memory import NeuralMemoryEdge
from repositories.neural_edge import NeuralEdgeRepository
from services.edge_service import create_declared_edge
from tests.integration.test_2308_accept_supersede_candidate_db import _pair, _row
from tests.integration.test_identity_link_writes_db import _seed
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
