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

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

# ---------------------------------------------------------------------------
# Hermetic environment setup — must happen BEFORE importing the FastAPI app /
# db modules, which read DATABASE_URL / API_KEY_SECRET at import time.
#
# - Force ``DATABASE_URL`` to ``TEST_DATABASE_URL`` (or the project default
#   ``..._test`` DB) so a developer with a dev-DB ``DATABASE_URL`` exported
#   in their shell cannot have this test write to or DELETE FROM the wrong
#   database. Mirrors the safety pattern in
#   ``tests/integration/test_alembic_migrations.py``.
# - Provide test-only fallbacks for ``API_KEY_SECRET`` / ``JWT_SECRET`` via
#   ``setdefault`` (do NOT override real values when the operator sourced
#   ``.env.local`` themselves) so the test passes hermetically when run
#   under bare ``pytest tests/integration/...`` without ``.env.local``.
# ---------------------------------------------------------------------------
_DEFAULT_TEST_DB_URL = "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test"
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", _DEFAULT_TEST_DB_URL)
os.environ.setdefault("API_KEY_SECRET", "integration-test-api-key-secret-not-for-prod")
os.environ.setdefault("JWT_SECRET", "integration-test-jwt-secret-not-for-prod")

# Match the sys.path layout the rest of the backend tests use.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from api.main import app  # noqa: E402
from db.base import get_sync_session  # noqa: E402
from models.auth import OAuth2Client  # noqa: E402


def _check_db_available() -> bool:
    """Return ``True`` iff the test Postgres at ``DATABASE_URL`` accepts a connection.

    Mirrors the ``_check_db_available()`` helper in
    ``tests/api/test_api_integration.py`` so this module skips cleanly
    when the test DB isn't running, instead of erroring at fixture setup.
    """
    try:
        import psycopg2

        url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
        conn = psycopg2.connect(url, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _check_db_available(),
    reason="Test database not available (set TEST_DATABASE_URL)",
)


@pytest.fixture(scope="module", autouse=True)
def _ensure_oauth_clients_schema():
    """Apply alembic migrations so the test DB matches the production schema.

    Critical: setting up the schema via ``alembic upgrade head`` (instead of
    ``Base.metadata.create_all``) means this test actually exercises migration
    ``d04_519_oauth_owner_nullable``. Using ``create_all`` would build tables
    from the *current ORM models* (which already have ``owner_id`` nullable),
    so a missing or buggy migration would still let the test pass — defeating
    the regression-pin purpose. Only ``alembic upgrade head`` proves the
    migration itself produces the expected DB constraint.

    Mirrors the ``_alembic_at_test_db`` pattern in
    ``tests/integration/test_alembic_migrations.py``: alembic's ``env.py``
    re-reads ``get_database_url()`` on import and clobbers any
    ``Config.set_main_option("sqlalchemy.url", ...)``, so the test DB URL
    must be in the process env at command time. The module-level
    ``os.environ["DATABASE_URL"]`` override above is already in place;
    ``alembic.ini`` is at ``backend/alembic.ini``.
    """
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    command.upgrade(config, "head")
    yield


def _assert_oauth_clients_owner_id_nullable() -> None:
    """Pin that ``oauth_clients.owner_id`` is nullable post-migration.

    Reads ``information_schema.columns`` directly so the assertion does not
    depend on the ORM model's view of the column — this catches the case
    where the migration is missing or wrong even though the model still says
    ``nullable=True``.
    """
    sync_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    import psycopg2

    conn = psycopg2.connect(sync_url, connect_timeout=3)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'oauth_clients' AND column_name = 'owner_id'"
            )
            row = cur.fetchone()
            assert row is not None, "oauth_clients.owner_id column missing"
            assert row[0] == "YES", (
                f"oauth_clients.owner_id must be nullable post-migration; "
                f"information_schema reports is_nullable={row[0]!r}"
            )
    finally:
        conn.close()


def _assert_test_db(session) -> None:
    """Refuse to run destructive cleanup on non-test databases.

    Mirrors the safety pattern in ``test_alembic_migrations.py``. A
    misconfigured ``DATABASE_URL`` (despite the module-level override above)
    must not be able to ``DELETE FROM oauth_clients`` against a dev or prod
    database. The check fires at fixture teardown immediately before the
    cleanup statement runs.
    """
    db_name = session.execute(text("SELECT current_database()")).scalar()
    assert db_name and db_name.endswith("_test"), (
        f"Refusing to run integration test cleanup against non-test database "
        f"'{db_name}'. Set TEST_DATABASE_URL to a *_test database."
    )


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sync_db():
    """Yield a real sync DB session bound to the test database.

    Asserts the connected DB name ends with ``_test`` before yielding; the
    same guard runs again at teardown immediately before the cleanup DELETE
    so a config drift cannot wipe oauth_clients rows from a dev/prod DB.
    """
    session = get_sync_session()
    _assert_test_db(session)
    try:
        yield session
    finally:
        _assert_test_db(session)
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

    def test_migration_made_owner_id_nullable(self):
        """Pin the migration's DB-level effect via information_schema.

        This is the regression check that catches "migration is missing or
        wrong" — independent of the ORM model's view of the column. If
        ``d04_519_oauth_owner_nullable`` were dropped, the rest of the suite
        would still pass (because Pydantic + SQLAlchemy say nullable=True),
        but this assertion would fail on a freshly migrated DB.
        """
        _assert_oauth_clients_owner_id_nullable()

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
