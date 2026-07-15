"""#1247: MCP analysis error envelopes must not leak raw exception text.

Unexpected exceptions inside the analysis MCP handlers used to place
``str(e)`` — which can carry SQL / driver / BYOK-key internals — directly
into the error envelope returned to the caller. These tests pin the
hardened behavior: the envelope message is a fixed generic string and the
sensitive marker planted in the raised exception never reaches the caller.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.analysis import (
    _GENERIC_ANALYSIS_ERROR,
    handle_get_analysis,
)

# A marker string standing in for the kind of raw driver/SQL/credential
# detail an unexpected exception can carry. It must never surface in the
# caller-facing envelope.
_SECRET_MARKER = 'relation "memory_analyses" does not exist; password=SUPERSECRET host=10.0.0.5'


def _fake_get_db(db_mock):
    """Async-generator factory standing in for ``db.base.get_db``."""

    async def _gen():
        yield db_mock

    return _gen


def _envelope(result) -> dict:
    assert result, "handler returned an empty response list"
    return json.loads(result[0].text)


@pytest.fixture
def db_mock():
    m = MagicMock()
    m.execute = AsyncMock()
    m.commit = AsyncMock()
    m.rollback = AsyncMock()
    return m


@pytest.mark.asyncio
async def test_unexpected_service_error_envelope_is_generic(db_mock):
    """A service raising an exception must yield a generic envelope, never
    the raw exception text."""
    with (
        patch("db.base.get_db", _fake_get_db(db_mock)),
        patch(
            "auth.analysis_gates.check_memory_analysis_access_mcp",
            AsyncMock(return_value="UTC"),
        ),
        patch(
            "services.analysis.query_service.get_analysis",
            AsyncMock(side_effect=RuntimeError(_SECRET_MARKER)),
        ),
        patch("mcp_server.tools.analysis._log_tool_usage", AsyncMock()),
    ):
        result = await handle_get_analysis(
            {"run_id": str(uuid4())},
            "u1",
            uuid4(),
        )

    body = _envelope(result)
    assert body["status"] == "error"
    assert body["error"] == "get_analysis_error"
    assert body["message"] == _GENERIC_ANALYSIS_ERROR
    # The raw exception detail (and its sensitive fragments) must be absent.
    serialized = json.dumps(body)
    assert "SUPERSECRET" not in serialized
    assert "password=" not in serialized
    assert "10.0.0.5" not in serialized
    assert "memory_analyses" not in serialized


@pytest.mark.asyncio
async def test_gate_unexpected_error_envelope_is_generic(db_mock):
    """An unmapped exception from the gate chain routes through
    ``_gate_error_response`` and must also produce the generic envelope."""
    with (
        patch("db.base.get_db", _fake_get_db(db_mock)),
        patch(
            "auth.analysis_gates.check_memory_analysis_access_mcp",
            AsyncMock(side_effect=RuntimeError(_SECRET_MARKER)),
        ),
        patch("mcp_server.tools.analysis._log_tool_usage", AsyncMock()),
    ):
        result = await handle_get_analysis(
            {"run_id": str(uuid4())},
            "u1",
            uuid4(),
        )

    body = _envelope(result)
    assert body["status"] == "error"
    assert body["error"] == "internal_error"
    assert body["message"] == _GENERIC_ANALYSIS_ERROR
    assert "SUPERSECRET" not in json.dumps(body)
