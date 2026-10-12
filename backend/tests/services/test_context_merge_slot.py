"""#1943: merging copies memories into the target — a content write that needs a slot."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.context_service import ContextService
from utils.exceptions import FeatureNotAvailableError


@pytest.mark.asyncio
async def test_merge_into_a_context_without_a_slot_is_refused() -> None:
    ws = uuid4()
    source = SimpleNamespace(id=uuid4(), workspace_id=ws)
    target = SimpleNamespace(id=uuid4(), workspace_id=ws)
    service = ContextService(MagicMock())
    service.get_context = AsyncMock(side_effect=[source, target])  # type: ignore[method-assign]
    refused = FeatureNotAvailableError("paused", gate="plan", feature="contexts")
    with (
        patch(
            "services.context_service.ensure_context_writable", AsyncMock(side_effect=refused)
        ) as slot,
        patch("services.permission_service.PermissionService") as perm_cls,
    ):
        owner_check = perm_cls.return_value.check_context_owner = AsyncMock()
        with pytest.raises(FeatureNotAvailableError):
            await service.merge_contexts("u", source.id, target.id)
    assert slot.await_args.args[1] is target
    # Authorization first: the slot never answers before the two owner checks.
    assert owner_check.await_count == 2
