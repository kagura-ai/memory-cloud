"""``preload_form`` keeps only the textual parts of an OAuth form (#2190).

Authlib reads ``request.state.form_data`` / ``form_items`` (through
``StarletteOAuth2Payload``), where the form overrides the query string. A
multipart *file* part would reach it as an ``UploadFile`` and fail on the
first string operation, so a non-string part counts as absent.

The body is read bounded before it is parsed (#2200): a body over
``_OAUTH_FORM_MAX_BODY_BYTES`` is refused with a 413 ``invalid_request``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from api.routes.oauth import (
    _OAUTH_FORM_MAX_BODY_BYTES,
    _DeviceAuthorizationRequestError,
    preload_form,
)
from utils.oauth_errors import rfc6749_error_response

_app = FastAPI()


@_app.api_route("/form", methods=["GET", "POST"], response_model=None)
async def _form(request: Request) -> dict[str, Any] | JSONResponse:
    try:
        await preload_form(request)
    except _DeviceAuthorizationRequestError as e:
        return rfc6749_error_response(
            error="invalid_request", description=e.description, status_code=e.status_code
        )
    data = request.state.form_data
    items = request.state.form_items
    assert not any(isinstance(v, UploadFile) for v in data.values())
    assert not any(isinstance(v, UploadFile) for _, v in items)
    return {"data": data, "items": [list(item) for item in items]}


def _client() -> TestClient:
    return TestClient(_app)


def test_multipart_keeps_only_text_parts() -> None:
    response = _client().post(
        "/form",
        data={"confirm": "yes", "default_context": "abc"},
        files={
            "scope": ("s.txt", b"memory:admin", "text/plain"),
            "default_context": ("d.txt", b"other", "text/plain"),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"] == {"confirm": "yes", "default_context": "abc"}
    assert body["items"] == [["confirm", "yes"], ["default_context", "abc"]]


def test_repeated_text_field_keeps_every_value_and_the_last_wins() -> None:
    response = _client().post(
        "/form",
        content=b"resource=a&resource=b&grant_type=x",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    body = response.json()
    assert body["data"] == {"resource": "b", "grant_type": "x"}
    assert body["items"] == [["resource", "a"], ["resource", "b"], ["grant_type", "x"]]


def test_get_reads_no_body() -> None:
    body = _client().get("/form", params={"scope": "memory:read"}).json()

    assert body == {"data": {}, "items": []}


_closed_app = FastAPI()


@_closed_app.post("/form")
async def _closed(request: Request) -> dict[str, Any]:
    await preload_form(request)
    form = await request.form()  # the parsed form, cached on the request
    uploads = [v for _, v in form.multi_items() if isinstance(v, UploadFile)]
    return {"closed": [upload.file.closed for upload in uploads]}


def test_a_dropped_file_part_is_closed_right_away() -> None:
    response = TestClient(_closed_app).post(
        "/form", data={"confirm": "yes"}, files={"scope": ("s.txt", b"x", "text/plain")}
    )

    assert response.json() == {"closed": [True]}


_FORM = "application/x-www-form-urlencoded"


def _assert_body_refused(response: Any, status_code: int = 413) -> None:
    assert response.status_code == status_code, response.text
    assert response.json()["error"] == "invalid_request"
    assert response.headers["cache-control"] == "no-store"


def test_the_limit_is_16_kib() -> None:
    assert _OAUTH_FORM_MAX_BODY_BYTES == 16384


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_body_over_the_limit_is_refused_unparsed(chunked: bool) -> None:
    body = b"confirm=yes&pad=" + b"x" * _OAUTH_FORM_MAX_BODY_BYTES
    # An iterator makes httpx send the body chunked, without Content-Length.
    content = iter([body[:4096], body[4096:]]) if chunked else body
    with patch("starlette.requests.FormParser") as form_parser:
        response = _client().post("/form", content=content, headers={"content-type": _FORM})

    _assert_body_refused(response)
    form_parser.assert_not_called()


def test_a_file_part_over_the_limit_is_refused_before_spooling() -> None:
    with patch("starlette.formparsers.SpooledTemporaryFile") as spooled:
        response = _client().post(
            "/form",
            data={"confirm": "yes"},
            files={"scope": ("s.bin", b"x" * (2 * _OAUTH_FORM_MAX_BODY_BYTES), "text/plain")},
        )

    _assert_body_refused(response)
    spooled.assert_not_called()


def test_body_at_the_limit_is_parsed() -> None:
    prefix = b"confirm=yes&pad="
    body = prefix + b"x" * (_OAUTH_FORM_MAX_BODY_BYTES - len(prefix))
    assert len(body) == _OAUTH_FORM_MAX_BODY_BYTES

    response = _client().post("/form", content=body, headers={"content-type": _FORM})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["confirm"] == "yes"
    assert len(data["pad"]) == _OAUTH_FORM_MAX_BODY_BYTES - len(prefix)


async def test_client_disconnect_mid_body_is_a_400() -> None:
    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/form",
        "headers": [(b"content-type", _FORM.encode())],
    }
    with pytest.raises(_DeviceAuthorizationRequestError) as excinfo:
        await preload_form(Request(scope, receive))

    assert excinfo.value.status_code == 400
