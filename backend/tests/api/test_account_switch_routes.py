"""Account list/switch endpoints (#1488 Phase 2).

The whole feature rests on one rule: a switch may only target an account that
is ALREADY in the caller's own session container. There is no lookup anywhere
in the path, so no id a caller invents can widen their access. These tests
exist mainly to pin that, because the failure would be catastrophic and quiet —
a working switch to an arbitrary user id looks exactly like a working switch.
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

    def scan(self, cursor: int, match: str = "*", count: int = 100):
        return 0, [k for k in list(self.store) if k.startswith(match.rstrip("*"))]

    def pipeline(self):
        return _Pipe(self)


class _Pipe:
    def __init__(self, r: FakeRedis) -> None:
        self.r, self.ops = r, []

    def delete(self, key: str) -> None:
        self.ops.append(key)

    def execute(self):
        return [self.r.delete(k) for k in self.ops]


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
    # SessionUser -> require_session_auth (dependencies.py:927). The routes take
    # it only to require authentication; the identity they act on comes from the
    # cookie's own container, never from this dependency.
    app.dependency_overrides[require_session_auth] = lambda: dict(ALICE)

    c = TestClient(app)
    c.app = app  # type: ignore[attr-defined]
    c.manager = manager  # type: ignore[attr-defined]
    c.fake = fake  # type: ignore[attr-defined]
    return c


def signed_in(client, *identities) -> str:
    sid = client.manager.create_session(identities[0])
    for extra in identities[1:]:
        client.manager.add_account(sid, extra)
    client.cookies.set("kagura_session", sid)
    return sid


class TestListAccounts:
    def test_lists_both_and_flags_active(self, client):
        signed_in(client, ALICE, BOB)
        body = client.get("/auth/accounts").json()
        assert {a["user_id"] for a in body["accounts"]} == {"alice", "bob"}
        assert [a["user_id"] for a in body["accounts"] if a["is_active"]] == ["bob"]

    def test_returns_display_fields_only(self, client):
        """A menu must not become a data-leak surface."""
        sid = client.manager.create_session({**ALICE, "role": "admin", "secret": "x"})
        client.cookies.set("kagura_session", sid)
        account = client.get("/auth/accounts").json()["accounts"][0]
        assert set(account) == {"user_id", "email", "name", "picture", "is_active"}

    def test_unauthenticated_is_401_not_an_empty_list(self, client):
        """Exercises the REAL dependency, not the override.

        `SessionUser` -> require_session_auth -> get_current_user reads the
        session the middleware resolved from the cookie, so an anonymous caller
        never reaches the handler. A test that overrides that dependency and
        then asserts `{"accounts": []}` is asserting something production cannot
        produce — the override is the only reason it passes. Clear it so the
        assertion means what it says.
        """
        client.app.dependency_overrides.clear()
        client.cookies.clear()
        assert client.get("/auth/accounts").status_code == 401


class TestSwitch:
    def test_switches_to_a_member(self, client):
        sid = signed_in(client, ALICE, BOB)
        assert client.post("/auth/accounts/switch", json={"user_id": "alice"}).status_code == 200
        assert client.manager.get_session(sid)["user_id"] == "alice"

    def test_REFUSES_a_non_member(self, client):
        """The security boundary. A 200 here would be a full account takeover."""
        sid = signed_in(client, ALICE)
        r = client.post("/auth/accounts/switch", json={"user_id": "bob"})
        assert r.status_code == 404
        # ...and the session is untouched.
        assert client.manager.get_session(sid)["user_id"] == "alice"

    def test_a_refused_switch_does_not_add_the_account(self, client):
        sid = signed_in(client, ALICE)
        client.post("/auth/accounts/switch", json={"user_id": "intruder"})
        stored = json.loads(client.fake.store[f"session:{sid}"])
        assert "intruder" not in stored["accounts"]

    def test_404_not_403_so_existence_is_not_disclosed(self, client):
        """Whether another account exists is not the caller's business."""
        signed_in(client, ALICE)
        # 'bob' exists elsewhere in the system but not in this session.
        client.manager.create_session(BOB)
        assert client.post("/auth/accounts/switch", json={"user_id": "bob"}).status_code == 404

    def test_requires_a_session_cookie(self, client):
        """Also through the real dependency — see the note on the GET case."""
        client.app.dependency_overrides.clear()
        client.cookies.clear()
        assert client.post("/auth/accounts/switch", json={"user_id": "alice"}).status_code == 401

    def test_rejects_an_empty_user_id(self, client):
        signed_in(client, ALICE)
        assert client.post("/auth/accounts/switch", json={"user_id": ""}).status_code == 422
