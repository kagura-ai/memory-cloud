"""Unit tests for RoleManager.ensure_user Postgres path (mocked DB).

Issue #481: Lookup-key swap from email to user_id, email/name sync,
HMAC-keyed audit log, IntegrityError → ConflictError.
"""

from contextlib import suppress
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from sqlalchemy.exc import IntegrityError

from auth.roles import _OAUTH_CALLBACK_ACTOR, Role, RoleManager, _is_email_unique_violation
from utils.datetime import utcnow
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


def _user_row(
    *,
    user_id="u1",
    email="alice@example.com",
    name="Alice",
    role="user",
    auth_provider="google",
):
    """Build a minimal mock that mimics models.auth.User attribute access.

    ``auth_provider`` is the account's primary provider (#1811): only a
    sign-in through it syncs email/name.
    """
    user = MagicMock()
    user.user_id = user_id
    user.email = email
    user.name = name
    user.role = role
    user.auth_provider = auth_provider
    return user


def _oauth_link_row(*, user_id="u1"):
    """Build a minimal mock mimicking ``models.auth.UserOAuthProvider`` access.

    Issue #517 dual-read: the NEW resolution path first selects a
    ``UserOAuthProvider`` by ``(provider, oauth_sub)`` then loads the owning
    ``User`` by ``link.user_id`` and touches ``link.last_used_at``. The mock
    exposes ``user_id`` for the owner load and accepts the ``last_used_at``
    write.
    """
    link = MagicMock()
    link.user_id = user_id
    return link


@pytest.fixture
def role_manager():
    return RoleManager(use_postgres=True)


class TestLookupKeyIsUserId:
    """Verify the SELECT statement filters on user_id, not email (Issue #481 core)."""

    @pytest.mark.asyncio
    async def test_lookup_uses_user_id_filter(self, role_manager):
        existing = _user_row(user_id="github-999", email="alice@old.com")
        # #517 NEW path: link lookup (execute #1) → owner User load (execute #2).
        # #938 removed the legacy user_id fallback, so the owner is loaded via the
        # link; the User SELECT (#2) must still filter by user_id, not email (#481).
        db = _make_db_mock(_execute_returns(_oauth_link_row(user_id="github-999"), existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@old.com",
                user_id="github-999",
                auth_provider="github",
                email_verified=True,
            )

        # Inspect the User SELECT (the 2nd execute — owner load by link.user_id).
        user_stmt = db.execute.call_args_list[1].args[0]
        compiled = str(user_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "users.user_id" in compiled
        assert "WHERE" in compiled
        # Ensure email is not the primary lookup criterion in this SELECT.
        assert "users.email" not in compiled.split("WHERE", 1)[1]


class TestSyncEmail:
    @pytest.mark.asyncio
    async def test_syncs_email_when_verified_and_changed(self, role_manager):
        existing = _user_row(email="alice@old.com")
        # NEW path (#517): link lookup → owner load. Legacy user_id fallback gone (#938).
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))

        with _patch_get_db(db):
            role = await role_manager.ensure_user(
                email="alice@new.com",
                user_id="u1",
                auth_provider="google",
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
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@new.com",
                user_id="u1",
                auth_provider="google",
                email_verified=False,
            )

        assert existing.email == "alice@old.com"
        db.add.assert_not_called()
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_sync_when_email_unchanged(self, role_manager):
        existing = _user_row(email="alice@example.com")
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@example.com",
                user_id="u1",
                auth_provider="google",
                email_verified=True,
            )

        db.add.assert_not_called()
        db.commit.assert_awaited_once()


