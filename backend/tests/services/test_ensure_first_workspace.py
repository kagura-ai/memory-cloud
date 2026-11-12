"""``ensure_first_workspace`` keeps the OAuth callbacks' rule (#1734, #2785).

Pending invitations addressed to the account stand in for its personal
workspace only when the account's address is verified (addressed invitations
are accepted by a verified address); otherwise, or with nothing pending, the
personal workspace is created. It never raises.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import services.workspace_service as ws_module
from services.workspace_service import ensure_first_workspace


@pytest.fixture
def world(monkeypatch):
    invitations = MagicMock()
    invitations.return_value.get_pending_invitations_for_email = AsyncMock(return_value=[])
    invitations.return_value.account_address_verified = AsyncMock(return_value=True)
    monkeypatch.setattr("services.invitation_service.InvitationService", invitations)
    workspaces = MagicMock()
    workspaces.return_value.ensure_personal_workspace = AsyncMock()
    monkeypatch.setattr(ws_module, "WorkspaceService", workspaces)
    return invitations.return_value, workspaces.return_value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pending", "verified", "created"),
    [([], True, 1), ([MagicMock()], True, 0), ([MagicMock()], False, 1)],
    ids=["nothing-pending", "pending-verified", "pending-unverified"],
)
async def test_personal_workspace_unless_a_verified_address_has_invitations(
    world, pending, verified, created
) -> None:
    invitations, workspaces = world
    invitations.get_pending_invitations_for_email.return_value = pending
    invitations.account_address_verified.return_value = verified

    await ensure_first_workspace(MagicMock(), "email:abc", "a@example.test")

    assert workspaces.ensure_personal_workspace.await_count == created
    if pending:
        invitations.account_address_verified.assert_awaited_once_with("email:abc")


@pytest.mark.asyncio
async def test_never_raises(world) -> None:
    invitations, _ = world
    invitations.get_pending_invitations_for_email.side_effect = RuntimeError("db down")
    await ensure_first_workspace(MagicMock(), "email:abc", "a@example.test")
