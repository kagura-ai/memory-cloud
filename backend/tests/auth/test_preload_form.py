"""``preload_form`` keeps only the textual parts of an OAuth form (#2190).

Authlib reads ``request.state.form_data`` / ``form_items`` (through
``StarletteOAuth2Payload``), where the form overrides the query string. A
multipart *file* part would reach it as an ``UploadFile`` and fail on the
first string operation, so a non-string part counts as absent.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from api.routes.oauth import preload_form

_app = FastAPI()


@_app.api_route("/form", methods=["GET", "POST"])
async def _form(request: Request) -> dict[str, Any]:
    await preload_form(request)
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
