"""Unit tests for the ``mcp_asgi_app`` helpers extracted in #1456.

``mcp_asgi_app`` had no unit coverage at all — ``test_transport_challenge.py``
exercises one string sanitizer, and the end-to-end tests live in
``tests/integration/`` (excluded from the backend-unit job, and they need a
database). These cover the three pieces that could be lifted out without
touching the auth or session control flow, so the parts that ARE now testable
are tested.
"""

import json

import pytest

from mcp_server.transport import (
    _extract_session_id,
    _normalize_mcp_path,
    _send_json_error,
)


class _Recorder:
    """Minimal ASGI ``send`` double."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


# --------------------------------------------------------------- _send_json_error


@pytest.mark.asyncio
async def test_send_json_error_emits_start_then_body():
    send = _Recorder()
    await _send_json_error(send, 404, {"error": "nope"})

    assert [m["type"] for m in send.messages] == [
        "http.response.start",
        "http.response.body",
    ]
    start, body = send.messages
    assert start["status"] == 404
    assert start["headers"] == [[b"content-type", b"application/json"]]
    assert json.loads(body["body"]) == {"error": "nope"}


@pytest.mark.asyncio
async def test_send_json_error_appends_extra_headers_after_content_type():
    """The 401 path adds ``www-authenticate``; content-type must stay first."""
    send = _Recorder()
    await _send_json_error(
        send, 401, {"error": "invalid_token"}, [[b"www-authenticate", b'Bearer realm="x"']]
    )

    headers = send.messages[0]["headers"]
    assert headers[0] == [b"content-type", b"application/json"]
    assert [b"www-authenticate", b'Bearer realm="x"'] in headers


@pytest.mark.asyncio
async def test_send_json_error_body_is_utf8_encoded():
    """Non-ASCII descriptions must not raise or mangle — error text can echo
    back user-supplied bytes."""
    send = _Recorder()
    await _send_json_error(send, 400, {"error_description": "コンテキストが見つかりません"})

    body = send.messages[1]["body"]
    assert isinstance(body, bytes)
    assert json.loads(body)["error_description"] == "コンテキストが見つかりません"


# ------------------------------------------------------------- _extract_session_id


def test_session_id_from_legacy_path_wins():
    """Path is checked first, ahead of a header naming a different session."""
    got = _extract_session_id(
        "POST",
        "/mcp/messages/mcp-from-path/",
        {b"mcp-session-id": b"mcp-from-header"},
        b"",
    )
    assert got == "mcp-from-path"


def test_session_id_path_form_only_applies_to_post():
    """A GET on the legacy shape must fall through to the header."""
    got = _extract_session_id(
        "GET", "/mcp/messages/mcp-from-path/", {b"mcp-session-id": b"mcp-from-header"}, b""
    )
    assert got == "mcp-from-header"


def test_session_id_from_header_beats_query():
    got = _extract_session_id("POST", "/mcp", {b"mcp-session-id": b"mcp-hdr"}, b"session_id=mcp-qs")
    assert got == "mcp-hdr"


def test_session_id_from_query_parameter():
    got = _extract_session_id("GET", "/mcp", {}, b"foo=1&session_id=mcp-qs&bar=2")
    assert got == "mcp-qs"


def test_session_id_absent_is_none():
    """An initialize POST carries no session id — that must stay ``None`` so the
    caller creates one rather than looking up the empty string."""
    assert _extract_session_id("POST", "/mcp", {}, b"") is None


def test_session_id_short_legacy_path_does_not_index_error():
    """``/mcp/messages/`` with no id must not raise on ``path_parts[2]``."""
    assert _extract_session_id("POST", "/mcp/messages/", {}, b"") is None


def test_session_id_query_value_may_contain_equals():
    """``split("=", 1)`` keeps the remainder intact."""
    got = _extract_session_id("GET", "/mcp", {}, b"session_id=a=b=c")
    assert got == "a=b=c"


# ------------------------------------------------------------- _normalize_mcp_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/mcp", "/"),
        ("/mcp/", "/"),
        ("/mcp/messages/abc/", "/messages/abc/"),
        ("/mcp/sse", "/sse"),
        # Not under the mount prefix — passed through untouched.
        ("/other", "/other"),
        ("/mcpx", "/mcpx"),
        ("", ""),
    ],
)
def test_normalize_mcp_path(path, expected):
    assert _normalize_mcp_path(path) == expected
