"""Cascade-delete integration test for OAuth2Client → OAuth2Token.

Issue #595 (gate1 acceptance follow-up): pin the cascade-delete behavior
that keeps OAuth2 tokens from outliving their parent client. PR-B's
SQLAlchemy 2.0 migration preserves the kwargs verbatim, but kwarg
preservation is not the same as behavior verification — this test is
the behavior verification.

Two protections are exercised independently:

1. **ORM-driven cascade**: ``OAuth2Client.tokens`` declares
   ``cascade="all, delete"``. Deleting the parent via
   ``session.delete(client)`` must remove the child tokens through the
   ORM unit-of-work.

2. **DB-level cascade**: ``OAuth2Token.client_id`` has a FK with
   ``ondelete="CASCADE"``. A raw SQL ``DELETE FROM oauth_clients``
   that bypasses the ORM must also remove the child tokens at the
   DB layer (defense in depth — if a future refactor drops the ORM
   cascade kwarg, this layer still holds).

If either path silently regresses (a cascade kwarg dropped, an FK
relaxed by a migration), one of the assertions here will fire.
"""

from __future__ import annotations

import os
import sys
import uuid
import warnings
from pathlib import Path

# Match the sys.path layout the rest of the backend integration tests use.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from db.base import get_sync_session  # noqa: E402
from models.auth import OAuth2Client, OAuth2Token  # noqa: E402
from utils.datetime import utcnow  # noqa: E402


def _check_db_available() -> bool:
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


def _assert_test_db(session) -> None:
    db_name = session.execute(text("SELECT current_database()")).scalar()
    assert db_name and db_name.endswith("_test"), (
        f"Refusing to run cascade integration test cleanup against non-test database "
        f"'{db_name}'. Set TEST_DATABASE_URL to a *_test database."
    )


def _assert_alembic_target_is_test_db() -> None:
    """Refuse to run ``alembic upgrade head`` against a non-test database.

    Mirrors the safety guard in ``test_oauth_dcr_integration.py``. A
    misconfigured ``TEST_DATABASE_URL`` (despite the conftest steering)
    must not be able to migrate a dev or prod DB during fixture setup.
    """
    import psycopg2

    sync_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(sync_url, connect_timeout=3)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            row = cur.fetchone()
            db_name = row[0] if row else None
            assert db_name and db_name.endswith("_test"), (
                f"Refusing to run 'alembic upgrade head' against non-test "
                f"database '{db_name}'. Set TEST_DATABASE_URL to a *_test database."
            )
    finally:
        conn.close()


def _reset_alembic_state() -> None:
    """Drop the public schema so ``alembic upgrade head`` starts clean.

    Mirrors the helper of the same name in
    ``tests/integration/test_oauth_dcr_integration.py``. Needed when another
    fixture (e.g. ``tests/conftest.py``'s session-scoped ``async_engine``
    that does ``Base.metadata.create_all``) has already created tables
    in the test DB but did not stamp ``alembic_version`` — running
    ``command.upgrade()`` then fails because migrations attempt to
    create tables that already exist.

    Safety guard: refuses to drop a non-test database;
    ``_assert_alembic_target_is_test_db`` above must already have passed
    before this is invoked.
    """
    from sqlalchemy import create_engine

    sync_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    db_name = sync_url.rsplit("/", 1)[-1].split("?")[0]
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"Refusing to DROP SCHEMA on non-test database '{db_name}'. "
            f"Set TEST_DATABASE_URL to a *_test database."
        )

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def _ensure_oauth_tables_schema():
    """Apply alembic migrations so oauth_clients/oauth_tokens exist in the test DB.

    Mirrors the ``_ensure_oauth_clients_schema`` fixture in
    ``test_oauth_dcr_integration.py``. The drop-and-recreate via
    ``_reset_alembic_state()`` is necessary for mixed-suite runs:
    if a sibling fixture (e.g. ``tests/conftest.py``'s session-scoped
    ``async_engine`` doing ``Base.metadata.create_all``) has already
    created tables without stamping ``alembic_version``, the bare
    ``command.upgrade()`` would fail with "table already exists". Reset
    first, then upgrade head, so the FK ``ondelete="CASCADE"`` on
    ``oauth_tokens.client_id`` is the actual migration-produced constraint,
    not an ORM-recreated one.
    """
    _assert_alembic_target_is_test_db()
    _reset_alembic_state()
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    command.upgrade(config, "head")
    yield


