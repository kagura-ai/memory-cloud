"""Over the per-peer OIDC budget, issued credentials are counted per client and user (#2903).

The real limiter runs against an in-memory counter, and its one recognition
lookup runs on the same SQLite store the protocol code uses. Every other
piece (Authlib, the signed-in browser, client authentication) is the shared
protocol fixture of ``test_oidc_provider``.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import Mock

import bcrypt
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.routes import oidc
from auth import oidc_limits
from config.settings import get_settings
from models.auth import User
from models.oidc import OIDCClient
from tests.api.test_oidc_provider import (
    CALLBACK,
    CLIENT,
    SECRET,
    SUBJECT,
    authorization_params,
    authorize,
    exchange,
    refresh,
)
from tests.api.test_oidc_provider import (
    store as store,
)

LIMIT = 10
OTHER_CLIENT = CLIENT + "-other"
OTHER_USER = SUBJECT + "-other"
HOLDER = f"oidc-client:{CLIENT}:user:{SUBJECT}"
PEER_429 = {"error": "temporarily_unavailable", "error_description": "OIDC rate limit exceeded."}


def holder_key(operation: str, holder: str) -> str:
    return f"oidc_rate_limit:{operation}:holder:{hashlib.sha256(holder.encode()).hexdigest()}:2"


class Counter:
    """In-memory stand-in for the Redis counter with a switch for the peer budget."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.peer_exhausted = False
        self.fail_on: str | None = None

    async def __call__(self, key: str, ttl: int) -> int:
        assert ttl == 60
        if self.fail_on is not None and self.fail_on in key:
            raise RuntimeError("counter unavailable")
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.peer_exhausted and ":holder:" not in key:
            return LIMIT + self.counts[key]
        return self.counts[key]

    def holder(self, operation: str, holder: str) -> int:
        return self.counts.get(holder_key(operation, holder), 0)


