"""The observability API v1 fixtures match the response models and error bodies (#2679).

The fixtures under ``tests/fixtures/observability_api_v1/`` are what a
self-hosted dashboard builds against. Each response fixture validates against
its (extra-field-refusing) model and survives a JSON round trip unchanged;
each error fixture is byte-for-byte the body the server sends for that error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import Request

from api.routes import observability as routes
from utils import exceptions as exc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "observability_api_v1"

RESPONSES = {
    "capabilities.json": routes.CapabilitiesV1,
    "resources.json": routes.ResourcesV1,
    "snapshots.json": routes.SnapshotsV1,
    "ingestion_events.json": routes.IngestionEventsPageV1,
}

ERRORS = {
    "error_unsupported_schema_version.json": (
        lambda: exc.UnsupportedSchemaVersionError(2, [1]),
        400,
    ),
    "error_cursor_invalid.json": (lambda: exc.CursorInvalidError(), 400),
    "error_cursor_expired.json": (
        lambda: exc.CursorExpiredError(oldest_cursor="eyJ2IjoxLCJpIjoxNn0"),
        410,
    ),
    "error_resource_not_found.json": (lambda: exc.ObservabilityResourceNotFoundError(), 404),
    "error_unauthorized.json": (
        lambda: exc.AuthenticationError("Invalid or missing observation credential"),
        401,
    ),
    "error_disabled.json": (lambda: exc.ObservabilityNotFoundError(), 404),
    "error_rate_limited.json": (lambda: exc.ObservabilityRateLimitError(), 429),
    "error_temporarily_unavailable.json": (lambda: exc.TemporarilyUnavailableError(), 503),
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_every_fixture_is_covered() -> None:
    names = {p.name for p in FIXTURES.glob("*.json")}
    assert names == set(RESPONSES) | set(ERRORS)


@pytest.mark.parametrize("name", sorted(RESPONSES))
def test_response_fixture_matches_its_model(name: str) -> None:
    data = _load(name)
    model = RESPONSES[name].model_validate(data)
    assert json.loads(model.model_dump_json()) == data


@pytest.mark.parametrize("name", sorted(ERRORS))
async def test_error_fixture_is_the_served_body(name: str) -> None:
    from api.main import memory_cloud_exception_handler

    build, status = ERRORS[name]
    request = Request({"type": "http", "method": "GET", "path": "/x", "headers": []})
    response = await memory_cloud_exception_handler(request, build())
    assert response.status_code == status
    assert json.loads(bytes(response.body)) == _load(name)
    if status in (429, 503):
        assert "retry-after" in response.headers


def test_fixtures_carry_no_forbidden_fields() -> None:
    forbidden = {"payload", "doc_id", "version", "memory_id", "details", "metrics", "summary"}

    def keys(value) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {k for v in value.values() for k in keys(v)}
        if isinstance(value, list):
            return {k for v in value for k in keys(v)}
        return set()

    for name in RESPONSES:
        assert not (keys(_load(name)) & forbidden), name
