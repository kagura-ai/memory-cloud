"""MCP tool tests for the zero-knowledge secret store (#1128).

Covers the MCP-specific surface: registry/definition consistency and the
workspace-guard + argument-validation branches (which return before any DB
access). The data path itself is exercised by the service and route integration
tests — the MCP handlers are thin wrappers over the same ``SecretStoreService``.
"""

from __future__ import annotations

import json
import uuid

import pytest

from mcp_server.tools import _TOOLS_WITHOUT_CONTEXT_ID, _build_registry, get_tool_definitions
from mcp_server.tools.secrets import (
    handle_secret_get,
    handle_secret_list,
    handle_secret_put,
    handle_secret_register_pubkey,
    handle_secret_revoke_grant,
)

SECRET_TOOLS = [
    "secret_register_pubkey",
    "secret_put",
    "secret_get",
    "secret_list",
    "secret_revoke_grant",
]


def _decode(resp):
    assert len(resp) == 1
    return json.loads(resp[0].text)


def test_secret_tools_registered_and_defined():
    reg = _build_registry()
    defs = {d["name"] for d in get_tool_definitions()}
    for name in SECRET_TOOLS:
        assert name in reg, f"{name} missing from registry"
        assert name in defs, f"{name} missing from definitions"
        # Workspace-scoped, not memory-context-scoped.
        assert name in _TOOLS_WITHOUT_CONTEXT_ID


def test_definitions_have_required_schema_fields():
    by_name = {d["name"]: d for d in get_tool_definitions()}
    assert by_name["secret_put"]["inputSchema"]["required"] == [
        "name",
        "ciphertext",
        "recipients_snapshot",
        "grant_pubkey_ids",
    ]
    assert by_name["secret_get"]["inputSchema"]["required"] == ["name"]
    # secret_list takes no args.
    assert by_name["secret_list"]["inputSchema"]["properties"] == {}


@pytest.mark.asyncio
async def test_workspace_required_guard():
    # Every tool fails closed when there is no workspace context.
    for handler, args in [
        (handle_secret_register_pubkey, {"pubkey": "age1xxx"}),
        (handle_secret_put, {"name": "n"}),
        (handle_secret_get, {"name": "n"}),
        (handle_secret_list, {}),
        (handle_secret_revoke_grant, {"name": "n", "recipient_pubkey_id": str(uuid.uuid4())}),
    ]:
        out = _decode(await handler(args, "user-1", None))
        assert out["status"] == "error"
        assert out["error"] == "workspace_required"


@pytest.mark.asyncio
async def test_register_requires_pubkey():
    out = _decode(await handle_secret_register_pubkey({}, "user-1", uuid.uuid4()))
    assert out["status"] == "error"
    assert out["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_put_validates_required_args():
    ws = uuid.uuid4()
    # Missing ciphertext.
    out = _decode(await handle_secret_put({"name": "n"}, "u", ws))
    assert out["error"] == "invalid_arguments"
    # Bad UUID in grant list.
    out = _decode(
        await handle_secret_put(
            {
                "name": "n",
                "ciphertext": "ct",
                "recipients_snapshot": ["fp"],
                "grant_pubkey_ids": ["not-a-uuid"],
            },
            "u",
            ws,
        )
    )
    assert out["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_get_validates_version_number_type():
    out = _decode(
        await handle_secret_get({"name": "n", "version_number": "two"}, "u", uuid.uuid4())
    )
    assert out["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_revoke_grant_validates_uuid():
    out = _decode(
        await handle_secret_revoke_grant(
            {"name": "n", "recipient_pubkey_id": "nope"}, "u", uuid.uuid4()
        )
    )
    assert out["error"] == "invalid_arguments"
