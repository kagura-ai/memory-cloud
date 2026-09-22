"""Route tests for GET /memory/guardrails/digest (#1621).

Direct-call convention (like ``test_memory_guardrails_route.py``): invoke the
handler with a fake session and the entry source patched, asserting the body
equals the builder output, the response headers, the ``target`` switch, the
422 / 404 mapping and that the PURE key scope is forwarded.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

import services.guardrail_digest as digest_mod
from api.routes.memory import GUARDRAIL_DIGEST_VERSION_HEADER, guardrail_digest
from mcp_server.transport import SERVER_INSTRUCTIONS_BASE
from services.guardrail_digest import (
    EXPORT_END_MARKER,
    DigestEntries,
    DigestEntry,
    render_export_block,
    render_instructions,
)
from utils.exceptions import NotFoundException

MOCK_USER = {"user_id": "u1", "current_workspace_id": uuid4()}


def _entries(ctx, *summaries: str, total: int | None = None) -> DigestEntries:
    items = [
        DigestEntry(
            memory_id=str(uuid4()),
            summary=s,
            importance=0.8,
            authored_by_caller=True,
            source_type="manual",
        )
        for s in summaries
    ]
    total = len(items) if total is None else total
    return DigestEntries(
        context_id=ctx,
        entries=items,
        total_available=total,
        truncated=total > len(items),
        tool_triggered_version="0123456789abcdef",
    )


@pytest.fixture
def source(monkeypatch):
    """Patch ``fetch_entries``; ``state.result`` is what the route receives."""
    from types import SimpleNamespace

    state = SimpleNamespace(result=None, kwargs=None)

    async def fake_fetch_entries(db, **kwargs):
        state.kwargs = kwargs
        return state.result

    monkeypatch.setattr(digest_mod, "fetch_entries", fake_fetch_entries)
    return state


async def _call(user=MOCK_USER, **query):
    params = {"target": "export", "profile": None, "tools": None, **query}
    return await guardrail_digest(user=user, db=AsyncMock(), **params)


@pytest.mark.asyncio
async def test_export_body_is_the_builder_output_with_markers_and_headers(source):
    ctx = uuid4()
    source.result = _entries(ctx, "one", "two")

    response = await _call(context_id=str(ctx))

    assert response.status_code == 200
    body = response.body.decode("utf-8")
    assert body == render_export_block(source.result)
    assert body.startswith(
        f"<!-- kagura-memory:guardrails begin context={ctx} tool_triggered_version=0123456789abcdef -->\n"
    )
    assert body.endswith(EXPORT_END_MARKER + "\n")
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers[GUARDRAIL_DIGEST_VERSION_HEADER.lower()] == "0123456789abcdef"
    assert "x-kagura-guardrails-version" not in response.headers  # never the bare name
    assert source.kwargs["context_id"] == ctx
    assert source.kwargs["limit"] == 20


@pytest.mark.asyncio
async def test_export_of_an_unmarked_or_external_context_is_200_with_an_empty_body(source):
    ctx = uuid4()
    source.result = _entries(ctx)

    response = await _call(context_id=str(ctx))

    assert response.status_code == 200
    assert response.body == b""
    assert response.headers[GUARDRAIL_DIGEST_VERSION_HEADER.lower()] == "0123456789abcdef"


@pytest.mark.asyncio
async def test_instructions_target_previews_exactly_what_lane_a_serves(source):
    ctx = uuid4()
    source.result = _entries(ctx, "a", total=3)

    full = await _call(context_id=str(ctx), target="instructions")
    core = await _call(context_id=str(ctx), target="instructions", profile="core")
    allow = await _call(context_id=str(ctx), target="instructions", tools="remember,recall")

    assert full.headers["content-type"] == "text/plain; charset=utf-8"
    assert full.body.decode() == render_instructions(
        SERVER_INSTRUCTIONS_BASE, source.result, tool_names=frozenset({"load_guardrails"})
    )
    assert full.body.decode().startswith(SERVER_INSTRUCTIONS_BASE + "\n\n")
    assert full.body.decode().endswith("(+2 more: load_guardrails(context_id))")
    assert core.body.decode().endswith("(+2 more: get_context_info(context_id))")
    assert allow.body.decode().endswith("(+2 more: get_context_info(context_id))")
    assert source.kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_instructions_target_of_an_empty_context_is_the_base_text(source):
    ctx = uuid4()
    source.result = _entries(ctx)

    response = await _call(context_id=str(ctx), target="instructions")

    assert response.body.decode() == SERVER_INSTRUCTIONS_BASE


@pytest.mark.asyncio
async def test_malformed_context_id_is_a_422_before_any_read(source):
    with pytest.raises(HTTPException) as exc:
        await _call(context_id="not-a-uuid")
    assert exc.value.status_code == 422
    assert "context_id must be a valid UUID" in exc.value.detail
    assert source.kwargs is None


@pytest.mark.asyncio
async def test_unknown_target_is_a_422_before_any_read(source):
    with pytest.raises(HTTPException) as exc:
        await _call(context_id=str(uuid4()), target="html")
    assert exc.value.status_code == 422
    assert source.kwargs is None


@pytest.mark.asyncio
async def test_denied_context_is_the_uniform_not_found(source):
    """``fetch_entries`` returns ``None`` on every deny; the route raises the
    domain ``NotFoundException`` the global handler maps to a 404 — the same
    body as ``/guardrails`` for a context the caller may not read."""
    source.result = None
    ctx = uuid4()

    with pytest.raises(NotFoundException) as exc:
        await _call(context_id=str(ctx))
    assert exc.value.status_code == 404
    assert "Context" in str(exc.value)


@pytest.mark.asyncio
async def test_route_forwards_the_pure_api_key_workspace_scope(source):
    key_ws = uuid4()
    user = {"user_id": "u1", "current_workspace_id": uuid4(), "api_key_workspace_id": key_ws}
    source.result = _entries(uuid4(), "x")

    await _call(user=user, context_id=str(uuid4()))

    assert source.kwargs["key_workspace_id"] == key_ws
    assert source.kwargs["key_workspace_id"] != user["current_workspace_id"]
    assert source.kwargs["user_id"] == "u1"


@pytest.mark.asyncio
async def test_route_without_a_key_scope_forwards_none(source):
    source.result = _entries(uuid4(), "x")

    await _call(context_id=str(uuid4()))

    assert source.kwargs["key_workspace_id"] is None


def test_route_is_declared_as_text_not_json():
    """``/api-docs-audit`` and the OpenAPI page must not show a JSON schema
    for a text body."""
    from api.routes.memory import router

    route = next(r for r in router.routes if getattr(r, "path", "") == "/guardrails/digest")
    assert route.methods == {"GET"}
    assert route.response_class.__name__ == "PlainTextResponse"
    assert "text/markdown" in route.responses[200]["content"]
