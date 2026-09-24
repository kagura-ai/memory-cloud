"""#1247: MCP analysis error envelopes must not leak raw exception text.

Unexpected exceptions inside the analysis MCP handlers used to place
``str(e)`` — which can carry SQL / driver / BYOK-key internals — directly
into the error envelope returned to the caller. These tests pin the
hardened behavior: the envelope carries the shared #1684 server-failure
fields (fixed message, ``cause``, ``correlation_id``) and the sensitive
marker planted in the raised exception never reaches the caller — a plain
``ValueError`` included, since these handlers always returned a fixed message.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.analysis import handle_get_analysis

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


# A plain ValueError is a refusal on the dispatch path; here it must not be
# echoed (``echo_value_error=False``, #1684).
_RAISED = pytest.mark.parametrize("exc_type", [RuntimeError, ValueError])


@pytest.mark.asyncio
@_RAISED
async def test_unexpected_service_error_envelope_is_generic(db_mock, exc_type):
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
            AsyncMock(side_effect=exc_type(_SECRET_MARKER)),
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
    assert body["cause"] == "internal_error"
    assert body["message"] == "get_analysis failed because of an unexpected server error."
    assert body["correlation_id"]
    # The raw exception detail (and its sensitive fragments) must be absent.
    serialized = json.dumps(body)
    assert "SUPERSECRET" not in serialized
    assert "password=" not in serialized
    assert "10.0.0.5" not in serialized
    assert "memory_analyses" not in serialized


@pytest.mark.asyncio
@_RAISED
async def test_gate_unexpected_error_envelope_is_generic(db_mock, exc_type):
    """An unmapped exception from the gate chain routes through
    ``_gate_error_response`` and must also produce the generic envelope."""
    with (
        patch("db.base.get_db", _fake_get_db(db_mock)),
        patch(
            "auth.analysis_gates.check_memory_analysis_access_mcp",
            AsyncMock(side_effect=exc_type(_SECRET_MARKER)),
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
    assert body["message"] == "get_analysis failed because of an unexpected server error."
    assert body["correlation_id"]
    assert "SUPERSECRET" not in json.dumps(body)
