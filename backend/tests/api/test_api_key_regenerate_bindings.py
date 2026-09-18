"""Regenerate keeps the immutable bindings of the key it replaces (#1537 review).

``agent_id`` and ``bound_context_id`` are set once at mint time and never
edited. The regenerate route revokes the old row and mints a new one, so it
must carry both across — otherwise an agent-bound key silently loses its
agent containment (and, with default expiry now in play, picks up the longer
workspace default instead of the agent one), and a public-bound key becomes
an unbound key. DB and APIKeyManager are mocked; the route is called directly.

#1551 (block-new-only): rotation of an existing bound key is *grandfathered*
— it is the "may serve" side of the XL-only ``public_contexts`` gate, so the
create gate is deliberately not consulted here. The tests at the bottom pin
that, and pin why it is not a bypass: the route has no body that could bind an
unbound key or re-point a bound one at a different context.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routes.api_keys import regenerate_api_key
from utils.datetime import utcnow

USER = {"user_id": "user-1", "email": "u@example.com", "role": "user"}
AGENT_ID = uuid.uuid4()
CONTEXT_ID = uuid.uuid4()
WORKSPACE_ID = uuid.uuid4()


def _old_key(**over) -> SimpleNamespace:
    base = {
        "id": 7,
        "name": "ci",
        "user_id": "user-1",
        "workspace_id": WORKSPACE_ID,
        "expires_at": None,
        "revoked_at": None,
        "agent_id": None,
        "bound_context_id": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _new_key() -> SimpleNamespace:
    return SimpleNamespace(
        id=8,
        key_prefix="kmc_live_abcdefgh",
        name="ci",
        user_id="user-1",
        created_at=utcnow(),
        last_used_at=None,
        revoked_at=None,
        expires_at=None,
    )


def _db(old_key) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=old_key))
    )
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


def _manager(create_side_effect=None) -> MagicMock:
    manager = MagicMock()
    manager.create_key = AsyncMock(
        return_value=("plaintext", _new_key()), side_effect=create_side_effect
    )
    return manager


@pytest.mark.asyncio
async def test_regenerate_carries_agent_binding_forward():
    old = _old_key(agent_id=AGENT_ID)
    manager = _manager()

    await regenerate_api_key(key_id=7, user=USER, manager=manager, db=_db(old))

    kwargs = manager.create_key.await_args.kwargs
    assert kwargs["agent_id"] == AGENT_ID
    assert kwargs["bound_context_id"] is None
    assert kwargs["workspace_id"] == WORKSPACE_ID
    # No expiry on the old key → None → create_key applies the *agent* default
    # because agent_id is present (not the longer workspace default).
    assert kwargs["expires_days"] is None
    assert old.revoked_at is not None


@pytest.mark.asyncio
async def test_regenerate_carries_public_context_binding_forward():
    old = _old_key(workspace_id=None, bound_context_id=CONTEXT_ID)
    manager = _manager()

    await regenerate_api_key(key_id=7, user=USER, manager=manager, db=_db(old))

    kwargs = manager.create_key.await_args.kwargs
    assert kwargs["bound_context_id"] == CONTEXT_ID
    assert kwargs["agent_id"] is None
    assert kwargs["workspace_id"] is None


@pytest.mark.asyncio
async def test_regenerate_surfaces_binding_revalidation_as_400():
    """create_key re-checks the carried binding (agent active / context public);
    a rejection is a client error, not a 500."""
    old = _old_key(agent_id=AGENT_ID)
    manager = _manager(create_side_effect=ValueError("agent is 'suspended'"))

    with pytest.raises(HTTPException) as exc_info:
        await regenerate_api_key(key_id=7, user=USER, manager=manager, db=_db(old))

    assert exc_info.value.status_code == 400
    assert "suspended" in exc_info.value.detail


# ---------------------------------------------------------------------------
# #1551 — rotating an existing bound key is grandfathered (block-new-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pro_workspace_can_still_rotate_an_existing_bound_key() -> None:
    """A workspace that left XL keeps rotating the bound keys it already has:
    no FEAT-001, the create gate helpers are never consulted, the old row is
    revoked and the replacement is bound to the SAME context (count unchanged)."""
    old = _old_key(workspace_id=None, bound_context_id=CONTEXT_ID)
    manager = _manager()

    with (
        patch(
            "config.plan_tiers.has_feature",
            side_effect=AssertionError("regenerate consulted the create gate"),
        ),
        patch(
            "config.plan_tiers.feature_denied_message",
            side_effect=AssertionError("regenerate built a create-gate refusal"),
        ),
    ):
        response = await regenerate_api_key(key_id=7, user=USER, manager=manager, db=_db(old))

    assert response.api_key == "plaintext"
    assert old.revoked_at is not None  # the old key is gone …
    manager.create_key.assert_awaited_once()  # … and exactly one replacement minted
    assert manager.create_key.await_args.kwargs["bound_context_id"] == CONTEXT_ID


def test_regenerate_route_never_references_the_create_gate() -> None:
    """Source pin for the grandfathering decision (#1551): the route neither
    gates on the ``public_contexts`` feature nor raises FEAT-001."""
    src = inspect.getsource(regenerate_api_key)
    for token in ("has_feature", "feature_denied_message", "FeatureNotAvailableError"):
        assert token not in src, token


def test_regenerate_cannot_bind_an_unbound_key_or_rebind_to_another_context() -> None:
    """Why grandfathering is not a bypass: the only input is ``key_id`` — there
    is no request body that could carry a ``bound_context_id`` — and the
    binding is copied from the OLD row, so unbound stays unbound and bound
    stays bound to the same context."""
    params = inspect.signature(regenerate_api_key).parameters
    assert set(params) == {"key_id", "user", "manager", "db"}
    assert "bound_context_id" not in params


@pytest.mark.asyncio
async def test_regenerate_keeps_an_unbound_key_unbound() -> None:
    old = _old_key(workspace_id=None, bound_context_id=None)
    manager = _manager()

    await regenerate_api_key(key_id=7, user=USER, manager=manager, db=_db(old))

    assert manager.create_key.await_args.kwargs["bound_context_id"] is None
