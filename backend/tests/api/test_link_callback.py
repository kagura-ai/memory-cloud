"""Tests for the link-mode short-circuit in the OAuth callbacks (Issue #517).

The provider callbacks (`google_callback` / `github_callback`) delegate the
account-linking decision to ``api.routes.auth._maybe_link_redirect``. That
helper is the security-sensitive seam: it must

1. recognise a link round-trip (``oauth2_state_intent:{state}="link"``) vs a
   normal login (return ``None`` and let the caller log in as before),
2. bind the freshly-authorized identity to the *initiating* session user via
   ``AccountLinkingService`` — never to a brand-new user,
3. route every failure (conflict / composite-UNIQUE race / expired state) to a
   ``/profile?error=...`` redirect, never a 500, and
4. NOT create / rotate / delete any session (link ≠ login).

A subset of the tests run against the real PostgreSQL test DB (the
``db_session`` fixture skips when unreachable) to prove an actual
``user_oauth_providers`` row / failed-attempt audit row is written. The rest
are pure unit tests of the branch logic, mirroring
``test_auth_refresh_callback.py``.

The companion ``_email_in_use_redirect`` (login email-collision) change — the
``&link_hint=true`` nudge — is pinned at the bottom.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth as auth_module
from api.routes.auth import _email_in_use_redirect, _maybe_link_redirect
from auth.session import SESSION_COOKIE_NAME
from models.auth import AuditLog, User, UserOAuthProvider
from services.account_linking_service import AccountLinkingService

SESSION_ID = "sess-initiator"


def _mock_session_manager_with_redis(values: dict[str, str | None], *, holds_user: bool = True):
    """Build a session_manager whose Redis ``.get(key)`` returns the
    pre-staged value (or None for absent keys), and whose ``.delete`` is a
    no-op spy. Mirrors the refresh-callback test helper.

    ``holds_user`` is what ``session_holds_user`` answers: whether the pinned
    session still has the pinned account signed in."""
    redis = MagicMock()
    redis.get.side_effect = lambda key: values.get(key)
    redis.delete = MagicMock()
    sm = MagicMock()
    sm._redis = redis
    sm.session_holds_user.return_value = holds_user
    return sm, redis


def _link_state(state: str, user_id: str, *, return_to: str | None = "/profile?linked=1") -> dict:
    """The keys ``POST /me/account/link-provider`` writes under ``state``,
    pinned to ``SESSION_ID``."""
    values = {
        f"oauth2_state_intent:{state}": "link",
        f"oauth2_state_user:{state}": user_id,
        f"oauth2_state_session:{state}": SESSION_ID,
    }
    if return_to is not None:
        values[f"oauth2_return_to:{state}"] = return_to
    return values


def _request(cookie: str | None = SESSION_ID) -> SimpleNamespace:
    """Minimal Request stand-in: the callback reads only the session cookie."""
    cookies = {} if cookie is None else {SESSION_COOKIE_NAME: cookie}
    return SimpleNamespace(cookies=cookies)


def _patch_get_db(db: AsyncSession):
    """Patch ``auth_module.get_db`` to yield the test's real session so the
    helper's internal ``async for db in get_db()`` reuses it (commits become
    visible to the test's subsequent queries)."""

    async def _fake_get_db():
        yield db

    return patch.object(auth_module, "get_db", _fake_get_db)


