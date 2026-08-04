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

import ast

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
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-A")) == (
            "add",
            "sess-A",
        )

    def test_absent_intent_means_a_normal_login(self, mgr):
        assert auth_routes._take_add_account_intent("nope", FakeRequest("sess-A")) == (
            "none",
            None,
        )


class TestStateAloneIsNotAuthority:
    def test_a_different_cookie_cannot_use_the_intent(self, mgr):
        """The attack the cookie check exists for.

        If `state` leaked, completing the flow would otherwise append the
        attacker's account to the victim's session — after which the victim
        could switch into it.
        """
        auth_routes._remember_add_account_intent("st1", "sess-A")
        status, sid = auth_routes._take_add_account_intent("st1", FakeRequest("sess-EVIL"))
        assert (status, sid) == ("unusable", None)

    def test_no_cookie_at_all_cannot_use_the_intent(self, mgr):
        auth_routes._remember_add_account_intent("st1", "sess-A")
        assert auth_routes._take_add_account_intent("st1", FakeRequest(None)) == (
            "unusable",
            None,
        )

    def test_a_dead_session_is_refused(self, mgr):
        """Must be UNUSABLE, never NONE.

        `none` makes the caller mint a fresh session, which replaces the cookie
        and discards every other account already signed in — the opposite of
        what someone asking to *add* an account wants. Two-valued, that bug is
        invisible; this assertion is the reason the status is named.
        """
        auth_routes._remember_add_account_intent("st1", "sess-GONE")
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-GONE")) == (
            "unusable",
            None,
        )


class TestSingleUse:
    def test_intent_is_consumed_on_success(self, mgr):
        auth_routes._remember_add_account_intent("st1", "sess-A")
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-A"))[0] == "add"
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-A"))[0] == "none"

    def test_intent_is_consumed_even_when_rejected(self, mgr):
        """A rejected attempt must not leave a reusable key behind."""
        auth_routes._remember_add_account_intent("st1", "sess-A")
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-EVIL"))[0] == (
            "unusable"
        )
        # ...and the legitimate browser cannot use it afterwards either.
        assert auth_routes._take_add_account_intent("st1", FakeRequest("sess-A"))[0] == "none"


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

    def test_neither_callback_replaces_the_session_on_an_unusable_intent(self):
        """The destructive fallback, pinned.

        Falling through to create_session here mints a new cookie and drops
        every other signed-in account. Both callbacks must refuse instead.
        """
        import inspect

        for fn in (auth_routes.google_callback, auth_routes.github_callback):
            src = inspect.getsource(fn)
            assert 'intent == "unusable"' in src, fn.__name__
            assert "add_account_failed" in src, fn.__name__


class TestTheInvalidationCannotEatTheSessionItIsAddingTo:
    """Composition pin for the ordering defect (#1488 Phase 4 fix).

    The behaviour lives in `SessionManager.delete_user_sessions`'s exclusion
    and is covered there, exhaustively and by mutation, in
    tests/auth/test_session_container.py::TestAddingAnAccountMustNotDestroyItsOwnSession.
    What THAT cannot see is whether the callbacks actually pass the exclusion,
    and whether they learn the session id early enough to have one to pass.

    Pinned by source ORDER rather than substring presence. The bug was not a
    missing call — every symbol involved was already there and the grep-style
    assertions above all passed while the flow was broken. It was that
    `delete_user_sessions` ran BEFORE `_take_add_account_intent`, so the id to
    spare was not known yet. Comparing positions is the smallest assertion that
    fails on the real regression.

    Following this repo's existing convention for the OAuth callbacks (see
    tests/api/test_oauth_callback_safe_redirect.py): they depend on Authlib, the
    OAuth2 manager, a live DB and Redis, and there is no full-callback
    integration fixture to model from.

    Measured on the AST, not the text. The first text-search version of this
    class failed against CORRECT code, because the first textual occurrence of
    `delete_user_sessions` in the fixed callback is inside the comment
    explaining the fix. An assertion a prose edit can break is not measuring
    the code.
    """

    @staticmethod
    def _calls(fn) -> dict[str, list[ast.Call]]:
        """Every call in ``fn``, grouped by the name being called."""
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        found: dict[str, list[ast.Call]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name:
                found.setdefault(name, []).append(node)
        return found

    @pytest.mark.parametrize("fn_name", ["google_callback", "github_callback"])
    def test_the_intent_is_read_before_the_invalidation(self, fn_name):
        fn = getattr(auth_routes, fn_name)
        calls = self._calls(fn)
        intent_at = min(c.lineno for c in calls["_take_add_account_intent"])
        invalidate_at = min(c.lineno for c in calls["delete_user_sessions"])
        assert intent_at < invalidate_at, (
            f"{fn_name}: delete_user_sessions runs before the add-account intent "
            "is known, so it cannot be told which session to spare — re-adding an "
            "already-signed-in account destroys the whole container. See #1488."
        )

    @pytest.mark.parametrize("fn_name", ["google_callback", "github_callback"])
    def test_the_invalidation_is_told_what_to_spare(self, fn_name):
        fn = getattr(auth_routes, fn_name)
        for call in self._calls(fn)["delete_user_sessions"]:
            kwargs = {kw.arg for kw in call.keywords}
            assert "exclude_session_id" in kwargs, (
                f"{fn_name}: delete_user_sessions is called without "
                "exclude_session_id; an add-account login will delete the "
                "session it is appending to."
            )

    @pytest.mark.parametrize("fn_name", ["google_callback", "github_callback"])
    def test_an_unusable_intent_refuses_before_anything_is_deleted(self, fn_name):
        """Order matters here too: refusing after the delete would sign the user
        out on the way to reporting that it could not add an account."""
        fn = getattr(auth_routes, fn_name)
        calls = self._calls(fn)
        refusals = [
            c.lineno
            for c in calls.get("_oauth_error_redirect", [])
            if any(isinstance(a, ast.Constant) and a.value == "add_account_failed" for a in c.args)
        ]
        assert refusals, f"{fn_name}: no add_account_failed refusal found"
        invalidate_at = min(c.lineno for c in calls["delete_user_sessions"])
        assert min(refusals) < invalidate_at, fn_name


class TestTheAddFlowActuallyOffersAChoice:
    """#1488: the login leg must ask Google for the account chooser.

    Pinned separately from the callback ordering above because this defect had
    the same *symptom* as a broken switcher while every mechanism worked: the
    intent was recorded, the callback honoured it, `add_account` returned True
    — and the container still held one account, because Google had handed back
    the identity it already held. Reported as "adding an account does not let
    me switch".
    """

    def test_google_login_requests_the_chooser_only_when_adding(self):
        calls = self._authorization_url_calls()
        assert calls, "google_login no longer builds an authorization URL"
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords}
            assert "select_account" in kwargs, (
                "google_login calls get_authorization_url_web without "
                "select_account; an add-account login will re-consent with the "
                "identity already signed in and never add a second account."
            )

    @staticmethod
    def _authorization_url_calls() -> list[ast.Call]:
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(auth_routes.google_login)))
        return [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get_authorization_url_web"
        ]

    def test_the_chooser_is_tied_to_the_add_account_flag(self):
        """Not hardcoded True — an ordinary login must keep its single click."""
        for call in self._authorization_url_calls():
            for kw in call.keywords:
                if kw.arg == "select_account":
                    assert isinstance(kw.value, ast.Name) and kw.value.id == "add_account", (
                        "select_account must follow the add_account flag, not be a constant"
                    )
