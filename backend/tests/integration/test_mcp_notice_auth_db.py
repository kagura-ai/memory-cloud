"""Service notices never reach agent-bound or connector keys (#2762), on real keys.

``authenticate_mcp_request`` runs for real against keys minted the way the
product mints them (``APIKeyManager.create_key`` for a person's own key and an
agent-bound key in ``shadow`` and ``enforce`` mode, connector provisioning for
the connector's shared key). Only the person's own key makes the request
eligible; with ``MCP_NOTICES_ENABLED`` on, the delivery gate is open for it
and closed for the others.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from mcp_server.auth import (
    authenticate_mcp_request,
    get_mcp_connector_credential,
    get_mcp_notice_eligible,
    set_mcp_notice_eligible,
)
from mcp_server.tools._notices import notice_delivery_allowed


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "mcp_notices_enabled", True)
    yield
    set_mcp_notice_eligible(False)


def _test_db(db_session: AsyncSession):
    """``db.base.get_db`` yielding sessions on the test database."""
    maker = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    async def gen() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    return gen


async def _authenticate(db_session: AsyncSession, plaintext: str) -> str:
    with patch("db.base.get_db", new=_test_db(db_session)):
        user_id, _, _ = await authenticate_mcp_request(f"Bearer {plaintext}")
    return user_id


@pytest.mark.asyncio
async def test_a_persons_own_key_is_eligible(db_session, connector_world) -> None:
    from auth.api_keys import APIKeyManager

    world = connector_world
    owner = await world.user("owner")
    ws = await world.workspace(owner)
    plaintext, _ = await APIKeyManager(db_session).create_key(
        name="own", user_id=owner, workspace_id=ws.id
    )
    await db_session.commit()

    user_id = await _authenticate(db_session, plaintext)

    assert user_id == owner
    assert get_mcp_notice_eligible() is True
    assert notice_delivery_allowed(user_id) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_an_agent_bound_key_is_never_eligible(db_session, connector_world, mode) -> None:
    world = connector_world
    owner = await world.user("owner")
    ws = await world.workspace(owner)
    agent = await world.agent(ws, by=owner)
    agent.enforcement_mode = mode
    plaintext, _ = await world.agent_key(owner, ws, agent)
    await db_session.commit()

    user_id = await _authenticate(db_session, plaintext)

    assert user_id == owner
    assert get_mcp_notice_eligible() is False
    assert notice_delivery_allowed(user_id) is False


@pytest.mark.asyncio
async def test_a_connector_key_is_never_eligible(db_session, connector_world) -> None:
    world = connector_world
    owner = await world.user("owner")
    admin = await world.user("admin")
    ws = await world.workspace(owner, **{admin: WorkspaceRole.ADMIN})
    provisioned = await world.connector(ws, by=admin)
    await db_session.commit()

    user_id = await _authenticate(db_session, provisioned.plaintext_kmc_api_key)

    assert get_mcp_connector_credential() is True
    assert get_mcp_notice_eligible() is False
    assert notice_delivery_allowed(user_id) is False


@pytest.mark.asyncio
async def test_the_flag_does_not_survive_into_the_next_request(db_session, connector_world) -> None:
    from auth.api_keys import APIKeyManager

    world = connector_world
    owner = await world.user("owner")
    ws = await world.workspace(owner)
    own, _ = await APIKeyManager(db_session).create_key(
        name="own", user_id=owner, workspace_id=ws.id
    )
    agent = await world.agent(ws, by=owner)
    bound, _ = await world.agent_key(owner, ws, agent)
    await db_session.commit()

    await _authenticate(db_session, own)
    assert get_mcp_notice_eligible() is True
    await _authenticate(db_session, bound)
    assert get_mcp_notice_eligible() is False
