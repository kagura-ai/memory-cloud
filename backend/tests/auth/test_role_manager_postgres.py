"""Unit tests for RoleManager.ensure_user Postgres path (mocked DB).

Issue #481: Lookup-key swap from email to user_id, email/name sync,
HMAC-keyed audit log, IntegrityError → ConflictError.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from sqlalchemy.exc import IntegrityError

from auth.roles import _OAUTH_CALLBACK_ACTOR, Role, RoleManager
from utils.exceptions import ConflictError


class _AsyncpgUniqueViolationStub(Exception):
    """asyncpg-shaped UNIQUE violation stub for the narrowing check.

    ``_is_email_unique_violation`` reads ``exc.orig.sqlstate`` and
    ``exc.orig.constraint_name``. Real asyncpg surfaces these as instance
    attributes on ``UniqueViolationError``; this minimal stub mimics that
    shape so unit tests don't need a live PostgreSQL connection.
    """

    def __init__(self, constraint_name: str = "ix_users_email"):
        super().__init__(f"unique violation on {constraint_name}")
        self.sqlstate = "23505"
        self.constraint_name = constraint_name


def _email_unique_violation() -> IntegrityError:
    """IntegrityError with a properly-shaped ``orig`` for the email path."""
    return IntegrityError("UNIQUE", params={}, orig=_AsyncpgUniqueViolationStub())


def _execute_returns(*results):
    """Build a side_effect list of MagicMock execute results.

    Each entry models a single ``await db.execute(...)`` call. The result
    object exposes ``scalar_one_or_none`` AND ``scalar`` because callers use
    different terminators on different queries (User lookup vs count(*)).
    """
    side_effects = []
    for r in results:
        result_mock = MagicMock()
        if isinstance(r, dict) and "scalar" in r:
            result_mock.scalar = MagicMock(return_value=r["scalar"])
        else:
            result_mock.scalar_one_or_none = MagicMock(return_value=r)
        side_effects.append(result_mock)
    return side_effects


def _make_db_mock(execute_results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_results)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    return db


def _patch_get_db(db_mock):
    async def _fake_get_db():
        yield db_mock

    return patch("db.base.get_db", new=_fake_get_db)


def _user_row(*, user_id="u1", email="alice@example.com", name="Alice", role="user"):
    """Build a minimal mock that mimics models.auth.User attribute access."""
    user = MagicMock()
    user.user_id = user_id
    user.email = email
    user.name = name
    user.role = role
    return user


@pytest.fixture
def role_manager():
    return RoleManager(use_postgres=True)


class TestLookupKeyIsUserId:
    """Verify the SELECT statement filters on user_id, not email (Issue #481 core)."""

    @pytest.mark.asyncio
    async def test_lookup_uses_user_id_filter(self, role_manager):
        existing = _user_row(user_id="github-999", email="alice@old.com")
        db = _make_db_mock(_execute_returns(existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@old.com",
                user_id="github-999",
                email_verified=True,
            )

        # Inspect the executed SELECT statement: must filter by user_id.
        first_stmt = db.execute.call_args_list[0].args[0]
        compiled = str(first_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "users.user_id" in compiled
        assert "WHERE" in compiled
        # Ensure email is not the primary lookup criterion in this SELECT.
        assert "users.email" not in compiled.split("WHERE", 1)[1]


class TestSyncEmail:
    @pytest.mark.asyncio
    async def test_syncs_email_when_verified_and_changed(self, role_manager):
        existing = _user_row(email="alice@old.com")
        db = _make_db_mock(_execute_returns(existing))

        with _patch_get_db(db):
            role = await role_manager.ensure_user(
                email="alice@new.com",
                user_id="u1",
                email_verified=True,
            )

        assert role == Role.USER
        assert existing.email == "alice@new.com"
        # Audit log row added before commit.
        added = [c.args[0] for c in db.add.call_args_list]
        audits = [a for a in added if getattr(a, "action", None) == "oauth_user_email_synced"]
        assert len(audits) == 1
        # user_email is a sentinel actor, NOT the subject's email — keeps
        # plaintext PII out of audit_logs.user_email even after future syncs.
        assert audits[0].user_email == _OAUTH_CALLBACK_ACTOR
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_sync_when_email_not_verified(self, role_manager):
        existing = _user_row(email="alice@old.com")
        db = _make_db_mock(_execute_returns(existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@new.com",
                user_id="u1",
                email_verified=False,
            )

        assert existing.email == "alice@old.com"
        db.add.assert_not_called()
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_sync_when_email_unchanged(self, role_manager):
        existing = _user_row(email="alice@example.com")
        db = _make_db_mock(_execute_returns(existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@example.com",
                user_id="u1",
                email_verified=True,
            )

        db.add.assert_not_called()
        db.commit.assert_awaited_once()


class TestSyncName:
    @pytest.mark.asyncio
    async def test_syncs_name_independently_of_email(self, role_manager):
        existing = _user_row(email="alice@example.com", name="Alice Old")
        db = _make_db_mock(_execute_returns(existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@example.com",
                user_id="u1",
                name="Alice New",
                email_verified=False,  # email won't sync, name still should
            )

        assert existing.name == "Alice New"
        assert existing.email == "alice@example.com"
        db.add.assert_not_called()  # No audit log for name-only changes

    @pytest.mark.asyncio
    async def test_does_not_sync_name_when_unchanged(self, role_manager):
        existing = _user_row(name="Alice")
        db = _make_db_mock(_execute_returns(existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email=existing.email,
                user_id="u1",
                name="Alice",
            )

        # last_login_at still updated, but no add/audit
        db.add.assert_not_called()


class TestAuditLogStructure:
    @pytest.mark.asyncio
    async def test_audit_log_uses_hmac_not_plaintext(self, role_manager):
        existing = _user_row(email="alice@old.com")
        db = _make_db_mock(_execute_returns(existing))

        with patch.dict("os.environ", {"AUDIT_HMAC_KEY": "test-key-32"}, clear=False):
            # Reset settings singleton so the env var is observed
            import config.settings as cs

            cs._settings = None
            with _patch_get_db(db):
                await role_manager.ensure_user(
                    email="alice@new.com",
                    user_id="u1",
                    email_verified=True,
                )
            cs._settings = None  # cleanup

        audit = db.add.call_args_list[0].args[0]
        assert audit.action == "oauth_user_email_synced"
        # No plaintext email leaked into hash columns
        assert audit.old_value_hash != "alice@old.com"
        assert audit.new_value_hash != "alice@new.com"
        # HMAC-SHA256 hex is exactly 64 chars
        assert len(audit.old_value_hash) == 64
        assert len(audit.new_value_hash) == 64
        # Different inputs → different digests
        assert audit.old_value_hash != audit.new_value_hash

    @pytest.mark.asyncio
    async def test_audit_log_captures_ip_user_agent(self, role_manager):
        existing = _user_row(email="alice@old.com")
        db = _make_db_mock(_execute_returns(existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@new.com",
                user_id="u1",
                auth_provider="google",
                email_verified=True,
                ip_address="203.0.113.1",
                user_agent="Mozilla/5.0",
            )

        audit = db.add.call_args_list[0].args[0]
        assert audit.ip_address == "203.0.113.1"
        assert audit.user_agent == "Mozilla/5.0"
        assert audit.user_metadata == {"auth_provider": "google"}


class TestUpdateCollision:
    @pytest.mark.asyncio
    async def test_collision_raises_conflict_and_logs_alert(self, role_manager):
        existing = _user_row(email="alice@old.com")
        db = _make_db_mock(_execute_returns(existing))

        # Commit raises IntegrityError (UNIQUE violation on users.email)
        db.commit = AsyncMock(side_effect=_email_unique_violation())

        with _patch_get_db(db), structlog.testing.capture_logs() as logs:
            with pytest.raises(ConflictError) as exc_info:
                await role_manager.ensure_user(
                    email="taken@example.com",
                    user_id="u1",
                    auth_provider="google",
                    email_verified=True,
                )

        assert exc_info.value.status_code == 409
        assert exc_info.value.error_code == "RES-002"
        db.rollback.assert_awaited_once()
        alerts = [e for e in logs if e.get("event") == "oauth_email_collision_attempt"]
        assert len(alerts) == 1
        assert alerts[0]["phase"] == "update"
        assert alerts[0]["auth_provider"] == "google"
        assert "new_email_hmac" in alerts[0]
        # No plaintext email in the alert
        assert alerts[0]["new_email_hmac"] != "taken@example.com"


class TestCreatePath:
    @pytest.mark.asyncio
    async def test_first_user_gets_admin(self, role_manager):
        # Sequence: lookup miss → count=0 → commit succeeds
        db = _make_db_mock(_execute_returns(None, {"scalar": 0}))

        with _patch_get_db(db):
            role = await role_manager.ensure_user(
                email="first@example.com",
                user_id="u1",
                email_verified=True,
            )

        assert role == Role.ADMIN
        added = db.add.call_args_list[0].args[0]
        assert added.role == "admin"
        assert added.is_initial_admin is True

    @pytest.mark.asyncio
    async def test_second_user_gets_user(self, role_manager):
        db = _make_db_mock(_execute_returns(None, {"scalar": 1}))

        with _patch_get_db(db):
            role = await role_manager.ensure_user(
                email="second@example.com",
                user_id="u2",
                email_verified=True,
            )

        assert role == Role.USER
        added = db.add.call_args_list[0].args[0]
        assert added.role == "user"
        assert added.is_initial_admin is False

    @pytest.mark.asyncio
    async def test_user_id_race_returns_existing_role(self, role_manager):
        race_existing = _user_row(role="admin")
        # Sequence: lookup miss → count=0 → commit raises (race) → re-lookup hits
        db = _make_db_mock(_execute_returns(None, {"scalar": 0}, race_existing))
        db.commit = AsyncMock(
            side_effect=[
                _email_unique_violation(),
                None,
            ]
        )

        with _patch_get_db(db):
            role = await role_manager.ensure_user(
                email="alice@example.com",
                user_id="u1",
                email_verified=True,
            )

        assert role == Role.ADMIN
        db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_email_collision_on_create_raises_conflict(self, role_manager):
        # Sequence: lookup miss → count → commit raises → re-lookup also miss
        db = _make_db_mock(_execute_returns(None, {"scalar": 5}, None))
        db.commit = AsyncMock(side_effect=_email_unique_violation())

        with _patch_get_db(db), structlog.testing.capture_logs() as logs:
            with pytest.raises(ConflictError):
                await role_manager.ensure_user(
                    email="taken@example.com",
                    user_id="brand-new-sub",
                    auth_provider="github",
                    email_verified=True,
                )

        alerts = [e for e in logs if e.get("event") == "oauth_email_collision_attempt"]
        assert len(alerts) == 1
        assert alerts[0]["phase"] == "create"
