"""OAuth form and diagnostic hardening regressions (#2238), Basic client auth (#2778)."""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from authlib.oauth2.rfc6749.errors import InvalidClientError, InvalidRequestError
from fastapi.testclient import TestClient
from python_multipart.exceptions import MultipartParseError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from api.main import app
from api.routes.oauth import (
    _redact_dcr_redirect_uri_for_log,
    _redact_redirect_uri_for_log,
    _render_authorization_request_error,
    _render_invalid_redirect_uri_error,
)
from auth.oauth2_server import authenticate_client_secret_basic

_ROUTES = ["token", "authorize", "revoke", "introspect"]


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize(
    "error",
    [
        StarletteHTTPException(400, "Too many fields: private-client-secret"),
        StarletteHTTPException(400, "Malformed multipart: private-client-secret"),
        MultipartParseError("private-client-secret"),
    ],
)
def test_parse_errors_are_safe_oauth_errors(route, error):
    with patch("starlette.requests.Request.form", AsyncMock(side_effect=error)):
        response = TestClient(app, raise_server_exceptions=False).post(
            f"/api/v1/oauth/{route}",
            content=b"client_secret=private-client-secret",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 400, response.text
    assert response.json() == {
        "error": "invalid_request",
        "error_description": "Malformed form body",
    }
    assert response.headers["cache-control"] == "no-store"
    assert "private-client-secret" not in response.text


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize(
    "content_type,body",
    [
        ("multipart/form-data", b"private-client-secret"),
        ("multipart/form-data; boundary=test", b"invalid-private-client-secret\r\n"),
    ],
)
def test_real_malformed_multipart_is_invalid_request(route, content_type, body):
    response = TestClient(app, raise_server_exceptions=False).post(
        f"/api/v1/oauth/{route}",
        content=body,
        headers={"content-type": content_type},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_request"
    assert "private-client-secret" not in response.text


@pytest.mark.parametrize("renderer", ["redirect", "request"])
def test_authorize_error_pages_have_consent_security_headers(renderer):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/oauth/authorize",
            "query_string": b"",
            "headers": [],
        }
    )
    response = (
        _render_invalid_redirect_uri_error(request, "en", "https://evil.example/callback")
        if renderer == "redirect"
        else _render_authorization_request_error(request, "en", InvalidRequestError())
    )
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"


@pytest.mark.parametrize("redact", [_redact_redirect_uri_for_log, _redact_dcr_redirect_uri_for_log])
@pytest.mark.parametrize(
    "uri",
    [
        "https://user:password@evil.example/r/user_bound_custom-mcp-private-binding?secret=query#fragment",
        "https://user:password@evil.example/r/user_bound_custom%2Dmcp-private-binding?secret=query",
        "https://user:password@[invalid/r/user_bound_custom-mcp-private-binding?secret=query",
        "user:password@not-a-url?secret=query",
    ],
)
def test_all_redirect_logs_mask_credentials_and_binding(redact, uri):
    output = redact(uri)
    assert len(output) <= 256
    for sensitive in ("password", "private-binding", "secret=query", "fragment"):
        assert sensitive not in output


@pytest.mark.asyncio
async def test_non_parse_http_errors_are_not_reclassified():
    from api.routes.oauth import _preload_oauth_form

    request = Request({"type": "http", "method": "POST", "path": "/oauth/token", "headers": []})
    with patch("api.routes.oauth.preload_form", AsyncMock(side_effect=StarletteHTTPException(503))):
        with pytest.raises(StarletteHTTPException) as caught:
            await _preload_oauth_form(request)
    assert caught.value.status_code == 503


# --- #2778: malformed HTTP Basic client credentials at /token ----------------


def _token_with_basic(credentials: bytes):
    """POST a refresh request to /token carrying only ``Authorization: Basic``.

    The sync session refuses a NUL in a query parameter the way the Postgres
    driver does, so a credential that reaches the client lookup is a 500.
    """
    db = MagicMock()
    db.query.side_effect = ValueError("A string literal cannot contain NUL (0x00) characters.")
    with (
        patch("api.routes.oauth.get_sync_session", return_value=db),
        patch("api.routes.oauth.increment_counter", AsyncMock(return_value=1)),
    ):
        response = TestClient(
            app, base_url="https://testserver", raise_server_exceptions=False
        ).post(
            "/api/v1/oauth/token",
            content=b"grant_type=refresh_token&refresh_token=x",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "Authorization": b"Basic " + credentials,
            },
        )
    return response, db


@pytest.mark.parametrize(
    "credentials",
    [
        b"//79",  # base64 of bytes that are not UTF-8
        base64.b64encode(b"\xff\xfe:secret"),
        base64.b64encode(b"oauth\x00client:secret"),  # NUL in the client_id
        base64.b64encode(b"oauth_client:sec\x00ret"),  # NUL in the secret
        b"not base64!",
        b"\xe9t\xe9",  # not ASCII (Latin-1 on the wire)
    ],
)
def test_malformed_basic_credentials_at_token_are_invalid_client(credentials):
    response, db = _token_with_basic(credentials)

    assert response.status_code == 401, response.text
    assert response.json()["error"] == "invalid_client"
    db.query.assert_not_called()


def _basic_request(header: str | None):
    headers = {} if header is None else {"Authorization": header}
    return SimpleNamespace(headers=headers)


def _basic(raw: bytes) -> str:
    return "Basic " + base64.b64encode(raw).decode()


def test_basic_client_auth_returns_the_client_whose_secret_matches():
    client = MagicMock()
    client.check_client_secret.side_effect = lambda secret: secret == "s e+c"
    query_client = MagicMock(return_value=client)

    # RFC 6749 §2.3.1: both parts are form-urlencoded before base64.
    found = authenticate_client_secret_basic(
        query_client, _basic_request(_basic(b"oauth%3Aclient:s+e%2Bc"))
    )

    assert found is client
    query_client.assert_called_once_with("oauth:client")


@pytest.mark.parametrize(
    "header",
    [None, "Bearer abc", _basic(b"oauth_client:"), _basic(b"oauth_client:wrong")],
)
def test_basic_client_auth_without_matching_credentials_is_none(header):
    client = MagicMock()
    client.check_client_secret.return_value = False
    assert (
        authenticate_client_secret_basic(MagicMock(return_value=client), _basic_request(header))
        is None
    )


def test_basic_client_auth_for_an_unknown_client_is_invalid_client_401():
    with pytest.raises(InvalidClientError) as raised:
        authenticate_client_secret_basic(
            MagicMock(return_value=None), _basic_request(_basic(b"nobody:secret"))
        )
    assert raised.value.status_code == 401
