"""The original manual requester's recheck answers one fixed conflict (#2912).

``validate_manual_request`` reloads the requester's identity and membership. Any
refusal of that recheck (a lost seat, a capacity lock, a lost role or token) is
about the original requester, never about the caller (the worker or another
Editor), so it must leave the function as ``ConflictError`` and never as 403.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from services import sync_requests
from services.sync_feed_authority import FeedFence
from utils.datetime import utcnow
from utils.exceptions import (
    AuthorizationError,
    CapacityLockedError,
    ConflictError,
    FeatureNotAvailableError,
    NotFoundException,
)

pytestmark = pytest.mark.asyncio

CLIENT = "browser-client-2912"


def _capacity_locked() -> CapacityLockedError:
    return CapacityLockedError(
        memory_count=11,
        memory_limit=10,
        over_memories=1,
        used_bytes=None,
        storage_limit_bytes=None,
        over_bytes=None,
        cleanup_url=None,
    )


@pytest.fixture
def recheck(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A current manual request whose requester recheck is under test control."""
    now = utcnow()
    request = SimpleNamespace(
        id=uuid4(),
        requested_by="requester-2912",
        token_id=uuid4(),
        generation=3,
        ownership_epoch=5,
        expires_at=now + timedelta(minutes=10),
        browser_client_id=CLIENT,
        source_version_hash="v" * 64,
    )
    token = SimpleNamespace(id=request.token_id, access_token_hash="h" * 64)
    rows = iter([request, token])

    async def locked(_db, _model, *_where):
        return next(rows)

    monkeypatch.setattr(sync_requests, "locked", locked)
    monkeypatch.setattr(sync_requests, "source_version", AsyncMock(return_value="v" * 64))
    live = SimpleNamespace(
        user_id="requester-2912", client_id=CLIENT, expires_at=now + timedelta(minutes=5)
    )
    identity = AsyncMock(return_value=live)
    membership = AsyncMock(return_value=None)
    monkeypatch.setattr(sync_requests, "load_login_identity", identity)
    monkeypatch.setattr(sync_requests, "lock_membership", membership)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(ownership_epoch=5)))
    installation = SimpleNamespace(id=uuid4(), workspace_id=uuid4(), generation=3)
    return SimpleNamespace(
        db=db,
        installation=installation,
        request=request,
        identity=identity,
        membership=membership,
    )


async def _validate(recheck: SimpleNamespace):
    return await sync_requests.validate_manual_request(
        recheck.db,
        recheck.installation,
        CLIENT,
        recheck.request.id,
        fence=FeedFence(),
    )


async def test_current_requester_passes_the_recheck(recheck):
    assert await _validate(recheck) is recheck.request


async def test_recheck_loads_the_login_behind_the_stored_token_row(recheck):
    """#2911: the grant behind the stored token row, by its id, under shared locks."""
    await _validate(recheck)
    recheck.identity.assert_awaited_once_with(
        recheck.db, recheck.request.token_id, "integrations:manage", lock=True
    )


@pytest.mark.parametrize(
    "refusal",
    [
        FeatureNotAvailableError("No seat on this plan", feature="members"),
        FeatureNotAvailableError("No writable context slot", feature="contexts"),
        _capacity_locked(),
        AuthorizationError("Editor role required"),
        NotFoundException("Context"),
        HTTPException(status_code=403, detail="forbidden"),
    ],
    ids=["seat", "context-slot", "capacity", "role", "not-found", "http"],
)
async def test_requester_membership_refusal_becomes_the_fixed_conflict(recheck, refusal):
    recheck.membership.side_effect = refusal
    with pytest.raises(ConflictError) as raised:
        await _validate(recheck)
    assert raised.value.message == "Manual request authority changed"
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "refusal",
    [_capacity_locked(), FeatureNotAvailableError("No seat", feature="members")],
    ids=["capacity", "seat"],
)
async def test_requester_identity_refusal_becomes_the_fixed_conflict(recheck, refusal):
    recheck.identity.side_effect = refusal
    with pytest.raises(ConflictError) as raised:
        await _validate(recheck)
    assert raised.value.message == "Manual request authority changed"
    recheck.membership.assert_not_awaited()


async def test_conflict_from_the_recheck_passes_through_unchanged(recheck):
    original = ConflictError("Synchronization feed is busy")
    recheck.membership.side_effect = original
    with pytest.raises(ConflictError) as raised:
        await _validate(recheck)
    assert raised.value is original


async def test_unexpected_errors_are_not_turned_into_a_conflict(recheck):
    recheck.membership.side_effect = RuntimeError("database went away")
    with pytest.raises(RuntimeError):
        await _validate(recheck)
