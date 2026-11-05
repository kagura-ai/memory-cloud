"""The Web UI tool picker uses the same registry as MCP tools/list."""

import json
from pathlib import Path

import pytest

from api.routes.mcp import list_mcp_tools
from mcp_server.tools._definitions import get_tool_definitions


@pytest.mark.asyncio
async def test_tool_catalog_matches_mcp_registry():
    response = await list_mcp_tools(user={"user_id": "catalog-test"})
    definitions = get_tool_definitions()

    assert response.total == len(definitions)
    assert [tool.name for tool in response.tools] == [
        definition["name"] for definition in definitions
    ]
    assert {"explore", "setup_resource"} <= {tool.name for tool in response.tools}
    for tool, definition in zip(response.tools, definitions, strict=True):
        assert tool.input_schema == definition["inputSchema"]
        assert tool.description == definition["description"]


@pytest.mark.parametrize("locale", ["ja", "en"])
def test_frontend_tool_descriptions_cover_the_registry(locale):
    root = Path(__file__).resolve().parents[3]
    messages = json.loads(
        (root / "frontend" / "src" / "messages" / f"{locale}.json").read_text(encoding="utf-8")
    )
    descriptions = messages["mcpToolDescriptions"]
    assert set(descriptions) == {tool["name"] for tool in get_tool_definitions()}
    assert all(item["title"] and item["description"] for item in descriptions.values())