@pytest.fixture
def burst(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    counter = Counter()
    lookups: list[object] = []

    async def lookup_on_store(statement):
        lookups.append(statement)
        with store.factory() as db:
            row = db.execute(statement).first()
            return None if row is None else tuple(row)

    monkeypatch.setattr(get_settings(), "oidc_rate_limit_per_minute", LIMIT)
    monkeypatch.setattr(oidc_limits, "increment_counter", counter)
    monkeypatch.setattr(oidc_limits, "time", SimpleNamespace(time=lambda: 125.0))
    monkeypatch.setattr(oidc_limits, "_first_row", lookup_on_store)
    return SimpleNamespace(counter=counter, lookups=lookups)


@pytest.fixture
def api(store: SimpleNamespace, burst: SimpleNamespace) -> Iterator[TestClient]:
    """The OIDC router with the real limiter (no stub) and a signed-in browser."""
    app = FastAPI()

    @app.middleware("http")
    async def browser_session(request: Request, call_next):
        if request.cookies.get("kagura_session") == "test-session":
            request.state.user = {"user_id": SUBJECT, "name": "Identity User"}
        elif request.cookies.get("kagura_session") == "test-session-other":
            request.state.user = {"user_id": OTHER_USER, "name": "Other User"}
        return await call_next(request)

    app.include_router(oidc.router, prefix="/oidc")
    with TestClient(app, base_url="https://identity.example.test") as client:
        client.cookies.set("kagura_session", "test-session")
        yield client


@pytest.fixture
def secret_checks(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Every bcrypt client-secret verification, still performed for real."""
    checks = Mock(side_effect=bcrypt.checkpw)
    monkeypatch.setattr(bcrypt, "checkpw", checks)
    return checks


def forbid_protocol_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden():
        raise AssertionError("An unrecognised request must not reach protocol work")

    monkeypatch.setattr(oidc, "get_sync_session", forbidden)


def add_other_client(store: SimpleNamespace) -> None:
    with store.factory() as db:
        db.add(
            OIDCClient(
                client_id=OTHER_CLIENT,
                client_secret_hash=bcrypt.hashpw(SECRET.encode(), bcrypt.gensalt(4)).decode(),
                client_name="Other identity test",
                redirect_uris=[CALLBACK],
                scope="openid profile workspaces:read",
            )
        )
        db.commit()


def sign_in_other_user(api: TestClient, store: SimpleNamespace) -> None:
    with store.factory() as db:
        db.add(User(user_id=OTHER_USER, email="other@example.test", name="Other User"))
        db.commit()
    sessions = {"test-session": SUBJECT, "test-session-other": OTHER_USER}
    store.manager.session_holds_user = lambda sid, uid: sessions.get(sid) == uid
    api.cookies.set("kagura_session", "test-session-other")


def test_over_budget_peer_still_exchanges_and_refreshes_counted_per_client_and_user(
    api, burst, secret_checks
):
    code, verifier = authorize(api)
    burst.counter.peer_exhausted = True
    issued = exchange(api, code, verifier)
    assert issued.status_code == 200, issued.text
    rotated = refresh(api, issued.json()["refresh_token"])
    assert rotated.status_code == 200, rotated.text
    assert burst.counter.holder("token", HOLDER) == 2
    assert secret_checks.call_count == 2  # admitted requests still authenticate the client


def test_over_budget_peer_still_reads_userinfo_and_revokes_counted_per_client_and_user(api, burst):
    code, verifier = authorize(api)
    issued = exchange(api, code, verifier).json()
    burst.counter.peer_exhausted = True
    bearer = {"Authorization": "Bearer " + issued["access_token"]}
    info = api.get("/oidc/userinfo", headers=bearer)
    assert info.status_code == 200, info.text
    assert info.json()["sub"] == SUBJECT
    revoked = api.post(
        "/oidc/revoke", auth=(CLIENT, SECRET), data={"token": issued["refresh_token"]}
    )
    assert revoked.status_code == 200, revoked.text
    assert api.get("/oidc/userinfo", headers=bearer).status_code == 401
    assert burst.counter.holder("userinfo", HOLDER) == 2
    assert burst.counter.holder("revoke", HOLDER) == 1


def test_over_budget_signed_in_browser_still_authorizes_counted_per_user(api, burst):
    burst.counter.peer_exhausted = True
    code, verifier = authorize(api)
    assert burst.counter.holder("authorize", "user:" + SUBJECT) == 2
    assert not burst.lookups  # the session middleware's lookup is reused
    assert exchange(api, code, verifier).status_code == 200
    api.cookies.clear()
    _, params = authorization_params()
    anonymous = api.get("/oidc/authorize", params=params, follow_redirects=False)
    assert anonymous.status_code == 429
    assert anonymous.json() == PEER_429


UNKNOWN_CODE = "kmc_oidc_code_" + secrets.token_urlsafe(32)
UNKNOWN_REFRESH = "kmc_oidc_rt_" + secrets.token_urlsafe(32)
CLIENT_AUTH = {"client_id": CLIENT, "client_secret": SECRET}


@pytest.mark.parametrize(
    "path,body,lookups",
    [
        (
            "/oidc/token",
            {"grant_type": "authorization_code", "code": UNKNOWN_CODE, **CLIENT_AUTH},
            1,
        ),
        (
            "/oidc/token",
            {"grant_type": "refresh_token", "refresh_token": UNKNOWN_REFRESH, **CLIENT_AUTH},
            1,
        ),
        ("/oidc/token", {"grant_type": "authorization_code", "code": "guess", **CLIENT_AUTH}, 0),
        ("/oidc/token", {"grant_type": "client_credentials", **CLIENT_AUTH}, 0),
        ("/oidc/token", {"grant_type": "password", **CLIENT_AUTH}, 0),
        ("/oidc/token", None, 0),
        ("/oidc/revoke", {"token": UNKNOWN_REFRESH, **CLIENT_AUTH}, 1),
        ("/oidc/revoke", {"token": "opaque", **CLIENT_AUTH}, 0),
    ],
)
def test_over_budget_unknown_credential_is_refused_before_client_authentication(
    api, burst, secret_checks, monkeypatch, path, body, lookups
):
    forbid_protocol_database(monkeypatch)
    burst.counter.peer_exhausted = True
    if body is None:
        response = api.post(path, json={"grant_type": "authorization_code"})
    else:
        response = api.post(path, data=body)
    assert response.status_code == 429
    assert response.json() == PEER_429
    assert response.headers["cache-control"] == "no-store"
    secret_checks.assert_not_called()
    assert len(burst.lookups) == lookups
    assert not [key for key in burst.counter.counts if ":holder:" in key]


def test_over_budget_unknown_access_token_is_refused_before_protocol_work(api, burst, monkeypatch):
    forbid_protocol_database(monkeypatch)
    burst.counter.peer_exhausted = True
    response = api.get(
        "/oidc/userinfo",
        headers={"Authorization": "Bearer kmc_oidc_at_" + secrets.token_urlsafe(32)},
    )
    assert response.status_code == 429
    assert response.json() == PEER_429
    assert len(burst.lookups) == 1


def test_client_bucket_refuses_only_its_own_client(api, burst, store):
    add_other_client(store)
    code, verifier = authorize(api)
    other_code, other_verifier = authorize(api, client_id=OTHER_CLIENT)
    burst.counter.peer_exhausted = True
    own = holder_key("token", HOLDER)
    burst.counter.counts[own] = LIMIT
    refused = exchange(api, code, verifier)
    assert refused.status_code == 429
    assert refused.json() == PEER_429
    admitted = exchange(api, other_code, other_verifier, client_id=OTHER_CLIENT)
    assert admitted.status_code == 200, admitted.text
    # The refusal did not touch the code: with room in its client's bucket it
    # still exchanges once.
    burst.counter.counts[own] = 0
    assert exchange(api, code, verifier).status_code == 200


def test_counter_unavailable_still_answers_503(api, burst):
    code, verifier = authorize(api)
    burst.counter.fail_on = ":token:"
    assert exchange(api, code, verifier).status_code == 503
    assert not burst.lookups
    burst.counter.fail_on = ":holder:"
    burst.counter.peer_exhausted = True
    unavailable = exchange(api, code, verifier)
    assert unavailable.status_code == 503
    assert unavailable.json() == {"error": "temporarily_unavailable"}
    burst.counter.fail_on = None
    assert exchange(api, code, verifier).status_code == 200


def test_over_budget_invalid_request_gets_the_same_answer_as_within_budget(api, burst):
    code, verifier = authorize(api)
    within = exchange(api, code, verifier, client_secret="wrong")
    burst.counter.peer_exhausted = True
    over = exchange(api, code, verifier, client_secret="wrong")
    assert (over.status_code, over.json()) == (within.status_code, within.json())
    assert exchange(api, code, verifier).status_code == 200
    burst.counter.peer_exhausted = False
    replay_within = exchange(api, code, verifier)
    burst.counter.peer_exhausted = True
    replay_over = exchange(api, code, verifier)
    assert replay_within.status_code == 400
    assert (replay_over.status_code, replay_over.json()) == (
        replay_within.status_code,
        replay_within.json(),
    )


@pytest.mark.parametrize("presenter", ["authorization_code", "refresh_token"])
def test_replayed_user_credential_counts_only_in_that_users_bucket(api, burst, store, presenter):
    code, verifier = authorize(api)
    issued = exchange(api, code, verifier)  # spends the code
    assert issued.status_code == 200, issued.text
    sign_in_other_user(api, store)
    other_code, other_verifier = authorize(api)
    burst.counter.peer_exhausted = True
    if presenter == "authorization_code":
        replay = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK}
    else:
        replay = {"grant_type": "refresh_token", "refresh_token": issued.json()["refresh_token"]}
    answers = [api.post("/oidc/token", data=replay).status_code for _ in range(LIMIT + 3)]
    assert 429 in answers  # the presenting user's own bucket ran out
    assert burst.counter.holder("token", HOLDER) == LIMIT + 3
    other = exchange(api, other_code, other_verifier)
    assert other.status_code == 200, other.text
    assert burst.counter.holder("token", f"oidc-client:{CLIENT}:user:{OTHER_USER}") == 1
