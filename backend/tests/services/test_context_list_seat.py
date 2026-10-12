"""#1943: ``ContextService.list_contexts`` refuses a member without a seat.

The access lookup used to turn every failure into an empty list (or, for
MCP, ``ContextListingFailedError``). The seat refusal is neither: it is the
same ``403 FEAT-001`` every other route answers, so it passes through.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.context_service import ContextListingFailedError, ContextService
from utils.exceptions import FeatureNotAvailableError


def _service() -> ContextService:
    service = ContextService(MagicMock())
    service._get_user_current_workspace_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_on_lookup_error", [False, True])
async def test_a_seat_refusal_passes_through(raise_on_lookup_error: bool) -> None:
    refused = FeatureNotAvailableError(
        "Feature 'members' is paused", gate="plan", feature="members"
    )
    with (
        patch(
            "services.permission_service.PermissionService.get_accessible_contexts",
            AsyncMock(side_effect=refused),
        ),
        pytest.raises(FeatureNotAvailableError) as exc,
    ):
        await _service().list_contexts("u", raise_on_lookup_error=raise_on_lookup_error)
    assert exc.value is refused


@pytest.mark.asyncio
async def test_other_lookup_failures_keep_their_shape() -> None:
    with patch(
        "services.permission_service.PermissionService.get_accessible_contexts",
        AsyncMock(side_effect=RuntimeError("db")),
    ):
        assert await _service().list_contexts("u") == []
        with pytest.raises(ContextListingFailedError):
            await _service().list_contexts("u", raise_on_lookup_error=True)