class TestLinkedProviderSkipsSync:
    """#1811: a provider linked to the account is not its primary identity."""

    @pytest.mark.asyncio
    async def test_linked_provider_leaves_email_and_name(self, role_manager):
        existing = _user_row(email="alice@old.com", name="Alice", auth_provider="google")
        link = _oauth_link_row()
        db = _make_db_mock(_execute_returns(link, existing))

        with (
            _patch_get_db(db),
            patch(
                "services.security_notification_service.spawn_email_change_notification"
            ) as notify,
        ):
            role = await role_manager.ensure_user(
                email="alice@github.example",
                user_id="gh-1",
                name="alice-gh",
                auth_provider="github",
                email_verified=True,
            )

        assert role == Role.USER
        assert existing.email == "alice@old.com"
        assert existing.name == "Alice"
        db.add.assert_not_called()
        notify.assert_not_called()
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_race_retry_through_non_primary_provider_skips_sync(self, role_manager):
        """The user_id-race retry path applies the same primary-provider guard.

        The re-resolved row's ``auth_provider`` (None here) differs from the
        provider signing in, so its email and name stay as they are.
        """
        race_existing = _user_row(email="alice@old.com", name="Alice", auth_provider=None)
        db = _make_db_mock(_execute_returns(None, {"scalar": 0}, race_existing))
        db.commit = AsyncMock(side_effect=[_user_id_unique_violation(), None])

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@new.com",
                user_id="u1",
                name="Alice New",
                auth_provider="google",
                email_verified=True,
            )

        assert race_existing.email == "alice@old.com"
        assert race_existing.name == "Alice"


