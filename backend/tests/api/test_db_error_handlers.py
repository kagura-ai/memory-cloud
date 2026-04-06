"""Regression tests for DB-unavailable exception handlers (#193).

Public routes — the ones that run before any auth dependency — used to
return bare ``500 Internal Server Error`` responses when the database
was briefly unreachable. The ``SQLAlchemyError`` and ``ConnectionError``
handlers in ``api.main`` now convert those into structured 503 JSON
responses with ``Retry-After``.

These tests build an *isolated* FastAPI app and wire the same handlers
onto it so we can add throw-routes without polluting the global ``app``
that the smoke test scans.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from api.main import connection_error_handler, sqlalchemy_exception_handler


def _build_isolated_app() -> FastAPI:
    """FastAPI app with only the #193 handlers and the test routes.

    Mirrors the production registration in ``api.main``: handlers are
    registered on specific OSError subclasses (NOT on plain
    ``ConnectionError``), so we don't accidentally catch the plain
    ``ConnectionError`` that ``auth/session.py`` raises for Redis.
    """
    app = FastAPI()
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
    for exc_cls in (
        ConnectionRefusedError,
        ConnectionResetError,
        ConnectionAbortedError,
        BrokenPipeError,
    ):
        app.add_exception_handler(exc_cls, connection_error_handler)

    @app.get("/raise-sqlalchemy")
    async def _raise_sqlalchemy() -> dict:
        raise OperationalError("SELECT 1", {}, Exception("simulated connection drop"))

    @app.get("/raise-connection")
    async def _raise_connection() -> dict:
        raise ConnectionRefusedError(111, "Connect call failed")

    @app.get("/raise-redis-style")
    async def _raise_redis_style() -> dict:
        # auth/session.py raises this exact pattern for Redis failures.
        raise ConnectionError("Failed to connect to Redis: timeout")

    @app.get("/raise-value")
    async def _raise_value() -> dict:
        raise ValueError("unrelated bug")

    return app


def _assert_database_unavailable_response(response) -> None:
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    body = response.json()
    # DatabaseConnectionError error_code (see utils/exceptions.py)
    assert body["error"] == "DB-002"
    assert body["message"] == "Database connection failed"


class TestDatabaseErrorHandlers:
    def test_sqlalchemy_error_returns_503(self):
        with TestClient(_build_isolated_app(), raise_server_exceptions=False) as client:
            response = client.get("/raise-sqlalchemy")
        _assert_database_unavailable_response(response)

    def test_connection_refused_returns_503(self):
        """Regression for #193: raw asyncpg ConnectionRefusedError was
        unwrapped by SQLAlchemy and reached Starlette as a bare 500."""
        with TestClient(_build_isolated_app(), raise_server_exceptions=False) as client:
            response = client.get("/raise-connection")
        _assert_database_unavailable_response(response)

    def test_non_db_errors_are_not_caught(self):
        """ConnectionError handler must not swallow ValueError or other
        unrelated exceptions — those should still surface as 500."""
        with TestClient(_build_isolated_app(), raise_server_exceptions=False) as client:
            response = client.get("/raise-value")
        assert response.status_code == 500

    def test_plain_connection_error_is_not_caught(self):
        """Regression for PR #202 review: auth/session.py raises plain
        ``ConnectionError("Failed to connect to Redis: ...")`` for Redis
        failures. We must NOT report Redis outages as DB-002, so the
        handler is registered on specific OSError subclasses
        (ConnectionRefusedError / Reset / Aborted / BrokenPipe) — never
        on the bare ``ConnectionError`` base class.
        """
        with TestClient(_build_isolated_app(), raise_server_exceptions=False) as client:
            response = client.get("/raise-redis-style")
        assert response.status_code == 500
        # Body should NOT carry the DB-002 error code
        body = response.text
        assert "DB-002" not in body
        assert "Database connection failed" not in body
