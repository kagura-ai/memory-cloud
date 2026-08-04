"""Signing out when a session can hold several accounts (#1488 Phase 4).

Phases 1-3 made one session able to carry several identities, which quietly
turned "log out" into two different actions. `remove_account` was written in
Phase 2 for exactly this and then never called by any route — the same
primitive-exists-but-is-unreachable gap Phase 2 itself shipped. This is where
it becomes reachable.

Three properties carry the weight, and each fails silently rather than loudly:

- the DEFAULT is still a full sign-out, so an older frontend served during a
  rollout cannot accidentally start doing the narrower thing;
- ``scope="current"`` must not evict the accounts the user did not ask about;
- the cookie must survive exactly when the session does. Clearing it while
  other accounts remain would log the user out anyway while orphaning their
  records in Redis — a bug that looks, from the browser, like the feature
  simply not working.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import auth as auth_routes
from auth.dependencies import require_session_auth


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def expire(self, key: str, _ttl: int) -> bool:
        return key in self.store

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)


ALICE = {"sub": "alice", "user_id": "alice", "email": "alice@example.com", "name": "Alice"}
BOB = {"sub": "bob", "user_id": "bob", "email": "bob@example.com", "name": "Bob"}


@pytest.fixture
def client(monkeypatch):
    from auth.session import SessionManager

    fake = FakeRedis()
    monkeypatch.setattr(
        SessionManager, "_get_or_create_redis_client", staticmethod(lambda _url: fake)
    )
    manager = SessionManager(redis_url="redis://fake:6379")
    monkeypatch.setattr(auth_routes, "_session_manager", manager)

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[require_session_auth] = lambda: dict(ALICE)

    c = TestClient(app)
    c.app = app  # type: ignore[attr-defined]
    c.manager = manager  # type: ignore[attr-defined]
    c.fake = fake  # type: ignore[attr-defined]
    return c


def signed_in(client, *identities) -> str:
    """Create a session holding these identities; the LAST one is active."""
    sid = client.manager.create_session(identities[0])
    for extra in identities[1:]:
        client.manager.add_account(sid, extra)
    client.cookies.set("kagura_session", sid)
    return sid


def clears_cookie(response) -> bool:
    """Did this response tell the browser to drop the session cookie?"""
    return any(
        "kagura_session=" in value and ("Max-Age=0" in value or "expires=" in value.lower())
        for key, value in response.headers.multi_items()
        if key.lower() == "set-cookie"
    )


class TestDefaultIsStillAFullSignOut:
    """The compatibility guarantee, pinned.

    Every existing caller posts to /auth/logout with no query string. If the
    default ever became "current", those callers would silently start leaving
    accounts signed in — a security-relevant change with no visible symptom.
    """

    def test_no_scope_ends_the_whole_session(self, client):
        sid = signed_in(client, ALICE, BOB)
        r = client.post("/auth/logout")
        assert r.status_code == 200
        assert r.json()["session_ended"] is True
        assert client.manager.get_session(sid) is None

    def test_no_scope_clears_the_cookie(self, client):
        signed_in(client, ALICE, BOB)
        assert clears_cookie(client.post("/auth/logout"))

    def test_explicit_all_drops_every_account(self, client):
        sid = signed_in(client, ALICE, BOB)
        client.post("/auth/logout?scope=all")
        assert f"session:{sid}" not in client.fake.store


class TestSignOutOfTheActiveAccountOnly:
    def test_the_other_account_stays_signed_in(self, client):
        sid = signed_in(client, ALICE, BOB)  # BOB active
        r = client.post("/auth/logout?scope=current")

        assert r.status_code == 200
        body = r.json()
        assert body["session_ended"] is False
        assert body["active_user_id"] == "alice"
        assert client.manager.get_session(sid)["user_id"] == "alice"

    def test_the_cookie_survives_because_the_session_does(self, client):
        """Clearing it here would log the user out of the account they kept."""
        signed_in(client, ALICE, BOB)
        assert not clears_cookie(client.post("/auth/logout?scope=current"))

    def test_the_signed_out_account_is_really_gone(self, client):
        sid = signed_in(client, ALICE, BOB)
        client.post("/auth/logout?scope=current")
        stored = json.loads(client.fake.store[f"session:{sid}"])
        assert set(stored["accounts"]) == {"alice"}

    def test_it_can_be_repeated_down_to_a_full_sign_out(self, client):
        """Two accounts, two 'sign out this one's, and the session is gone."""
        sid = signed_in(client, ALICE, BOB)
        assert client.post("/auth/logout?scope=current").json()["session_ended"] is False
        second = client.post("/auth/logout?scope=current")
        assert second.json()["session_ended"] is True
        assert client.manager.get_session(sid) is None


class TestTheLastAccount:
    """With one account, 'this account' and 'all accounts' are the same act."""

    def test_reports_the_session_as_ended(self, client):
        sid = signed_in(client, ALICE)
        r = client.post("/auth/logout?scope=current")
        assert r.json()["session_ended"] is True
        assert client.manager.get_session(sid) is None

    def test_and_clears_the_cookie(self, client):
        """The client sends the user to /login off `session_ended`; the cookie
        must actually be gone or the next request re-authenticates a dead id."""
        signed_in(client, ALICE)
        assert clears_cookie(client.post("/auth/logout?scope=current"))

    def test_leaves_no_empty_container_behind(self, client):
        sid = signed_in(client, ALICE)
        client.post("/auth/logout?scope=current")
        assert f"session:{sid}" not in client.fake.store


class TestForgivingByDesign:
    """Logging out must never fail. A client that cannot log out is stuck."""

    def test_no_cookie_still_succeeds(self, client):
        client.cookies.clear()
        r = client.post("/auth/logout")
        assert r.status_code == 200
        assert r.json()["session_ended"] is True

    def test_an_expired_session_still_succeeds(self, client):
        client.cookies.set("kagura_session", "long-gone")
        for scope in ("", "?scope=current", "?scope=all"):
            r = client.post(f"/auth/logout{scope}")
            assert r.status_code == 200, scope
            assert r.json()["session_ended"] is True, scope

    def test_stays_unauthenticated(self, client):
        """Pinned deliberately.

        Adding `SessionUser` here would look like a hardening improvement and
        would instead 401 exactly the users who most need to log out: those
        whose session already expired. Nothing is protected by requiring auth —
        the caller must already hold the cookie to affect anything.
        """
        client.app.dependency_overrides.clear()
        client.cookies.clear()
        assert client.post("/auth/logout").status_code == 200

    def test_an_unknown_scope_is_rejected_rather_than_guessed(self, client):
        signed_in(client, ALICE, BOB)
        assert client.post("/auth/logout?scope=everything").status_code == 422


class TestTheFailSafe:
    """If the narrow sign-out cannot be performed, do the wide one.

    `remove_account` can refuse — a concurrent switch or removal in another tab
    moves the container out from under the read. The tempting reaction is to
    report failure and stop, which leaves the user signed in as the account
    they just asked to leave. The choice here is the opposite: degrade to a
    full sign-out, which always satisfies the request and never over-grants.

    Pinned because it is stated as a guarantee in the route's docstring, and a
    docstring is not a test.
    """

    def test_a_refused_removal_ends_the_session_instead(self, client, monkeypatch):
        sid = signed_in(client, ALICE, BOB)
        monkeypatch.setattr(type(client.manager), "remove_account", lambda self, *_a, **_k: False)

        r = client.post("/auth/logout?scope=current")

        assert r.status_code == 200
        assert r.json()["session_ended"] is True
        assert client.manager.get_session(sid) is None
        assert clears_cookie(r)

    def test_it_does_not_silently_leave_the_account_signed_in(self, client, monkeypatch):
        """The failure this branch exists to prevent, stated as its own case."""
        sid = signed_in(client, ALICE, BOB)
        monkeypatch.setattr(type(client.manager), "remove_account", lambda self, *_a, **_k: False)

        client.post("/auth/logout?scope=current")

        assert f"session:{sid}" not in client.fake.store