class TestAdoptsAPrimaryProvider:
    """#1875: an OAuth account with no ``auth_provider`` gets the signing-in
    provider, so its email and name sync again — but only from the identity
    the account was created with (its sub is the ``user_id``), and only once
    its link is established: a row older than the identity-link window, or
    written with the account. A provider attached to the account later is
    never adopted, however old its link."""

    OLD = timedelta(days=365)

    @staticmethod
    def _account(*, user_id="u1", created=OLD):
        user = _user_row(user_id=user_id, email="alice@old.com", name="Alice", auth_provider=None)
        user.auth_method = "oauth"
        user.created_at = utcnow() - created
        return user

    @staticmethod
    def _link(*, user_id="u1", oauth_sub="gh-1", linked: timedelta):
        link = _oauth_link_row(user_id=user_id)
        link.oauth_sub = oauth_sub
        link.linked_at = utcnow() - linked
        return link

    @staticmethod
    async def _sign_in(role_manager, db, *, sub="gh-1", provider="github"):
        with (
            _patch_get_db(db),
            patch("services.security_notification_service.spawn_email_change_notification"),
        ):
            await role_manager.ensure_user(
                email="alice@new.com",
                user_id=sub,
                name="Alice New",
                auth_provider=provider,
                email_verified=True,
            )

    @pytest.mark.asyncio
    async def test_the_original_identity_with_a_link_older_than_the_window_is_adopted(
        self, role_manager
    ):
        """Its sub is the ``user_id``; the row was written later than the
        account (attached again after an unlink, or a legacy row healed on a
        later sign-in) but has stood longer than the window."""
        existing = self._account(user_id="g-1")
        link = self._link(user_id="g-1", oauth_sub="g-1", linked=timedelta(minutes=11))
        db = _make_db_mock(_execute_returns(link, existing))

        await self._sign_in(role_manager, db, sub="g-1", provider="google")

        assert existing.auth_provider == "google"
        assert existing.email == "alice@new.com"
        assert existing.name == "Alice New"
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", [timedelta(minutes=11), timedelta(days=365)])
    async def test_a_provider_attached_later_is_never_adopted(self, role_manager, linked):
        """A link whose sub is not the ``user_id`` was attached to the account
        through ``link-provider`` by whoever held a session. However old it
        is, it signs in as a linked provider: no adoption, no profile sync."""
        existing = self._account()
        link = self._link(linked=linked)
        db = _make_db_mock(_execute_returns(link, existing))

        await self._sign_in(role_manager, db)

        assert existing.auth_provider is None
        assert existing.email == "alice@old.com"
        assert existing.name == "Alice"
        db.add.assert_not_called()
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_freshly_linked_provider_is_not_adopted(self, role_manager):
        existing = self._account()
        link = self._link(linked=timedelta(minutes=3))
        db = _make_db_mock(_execute_returns(link, existing))

        await self._sign_in(role_manager, db)

        # The sign-in itself goes through; nothing of the profile moves.
        assert existing.auth_provider is None
        assert existing.email == "alice@old.com"
        assert existing.name == "Alice"
        db.add.assert_not_called()
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_original_identity_is_adopted_at_once(self, role_manager):
        """Its sub is the ``user_id`` and its row was written with the account."""
        created = timedelta(minutes=2)
        existing = self._account(user_id="g-1", created=created)
        link = self._link(user_id="g-1", oauth_sub="g-1", linked=created)
        db = _make_db_mock(_execute_returns(link, existing))

        await self._sign_in(role_manager, db, sub="g-1", provider="google")

        assert existing.auth_provider == "google"
        assert existing.email == "alice@new.com"

    @pytest.mark.asyncio
    async def test_the_same_sub_linked_again_later_is_not_the_original(self, role_manager):
        existing = self._account(user_id="g-1")
        link = self._link(user_id="g-1", oauth_sub="g-1", linked=timedelta(minutes=3))
        db = _make_db_mock(_execute_returns(link, existing))

        await self._sign_in(role_manager, db, sub="g-1", provider="google")

        assert existing.auth_provider is None
        assert existing.email == "alice@old.com"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("linked", "adopted"), [(timedelta(minutes=11), True), (timedelta(minutes=3), False)]
    )
    async def test_race_retry_follows_the_same_rule(self, role_manager, linked, adopted):
        """The racing request wrote this identity's link row for the account
        (the row is selected by ``(provider, oauth_sub == user_id)``)."""
        race_existing = self._account()
        link = self._link(oauth_sub="u1", linked=linked)
        db = _make_db_mock(_execute_returns(None, {"scalar": 0}, race_existing, link))
        db.commit = AsyncMock(side_effect=[_user_id_unique_violation(), None])

        await self._sign_in(role_manager, db, sub="u1", provider="google")

        assert race_existing.auth_provider == ("google" if adopted else None)
        assert race_existing.email == ("alice@new.com" if adopted else "alice@old.com")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("link", [None, "someone-else"])
    async def test_race_retry_leaves_a_row_that_only_shares_the_id(self, role_manager, link):
        """Found by ``user_id`` alone: without a link row of its own for this
        identity, the row must not start syncing from this provider."""
        race_existing = self._account()
        link_row = None if link is None else self._link(user_id=link, linked=timedelta(days=30))
        db = _make_db_mock(_execute_returns(None, {"scalar": 0}, race_existing, link_row))
        db.commit = AsyncMock(side_effect=[_user_id_unique_violation(), None])

        await self._sign_in(role_manager, db, sub="u1", provider="google")

        assert race_existing.auth_provider is None
        assert race_existing.email == "alice@old.com"
        assert race_existing.name == "Alice"

    @pytest.mark.parametrize(
        ("auth_method", "pointer", "signing_in"),
        [
            ("password", None, "github"),  # a password account keeps its own profile
            ("oauth", "google", "github"),  # a primary provider is never replaced
            ("oauth", None, "okta"),  # not a provider an account can link
        ],
    )
    def test_anything_else_is_left_alone(self, auth_method, pointer, signing_in):
        from auth.roles import _adopt_primary_provider

        user = self._account()
        user.auth_provider = pointer
        user.auth_method = auth_method

        _adopt_primary_provider(user, signing_in, self._link(linked=timedelta(days=30)))

        assert user.auth_provider == pointer


