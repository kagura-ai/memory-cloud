"""A refused chunk names its record, field and reason (#3055).

``400 {"error":"invalid_record"}`` gains three optional keys, in this order:
``record`` (the 0-based index of the first refused record), ``field`` (absent
for ``record_too_large`` and for an unknown key that is not a definition name)
and ``reason`` (one of 37 codes). No value is ever reflected. Every other
worker error keeps its bare body.
"""

from __future__ import annotations

import typing

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from api.routes.sync_worker import SyncWorkerError, SyncWorkerRoute
from models.sync_feed_rules import REASONS, RecordRefusal
from services.sync_feed_runs import refusal_detail
from utils.exceptions import SyncProtocolError


def _client(error: Exception) -> TestClient:
    router = APIRouter(route_class=SyncWorkerRoute)

    @router.get("/refuse")
    async def refuse() -> dict:
        raise error

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_detail_keys_follow_the_error_in_wire_order():
    error = SyncProtocolError(
        "invalid_record", detail=refusal_detail(3, RecordRefusal("interval_order", "x_y"))
    )
    response = _client(error).get("/refuse")
    assert response.status_code == 400
    assert response.content == (
        b'{"error":"invalid_record","record":3,"field":"x_y","reason":"interval_order"}'
    )
    assert response.headers["cache-control"] == "no-store"


def test_a_refusal_without_a_field_has_no_field_key():
    error = SyncProtocolError(
        "invalid_record", detail=refusal_detail(0, RecordRefusal("record_too_large"))
    )
    assert _client(error).get("/refuse").content == (
        b'{"error":"invalid_record","record":0,"reason":"record_too_large"}'
    )


def test_every_other_protocol_error_keeps_its_bare_body():
    for code, status in (("invalid_record", 400), ("invalid_manifest", 400)):
        response = _client(SyncProtocolError(code)).get("/refuse")  # type: ignore[arg-type]
        assert response.status_code == status
        assert response.content == b'{"error":"' + code.encode() + b'"}'


def test_the_error_model_documents_three_optional_keys():
    schema = SyncWorkerError.model_json_schema()
    assert schema["required"] == ["error"]
    properties = schema["properties"]
    assert list(properties) == ["error", "record", "field", "reason"]
    assert properties["record"]["type"] == "integer"
    assert (properties["record"]["minimum"], properties["record"]["maximum"]) == (0, 99)
    assert properties["field"]["type"] == "string"
    assert properties["reason"]["enum"] == list(REASONS)
    assert all("default" not in properties[key] for key in ("record", "field", "reason"))
    assert all("anyOf" not in properties[key] for key in ("record", "field", "reason"))
    assert typing.get_args(SyncWorkerError.model_fields["reason"].annotation)
