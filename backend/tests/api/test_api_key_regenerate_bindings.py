"""Regenerate keeps the immutable bindings of the key it replaces (#1537 review).

``agent_id`` and ``bound_context_id`` are set once at mint time and never
edited. The regenerate route revokes the old row and mints a new one, so it
must carry both across — otherwise an agent-bound key silently loses its
agent containment (and, with default expiry now in play, picks up the longer
workspace default instead of the agent one), and a public-bound key becomes
an unbound key. DB and APIKeyManager are mocked; the route is called directly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
