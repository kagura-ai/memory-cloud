"""One ``resource_id`` format across MCP and REST (#2284).

Every surface that accepts a slug — MCP ``update_context`` / ``setup_resource``
(and the other resource tools), the REST ``ContextUpdate`` model and
``ContextService.update_context``, and connector provisioning — validates
against ``config.constants.RESOURCE_ID_PATTERN``. These tests feed the same
inputs to each and expect the same verdict, so a slug set on one surface can
be saved again on another.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pydantic
import pytest

from api.routes.contexts import ContextUpdate
from config.constants import RESOURCE_ID_MAX_LENGTH, RESOURCE_ID_PATTERN
from mcp_server.tools.context import _apply_resource_id
from mcp_server.tools.resource import _validate_resource_id
from services.connector_provisioning import ConnectorProvisioningService
from services.context_service import ContextService
from utils.exceptions import ValidationError

VALID = [
    "github_issues",
    "a",
    "test123",
    "sdk-test",
    "my-resource-123",
    "-leading-and-trailing_",
    "a" * RESOURCE_ID_MAX_LENGTH,
]

INVALID = [
    "GitHub-Issues",  # uppercase
    "MY_RESOURCE",
    "my resource",  # space
    "resource@id",
    "a.b",
    "a/b",  # would be a second URL path segment
    "日本語",
    "abc\n",  # ``re.match`` with ``$`` lets a trailing newline through
    "a" * (RESOURCE_ID_MAX_LENGTH + 1),
]


def test_the_pattern_allows_hyphens():
    assert RESOURCE_ID_PATTERN == r"^[a-z0-9_-]+$"
    assert RESOURCE_ID_MAX_LENGTH == 255


def _context(resource_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="ctx",
        display_name=None,
        description=None,
        summary=None,
        usage_guide=None,
        is_default=False,
        is_locked=False,
        sleep_mode="skip",
        is_private=False,
        is_public=True,
        resource_id=resource_id,
        created_by="owner-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        workspace_id=uuid.uuid4(),
    )


# --------------------------------------------------------------- per surface


async def _mcp_update_context_accepts(rid: str) -> bool:
    context = _context()
    error = await _apply_resource_id(AsyncMock(), context, rid, "owner-1")
    if error is None:
        assert context.resource_id == rid
        return True
    assert json.loads(error[0].text)["error"] == "invalid_resource_id"
    assert context.resource_id is None
    return False


def _mcp_resource_tools_accept(rid: str) -> bool:
    return _validate_resource_id(rid) is None


def _rest_model_accepts(rid: str) -> bool:
    try:
        ContextUpdate(resource_id=rid)
    except pydantic.ValidationError:
        return False
    return True


async def _rest_service_accepts(rid: str) -> bool:
    context = _context()
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    service = ContextService(db)
    with patch.object(service, "get_context", AsyncMock(return_value=context)):
        try:
            await service.update_context("owner-1", context.id, resource_id=rid)
        except ValidationError:
            db.commit.assert_not_awaited()
            return False
    assert context.resource_id == rid
    return True


def _connector_accepts(rid: str) -> bool:
    try:
        ConnectorProvisioningService._validate_inputs("slack", rid, 1000)
    except ValidationError:
        return False
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("rid", VALID + INVALID, ids=repr)
async def test_every_surface_gives_the_same_verdict(rid: str) -> None:
    expected = rid in VALID
    verdicts = {
        "mcp update_context": await _mcp_update_context_accepts(rid),
        "mcp resource tools": _mcp_resource_tools_accept(rid),
        "rest ContextUpdate": _rest_model_accepts(rid),
        "rest ContextService": await _rest_service_accepts(rid),
        "connector provisioning": _connector_accepts(rid),
    }
    assert verdicts == dict.fromkeys(verdicts, expected)


@pytest.mark.asyncio
async def test_empty_slug_is_refused_on_mcp_and_by_the_rest_model() -> None:
    assert not await _mcp_update_context_accepts("")
    assert not _mcp_resource_tools_accept("")
    assert not _rest_model_accepts("")
    assert not _connector_accepts("")


# ------------------------------------------------- the REST route end to end


@pytest.mark.asyncio
async def test_rest_route_saves_a_hyphenated_slug() -> None:
    """PUT /contexts/{id} with ``resource_id='sdk-test'`` reaches the row —
    before #2284 ContextService refused the hyphen the route model allowed."""
    from api.routes.contexts import update_context

    context = _context()
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    service = ContextService(db)
    perm = MagicMock()
    perm.check_context_owner = AsyncMock(return_value=context)

    with (
        patch("services.permission_service.PermissionService", return_value=perm),
        patch.object(service, "get_context", AsyncMock(return_value=context)),
    ):
        response = await update_context(
            context.id,
            ContextUpdate(resource_id="sdk-test"),
            {"user_id": "owner-1", "sub": "owner-1"},
            service,
            db,
        )

    assert context.resource_id == "sdk-test"
    assert response.resource_id == "sdk-test"
    db.commit.assert_awaited()


# --------------------------------------------- stored values are not re-judged


@pytest.mark.asyncio
async def test_rest_service_does_not_revalidate_the_stored_slug() -> None:
    """Re-sending the slug a context already has is a no-op, so a value stored
    under an older rule never blocks an unrelated save."""
    context = _context(resource_id="Legacy.Slug")
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    service = ContextService(db)
    with patch.object(service, "get_context", AsyncMock(return_value=context)):
        await service.update_context(
            "owner-1", context.id, resource_id="Legacy.Slug", display_name="New"
        )
    assert context.resource_id == "Legacy.Slug"
    assert context.display_name == "New"


@pytest.mark.asyncio
async def test_mcp_update_context_does_not_revalidate_the_stored_slug() -> None:
    context = _context(resource_id="Legacy.Slug")
    db = AsyncMock()
    with patch("auth.resource_tokens.ResourceTokenManager") as manager:
        error = await _apply_resource_id(db, context, "Legacy.Slug", "owner-1")
    assert error is None
    assert context.resource_id == "Legacy.Slug"
    manager.assert_not_called()  # an unchanged slug revokes nothing


# ------------------------------------------------------- tool text snapshots


def _resource_id_text(tool_name: str) -> str:
    from mcp_server.tools import get_tool_definitions

    tool = next(t for t in get_tool_definitions() if t["name"] == tool_name)
    return tool["inputSchema"]["properties"]["resource_id"]["description"]


def test_update_context_names_the_shared_charset() -> None:
    text = _resource_id_text("update_context")
    assert "lowercase alphanumerics, hyphens and underscores" in text
    assert "unique on this server" in text


def test_setup_resource_names_the_server_wide_uniqueness() -> None:
    """``setup_resource`` binds the slug to a context, and
    ``ux_contexts_resource_id_active`` is server-wide."""
    assert _resource_id_text("setup_resource") == (
        "Resource identifier, unique on this server (a slug another workspace's "
        "context uses also conflicts): lowercase alphanumerics, hyphens, "
        "underscores; max 255 chars, e.g. 'ec_products'."
    )


def test_setup_connector_keeps_the_per_workspace_uniqueness() -> None:
    """A connector only writes a ``resources`` row, whose unique key is
    ``(workspace_id, resource_id)`` (``uq_resources_workspace_resource_id``);
    it sets no ``contexts.resource_id``."""
    text = _resource_id_text("setup_connector")
    assert text.startswith("Resource identifier, unique in the workspace:")
