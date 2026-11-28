"""The REST key dependencies commit an accepted key's verification (#3052).

``APIKeyManager.verify_key`` writes the throttled ``last_used_at`` in the
request's session. ``get_user_from_api_key_or_session`` and
``verify_api_key_user`` commit it once the key is accepted, so the request
holds no lock on its key row while the route runs (a key rotation locks the
key's workspace before the key). A refused key commits nothing; a failed
commit is rolled back and does not fail the authentication. The lock itself is
pinned on PostgreSQL in
``tests/integration/test_config_api_keys_workspace_membership_db.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from auth.api_keys import APIKeyManager, VerifiedKey

DEPENDENCIES = ["get_user_from_api_key_or_session", "verify_api_key_user"]

_WORKSPACE = uuid4()
_ACCEPTED = VerifiedKey(
    id=1,
    user_id="owner-1",
    workspace_id=_WORKSPACE,
    bound_context_id=None,
    key_prefix="kagura_mine",
    public_id="akey_" + "a" * 22,
)
_BOUND = _ACCEPTED._replace(workspace_id=None, bound_context_id=uuid4())


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/v1/probe", "headers": []})


async def _authenticate(name: str, verified: VerifiedKey, db: AsyncMock) -> dict:
    from auth import dependencies

    with patch.object(APIKeyManager, "verify_key", new=AsyncMock(return_value=verified)):
        return await getattr(dependencies, name)(_request(), api_key="kagura_test", db=db)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", DEPENDENCIES)
async def test_an_accepted_key_commits_its_verification(name: str) -> None:
    db = AsyncMock()

    principal = await _authenticate(name, _ACCEPTED, db)

    assert principal["user_id"] == "owner-1"
    assert principal["current_workspace_id"] == _WORKSPACE
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", DEPENDENCIES)
async def test_a_refused_bound_key_commits_nothing(name: str) -> None:
    db = AsyncMock()

    with pytest.raises(HTTPException) as refused:
        await _authenticate(name, _BOUND, db)

    assert refused.value.status_code == 403
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", DEPENDENCIES)
async def test_a_failed_commit_is_rolled_back_and_does_not_fail_the_authentication(
    name: str,
) -> None:
    db = AsyncMock()
    db.commit.side_effect = RuntimeError("connection lost")
    logger = MagicMock()

    with patch("auth.dependencies.logger", logger):
        principal = await _authenticate(name, _ACCEPTED, db)

    assert principal["user_id"] == "owner-1"
    db.rollback.assert_awaited_once()
    logger.warning.assert_any_call("api_key_last_used_commit_failed", error_type="RuntimeError")
