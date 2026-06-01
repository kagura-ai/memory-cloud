"""Unit tests for the read-only binding-introspection MCP tools (Issue #629).

Covers list_my_bindings / describe_binding handler behavior: response shape,
the key_id-XOR-context_id validation, owner-scoped uniform not-found (no
existence oracle), and the multi-binding note. The api_key_bound 403 restriction
is enforced at the MCP auth boundary (a bound key never reaches these handlers),
so it is pinned by the auth-layer regression test, not here.
"""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.api_keys import handle_describe_binding, handle_list_my_bindings


def _row(*, key_id, name, context_id, display_name, name_slug, prefix, created=None):
    """One (APIKey, Context.display_name, Context.name) result row."""
    api_key = SimpleNamespace(
        id=key_id,
        name=name,
        bound_context_id=context_id,
        key_prefix=prefix,
        created_at=created or datetime(2026, 6, 1, 12, 0, 0),
    )
    return (api_key, display_name, name_slug)


def _mock_db(rows):
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute.return_value = result
    db.rollback = AsyncMock()
    return db


def _patches(db):
    async def mock_get_db():
        yield db

    return (
        patch("db.base.get_db", new=mock_get_db),
        patch("mcp_server.tools.api_keys._log_tool_usage", new=AsyncMock()),
    )


@pytest.mark.asyncio
class TestListMyBindings:
    async def test_returns_bindings_without_prefix(self):
        ctx = uuid4()
        rows = [
            _row(
                key_id=7,
                name="slack-bot",
                context_id=ctx,
                display_name="Slack Bot",
                name_slug="slack-bot",
                prefix="kagura_pub_abcd",
            )
        ]
        db = _mock_db(rows)
        get_db_patch, log_patch = _patches(db)
        with get_db_patch, log_patch:
            result = await handle_list_my_bindings({}, "user-1", None)

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["count"] == 1
        b = data["bindings"][0]
        assert b == {
            "key_id": 7,
            "name": "slack-bot",
            "context_id": str(ctx),
            "context_name": "Slack Bot",
            "created_at": "2026-06-01T12:00:00Z",
        }
        assert "key_prefix" not in b  # prefix is a describe_binding detail

    async def test_empty(self):
        db = _mock_db([])
        get_db_patch, log_patch = _patches(db)
        with get_db_patch, log_patch:
            result = await handle_list_my_bindings({}, "user-1", None)
        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["count"] == 0
        assert data["bindings"] == []

    async def test_context_name_falls_back_to_slug(self):
        rows = [
            _row(
                key_id=1,
                name="k",
                context_id=uuid4(),
                display_name=None,
                name_slug="ctx-slug",
                prefix="p",
            )
        ]
        db = _mock_db(rows)
        get_db_patch, log_patch = _patches(db)
        with get_db_patch, log_patch:
            result = await handle_list_my_bindings({}, "user-1", None)
        assert json.loads(result[0].text)["bindings"][0]["context_name"] == "ctx-slug"