class TestSyncName:
    @pytest.mark.asyncio
    async def test_syncs_name_independently_of_email(self, role_manager):
        existing = _user_row(email="alice@example.com", name="Alice Old")
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@example.com",
                user_id="u1",
                name="Alice New",
                auth_provider="google",
                email_verified=False,  # email won't sync, name still should
            )

        assert existing.name == "Alice New"
        assert existing.email == "alice@example.com"
        db.add.assert_not_called()  # No audit log for name-only changes

    @pytest.mark.asyncio
    async def test_does_not_sync_name_when_unchanged(self, role_manager):
        existing = _user_row(name="Alice")
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))

        with _patch_get_db(db):
            await role_manager.ensure_user(
                email=existing.email,
                user_id="u1",
                name="Alice",
                auth_provider="google",
            )

        # last_login_at still updated, but no add/audit
        db.add.assert_not_called()


class TestAuditLogStructure:
    @pytest.mark.asyncio
    async def test_audit_log_uses_hmac_not_plaintext(self, role_manager):
        existing = _user_row(email="alice@old.com")
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))
        test_key = "test-key-32"

        with patch.dict("os.environ", {"AUDIT_HMAC_KEY": test_key}, clear=False):
            # Reset settings singleton so the env var is observed
            import config.settings as cs

            cs._settings = None
            with _patch_get_db(db):
                await role_manager.ensure_user(
                    email="alice@new.com",
                    user_id="u1",
                    auth_provider="google",
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
        # #517 dual-read with a known provider: link lookup hits → owner load.
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))

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
        # #517 dual-read with a known provider: link lookup hits → owner load.
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))

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
        # Sequence (#517 NEW path, #938 no legacy fallback): link lookup miss →
        # count=0 → commit succeeds.
        db = _make_db_mock(_execute_returns(None, {"scalar": 0}))

        with _patch_get_db(db):
            role = await role_manager.ensure_user(
                email="first@example.com",
                user_id="u1",
                auth_provider="google",
                email_verified=True,
            )

        assert role == Role.ADMIN
        added = db.add.call_args_list[0].args[0]
        assert added.role == "admin"
        assert added.is_initial_admin is True

    @pytest.mark.asyncio
    async def test_second_user_gets_user(self, role_manager):
        # Sequence (#517 NEW path, #938 no legacy fallback): link lookup miss →
        # count=1 → commit succeeds.
        db = _make_db_mock(_execute_returns(None, {"scalar": 1}))

        with _patch_get_db(db):
            role = await role_manager.ensure_user(
                email="second@example.com",
                user_id="u2",
                auth_provider="google",
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
        # #517 NEW path (#938 legacy fallback removed): link lookup MISS → count →
        # race re-lookup HIT. Known provider also inserts a UserOAuthProvider row in
        # the create unit of work (rolled back with the user on the race).
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
        # #517 NEW path (#938 legacy fallback removed): link lookup MISS → count=5
        # → commit raises → re-lookup also miss → ConflictError.
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


class TestEmailVerifiedAt:
    """#1752: ``email_verified_at`` follows the IdP's verified attestation only."""

    @pytest.mark.parametrize(("verified", "expect_set"), [(True, True), (False, False)])
    @pytest.mark.asyncio
    async def test_new_user(self, role_manager, verified, expect_set):
        db = _make_db_mock(_execute_returns(None, {"scalar": 1}))
        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="new@example.com",
                user_id="g-new",
                auth_provider="google",
                email_verified=verified,
            )
        added = db.add.call_args_list[0].args[0]
        assert (added.email_verified_at is not None) is expect_set

    @pytest.mark.asyncio
    async def test_existing_user_verified_on_sign_in(self, role_manager):
        existing = _user_row(email="alice@example.com")
        existing.email_verified_at = None
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))
        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@example.com",
                user_id="u1",
                auth_provider="google",
                email_verified=True,
            )
        assert existing.email_verified_at is not None
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unverified_assertion_never_sets_it(self, role_manager):
        existing = _user_row(email="alice@example.com")
        existing.email_verified_at = None
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))
        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@example.com",
                user_id="u1",
                auth_provider="google",
                email_verified=False,
            )
        assert existing.email_verified_at is None

    @pytest.mark.asyncio
    async def test_existing_verification_time_is_kept(self, role_manager):
        from datetime import datetime

        earlier = datetime(2026, 1, 1)
        existing = _user_row(email="alice@example.com")
        existing.email_verified_at = earlier
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))
        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@example.com",
                user_id="u1",
                auth_provider="google",
                email_verified=True,
            )
        assert existing.email_verified_at == earlier

    @pytest.mark.asyncio
    async def test_verified_email_change_resets_it(self, role_manager):
        from datetime import datetime

        earlier = datetime(2026, 1, 1)
        existing = _user_row(email="alice@old.com")
        existing.email_verified_at = earlier
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))
        with _patch_get_db(db):
            await role_manager.ensure_user(
                email="alice@new.com",
                user_id="u1",
                auth_provider="google",
                email_verified=True,
            )
        assert existing.email == "alice@new.com"
        assert existing.email_verified_at is not None
        assert existing.email_verified_at != earlier


