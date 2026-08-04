"""Session records are containers, and nothing outside notices (#1488 Phase 1).

The record shape used to be a flat dict, which can only ever describe one
account — the first of three things blocking multi-account switching. It is now

    {"v": 2, "accounts": {"<uid>": {...}}, "active": "<uid>", ...}

holding exactly one account. This phase is a refactor: `get_session()` still
returns the flat shape, because ~30 route handlers read `user["user_id"]`
straight off `request.state.user` and making them account-aware is Phase 2.

Two properties are load-bearing for the DEPLOY, not for the feature:

1. Records minted before this change are flat and still in Redis. If they were
   rejected, every signed-in user would be logged out the moment it ships.
2. `delete_user_sessions` scans RAW records. Reading `user_id` off the top level
   of a container matches nothing, which would silently retire #114's
   one-session-per-user guarantee — a security regression that no existing test
   would have caught, because the function would still return 0 happily.

A fake Redis is used rather than mocks so the JSON round-trip through storage is
real; the shape surviving `json.dumps`/`loads` is the whole point.
"""

from __future__ import annotations

import json

import pytest

from auth.session import (
    SessionManager,
    is_container,
    project_active,
    to_container,
)


class FakeRedis:
    """Minimal Redis stand-in: the calls SessionManager actually makes."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                n += 1
        return n

    def scan(self, cursor: int, match: str = "*", count: int = 100):
        # Single-shot scan; `match` is only ever "session:*" here.
        prefix = match.rstrip("*")
        return 0, [k for k in list(self.store) if k.startswith(prefix)]

    def pipeline(self):
        return _FakePipeline(self)

    def keys(self, pattern: str):
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]

    def ttl(self, _key: str) -> int:
        return 100


class _FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[str] = []

    def delete(self, key: str) -> None:
        self._ops.append(key)

    def execute(self) -> list[int]:
        return [self._redis.delete(k) for k in self._ops]


@pytest.fixture
def manager(monkeypatch) -> SessionManager:
    fake = FakeRedis()
    monkeypatch.setattr(
        SessionManager, "_get_or_create_redis_client", staticmethod(lambda _url: fake)
    )
    mgr = SessionManager(redis_url="redis://fake:6379")
    return mgr


def raw(manager: SessionManager, session_id: str) -> dict:
    return json.loads(manager._redis.store[f"session:{session_id}"])


USER = {"sub": "google_1", "user_id": "google_1", "email": "a@example.com", "role": "member"}


class TestStoredShape:
    def test_new_sessions_are_containers(self, manager):
        sid = manager.create_session(USER)
        stored = raw(manager, sid)
        assert is_container(stored)
        assert stored["active"] == "google_1"
        assert list(stored["accounts"]) == ["google_1"]
        assert stored["accounts"]["google_1"]["email"] == "a@example.com"

    def test_callers_still_see_the_flat_shape(self, manager):
        """The contract ~30 handlers depend on."""
        sid = manager.create_session(USER)
        session = manager.get_session(sid)
        assert session is not None
        assert session["user_id"] == "google_1"
        assert session["email"] == "a@example.com"
        assert session["role"] == "member"
        assert "created_at" in session and "last_accessed" in session
        # The container internals must NOT leak to callers.
        assert "accounts" not in session
        assert "active" not in session


class TestLegacyRecordsKeepWorking:
    """A deploy must not sign everyone out."""

    def test_a_flat_record_is_still_readable(self, manager):
        manager._redis.store["session:legacy"] = json.dumps(
            {**USER, "created_at": "2020-01-01T00:00:00", "last_accessed": "2020-01-02T00:00:00"}
        )
        session = manager.get_session("legacy")
        assert session is not None
        assert session["user_id"] == "google_1"
        assert session["email"] == "a@example.com"

    def test_a_flat_record_is_migrated_when_touched(self, manager):
        manager._redis.store["session:legacy"] = json.dumps(
            {**USER, "created_at": "2020-01-01T00:00:00"}
        )
        manager.get_session("legacy")  # update_access=True rewrites it
        assert is_container(raw(manager, "legacy"))

    def test_migration_preserves_created_at(self, manager):
        """A migrated session must not look newly created."""
        manager._redis.store["session:legacy"] = json.dumps(
            {**USER, "created_at": "2020-01-01T00:00:00", "last_accessed": "2020-01-02T00:00:00"}
        )
        manager.get_session("legacy")
        assert raw(manager, "legacy")["created_at"] == "2020-01-01T00:00:00"

    def test_read_only_access_does_not_rewrite(self, manager):
        before = json.dumps({**USER, "created_at": "2020-01-01T00:00:00"})
        manager._redis.store["session:legacy"] = before
        manager.get_session("legacy", update_access=False)
        assert manager._redis.store["session:legacy"] == before


class TestOneSessionPerUserStillHolds:
    """#114. The regression here would be silent."""

    def test_deletes_a_container_session_by_account(self, manager):
        sid = manager.create_session(USER)
        assert manager.delete_user_sessions("google_1") == 1
        assert manager.get_session(sid) is None

    def test_deletes_a_legacy_flat_session_too(self, manager):
        manager._redis.store["session:legacy"] = json.dumps(USER)
        assert manager.delete_user_sessions("google_1") == 1

    def test_leaves_other_users_alone(self, manager):
        mine = manager.create_session(USER)
        theirs = manager.create_session({**USER, "sub": "google_2", "user_id": "google_2"})
        assert manager.delete_user_sessions("google_1") == 1
        assert manager.get_session(mine) is None
        assert manager.get_session(theirs) is not None

    def test_login_flow_invalidates_the_previous_session(self, manager):
        """End to end: the sequence every login performs."""
        first = manager.create_session(USER)
        manager.delete_user_sessions("google_1")
        second = manager.create_session(USER)
        assert manager.get_session(first) is None
        assert manager.get_session(second) is not None


class TestUpdateSession:
    def test_updates_land_on_the_active_identity(self, manager):
        sid = manager.create_session(USER)
        assert manager.update_session(sid, {"role": "admin"}) is True
        assert raw(manager, sid)["accounts"]["google_1"]["role"] == "admin"
        assert manager.get_session(sid)["role"] == "admin"

    def test_update_does_not_collapse_the_container(self, manager):
        """Writing the flat projection back would drop every other account."""
        sid = manager.create_session(USER)
        manager.update_session(sid, {"role": "admin"})
        assert is_container(raw(manager, sid))

    def test_envelope_keys_stay_on_the_envelope(self, manager):
        sid = manager.create_session(USER)
        manager.update_session(sid, {"last_accessed": "2030-01-01T00:00:00"})
        stored = raw(manager, sid)
        assert stored["last_accessed"] == "2030-01-01T00:00:00"
        assert "last_accessed" not in stored["accounts"]["google_1"]


class TestHelpers:
    def test_round_trip(self):
        flat = {**USER, "created_at": "c", "last_accessed": "l"}
        projected = project_active(to_container(flat))
        assert projected == flat

    def test_sub_only_identity_is_keyed_by_sub(self):
        """OAuth records may carry `sub` without `user_id`."""
        container = to_container({"sub": "gh_9", "email": "b@example.com"})
        assert container["active"] == "gh_9"
        assert "gh_9" in container["accounts"]

    def test_a_corrupt_active_pointer_does_not_raise(self):
        """Prefer logging the user out over 500-ing every request."""
        projected = project_active({"accounts": {"a": {"x": 1}}, "active": "missing"})
        assert projected.get("x") is None

    def test_is_container_rejects_a_flat_record(self):
        assert is_container(USER) is False
        assert is_container(to_container(USER)) is True