_TEST_CLIENT_NAME_ORM = "CascadeTest ORM Path"
_TEST_CLIENT_NAME_DB = "CascadeTest DB Path"


@pytest.fixture
def sync_db():
    """Yield a real sync DB session bound to the test database.

    Cleanup removes any oauth_clients rows this test created. The
    cascade itself is the behavior under test, so the cleanup uses a
    name-filter that survives partial test failures.
    """
    session = get_sync_session()
    _assert_test_db(session)
    try:
        yield session
    finally:
        # If the test left the session in a failed-flush state
        # (PendingRollbackError), clear it before attempting cleanup so the
        # name-filter DELETE can still run on a fresh transaction. Surface
        # rollback failures as a warning so a regression in session lifecycle
        # is diagnosable instead of silently swallowed.
        try:
            session.rollback()
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            warnings.warn(
                f"session.rollback() failed during cascade-test teardown: {exc!r}",
                RuntimeWarning,
                stacklevel=2,
            )
        _assert_test_db(session)
        session.execute(
            text("DELETE FROM oauth_clients WHERE client_name IN (:n1, :n2)"),
            {"n1": _TEST_CLIENT_NAME_ORM, "n2": _TEST_CLIENT_NAME_DB},
        )
        session.commit()
        session.close()


def _make_client(client_name: str) -> OAuth2Client:
    """Build an OAuth2Client with the minimal required columns.

    ``owner_id`` is set to a non-null sentinel because some test DB schemas
    may pre-date migration ``d04_519_oauth_owner_nullable`` (#519). The
    cascade behavior under test is independent of owner_id nullability.
    """
    return OAuth2Client(
        client_id=f"cascadetest_{uuid.uuid4().hex[:16]}",
        client_secret_hash="0" * 64,  # SHA256 hash placeholder; not validated here
        client_name=client_name,
        owner_id="cascadetest_owner",
        redirect_uris=["http://localhost:9999/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )


def _make_token(client_id: str, *, label: str) -> OAuth2Token:
    """Build an OAuth2Token for the given client_id."""
    return OAuth2Token(
        client_id=client_id,
        user_id=f"cascadetest_user_{label}",
        access_token=f"cascadetest_at_{uuid.uuid4().hex}",
        refresh_token=f"cascadetest_rt_{uuid.uuid4().hex}",
        issued_at=utcnow(),
        expires_in=3600,
    )


def _count_tokens_for_client(session, client_id: str) -> int:
    return session.execute(
        text("SELECT COUNT(*) FROM oauth_tokens WHERE client_id = :cid"),
        {"cid": client_id},
    ).scalar_one()


class TestOAuth2ClientTokenCascade:
    """Issue #595: pin the OAuth2Client → OAuth2Token cascade-delete behavior.

    Three independent assertions:

    1. ORM relationship metadata: ``OAuth2Client.tokens.property.cascade.delete``
       is configured. This isolates the ORM ``cascade="all, delete"`` kwarg
       from the DB-level FK — config introspection cannot pass via FK fallback.
    2. End-to-end ORM API: ``session.delete(client)`` followed by commit
       removes child tokens. Both layers contribute here (ORM cascade fires
       first, FK cascade is the safety net) — this test verifies the
       composite behavior the application code actually relies on.
    3. DB-level FK: raw SQL ``DELETE FROM oauth_clients`` cascades via
       ``ondelete="CASCADE"`` on ``OAuth2Token.client_id``, exercising the
       FK layer in isolation by bypassing the ORM entirely.
    """

    def test_orm_relationship_has_delete_cascade_configured(self):
        """The ORM ``cascade="all, delete"`` kwarg is present on the relationship.

        Pure metadata check — does not exercise the DB. Pins the kwarg
        independently of the FK ``ondelete``, so a regression that drops
        the ORM cascade (but leaves the FK in place) is still caught here
        even though the end-to-end ORM behavior test (below) would still
        pass via FK fallback.

        Addresses copilot-pull-request-reviewer feedback on the original
        test name "test_orm_session_delete_cascades_tokens" — that name
        implied the test isolated the ORM cascade layer, which it could
        not while the FK ``ondelete="CASCADE"`` is also active.
        """
        cascade = OAuth2Client.tokens.property.cascade
        assert cascade.delete is True, (
            f"OAuth2Client.tokens must have cascade='all, delete' (or equivalent "
            f"including 'delete'); got cascade options: {cascade!r}"
        )

    def test_orm_session_delete_removes_tokens_end_to_end(self, sync_db):
        """``session.delete(client)`` removes child tokens (ORM API end-to-end).

        Verifies the application-facing behavior: code that calls
        ``session.delete(oauth_client)`` will see related tokens deleted
        on commit. Both the ORM ``cascade="all, delete"`` and the FK
        ``ondelete="CASCADE"`` can contribute to this outcome — they are
        layered defenses. The dedicated config-introspection test above
        isolates the ORM layer; the dedicated raw-SQL test below isolates
        the FK layer; this test pins the composite behavior callers rely
        on.
        """
        client = _make_client(_TEST_CLIENT_NAME_ORM)
        sync_db.add(client)
        sync_db.flush()  # populate client.client_id without committing

        token_1 = _make_token(client.client_id, label="orm_1")
        token_2 = _make_token(client.client_id, label="orm_2")
        token_3 = _make_token(client.client_id, label="orm_3")
        sync_db.add_all([token_1, token_2, token_3])
        sync_db.commit()

        assert _count_tokens_for_client(sync_db, client.client_id) == 3, (
            "precondition: 3 tokens should be persisted before cascade"
        )

        sync_db.delete(client)
        sync_db.commit()

        assert _count_tokens_for_client(sync_db, client.client_id) == 0, (
            "session.delete(client) + commit should have removed all child tokens "
            "via the layered ORM-cascade + FK-cascade defense"
        )

    def test_raw_sql_delete_cascades_tokens_via_fk(self, sync_db):
        """Raw ``DELETE FROM oauth_clients`` cascades via FK ondelete="CASCADE".

        This bypasses the ORM cascade and exercises the DB-level FK
        constraint independently. If a future migration drops the FK
        ondelete clause, this assertion fires even if the ORM cascade
        kwarg is still in place.
        """
        client = _make_client(_TEST_CLIENT_NAME_DB)
        sync_db.add(client)
        sync_db.flush()
        captured_client_id = client.client_id

        token = _make_token(captured_client_id, label="fk")
        sync_db.add(token)
        sync_db.commit()

        assert _count_tokens_for_client(sync_db, captured_client_id) == 1, (
            "precondition: 1 token should be persisted before raw DELETE"
        )

        # Bypass the ORM unit-of-work entirely. The FK ondelete="CASCADE"
        # on OAuth2Token.client_id must remove the token at the DB layer.
        sync_db.execute(
            text("DELETE FROM oauth_clients WHERE client_id = :cid"),
            {"cid": captured_client_id},
        )
        sync_db.commit()

        assert _count_tokens_for_client(sync_db, captured_client_id) == 0, (
            "OAuth2Token.client_id FK ondelete='CASCADE' should have removed the "
            "child token at the DB layer when the parent OAuth2Client row was "
            "deleted via raw SQL"
        )
