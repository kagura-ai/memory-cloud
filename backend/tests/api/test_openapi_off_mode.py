"""SYSTEM_ADMIN_MODE=off leaves the admin operations out of the OpenAPI document (#2767).

``/openapi.json``, ``/docs``, ``/redoc`` and the ``/static/api/openapi.json``
fallback all call ``app.openapi()``. ``session`` and ``service`` serve the stock
document (the snapshot test runs in ``session``).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import app
from auth.system_admin_mode import routes_behind_require_admin
from config.settings import get_settings


@pytest.fixture
def mode(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(get_settings(), "system_admin_mode", value)

    return _set


def _operations(schema: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, item in schema["paths"].items()
        for method in item
        if method in {"get", "put", "post", "delete", "patch"}
    }


ADMIN_OPERATIONS = {
    (method, context.path)
    for context in routes_behind_require_admin(app)
    for method in context.methods - {"HEAD", "OPTIONS"}
}


def test_off_document_has_no_admin_operation(mode):
    mode("off")
    schema = app.openapi()
    assert len(ADMIN_OPERATIONS) == 58  # #2762: + the three mcp-notices routes
    assert not ADMIN_OPERATIONS & _operations(schema)
    assert "/api/v1/admin/users" not in schema["paths"]
    # Everything else stays.
    mode("session")
    stock = app.openapi()
    assert _operations(schema) == _operations(stock) - ADMIN_OPERATIONS


def test_off_document_drops_tags_left_without_an_operation(mode):
    mode("off")
    schema = app.openapi()
    used = {
        tag
        for item in schema["paths"].values()
        for op in item.values()
        for tag in op.get("tags", [])
    }
    declared = {tag["name"] for tag in schema.get("tags", [])}
    stock_declared = {tag["name"] for tag in FastAPI.openapi(app).get("tags", [])}
    emptied = stock_declared - declared
    assert emptied, "at least the admin-only tags are dropped"
    assert not emptied & used
    # A tag that still has an operation is kept.
    assert "system" in declared


@pytest.mark.parametrize("value", ["session", "service"])
def test_session_and_service_serve_the_stock_document(mode, value):
    mode(value)
    schema = app.openapi()
    assert schema == FastAPI.openapi(app)
    assert ADMIN_OPERATIONS <= _operations(schema)


def test_every_served_copy_follows_the_mode(mode):
    mode("off")
    client = TestClient(app)
    for url in ("/openapi.json", "/static/api/openapi.json"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert not ADMIN_OPERATIONS & _operations(response.json()), url
    mode("session")
    assert ADMIN_OPERATIONS <= _operations(client.get("/openapi.json").json())