def _fake_db_with_user(*, email: str = "init@example.com", found: bool = True):
    """A non-DB session stub whose ``execute(...).scalar_one_or_none()`` returns
    the initiating session user (or None when ``found=False``).

    The link-mode callback now loads the session user in the same unit of work
    as ``link()`` so it can audit under the actor's own email (FIX 1) — the
    pure-unit tests that patch ``AccountLinkingService`` therefore need ``db``
    to answer that SELECT before delegating to the mocked service."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(email=email) if found else None
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


async def _make_user(db: AsyncSession, *, suffix: str) -> User:
    user = User(
        email=f"linkcb-{suffix}@example.com",
        user_id=f"u-{suffix}",
        name="Link CB Tester",
        role="user",
        auth_method="oauth",
        auth_provider="google",
    )
    db.add(user)
    await db.commit()
    return user


# ---------------------------------------------------------------------------
# DB-backed: prove a real row / audit is written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unbound_link_creates_row_and_redirects(db_session: AsyncSession, monkeypatch):
    """intent=link + a fresh IdP sub → callback binds the identity to the
    initiating user, a ``user_oauth_providers`` row now exists, and the
    browser is 303-redirected to the ``/profile?linked=1`` return_to."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    suffix = uuid4().hex[:8]
    user = await _make_user(db_session, suffix=suffix)
    new_sub = f"gh-fresh-{suffix}"

    sm, _redis = _mock_session_manager_with_redis(_link_state(suffix, user.user_id))

    with patch.object(auth_module, "_session_manager", sm), _patch_get_db(db_session):
        result = await _maybe_link_redirect(
            state=suffix,
            request=_request(),
            provider="github",
            idp_sub=new_sub,
            idp_email=user.email,
            ip_address="1.2.3.4",
            user_agent="pytest",
        )

    assert isinstance(result, RedirectResponse)
    assert result.status_code == 303
    assert result.headers["location"] == "http://localhost:3000/profile?linked=1"

    row = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="github", oauth_sub=new_sub)
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.user_id == user.user_id


@pytest.mark.asyncio
async def test_conflict_link_redirects_not_500_and_audits(db_session: AsyncSession, monkeypatch):
    """The returned sub already belongs to ANOTHER user → redirect to
    ``/profile?error=provider_already_linked`` (never a 500), and a
    ``oauth_provider_link_failed`` audit row exists for the initiator."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    suffix = uuid4().hex[:8]
    owner = await _make_user(db_session, suffix=f"owner-{suffix}")
    initiator = await _make_user(db_session, suffix=f"init-{suffix}")
    shared_sub = f"gh-shared-{suffix}"

    # Pre-bind the identity to the owner via the real service.
    await AccountLinkingService(db_session).link(
        user_id=owner.user_id,
        provider="github",
        oauth_sub=shared_sub,
        email=owner.email,
    )

    sm, _redis = _mock_session_manager_with_redis(_link_state(suffix, initiator.user_id))

    with patch.object(auth_module, "_session_manager", sm), _patch_get_db(db_session):
        result = await _maybe_link_redirect(
            state=suffix,
            request=_request(),
            provider="github",
            idp_sub=shared_sub,
            idp_email=initiator.email,
            ip_address="1.2.3.4",
            user_agent="pytest",
        )

    assert isinstance(result, RedirectResponse)
    assert result.status_code == 303
    assert (
        result.headers["location"] == "http://localhost:3000/profile?error=provider_already_linked"
    )

    # The identity must still belong to the owner — never re-pointed.
    row = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="github", oauth_sub=shared_sub)
        )
    ).scalar_one()
    assert row.user_id == owner.user_id

    # Failed-attempt audit row written for the initiator (edge case 6).
    audits = (
        (
            await db_session.execute(
                select(AuditLog).filter_by(
                    user_id=initiator.user_id, action="oauth_provider_link_failed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1


# ---------------------------------------------------------------------------
# Pure unit: branch logic without a DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_intent_returns_none_for_normal_login():
    """No ``oauth2_state_intent`` key → normal login. Helper returns None and
    leaves all state keys alone (they belong to the login flow)."""
    sm, redis = _mock_session_manager_with_redis({})
    with patch.object(auth_module, "_session_manager", sm):
        result = await _maybe_link_redirect(
            state="s1",
            request=_request(),
            provider="google",
            idp_sub="sub-1",
            idp_email="x@example.com",
            ip_address=None,
            user_agent=None,
        )
    assert result is None
    redis.delete.assert_not_called()


@pytest.mark.asyncio
async def test_non_link_intent_returns_none():
    """A different intent value (e.g. 'refresh') is not ours → return None."""
    sm, _redis = _mock_session_manager_with_redis({"oauth2_state_intent:s1": "refresh"})
    with patch.object(auth_module, "_session_manager", sm):
        result = await _maybe_link_redirect(
            state="s1",
            request=_request(),
            provider="google",
            idp_sub="sub-1",
            idp_email="x@example.com",
            ip_address=None,
            user_agent=None,
        )
    assert result is None


@pytest.mark.asyncio
async def test_session_manager_missing_returns_none():
    """Module-level ``_session_manager`` is None during early boot → safe None."""
    with patch.object(auth_module, "_session_manager", None):
        result = await _maybe_link_redirect(
            state="s1",
            request=_request(),
            provider="google",
            idp_sub="sub-1",
            idp_email="x@example.com",
            ip_address=None,
            user_agent=None,
        )
    assert result is None


@pytest.mark.asyncio
async def test_expired_user_redirects_link_failed_without_linking(monkeypatch):
    """intent=link set but ``oauth2_state_user`` already TTL'd → cannot
    attribute the identity. Redirect to ``/profile?error=link_failed`` and the
    linking service is never invoked."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    sm, _redis = _mock_session_manager_with_redis(
        {
            "oauth2_state_intent:s1": "link",
            # No oauth2_state_user:s1 → expired.
        }
    )
    fake_service_cls = MagicMock()
    with (
        patch.object(auth_module, "_session_manager", sm),
        patch.object(auth_module, "AccountLinkingService", fake_service_cls),
    ):
        result = await _maybe_link_redirect(
            state="s1",
            request=_request(),
            provider="google",
            idp_sub="sub-1",
            idp_email="x@example.com",
            ip_address=None,
            user_agent=None,
        )
    assert isinstance(result, RedirectResponse)
    assert result.headers["location"] == "http://localhost:3000/profile?error=link_failed"
    fake_service_cls.assert_not_called()


