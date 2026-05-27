"""Unit tests for RoleManager.ensure_user Postgres path (mocked DB).

Issue #481: Lookup-key swap from email to user_id, email/name sync,
HMAC-keyed audit log, IntegrityError → ConflictError.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from sqlalchemy.exc import IntegrityError

from auth.roles import _OAUTH_CALLBACK_ACTOR, Role, RoleManager, _is_email_unique_violation
from utils.exceptions import ConflictError
from utils.hashing import hmac_sha256_hex


class _AsyncpgUniqueViolationStub(Exception):
    """asyncpg-shaped UNIQUE violation stub for the narrowing check.

    ``_is_email_unique_violation`` reads ``exc.orig.sqlstate`` and
    ``exc.orig.constraint_name`` (or its ``__cause__`` chain). Real
    asyncpg surfaces these as instance attributes on
    ``UniqueViolationError``; this minimal stub mimics that shape so
    unit tests don't need a live PostgreSQL connection.
    """

    def __init__(self, constraint_name: str = "ix_users_email"):
        super().__init__(f"unique violation on {constraint_name}")
        self.sqlstate = "23505"
        self.constraint_name = constraint_name


class _SqlAlchemyAsyncpgWrapperStub(Exception):
    """Match the SQLAlchemy 2.0 + asyncpg wrap shape that production hits.

    ``AsyncAdapt_asyncpg_dbapi.IntegrityError`` (the real wrapper) exposes
    ``sqlstate`` and ``pgcode`` on the instance but does NOT set
    ``constraint_name``. The native ``asyncpg.exceptions.UniqueViolationError``
    that carries ``constraint_name`` lives on ``__cause__``. This stub
    mimics that two-level shape so the regression test against the live
    asyncpg behaviour pins ``_is_email_unique_violation``'s cause-walk
    fallback added after a 503 leak was observed during local GitHub
    OAuth testing of PR #522.
    """

    def __init__(self, constraint_name: str = "ix_users_email"):
        super().__init__(
            f"<class 'asyncpg.exceptions.UniqueViolationError'>: "
            f'duplicate key value violates unique constraint "{constraint_name}"'
        )
        self.sqlstate = "23505"
        self.pgcode = "23505"
        # constraint_name intentionally NOT set on this wrapper — that's
        # the wrap behaviour we're regression-testing against.
        self.__cause__ = _AsyncpgUniqueViolationStub(constraint_name=constraint_name)


def _email_unique_violation() -> IntegrityError:
    """IntegrityError with a properly-shaped ``orig`` for the email path."""
    return IntegrityError("UNIQUE", params={}, orig=_AsyncpgUniqueViolationStub())


def _user_id_unique_violation() -> IntegrityError:
    """IntegrityError with constraint_name=ix_users_user_id (race-condition shape).

    Distinct from ``_email_unique_violation`` so race-recovery tests don't
    accidentally exercise the email-collision narrowing path inside
    ``_is_email_unique_violation`` — using the wrong constraint name would
    silently still pass today (the re-lookup-by-user_id branch fires before
    the narrowing check), but a future code reorder could mask a real bug.
    """
    return IntegrityError(
        "UNIQUE", params={}, orig=_AsyncpgUniqueViolationStub(constraint_name="ix_users_user_id")
    )


def _execute_returns(*results):
    """Build a side_effect list of MagicMock execute results.

    Each entry models a single ``await db.execute(...)`` call. The result
    object exposes both ``scalar_one_or_none`` and ``scalar`` so callers can
    use either terminator without surprise: a result built for a User-lookup
    sets ``scalar_one_or_none`` to the row (or None) and ``scalar`` to None;
    a result built for a count(*) sets ``scalar`` to the count and
    ``scalar_one_or_none`` to None. This avoids depending on MagicMock's
    auto-attribute behavior, which would silently return a fresh MagicMock
    instead of None and could mask future refactors.
    """
    side_effects = []
    for r in results:
        result_mock = MagicMock()
        if isinstance(r, dict) and "scalar" in r:
            result_mock.scalar = MagicMock(return_value=r["scalar"])
            result_mock.scalar_one_or_none = MagicMock(return_value=None)
        else:
            result_mock.scalar_one_or_none = MagicMock(return_value=r)
            result_mock.scalar = MagicMock(return_value=None)
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
        test_key = "test-key-32"

        with patch.dict("os.environ", {"AUDIT_HMAC_KEY": test_key}, clear=False):
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
        # Strong assertion: the stored hashes must be exactly the HMAC under
        # the configured key. A regression to plain sha256_hex (or any other
        # non-plaintext 64-char digest) would fail this check, whereas the
        # weaker "not equal to plaintext + 64 chars" form would pass silently.
        assert audit.old_value_hash == hmac_sha256_hex("alice@old.com", test_key)
        assert audit.new_value_hash == hmac_sha256_hex("alice@new.com", test_key)
        # Defense-in-depth: digests change when the key changes (proves the key
        # actually participates in the digest, not just the value).
        assert audit.old_value_hash != hmac_sha256_hex("alice@old.com", "different-key")

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
        # Sequence: User lookup miss → count=0 → commit succeeds
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
        # Sequence: User lookup miss → count=1 → commit succeeds
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
    async def test_user_id_race_routes_through_sync(self, role_manager):
        """Concurrent first-login race (CREATE → IntegrityError on user_id UNIQUE)
        must still update last_login_at AND sync changed email/name on the existing
        row. Returning the role bare-bones would silently skip those side-effects
        (Copilot review on PR #516).
        """
        # Existing row was just inserted by another concurrent request with a
        # stale email — the racing caller's IdP payload has the fresher value.
        race_existing = _user_row(email="alice@old.com", name="Alice Old", role="admin")
        # Sequence: User lookup miss → count=0 → commit raises (user_id
        # race — constraint_name ix_users_user_id, NOT email, so we
        # precisely model the user_id-collision shape rather than reusing
        # the email helper) → re-lookup hits → sync_existing_user commits
        # the update
        db = _make_db_mock(_execute_returns(None, {"scalar": 0}, race_existing))
        db.commit = AsyncMock(side_effect=[_user_id_unique_violation(), None])

        with _patch_get_db(db):
            role = await role_manager.ensure_user(
                email="alice@new.com",
                user_id="u1",
                name="Alice New",
                auth_provider="google",
                email_verified=True,
            )

        assert role == Role.ADMIN
        db.rollback.assert_awaited_once()
        # Race-recovered row was synced (email + name)
        assert race_existing.email == "alice@new.com"
        assert race_existing.name == "Alice New"
        # Audit row written for the email change
        added = [c.args[0] for c in db.add.call_args_list]
        audits = [a for a in added if getattr(a, "action", None) == "oauth_user_email_synced"]
        assert len(audits) == 1

    @pytest.mark.asyncio
    async def test_email_collision_on_create_raises_conflict(self, role_manager):
        # Sequence: User lookup miss → count=5 → commit raises → re-lookup
        # also miss
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


class TestIsEmailUniqueViolationWrapShapes:
    """Regression: ``_is_email_unique_violation`` must handle the
    SQLAlchemy 2.0 + asyncpg wrap shape where ``constraint_name`` lives
    on ``orig.__cause__`` (the native asyncpg exception), NOT on
    ``orig`` itself. The pre-fix version only read ``orig.constraint_name``
    and silently returned False for every cross-provider email collision,
    leaking the raw IntegrityError out as 503 DB-002 instead of the
    intended 409 ConflictError → ``/login?error=email_in_use`` redirect.
    Surfaced during local GitHub OAuth testing of PR #522.
    """

    def test_constraint_name_on_orig_directly(self):
        """Legacy / future-proof path: constraint_name on the wrapper itself."""
        exc = IntegrityError("UNIQUE", params={}, orig=_AsyncpgUniqueViolationStub())
        assert _is_email_unique_violation(exc) is True

    def test_constraint_name_on_cause_chain(self):
        """Today's production wrap shape: constraint_name on orig.__cause__.

        This is what real SQLAlchemy 2.0 + asyncpg produces when the
        users.email UNIQUE constraint trips during a github_callback
        INSERT after a same-email Google account already exists.
        """
        exc = IntegrityError("UNIQUE", params={}, orig=_SqlAlchemyAsyncpgWrapperStub())
        assert _is_email_unique_violation(exc) is True

    def test_message_fallback_when_neither_attribute_set(self):
        """Defensive layer: if a future driver upgrade drops both the
        wrapper and __cause__ constraint_name attributes, the message
        substring scan still hits.
        """

        class _MessageOnlyStub(Exception):
            def __init__(self) -> None:
                super().__init__('duplicate key value violates unique constraint "ix_users_email"')
                self.sqlstate = "23505"

        exc = IntegrityError("UNIQUE", params={}, orig=_MessageOnlyStub())
        assert _is_email_unique_violation(exc) is True

    def test_non_email_constraint_returns_false(self):
        """user_id collision (race) must NOT mis-route as an email
        ConflictError — it goes through the re-lookup-by-user_id branch
        instead. Pinning the negative case so future column additions
        on UNIQUE indexes can't silently piggyback on this narrow gate.
        """
        exc = IntegrityError(
            "UNIQUE",
            params={},
            orig=_AsyncpgUniqueViolationStub(constraint_name="ix_users_user_id"),
        )
        assert _is_email_unique_violation(exc) is False

    def test_non_unique_violation_returns_false(self):
        """A foreign-key or check-constraint violation has different
        sqlstate; the gate must not match."""

        class _FkViolation(Exception):
            sqlstate = "23503"  # FK violation
            constraint_name = "fk_users_workspace"

        exc = IntegrityError("FK", params={}, orig=_FkViolation())
        assert _is_email_unique_violation(exc) is False

    def test_orig_is_none_returns_false(self):
        """Driver-less IntegrityError (defensive): no orig → False.
        Should never happen with asyncpg but pin the fail-safe."""
        # SQLAlchemy's IntegrityError __init__ types orig as BaseException,
        # but the runtime accepts None and our predicate must handle it via
        # getattr(...).
        exc = IntegrityError("UNIQUE", params={}, orig=None)  # type: ignore[arg-type]
        assert _is_email_unique_violation(exc) is False
