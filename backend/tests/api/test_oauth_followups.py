"""OAuth form and diagnostic hardening regressions (#2238)."""

from unittest.mock import AsyncMock, patch

import pytest
from authlib.oauth2.rfc6749.errors import InvalidRequestError
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