@pytest.mark.asyncio
async def test_deleted_session_user_redirects_link_failed_without_linking(monkeypatch):
    """intent=link + a present ``oauth2_state_user`` BUT the user row was deleted
    mid-flow → the DB SELECT misses, so we redirect to link_failed and never
    call link() (FIX 1: the audit actor must be a loadable session user)."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    sm, _redis = _mock_session_manager_with_redis(_link_state("s1", "u-deleted"))
    fake_service = MagicMock()
    fake_service.link = AsyncMock(return_value=None)
    fake_service_cls = MagicMock(return_value=fake_service)
    with (
        patch.object(auth_module, "_session_manager", sm),
        patch.object(auth_module, "AccountLinkingService", fake_service_cls),
        _patch_get_db(_fake_db_with_user(found=False)),
    ):
        result = await _maybe_link_redirect(
            state="s1",
            request=_request(),
            provider="google",
            idp_sub="sub-1",
            idp_email="x@example.com",
            ip_address=None,
            user_agent=None,
        )
    assert isinstance(result, RedirectResponse)
    assert result.headers["location"] == "http://localhost:3000/profile?error=link_failed"
    fake_service.link.assert_not_called()


@pytest.mark.asyncio
async def test_link_audits_under_session_user_email_not_idp_email(
    db_session: AsyncSession, monkeypatch
):
    """FIX 1: the link audit actor email is the INITIATING SESSION USER's email,
    NOT the IdP-returned ``idp_email`` (which on arm-3 is another user's). Here
    idp_email is a stranger's address; the linked audit row must carry the
    initiator's email."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    suffix = uuid4().hex[:8]
    initiator = await _make_user(db_session, suffix=suffix)
    new_sub = f"gh-actor-{suffix}"

    sm, _redis = _mock_session_manager_with_redis(_link_state(suffix, initiator.user_id))

    with patch.object(auth_module, "_session_manager", sm), _patch_get_db(db_session):
        result = await _maybe_link_redirect(
            state=suffix,
            request=_request(),
            provider="github",
            idp_sub=new_sub,
            idp_email="stranger@evil.example.com",  # NOT the initiator's email
            ip_address=None,
            user_agent=None,
        )

    assert isinstance(result, RedirectResponse)
    audit = (
        await db_session.execute(
            select(AuditLog).filter_by(user_id=initiator.user_id, action="oauth_provider_linked")
        )
    ).scalar_one()
    assert audit.user_email == initiator.email


@pytest.mark.asyncio
async def test_integrity_race_redirects_provider_already_linked(monkeypatch):
    """A composite-UNIQUE race surfaces as ``IntegrityError`` from link() →
    routed to the same conflict redirect, never a 500."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    sm, _redis = _mock_session_manager_with_redis(_link_state("s1", "u-42"))

    fake_service = MagicMock()
    fake_service.link = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup")))
    fake_service_cls = MagicMock(return_value=fake_service)

    with (
        patch.object(auth_module, "_session_manager", sm),
        patch.object(auth_module, "AccountLinkingService", fake_service_cls),
        _patch_get_db(_fake_db_with_user()),
    ):
        result = await _maybe_link_redirect(
            state="s1",
            request=_request(),
            provider="google",
            idp_sub="sub-1",
            idp_email="x@example.com",
            ip_address=None,
            user_agent=None,
        )

    assert isinstance(result, RedirectResponse)
    assert (
        result.headers["location"] == "http://localhost:3000/profile?error=provider_already_linked"
    )


@pytest.mark.asyncio
async def test_link_keys_deleted_on_read(monkeypatch):
    """delete-on-read contract: the three link-only keys are all cleared so the
    state token can't be replayed."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    sm, redis = _mock_session_manager_with_redis(_link_state("s1", "u-42"))

    fake_service = MagicMock()
    fake_service.link = AsyncMock(return_value=None)
    fake_service_cls = MagicMock(return_value=fake_service)

    with (
        patch.object(auth_module, "_session_manager", sm),
        patch.object(auth_module, "AccountLinkingService", fake_service_cls),
        _patch_get_db(_fake_db_with_user()),
    ):
        result = await _maybe_link_redirect(
            state="s1",
            request=_request(),
            provider="google",
            idp_sub="sub-1",
            idp_email="x@example.com",
            ip_address=None,
            user_agent=None,
        )

    assert isinstance(result, RedirectResponse)
    assert result.headers["location"] == "http://localhost:3000/profile?linked=1"
    deleted = {call.args[0] for call in redis.delete.call_args_list}
    assert "oauth2_state_intent:s1" in deleted
    assert "oauth2_state_user:s1" in deleted
    assert "oauth2_state_session:s1" in deleted
    assert "oauth2_return_to:s1" in deleted


# ---------------------------------------------------------------------------
# Session binding: the callback must come from the browser that started the link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_without_session_cookie_links_nothing(db_session: AsyncSession, monkeypatch):
    """A callback that carries no session cookie is not the browser that
    started the link: redirect to ``/profile?error=link_failed`` and insert no
    ``user_oauth_providers`` row."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    suffix = uuid4().hex[:8]
    user = await _make_user(db_session, suffix=suffix)
    new_sub = f"gh-nocookie-{suffix}"

    sm, _redis = _mock_session_manager_with_redis(_link_state(suffix, user.user_id))

    with patch.object(auth_module, "_session_manager", sm), _patch_get_db(db_session):
        result = await _maybe_link_redirect(
            state=suffix,
            request=_request(cookie=None),
            provider="github",
            idp_sub=new_sub,
            idp_email=user.email,
            ip_address="1.2.3.4",
            user_agent="pytest",
        )

    assert isinstance(result, RedirectResponse)
    assert result.status_code == 303
    assert result.headers["location"] == "http://localhost:3000/profile?error=link_failed"
    row = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="github", oauth_sub=new_sub)
        )
    ).scalar_one_or_none()
    assert row is None


@pytest.mark.asyncio
async def test_callback_with_other_sessions_cookie_links_nothing(
    db_session: AsyncSession, monkeypatch
):
    """A callback from a browser holding a DIFFERENT session (the state and
    authorization URL were handed to someone else) must not attach that
    person's identity to the initiating account."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    suffix = uuid4().hex[:8]
    initiator = await _make_user(db_session, suffix=suffix)
    new_sub = f"gh-other-{suffix}"

    sm, _redis = _mock_session_manager_with_redis(_link_state(suffix, initiator.user_id))

    with patch.object(auth_module, "_session_manager", sm), _patch_get_db(db_session):
        result = await _maybe_link_redirect(
            state=suffix,
            request=_request(cookie="sess-someone-else"),
            provider="github",
            idp_sub=new_sub,
            idp_email="other@example.com",
            ip_address="1.2.3.4",
            user_agent="pytest",
        )

    assert isinstance(result, RedirectResponse)
    assert result.headers["location"] == "http://localhost:3000/profile?error=link_failed"
    row = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="github", oauth_sub=new_sub)
        )
    ).scalar_one_or_none()
    assert row is None
    rows = (
        (await db_session.execute(select(UserOAuthProvider).filter_by(user_id=initiator.user_id)))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_callback_without_pinned_session_fails_closed(monkeypatch):
    """intent=link and the user pinned, but no ``oauth2_state_session`` key
    (expired, or a state minted before the key existed) → refuse; the linking
    service is never invoked."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    values = _link_state("s1", "u-42")
    del values["oauth2_state_session:s1"]
    sm, redis = _mock_session_manager_with_redis(values)
    fake_service_cls = MagicMock()
    with (
        patch.object(auth_module, "_session_manager", sm),
        patch.object(auth_module, "AccountLinkingService", fake_service_cls),
    ):
        result = await _maybe_link_redirect(
            state="s1",
            request=_request(),
            provider="google",
            idp_sub="sub-1",
            idp_email="x@example.com",
            ip_address=None,
            user_agent=None,
        )
    assert isinstance(result, RedirectResponse)
    assert result.headers["location"] == "http://localhost:3000/profile?error=link_failed"
    fake_service_cls.assert_not_called()
    # The other link-only keys are still consumed: the state cannot be retried.
    deleted = {call.args[0] for call in redis.delete.call_args_list}
    assert {"oauth2_state_intent:s1", "oauth2_state_user:s1", "oauth2_return_to:s1"} <= deleted


@pytest.mark.asyncio
async def test_callback_when_session_no_longer_holds_user_fails_closed(monkeypatch):
    """The cookie matches the pinned session id, but that session no longer
    holds the pinned account (signed out, reset, or removed from the session
    mid-flow) → refuse without linking."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    sm, _redis = _mock_session_manager_with_redis(_link_state("s1", "u-42"), holds_user=False)
    fake_service_cls = MagicMock()
    with (
        patch.object(auth_module, "_session_manager", sm),
        patch.object(auth_module, "AccountLinkingService", fake_service_cls),
    ):
        result = await _maybe_link_redirect(
            state="s1",
            request=_request(),
            provider="google",
            idp_sub="sub-1",
            idp_email="x@example.com",
            ip_address=None,
            user_agent=None,
        )
    assert isinstance(result, RedirectResponse)
    assert result.headers["location"] == "http://localhost:3000/profile?error=link_failed"
    fake_service_cls.assert_not_called()
    sm.session_holds_user.assert_called_once_with(SESSION_ID, "u-42")


@pytest.mark.asyncio
async def test_success_default_return_to_when_unset(monkeypatch):
    """No ``oauth2_return_to`` → default to ``/profile?linked=1``."""
    monkeypatch.setenv("FRONTEND_URL", "http://example.com")
    sm, _redis = _mock_session_manager_with_redis(_link_state("s1", "u-42", return_to=None))
    fake_service = MagicMock()
    fake_service.link = AsyncMock(return_value=None)
    with (
        patch.object(auth_module, "_session_manager", sm),
        patch.object(auth_module, "AccountLinkingService", MagicMock(return_value=fake_service)),
        _patch_get_db(_fake_db_with_user()),
    ):
        result = await _maybe_link_redirect(
            state="s1",
            request=_request(),
            provider="google",
            idp_sub="sub-1",
            idp_email="x@example.com",
            ip_address=None,
            user_agent=None,
        )
    assert isinstance(result, RedirectResponse)
    assert result.headers["location"] == "http://example.com/profile?linked=1"


# ---------------------------------------------------------------------------
# Login path: email-collision now carries the link_hint nudge (Issue #517 B)
# ---------------------------------------------------------------------------


def test_email_in_use_redirect_has_link_hint(monkeypatch):
    """A normal-login email collision (ensure_user ConflictError) redirects to
    the login page with BOTH ``error=email_in_use`` and ``link_hint=true`` so
    the UI can nudge the user toward linking instead of a fresh signup."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    result = _email_in_use_redirect()
    assert isinstance(result, RedirectResponse)
    assert result.status_code == 303
    location = result.headers["location"]
    assert "error=email_in_use" in location
    assert "link_hint=true" in location
    assert location.startswith("http://localhost:3000/login")
