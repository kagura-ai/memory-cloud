"""MCP ``update_context(is_public=...)`` plan gate (#1551).

Making a context public is XL-only ("may create"), but a context that is
already public keeps serving on its current tier ("may serve") — so only the
transition INTO public is gated, and it is gated on ``public_contexts``, not on
``allows_shared_contexts`` (shared contexts stay on L and move independently).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server.tools.context import _apply_public_flag


def _db_for(plan_name: str) -> MagicMock:
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(plan_name=plan_name))
    return db


def _context(*, is_public: bool, resource_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(workspace_id="ws-1", is_public=is_public, resource_id=resource_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_name", ["free", "basic", "pro"])
async def test_making_public_is_refused_below_xl(plan_name: str) -> None:
    ctx = _context(is_public=False)
    error = await _apply_public_flag(_db_for(plan_name), ctx, True)

    assert error is not None
    payload = __import__("json").loads(error[0].text)
    assert payload["error"] == "plan_required"
    assert payload["required_plan"] == "promax"
    assert "XL" in payload["message"]
    assert ctx.is_public is False


@pytest.mark.asyncio
async def test_missing_workspace_row_fails_closed() -> None:
    """No workspace row → no plan → refused, never silently allowed."""
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    ctx = _context(is_public=False)

    error = await _apply_public_flag(db, ctx, True)

    assert error is not None
    assert __import__("json").loads(error[0].text)["error"] == "plan_required"
    assert ctx.is_public is False


@pytest.mark.asyncio
async def test_making_public_is_allowed_on_xl() -> None:
    ctx = _context(is_public=False)
    assert await _apply_public_flag(_db_for("promax"), ctx, True) is None
    assert ctx.is_public is True


@pytest.mark.asyncio
async def test_pro_shared_context_is_not_enough_for_public() -> None:
    """L allows shared contexts but no longer public ones — the two gates are
    separate fields and must not be conflated."""
    from config.plan_tiers import get_plan_tier

    assert get_plan_tier("pro").allows_shared_contexts
    ctx = _context(is_public=False)
    assert await _apply_public_flag(_db_for("pro"), ctx, True) is not None


@pytest.mark.asyncio
async def test_existing_public_context_on_pro_keeps_serving() -> None:
    """Block-new-only: re-asserting ``is_public=True`` on an already-public L
    context is a no-op, not a refusal — and the workspace plan is not even
    consulted."""
    db = _db_for("pro")
    ctx = _context(is_public=True, resource_id="products")

    assert await _apply_public_flag(db, ctx, True) is None
    assert ctx.is_public is True
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_unpublish_lock_is_unchanged() -> None:
    ctx = _context(is_public=True, resource_id="products")
    error = await _apply_public_flag(_db_for("pro"), ctx, False)

    assert error is not None
    assert __import__("json").loads(error[0].text)["error"] == "cannot_make_private"
    assert ctx.is_public is True
