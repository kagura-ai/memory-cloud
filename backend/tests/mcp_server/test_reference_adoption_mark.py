"""Unit tests: the once-per-session reference() read marker (#2139)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._reference_adoption import (
    REFERENCE_ADOPTION_SESSION_TTL_SECONDS,
    mark_first_read,
    reference_read_key,
)

MEM = uuid4()
AGENT = uuid4()


def _redis(set_result=True, set_error=None):
    client = MagicMock()
    client.set = AsyncMock(return_value=set_result, side_effect=set_error)
    return client


async def _mark(client, **overrides):
    kwargs = {"user_id": "sub-1", "agent_id": AGENT, "session_id": "run.1", "memory_id": MEM}
    kwargs.update(overrides)
    with patch("mcp_server.tools._reference_adoption.get_redis_client", return_value=client):
        return await mark_first_read(**kwargs)


@pytest.mark.asyncio
async def test_first_mark_is_true_and_sets_the_ttl():
    client = _redis(set_result=True)
    assert await _mark(client) is True
    client.set.assert_awaited_once_with(
        f"reference_read:sub-1:{AGENT}:run.1:{MEM}", "1", nx=True, ex=86_400
    )
    assert REFERENCE_ADOPTION_SESSION_TTL_SECONDS == 86_400


@pytest.mark.asyncio
async def test_second_mark_in_the_same_session_is_false():
    # SET NX returns None when the key already exists.
    assert await _mark(_redis(set_result=None)) is False


def test_mark_is_keyed_by_principal_session_and_memory():
    base = {"user_id": "sub-1", "agent_id": AGENT, "session_id": "run.1", "memory_id": MEM}
    variants = [
        base,
        {**base, "user_id": "sub-2"},
        {**base, "agent_id": uuid4()},
        {**base, "agent_id": None},
        {**base, "session_id": "run.2"},
        {**base, "memory_id": uuid4()},
    ]
    keys = [reference_read_key(**v) for v in variants]
    assert len(set(keys)) == len(keys)
    assert reference_read_key(**{**base, "agent_id": None}) == (
        f"reference_read:sub-1:-:run.1:{MEM}"
    )


@pytest.mark.asyncio
async def test_a_redis_failure_is_false_and_logs_error_type_only():
    secret = "redis://:hunter2@host:6379"
    with patch("mcp_server.tools._reference_adoption.logger") as log:
        assert await _mark(_redis(set_error=ConnectionError(secret))) is False
    log.warning.assert_called_once_with(
        "reference_adoption_mark_failed", error_type="ConnectionError"
    )


@pytest.mark.asyncio
async def test_an_unavailable_client_is_false():
    with (
        patch(
            "mcp_server.tools._reference_adoption.get_redis_client",
            side_effect=RuntimeError("no redis"),
        ),
        patch("mcp_server.tools._reference_adoption.logger"),
    ):
        assert (
            await mark_first_read(user_id="u", agent_id=None, session_id="s", memory_id=MEM)
            is False
        )
