"""Adding a second account to an existing session (#1488).

Phase 2 shipped `switch_account` and the endpoints, but every login path still
REPLACED the session — so nothing could ever put a second account in it and the
switcher had nothing to switch between. This is the missing half.

The intent travels as a short-lived Redis key beside the CSRF state, holding
the SESSION ID it may append to. Two properties make that safe, and both are
pinned here:

- possession of `state` is not authority: the callback must also arrive with a
  cookie naming that same session, so a leaked state cannot graft an account
  onto someone else's session;
- the intent is single-use, like the state itself.

Unit-level against the helpers, because the security decision lives there —
the OAuth round trip around it is already covered by the callback suites.
"""

from __future__ import annotations

import pytest

from api.routes import auth as auth_routes


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n


class FakeManager:
    """Only what the intent helpers touch."""

    def __init__(self, live_sessions: set[str]) -> None:
        self._redis = FakeRedis()
        self._live = live_sessions

    def get_session(self, session_id: str, update_access: bool = True):
        return {"user_id": "u"} if session_id in self._live else None


class FakeRequest:
    def __init__(self, cookie: str | None) -> None:
        self.cookies = {"kagura_session": cookie} if cookie else {}


@pytest.fixture
def mgr(monkeypatch):
    m = FakeManager(live_sessions={"sess-A"})
    monkeypatch.setattr(auth_routes, "_session_manager", m)
    return m


class TestIntentIsHonouredForTheRightBrowser:
    def test_round_trip(self, mgr):
        auth_routes._remember_add_account_intent("st1", "sess-A")
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-A")) == "sess-A"

    def test_absent_intent_means_a_normal_login(self, mgr):
        assert auth_routes._take_add_account_intent("nope", FakeRequest("sess-A")) is None


class TestStateAloneIsNotAuthority:
    def test_a_different_cookie_cannot_use_the_intent(self, mgr):
        """The attack the cookie check exists for.

        If `state` leaked, completing the flow would otherwise append the
        attacker's account to the victim's session — after which the victim
        could switch into it.
        """
        auth_routes._remember_add_account_intent("st1", "sess-A")
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-EVIL")) is None

    def test_no_cookie_at_all_cannot_use_the_intent(self, mgr):
        auth_routes._remember_add_account_intent("st1", "sess-A")
        assert auth_routes._take_add_account_intent("st1", FakeRequest(None)) is None

    def test_a_dead_session_is_refused(self, mgr):
        """Falling back to a replacing login would surprise someone who asked
        to ADD an account — they would silently lose the other one."""
        auth_routes._remember_add_account_intent("st1", "sess-GONE")
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-GONE")) is None


class TestSingleUse:
    def test_intent_is_consumed_on_success(self, mgr):
        auth_routes._remember_add_account_intent("st1", "sess-A")
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-A")) == "sess-A"
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-A")) is None

    def test_intent_is_consumed_even_when_rejected(self, mgr):
        """A rejected attempt must not leave a reusable key behind."""
        auth_routes._remember_add_account_intent("st1", "sess-A")
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-EVIL")) is None
        # ...and the legitimate browser cannot use it afterwards either.
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-A")) is None


class TestWiring:
    def test_both_providers_offer_the_flag(self):
        import inspect

        for fn in (auth_routes.google_login, auth_routes.github_login):
            assert "add_account" in inspect.signature(fn).parameters, fn.__name__

    def test_both_callbacks_consult_the_intent(self):
        import inspect

        for fn in (auth_routes.google_callback, auth_routes.github_callback):
            src = inspect.getsource(fn)
            assert "_take_add_account_intent" in src, fn.__name__
            assert "add_account(" in src, fn.__name__