class TestEmailChangeNotice:
    """#1752: the previous address is told when a sign-in changes the email."""

    async def _sync(self, role_manager, existing, *, new_email="alice@new.com", db=None):
        db = db or _make_db_mock(_execute_returns(_oauth_link_row(), existing))
        with (
            _patch_get_db(db),
            patch(
                "services.security_notification_service.spawn_email_change_notification"
            ) as spawn,
        ):
            # A colliding address is refused; the caller asserts on the notice.
            with suppress(ConflictError):
                await role_manager.ensure_user(
                    email=new_email,
                    user_id="u1",
                    auth_provider="google",
                    email_verified=True,
                    ip_address="192.0.2.7",
                    user_agent="pytest-agent/1.0",
                )
        return spawn, db

    @pytest.mark.asyncio
    async def test_verified_previous_address_is_notified_after_the_commit(self, role_manager):
        from datetime import datetime

        existing = _user_row(email="alice@old.com")
        existing.email_verified_at = datetime(2026, 1, 1)
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))
        order: list[str] = []
        db.commit.side_effect = lambda: order.append("commit")

        with (
            _patch_get_db(db),
            patch(
                "services.security_notification_service.spawn_email_change_notification",
                side_effect=lambda **kwargs: order.append("notice"),
            ) as spawn,
        ):
            await role_manager.ensure_user(
                email="alice@new.com",
                user_id="u1",
                auth_provider="google",
                email_verified=True,
                ip_address="192.0.2.7",
                user_agent="pytest-agent/1.0",
            )

        assert order == ["commit", "notice"]
        assert spawn.call_args.kwargs == {
            "user_id": "u1",
            "old_email": "alice@old.com",
            "ip": "192.0.2.7",
            "user_agent": "pytest-agent/1.0",
            "auth_provider": "google",
        }

    @pytest.mark.asyncio
    async def test_unverified_previous_address_is_not_notified(self, role_manager):
        existing = _user_row(email="alice@old.com")
        existing.email_verified_at = None
        spawn, _db = await self._sync(role_manager, existing)
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_unchanged_email_notifies_nothing(self, role_manager):
        from datetime import datetime

        existing = _user_row(email="alice@old.com")
        existing.email_verified_at = datetime(2026, 1, 1)
        spawn, _db = await self._sync(role_manager, existing, new_email="alice@old.com")
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_change_notifies_nothing(self, role_manager):
        from datetime import datetime

        existing = _user_row(email="alice@old.com")
        existing.email_verified_at = datetime(2026, 1, 1)
        db = _make_db_mock(_execute_returns(_oauth_link_row(), existing))
        db.commit = AsyncMock(side_effect=_email_unique_violation())
        spawn, _db = await self._sync(role_manager, existing, db=db)
        spawn.assert_not_called()
