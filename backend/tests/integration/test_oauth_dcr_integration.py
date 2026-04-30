"""Integration tests for the DCR endpoint against a real Postgres session.

Issue #519 (#513 follow-up): the unit tests in ``tests/api/test_oauth_dcr.py``
mock ``api.routes.oauth.get_sync_session`` and never reach the actual DB
engine, so the layer mismatch between the Pydantic response model
(``owner_id: str | None``) and the DB column (``oauth_clients.owner_id NOT
NULL``) was not caught until a self-hosted operator tried ``claude mcp add
http://localhost:8080/mcp`` against the merged code and saw a 500 from
``psycopg2.errors.NotNullViolation``.

This test exercises the full ``POST /api/v1/oauth/register`` path against
a real Postgres test database (TEST_DATABASE_URL). It pins three things:

1. DCR loopback path returns 201 (not 500) and persists ``owner_id=None``
   to the DB.
2. The serialized response includes ``owner_id: null`` (Pydantic Optional
   path round-trips).
3. The constraint relaxation does not regress the admin-managed path —
   ``OAuth2Client(owner_id="user-...")`` still inserts cleanly.

This test runs in ``backend/tests/integration/`` because it touches the DB.
``make test-integration`` (per dev-environment.md) runs this suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Match the sys.path layout the rest of the backend tests use.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from api.main import app  # noqa: E402
from db.base import get_sync_session  # noqa: E402
from models.auth import OAuth2Client  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sync_db():
    """Yield a real sync DB session and clean up oauth_clients rows."""
    session = get_sync_session()
    try:
        yield session
    finally:
        # Clean up any oauth_clients rows this test class created.
        session.execute(
            text(
                "DELETE FROM oauth_clients WHERE client_id LIKE 'oauth_%' "
                "AND client_name IN ("
                "  'IntegrationTest Claude Code Loopback', "
                "  'IntegrationTest Admin Path'"
                ")"
            )
        )
        session.commit()
        session.close()


class TestDcrLoopbackPersistsNullOwnerId:
    """Issue #519: ``oauth_clients.owner_id`` must allow NULL for DCR clients."""

    def test_dcr_loopback_returns_201_and_persists_null_owner(self, client, sync_db):
        # Mock the rate-limit counter so this test doesn't share quota with
        # adjacent runs. The DB session and encryptor are real.
        rate_limit = AsyncMock(return_value=1)
        with patch("db.redis.increment_counter", rate_limit):
            response = client.post(
                "/api/v1/oauth/register",
                json={
                    "client_name": "IntegrationTest Claude Code Loopback",
                    "redirect_uris": ["http://localhost:54321/callback"],
                    "token_endpoint_auth_method": "none",
                },
            )

        assert response.status_code == 201, (
            f"DCR loopback INSERT should succeed end-to-end now that #519 has "
            f"flipped oauth_clients.owner_id to NULLable; got {response.status_code} "
            f"{response.text}"
        )
        body = response.json()
        assert body["owner_id"] is None
        assert body["provider"] == "claude"
        assert body["token_endpoint_auth_method"] == "none"

        # Confirm the row actually landed in the DB with owner_id NULL.
        client_id = body["client_id"]
        row = sync_db.execute(
            text("SELECT owner_id, client_name FROM oauth_clients WHERE client_id = :cid"),
            {"cid": client_id},
        ).first()
        assert row is not None, f"DCR row missing for client_id={client_id}"
        assert row.owner_id is None, f"DB row should have owner_id=NULL, got {row.owner_id!r}"
        assert row.client_name == "IntegrationTest Claude Code Loopback"

    def test_admin_path_with_string_owner_still_works(self, sync_db):
        """The constraint relaxation must not regress the admin-managed path.

        Admin clients (``POST /api/v1/oauth/clients``) call
        ``OAuth2Client(owner_id=user.id)`` directly; #519 only relaxes the
        constraint, it does not change the admin handler. Insert a row with
        a non-null ``owner_id`` directly through the ORM to pin the
        regression.
        """
        c = OAuth2Client(
            client_id="oauth_integration_admin_test",
            client_secret_hash="0" * 64,
            client_name="IntegrationTest Admin Path",
            owner_id="user-integration-test-12345",
            redirect_uris=["https://chatgpt.com/cb"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="memory:read",
            token_endpoint_auth_method="client_secret_post",
            provider="chatgpt",
        )
        sync_db.add(c)
        sync_db.commit()
        sync_db.refresh(c)

        assert c.id is not None
        # Re-read to confirm round-trip
        fetched = sync_db.execute(
            text("SELECT owner_id FROM oauth_clients WHERE client_id = :cid"),
            {"cid": "oauth_integration_admin_test"},
        ).first()
        assert fetched is not None
        assert fetched.owner_id == "user-integration-test-12345"