@pytest.mark.asyncio
class TestDescribeBinding:
    async def test_by_key_id_includes_prefix(self):
        ctx = uuid4()
        db = _mock_db(
            [
                _row(
                    key_id=42,
                    name="bot",
                    context_id=ctx,
                    display_name="Bot",
                    name_slug="bot",
                    prefix="kagura_pub_xyz9",
                )
            ]
        )
        get_db_patch, log_patch = _patches(db)
        with get_db_patch, log_patch:
            result = await handle_describe_binding({"key_id": 42}, "user-1", None)
        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["binding"]["key_id"] == 42
        assert data["binding"]["key_prefix"] == "kagura_pub_xyz9"
        assert data["binding"]["context_id"] == str(ctx)

    async def test_by_context_id(self):
        ctx = uuid4()
        db = _mock_db(
            [_row(key_id=5, name="b", context_id=ctx, display_name="B", name_slug="b", prefix="pp")]
        )
        get_db_patch, log_patch = _patches(db)
        with get_db_patch, log_patch:
            result = await handle_describe_binding({"context_id": str(ctx)}, "user-1", None)
        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["binding"]["context_id"] == str(ctx)
        assert "note" not in data

    async def test_context_id_multiple_matches_adds_note(self):
        ctx = uuid4()
        db = _mock_db(
            [
                _row(
                    key_id=9,
                    name="b2",
                    context_id=ctx,
                    display_name="B",
                    name_slug="b",
                    prefix="p2",
                ),
                _row(
                    key_id=8,
                    name="b1",
                    context_id=ctx,
                    display_name="B",
                    name_slug="b",
                    prefix="p1",
                ),
            ]
        )
        get_db_patch, log_patch = _patches(db)
        with get_db_patch, log_patch:
            result = await handle_describe_binding({"context_id": str(ctx)}, "user-1", None)
        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["binding"]["key_id"] == 9  # most recent (first row)
        assert "note" in data and "2 of your keys" in data["note"]

    async def test_not_found_is_uniform(self):
        db = _mock_db([])
        get_db_patch, log_patch = _patches(db)
        with get_db_patch, log_patch:
            result = await handle_describe_binding({"key_id": 999999}, "user-1", None)
        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "binding_not_found"
        assert data["message"] == "Binding not found."

    async def test_both_selectors_rejected(self):
        result = await handle_describe_binding(
            {"key_id": 1, "context_id": str(uuid4())}, "user-1", None
        )
        data = json.loads(result[0].text)
        assert data["error"] == "invalid_arguments"

    async def test_neither_selector_rejected(self):
        result = await handle_describe_binding({}, "user-1", None)
        data = json.loads(result[0].text)
        assert data["error"] == "invalid_arguments"

    async def test_key_id_must_be_int(self):
        result = await handle_describe_binding({"key_id": "42"}, "user-1", None)
        data = json.loads(result[0].text)
        assert data["error"] == "invalid_arguments"

    async def test_key_id_bool_rejected(self):
        # bool is an int subclass — must be rejected explicitly.
        result = await handle_describe_binding({"key_id": True}, "user-1", None)
        data = json.loads(result[0].text)
        assert data["error"] == "invalid_arguments"

    async def test_context_id_must_be_uuid(self):
        result = await handle_describe_binding({"context_id": "not-a-uuid"}, "user-1", None)
        data = json.loads(result[0].text)
        assert data["error"] == "invalid_arguments"


@pytest.mark.asyncio
class TestBoundKeyCannotReachBindingTools:
    """#629 principal restriction is enforced at the MCP auth boundary.

    A public-bound key (``bound_context_id != None``) makes
    ``auth.dependencies.verify_api_key`` return None (pinned by
    ``tests/auth/test_api_key_binding.py::test_verify_api_key_standalone_rejects_bound_keys``).
    This test pins the consequence one layer up: ``authenticate_mcp_request``
    then raises ``AuthenticationError``, so a bound principal never authenticates
    and therefore can never reach ``list_my_bindings`` / ``describe_binding``.
    The "api_key_bound gets 403" AC is satisfied structurally here, not by an
    in-handler check (the handler signature carries no principal-type flag). If a
    future change loosens this gate, this test fails loudly.
    """

    async def test_bound_key_fails_mcp_auth(self):
        from mcp_server.auth import authenticate_mcp_request
        from utils.exceptions import AuthenticationError

        # verify_api_key returning None is the bound-key rejection shape on MCP.
        with (
            patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=None)),
            patch("mcp_server.auth._verify_oauth2_token", new=AsyncMock(return_value=None)),
        ):
            with pytest.raises(AuthenticationError):
                await authenticate_mcp_request("Bearer kagura_bound_key_token")


@pytest.mark.asyncio
class TestExecutorWiring:
    """The tools are registered AND exempt from the context_id pre-dispatch.

    Without ``_TOOLS_WITHOUT_CONTEXT_ID`` membership, list_my_bindings (no args)
    and describe_binding (key_id path) would be rejected with ``context_id_required``
    before reaching their handlers. These tests pin the registry + gate wiring.
    """

    async def test_list_my_bindings_not_blocked_by_context_id_gate(self):
        from mcp_server.tools import execute_tool_call

        db = _mock_db([])
        get_db_patch, log_patch = _patches(db)
        with get_db_patch, log_patch:
            result = await execute_tool_call("list_my_bindings", {}, "user-1", None)
        data = json.loads(result[0].text)
        assert data["status"] == "success"  # NOT context_id_required
        assert data["count"] == 0

    async def test_describe_binding_key_id_not_blocked_by_context_id_gate(self):
        from mcp_server.tools import execute_tool_call

        db = _mock_db([])
        get_db_patch, log_patch = _patches(db)
        with get_db_patch, log_patch:
            result = await execute_tool_call("describe_binding", {"key_id": 1}, "user-1", None)
        data = json.loads(result[0].text)
        # Reaches the handler → uniform not-found, NOT context_id_required.
        assert data["error"] == "binding_not_found"
