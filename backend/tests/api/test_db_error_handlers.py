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
    """FastAPI app with only the two #193 handlers and three raising routes."""
    app = FastAPI()
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
    app.add_exception_handler(ConnectionError, connection_error_handler)

    @app.get("/raise-sqlalchemy")
    async def _raise_sqlalchemy() -> dict:
        raise OperationalError("SELECT 1", {}, Exception("simulated connection drop"))

    @app.get("/raise-connection")
    async def _raise_connection() -> dict:
        raise ConnectionRefusedError(111, "Connect call failed")

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
