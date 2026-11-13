"""``/me/announcement-emails`` and the public unsubscribe routes (Issue #2761).

Settings (session only):
- GET reports the answer and whether the deployment offers the emails;
- PUT on needs the feature and the current wording (409 otherwise); PUT off
  always works, even with the feature off; every change is recorded with source
  ``settings``; per-user 429 for requests that turn it on (a request that
  turns it off is neither counted nor refused); API keys are refused.

Unsubscribe (no auth):
- the one-click POST answers the same 200 for every token, never 429, and does
  no database or Redis work for an invalid token; a valid token is applied, and
  a failed write is a 503;
- the GET redirects to the page with a token re-issued from a valid token's
  claim in the fragment (never the request's text; anything else lands on the
  page without one), no-store + no-referrer, and changes nothing;
- the page's body POST: the same 200; invalid tokens are counted per client
  address and refused with 429 past the limit; a valid one never.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from api.routes import announcement_emails as routes
from auth.announcement_unsubscribe import (
    UnsubscribeClaim,
    issue_unsubscribe_token,
    read_unsubscribe_token,
)
from auth.dependencies import require_session_auth
from config.settings import get_settings
from db.base import get_db
from services.announcement_consent_service import (
    ANNOUNCEMENT_CONSENT_WORDING_VERSION as V,
)
from services.announcement_consent_service import ConsentRecordResult, ConsentState
from utils.exceptions import RateLimitError, RedisError

USER = {"user_id": "u1", "email": "u@example.test"}
WHEN = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def enabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "announcement_emails_enabled", True)


@pytest.fixture
def disabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "announcement_emails_enabled", False)


@pytest.fixture
def no_limit(monkeypatch) -> AsyncMock:
    stub = AsyncMock(return_value=1)
    monkeypatch.setattr(routes, "increment_counter", stub)
    return stub


@pytest.fixture
def record(monkeypatch) -> AsyncMock:
    async def _record(db, *, user_id, value, source, wording_version):
        return ConsentRecordResult(
            state=ConsentState(
                value=value, source=source, wording_version=wording_version, recorded_at=WHEN
            ),
            recorded=True,
        )

    stub = AsyncMock(side_effect=_record)
    monkeypatch.setattr(routes, "record_announcement_consent", stub)
    return stub


def _db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


class TestGet:
    @pytest.mark.asyncio
    async def test_never_answered(self, monkeypatch, enabled) -> None:
        monkeypatch.setattr(routes, "current_announcement_consent", AsyncMock(return_value=None))

        body = await routes.get_announcement_emails(USER, db=_db())

        assert body.offered is True
        assert body.announcement_emails is False
        assert body.answered is False
        assert body.wording_version == V

    @pytest.mark.asyncio
    async def test_on_while_disabled_still_shows_it(self, monkeypatch, disabled) -> None:
        state = ConsentState(value=True, source="signup", wording_version=V, recorded_at=WHEN)
        monkeypatch.setattr(routes, "current_announcement_consent", AsyncMock(return_value=state))

        body = await routes.get_announcement_emails(USER, db=_db())

        assert body.offered is False
        assert body.announcement_emails is True
        assert body.source == "signup"
        assert body.wording_version is None
        assert body.model_dump(mode="json")["recorded_at"] == "2026-11-12T09:00:00Z"


class TestPut:
    @pytest.mark.asyncio
    async def test_on_records_settings(self, enabled, record, no_limit) -> None:
        db = _db()
        body = await routes.update_announcement_emails(
            routes.AnnouncementEmailsUpdateRequest(announcement_emails=True, wording_version=V),
            USER,
            db=db,
        )

        assert body.announcement_emails is True and body.recorded is True
        kwargs = record.await_args.kwargs
        assert kwargs == {
            "user_id": "u1",
            "value": True,
            "source": "settings",
            "wording_version": V,
        }
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_off_records_without_a_version(self, enabled, record, no_limit) -> None:
        body = await routes.update_announcement_emails(
            routes.AnnouncementEmailsUpdateRequest(announcement_emails=False, wording_version=V),
            USER,
            db=_db(),
        )

        assert body.announcement_emails is False
        assert record.await_args.kwargs["wording_version"] is None
        assert record.await_args.kwargs["source"] == "settings"

    @pytest.mark.asyncio
    async def test_off_works_while_disabled(self, disabled, record, no_limit) -> None:
        body = await routes.update_announcement_emails(
            routes.AnnouncementEmailsUpdateRequest(announcement_emails=False),
            USER,
            db=_db(),
        )

        assert body.offered is False
        assert body.announcement_emails is False
        record.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_while_disabled_is_409(self, disabled, record, no_limit) -> None:
        with pytest.raises(HTTPException) as exc:
            await routes.update_announcement_emails(
                routes.AnnouncementEmailsUpdateRequest(announcement_emails=True, wording_version=V),
                USER,
                db=_db(),
            )

        assert exc.value.status_code == 409
        assert "not offered" in exc.value.detail
        record.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", [None, "2020-01"])
    async def test_on_with_stale_wording_is_409(self, enabled, record, no_limit, version) -> None:
        with pytest.raises(HTTPException) as exc:
            await routes.update_announcement_emails(
                routes.AnnouncementEmailsUpdateRequest(
                    announcement_emails=True, wording_version=version
                ),
                USER,
                db=_db(),
            )

        assert exc.value.status_code == 409
        assert "Reload" in exc.value.detail
        record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_per_user_limit(self, enabled, record, monkeypatch) -> None:
        counter = AsyncMock(return_value=21)
        monkeypatch.setattr(routes, "increment_counter", counter)

        with pytest.raises(RateLimitError):
            await routes.update_announcement_emails(
                routes.AnnouncementEmailsUpdateRequest(announcement_emails=True, wording_version=V),
                USER,
                db=_db(),
            )

        assert counter.await_args.args[0] == "announce_pref_user:u1"
        record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_turning_it_off_is_never_refused_by_the_limit(
        self, enabled, record, monkeypatch
    ) -> None:
        """Withdrawing consent must always work, however many requests came first."""
        hits: dict[str, int] = {}

        async def _counter(key, ttl):
            hits[key] = hits.get(key, 0) + 1
            return hits[key]

        monkeypatch.setattr(routes, "increment_counter", AsyncMock(side_effect=_counter))
        on = routes.AnnouncementEmailsUpdateRequest(announcement_emails=True, wording_version=V)
        for _ in range(20):
            await routes.update_announcement_emails(on, USER, db=_db())
        for _ in range(5):
            with pytest.raises(RateLimitError):
                await routes.update_announcement_emails(on, USER, db=_db())

        body = await routes.update_announcement_emails(
            routes.AnnouncementEmailsUpdateRequest(announcement_emails=False),
            USER,
            db=_db(),
        )

        assert body.announcement_emails is False and body.recorded is True
        assert record.await_args.kwargs["value"] is False
        # Turning it off is not counted, so it cannot use up the allowance either.
        assert hits == {"announce_pref_user:u1": 25}

    @pytest.mark.asyncio
    async def test_off_never_touches_the_counter(self, enabled, record, monkeypatch) -> None:
        stub = AsyncMock(return_value=10_000)
        monkeypatch.setattr(routes, "increment_counter", stub)

        body = await routes.update_announcement_emails(
            routes.AnnouncementEmailsUpdateRequest(announcement_emails=False),
            USER,
            db=_db(),
        )

        assert body.recorded is True
        stub.assert_not_awaited()

    def test_turning_it_off_after_the_limit_is_200_over_http(
        self, enabled, record, monkeypatch
    ) -> None:
        """The same through the route: 20 on-requests, the 21st is 429, off is 200."""
        hits: dict[str, int] = {}

        async def _counter(key, ttl):
            hits[key] = hits.get(key, 0) + 1
            return hits[key]

        monkeypatch.setattr(routes, "increment_counter", AsyncMock(side_effect=_counter))
        db = _db()

        async def _get_db():
            yield db

        async def _user():
            return dict(USER)

        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[require_session_auth] = _user
        try:
            client = TestClient(app, raise_server_exceptions=False)
            url = "/api/v1/me/announcement-emails"
            on = {"announcement_emails": True, "wording_version": V}
            statuses = [client.put(url, json=on).status_code for _ in range(21)]
            response = client.put(url, json={"announcement_emails": False})
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_session_auth, None)

        assert statuses == [200] * 20 + [429]
        assert response.status_code == 200, response.text
        assert response.json()["announcement_emails"] is False
        assert hits == {"announce_pref_user:u1": 21}

    @pytest.mark.asyncio
    async def test_redis_outage_fails_open(self, enabled, record, monkeypatch) -> None:
        monkeypatch.setattr(routes, "increment_counter", AsyncMock(side_effect=RedisError("x")))

        body = await routes.update_announcement_emails(
            routes.AnnouncementEmailsUpdateRequest(announcement_emails=True, wording_version=V),
            USER,
            db=_db(),
        )

        assert body.recorded is True

    def test_value_must_be_a_real_bool(self) -> None:
        with pytest.raises(ValueError):
            routes.AnnouncementEmailsUpdateRequest(announcement_emails="true")  # type: ignore[arg-type]

    def test_session_only(self) -> None:
        client = TestClient(app)
        for method in ("get", "put"):
            response = getattr(client, method)(
                "/api/v1/me/announcement-emails",
                headers={"Authorization": "Bearer kagura_not_a_real_key"},
                **({"json": {"announcement_emails": False}} if method == "put" else {}),
            )
            assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Public unsubscribe routes
# ---------------------------------------------------------------------------


class _NoTouchDb:
    """A session that fails the test if anything uses it."""

    def __getattr__(self, name):  # pragma: no cover - only hit on a regression
        raise AttributeError(f"database used for an invalid token: {name}")


@pytest.fixture
def public_client(monkeypatch):
    holder: dict[str, object] = {"db": _NoTouchDb()}

    async def _get_db():
        yield holder["db"]

    app.dependency_overrides[get_db] = _get_db
    counter = AsyncMock(return_value=1)
    monkeypatch.setattr(routes, "increment_counter", counter)
    monkeypatch.setattr(get_settings(), "frontend_url", "https://app.example.com")
    try:
        yield TestClient(app), holder, counter
    finally:
        app.dependency_overrides.pop(get_db, None)


def _valid_token() -> str:
    return issue_unsubscribe_token(account_id=42, user_id="google-123")


class TestOneClick:
    @pytest.mark.parametrize(
        "token", ["dummy-token", "x" * 3000, "gAAAAAB" + "a" * 120, "%0D%0Aevil"]
    )
    def test_invalid_token_same_200_no_db_no_redis(self, public_client, token) -> None:
        client, _, counter = public_client

        response = client.post(
            f"/api/v1/announcement-emails/unsubscribe/{token}",
            data={"List-Unsubscribe": "One-Click"},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "accepted"}
        counter.assert_not_awaited()

    def test_valid_token_is_applied(self, public_client, monkeypatch) -> None:
        client, holder, counter = public_client
        holder["db"] = _db()
        apply = AsyncMock(return_value=True)
        monkeypatch.setattr(routes, "apply_unsubscribe", apply)

        response = client.post(f"/api/v1/announcement-emails/unsubscribe/{_valid_token()}")

        assert response.status_code == 200
        assert response.json() == {"status": "accepted"}
        assert apply.await_args.args[1] == UnsubscribeClaim(42, "google-123")
        holder["db"].commit.assert_awaited()
        counter.assert_not_awaited()

    def test_valid_token_never_429(self, public_client, monkeypatch) -> None:
        client, holder, counter = public_client
        counter.return_value = 10_000
        holder["db"] = _db()
        monkeypatch.setattr(routes, "apply_unsubscribe", AsyncMock(return_value=False))

        for _ in range(3):
            response = client.post(f"/api/v1/announcement-emails/unsubscribe/{_valid_token()}")
            assert response.status_code == 200

    def test_same_body_whether_or_not_the_account_exists(self, public_client, monkeypatch) -> None:
        client, holder, _ = public_client
        holder["db"] = _db()
        monkeypatch.setattr(routes, "apply_unsubscribe", AsyncMock(return_value=False))
        unknown = client.post(f"/api/v1/announcement-emails/unsubscribe/{_valid_token()}")
        holder["db"] = _NoTouchDb()
        forged = client.post("/api/v1/announcement-emails/unsubscribe/forged-token")

        assert unknown.status_code == forged.status_code == 200
        assert unknown.content == forged.content

    def test_failed_write_is_503(self, public_client, monkeypatch) -> None:
        client, holder, _ = public_client
        holder["db"] = _db()
        monkeypatch.setattr(
            routes, "apply_unsubscribe", AsyncMock(side_effect=RuntimeError("db down"))
        )

        response = client.post(f"/api/v1/announcement-emails/unsubscribe/{_valid_token()}")

        assert response.status_code == 503
        holder["db"].rollback.assert_awaited()


class TestGetRedirect:
    def test_valid_token_is_reissued_into_the_fragment(self, public_client) -> None:
        client, _, counter = public_client
        token = _valid_token()

        response = client.get(
            f"/api/v1/announcement-emails/unsubscribe/{token}", follow_redirects=False
        )

        assert response.status_code == 303
        location = response.headers["location"]
        page, _, fragment = location.partition("#")
        assert page == "https://app.example.com/email/unsubscribe"
        # Only a token this server minted reaches the fragment: a fresh one,
        # never the request's own text, naming the same account row.
        assert fragment
        assert fragment != token
        assert read_unsubscribe_token(fragment) == UnsubscribeClaim(
            account_id=42, user_id="google-123"
        )
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        counter.assert_not_awaited()

    @pytest.mark.parametrize(
        "token",
        [
            "%0D%0ASet-Cookie:%20x=1",
            "a.b",
            "x" * 2049,
            # Token-shaped but not ours (the old rule let these through).
            "forged-token",
            "gAAAAAB" + "a" * 120,
        ],
    )
    def test_other_values_land_without_a_fragment(self, public_client, token) -> None:
        client, _, counter = public_client

        response = client.get(
            f"/api/v1/announcement-emails/unsubscribe/{token}", follow_redirects=False
        )

        assert response.status_code == 303
        assert response.headers["location"] == "https://app.example.com/email/unsubscribe"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        counter.assert_not_awaited()

    def test_token_from_another_purpose_key_lands_without_a_fragment(self, public_client) -> None:
        client, _, _ = public_client
        other = (
            Fernet(Fernet.generate_key())
            .encrypt(b'{"p":"au1","i":42,"u":"google-123"}')
            .decode()
            .rstrip("=")
        )

        response = client.get(
            f"/api/v1/announcement-emails/unsubscribe/{other}", follow_redirects=False
        )

        assert response.headers["location"] == "https://app.example.com/email/unsubscribe"

    def test_redirect_never_reads_the_database(self, public_client) -> None:
        # CPU only: the route takes no database session at all, and a valid
        # token through the app touches none (the holder fails on any use).
        route = next(
            r
            for r in routes.public_router.routes
            if getattr(r, "path", "").endswith("/unsubscribe/{token}")
            and "GET" in getattr(r, "methods", set())
        )
        assert get_db not in {d.call for d in route.dependant.dependencies}
        client, holder, _ = public_client
        holder["db"] = _NoTouchDb()

        response = client.get(
            f"/api/v1/announcement-emails/unsubscribe/{_valid_token()}", follow_redirects=False
        )

        assert response.status_code == 303


class TestPagePost:
    def test_invalid_token_same_200_counted_per_ip(self, public_client) -> None:
        client, _, counter = public_client

        response = client.post("/api/v1/announcement-emails/unsubscribe", json={"token": "nope"})

        assert response.status_code == 200
        assert response.json() == {"status": "accepted"}
        assert counter.await_args.args[0].startswith("announce_unsub_bad_ip:")

    def test_invalid_tokens_past_the_limit_are_429(self, public_client) -> None:
        client, _, counter = public_client
        counter.return_value = 31

        response = client.post("/api/v1/announcement-emails/unsubscribe", json={"token": "nope"})

        assert response.status_code == 429

    def test_oversized_token_is_a_silent_no_op(self, public_client) -> None:
        client, _, _ = public_client

        response = client.post(
            "/api/v1/announcement-emails/unsubscribe", json={"token": "x" * 5000}
        )

        assert response.status_code == 200

    def test_valid_token_is_applied_and_never_429(self, public_client, monkeypatch) -> None:
        client, holder, counter = public_client
        counter.return_value = 10_000
        holder["db"] = _db()
        apply = AsyncMock(return_value=True)
        monkeypatch.setattr(routes, "apply_unsubscribe", apply)

        response = client.post(
            "/api/v1/announcement-emails/unsubscribe", json={"token": _valid_token()}
        )

        assert response.status_code == 200
        assert apply.await_args.args[1] == UnsubscribeClaim(42, "google-123")
        counter.assert_not_awaited()
