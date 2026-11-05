"""#2639: a toggle losing the enable race answers the documented 409.

``toggle_external_key`` pre-checks for another enabled key of the same
provider, then commits. A concurrent enable committed in between makes the
commit fail on ``uq_external_api_keys_workspace_provider_enabled``. Once
``integrity_error_constraint_name`` read asyncpg's constraint name (#2639),
that branch became reachable, and its message read ``key.provider`` after
``db.rollback()`` had expired the loaded row: on an ``AsyncSession`` the
reload raised ``MissingGreenlet`` and the client got a 500. The race here is
real: the competing row is committed by a second session between the
pre-check and the commit, so the commit raises the driver's own error.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes.external_keys import (
    ExternalKeyToggle,
    encrypt_value,
    toggle_external_key,
)
from models.auth import ExternalAPIKey, User, Workspace

from ._admin_helpers import make_user, make_workspace

PROVIDER = "cohere"


def _key(key_name: str, *, user_id: str, workspace_id: Any, enabled: bool) -> ExternalAPIKey:
    return ExternalAPIKey(
        key_name=key_name,
        provider=PROVIDER,
        encrypted_value=encrypt_value("sk-test-not-a-real-key"),
        user_id=user_id,
        workspace_id=workspace_id,
        enabled=enabled,
    )


@pytest.mark.asyncio
async def test_toggle_losing_the_enable_race_answers_409(
    db_session: AsyncSession, async_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = make_user()
    db_session.add(owner)
    await db_session.flush()
    ws = make_workspace(owner_user_id=owner.user_id)
    db_session.add(ws)
    await db_session.flush()
    db_session.add(_key("COHERE_API_KEY", user_id=owner.user_id, workspace_id=ws.id, enabled=False))
    await db_session.commit()
    # Plain values: the handler's rollback expires every row in this session.
    owner_id, ws_id = owner.user_id, ws.id
    user = {"user_id": owner_id, "email": owner.email, "current_workspace_id": ws_id}

    other = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def _concurrent_enable(**_kwargs: Any) -> None:
        # Runs after the handler's duplicate pre-check, before its commit.
        async with other() as s:
            s.add(_key("COHERE_API_KEY_2", user_id=owner_id, workspace_id=ws_id, enabled=True))
            await s.commit()

    monkeypatch.setattr(
        "api.routes.external_keys.validate_reranker_exclusivity", _concurrent_enable
    )
    try:
        with pytest.raises(HTTPException) as raised:
            await toggle_external_key(
                "COHERE_API_KEY",
                ExternalKeyToggle(enabled=True),
                user=user,
                db=db_session,
            )
        assert raised.value.status_code == 409
        assert f"Another enabled {PROVIDER} API key already exists" in raised.value.detail
        assert "concurrent toggle" in raised.value.detail
    finally:
        async with other() as s:
            await s.execute(delete(ExternalAPIKey).where(ExternalAPIKey.workspace_id == ws_id))
            await s.execute(delete(Workspace).where(Workspace.id == ws_id))
            await s.execute(delete(User).where(User.user_id == owner_id))
            await s.commit()
