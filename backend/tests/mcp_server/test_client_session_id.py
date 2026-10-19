"""A client-supplied session id on MCP requests (#2122).

Stateless (2026-07-28) requests carry no ``Mcp-Session-Id``, so a client
without OTel baggage used to write without a session. It can now name its own
run / conversation id:

- ``params._meta['ai.kagura/sessionId']`` (one tools/call), or
- the ``X-Session-Id`` header (the whole HTTP request);

with the baggage session's token rule (≤ 128 chars of ``[A-Za-z0-9._-]``;
invalid → dropped with a logged reason, never an error) and the precedence
baggage > ``_meta`` > header > the stateful transport's ``Mcp-Session-Id``.
Both transport eras are driven through ``mcp_asgi_app``; the tool schemas do
not change (no ``session_id`` argument on write tools, #2044).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import mcp_server.tools as tools_mod
from api.correlation import (
    CLIENT_SESSION_META_KEY,
    SESSION_SOURCE_BAGGAGE,
    SESSION_SOURCE_HEADER,
    SESSION_SOURCE_META,
    SESSION_SOURCE_TRANSPORT,
    CorrelationContext,
    build_correlation_from_headers,
    client_session_scope,
    get_correlation,
    set_correlation,
)
from services.write_provenance import current_provenance
from tests.mcp_server.test_transport_tool_profiles import (
    _legacy_request,
    _modern_headers,
    _modern_request,
    asgi,  # noqa: F401 - pytest fixture
)

LONG = "a" * 129


@pytest.fixture(autouse=True)
def _clean():
    set_correlation(None)
    yield
    set_correlation(None)


def _corr(session_id: str | None = None, source: str | None = None) -> CorrelationContext:
    return CorrelationContext(
        trace_id="0" * 31 + "1",
        span_id="0" * 15 + "1",
        session_id=session_id,
        session_source=source,
    )


# --------------------------------------------------------------- the header


def test_the_header_names_the_session_when_baggage_did_not():
    c = build_correlation_from_headers(
        traceparent=None, baggage=None, surface="mcp", client_session_id="run-7"
    )
    assert (c.session_id, c.session_source) == ("run-7", SESSION_SOURCE_HEADER)


def test_baggage_wins_over_the_header():
    c = build_correlation_from_headers(
        traceparent=None,
        baggage="gen_ai.conversation.id=from-baggage",
        surface="mcp",
        client_session_id="run-7",
    )
    assert (c.session_id, c.session_source) == ("from-baggage", SESSION_SOURCE_BAGGAGE)


@pytest.mark.parametrize("value", [LONG, "has space", "run/7", "", "日本"])
def test_an_invalid_header_is_dropped_never_an_error(value, caplog):
    c = build_correlation_from_headers(
        traceparent=None, baggage=None, surface="mcp", client_session_id=value
    )
    assert (c.session_id, c.session_source) == (None, None)


def test_the_128_char_limit_is_inclusive():
    c = build_correlation_from_headers(
        traceparent=None, baggage=None, surface="mcp", client_session_id="a" * 128
    )
    assert c.session_id == "a" * 128


def test_without_a_header_nothing_changes():
    c = build_correlation_from_headers(traceparent=None, baggage=None, surface="mcp")
    assert (c.session_id, c.session_source) == (None, None)


# ------------------------------------------------------------------ _meta


def test_meta_names_the_session_for_the_block_and_is_restored():
    original = _corr()
    set_correlation(original)
    with client_session_scope({CLIENT_SESSION_META_KEY: "conv-1"}):
        assert current_provenance().session_id == "conv-1"
        assert get_correlation().session_source == SESSION_SOURCE_META
    assert get_correlation() is original
    assert original.session_id is None


@pytest.mark.parametrize(
    ("session_id", "source"),
    [("run-7", SESSION_SOURCE_HEADER), ("mcp-0123456789abcdef", SESSION_SOURCE_TRANSPORT)],
)
def test_meta_wins_over_the_header_and_the_stateful_session(session_id, source):
    set_correlation(_corr(session_id, source))
    with client_session_scope({CLIENT_SESSION_META_KEY: "conv-1"}):
        assert current_provenance().session_id == "conv-1"
    assert get_correlation().session_id == session_id


@pytest.mark.parametrize("source", [SESSION_SOURCE_BAGGAGE, None])
def test_meta_never_overrides_baggage(source):
    """A session without a recorded source (hand-built) counts as baggage."""
    set_correlation(_corr("from-baggage", source))
    with client_session_scope({CLIENT_SESSION_META_KEY: "conv-1"}):
        assert current_provenance().session_id == "from-baggage"


@pytest.mark.parametrize("value", [LONG, "bad id!", "", 42, {"id": "x"}, ["x"]])
def test_an_invalid_meta_value_is_ignored_and_the_header_stays(value):
    set_correlation(_corr("run-7", SESSION_SOURCE_HEADER))
    with client_session_scope({CLIENT_SESSION_META_KEY: value}):
        assert current_provenance().session_id == "run-7"


@pytest.mark.parametrize("meta", [None, {}, {CLIENT_SESSION_META_KEY: None}, "x", []])
def test_absent_meta_is_a_no_op(meta):
    set_correlation(_corr())
    with client_session_scope(meta):
        assert current_provenance().session_id is None


def test_without_a_correlation_meta_is_a_no_op():
    with client_session_scope({CLIENT_SESSION_META_KEY: "conv-1"}):
        assert get_correlation() is None


def test_the_correlation_is_restored_when_the_call_raises():
    original = _corr()
    set_correlation(original)
    with pytest.raises(RuntimeError), client_session_scope({CLIENT_SESSION_META_KEY: "c"}):
        raise RuntimeError("boom")
    assert get_correlation() is original


def test_the_stateful_session_fills_in_with_its_source():
    from mcp_server.transport import _default_correlation_session

    set_correlation(_corr())
    _default_correlation_session("mcp-0123456789abcdef")
    assert get_correlation().session_source == SESSION_SOURCE_TRANSPORT
    # A header session is not replaced by the protocol session.
    set_correlation(_corr("run-7", SESSION_SOURCE_HEADER))
    _default_correlation_session("mcp-0123456789abcdef")
    assert (get_correlation().session_id, get_correlation().session_source) == (
        "run-7",
        SESSION_SOURCE_HEADER,
    )


# ------------------------------------------------- both eras, through the app


@pytest.fixture
def seen(monkeypatch):
    """A dispatcher that answers with what a write tool would echo."""
    recorded: dict = {}

    async def fake_execute(**kwargs):
        from mcp_server.tools._helpers import _session_response_field

        recorded["session_id"] = current_provenance().session_id
        recorded["arguments"] = kwargs["arguments"]
        return [SimpleNamespace(type="text", text=json.dumps(_session_response_field()))]

    monkeypatch.setattr(tools_mod, "execute_tool_call", fake_execute)
    return recorded


def _call(era: str, meta_session: object = None) -> tuple[dict, dict[bytes, bytes]]:
    args = {"summary": "s", "content": "c", "type": "note"}
    if era == "stateless":
        body = _modern_request("tools/call", name="remember", arguments=args)
        headers = _modern_headers(body)
    else:
        body = _legacy_request("tools/call", name="remember", arguments=args)
        body["params"]["_meta"] = {}
        headers = {b"mcp-session-id": b"sess-1"}
    if meta_session is not None:
        body["params"]["_meta"][CLIENT_SESSION_META_KEY] = meta_session
    return body, headers


def _echo(send) -> dict:
    return json.loads(send.body["result"]["content"][0]["text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("era", "meta", "header", "baggage", "expected"),
    [
        # No client id: the stateless era has no session, the stateful its own.
        ("stateless", None, None, None, None),
        ("session", None, None, None, "sess-1"),
        # _meta alone.
        ("stateless", "conv-1", None, None, "conv-1"),
        ("session", "conv-1", None, None, "conv-1"),
        # The header alone.
        ("stateless", None, "run-7", None, "run-7"),
        ("session", None, "run-7", None, "run-7"),
        # Both, different: _meta wins.
        ("stateless", "conv-1", "run-7", None, "conv-1"),
        ("session", "conv-1", "run-7", None, "conv-1"),
        # Baggage wins over both.
        ("stateless", "conv-1", "run-7", "session.id=bag-1", "bag-1"),
        ("session", "conv-1", "run-7", "gen_ai.conversation.id=bag-1", "bag-1"),
        # Invalid client ids are ignored, never an error.
        ("stateless", "x" * 129, None, None, None),
        ("session", "bad id!", None, None, "sess-1"),
        ("stateless", "bad id!", "run-7", None, "run-7"),
        ("session", None, "bad id!", None, "sess-1"),
    ],
)
async def test_the_session_a_write_records_and_echoes(  # noqa: PLR0913
    asgi,  # noqa: F811
    seen,
    era,
    meta,
    header,
    baggage,
    expected,
):
    body, headers = _call(era, meta)
    if header is not None:
        headers[b"x-session-id"] = header.encode()
    if baggage is not None:
        headers[b"baggage"] = baggage.encode()

    send = await asgi(body, headers, b"profile=core")

    assert send.status == 200
    assert "error" not in send.body
    assert seen["session_id"] == expected
    assert _echo(send) == ({"session_id": expected} if expected else {})
    # ``_meta`` is protocol metadata: it never reaches the tool's arguments.
    assert seen["arguments"] == {"summary": "s", "content": "c", "type": "note"}


@pytest.mark.asyncio
async def test_the_header_reaches_the_request_correlation_on_both_eras(asgi, seen):  # noqa: F811
    for era in ("stateless", "session"):
        body, headers = _call(era)
        headers[b"x-session-id"] = b"run-7"
        await asgi(body, headers, b"profile=core")
        assert seen["session_id"] == "run-7"


# --------------------------------------------------------- schemas unchanged


def test_no_tool_takes_a_client_session_argument():
    """The id travels in the request, never as a tool argument (#2044)."""
    from mcp_server.tools._definitions import get_tool_definitions

    for tool in get_tool_definitions():
        props = tool["inputSchema"].get("properties", {})
        assert CLIENT_SESSION_META_KEY not in props
        assert "sessionId" not in props
        if tool["name"] in {"remember", "remember_batch", "update_memory", "forget"}:
            assert "session_id" not in props
