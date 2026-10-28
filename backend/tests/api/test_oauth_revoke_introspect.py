"""Client authentication at the revocation and introspection endpoints (#1741).

RFC 7009 §2.1: the revocation endpoint authenticates confidential clients,
identifies public clients by ``client_id``, and ignores a token issued to
another client. Revoking a refresh token also revokes its access token.
RFC 7662 §2.1: the introspection endpoint requires caller authentication; a
confidential client may introspect only the tokens issued to it.

Runs against an in-memory SQLite database holding the OAuth tables (the
pattern of ``test_oauth_authorization_code_flow.py``).
"""

from __future__ import annotations

import base64
import hashlib
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import quote_plus, urlencode

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from api.main import app  # noqa: E402
from api.routes.oauth import _OAUTH_FORM_MAX_BODY_BYTES  # noqa: E402
from models.auth import OAuth2Client, OAuth2Token  # noqa: E402
from utils.datetime import utcnow  # noqa: E402

PUBLIC_A = "revoke_public_a"
PUBLIC_B = "revoke_public_b"
CONFIDENTIAL = "revoke_confidential"
CONFIDENTIAL_SECRET = "confidential secret:with+specials"
OTHER_CONFIDENTIAL = "revoke_confidential_other"
OTHER_SECRET = "other-confidential-secret"
MCP_RESOURCE = "https://memory.example.test/mcp"


def _client(client_id: str, secret: str | None) -> OAuth2Client:
    return OAuth2Client(
        client_id=client_id,
        client_secret_hash=hashlib.sha256(secret.encode()).hexdigest() if secret else "",
        client_name=client_id,
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="memory:read memory:write",
        token_endpoint_auth_method="client_secret_post" if secret else "none",
        provider="custom",
    )


def _token(client_id: str, name: str) -> OAuth2Token:
    return OAuth2Token(
        client_id=client_id,
        user_id="user-1",
        token_type="Bearer",
        access_token=f"at-{name}",
        refresh_token=f"rt-{name}",
        scope="memory:read memory:write",
        issued_at=utcnow(),
        expires_in=3600,
        resource=MCP_RESOURCE,
    )


@pytest.fixture
def db_factory() -> Iterator[sessionmaker]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    for model in (OAuth2Client, OAuth2Token):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                _client(PUBLIC_A, None),
                _client(PUBLIC_B, None),
                _client(CONFIDENTIAL, CONFIDENTIAL_SECRET),
                _client(OTHER_CONFIDENTIAL, OTHER_SECRET),
                _token(PUBLIC_A, "a"),
                _token(PUBLIC_B, "b"),
                _token(CONFIDENTIAL, "c"),
            ]
        )
        db.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def api(db_factory: sessionmaker) -> Iterator[TestClient]:
    with patch("api.routes.oauth.get_sync_session", side_effect=db_factory):
        yield TestClient(app, base_url="https://testserver")


def _basic(client_id: str, secret: str) -> dict[str, str]:
    raw = f"{quote_plus(client_id)}:{quote_plus(secret)}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _stored(db_factory: sessionmaker, name: str) -> OAuth2Token:
    with db_factory() as db:
        return db.query(OAuth2Token).filter_by(access_token=f"at-{name}").one()


def _revoke(api: TestClient, form: dict[str, str], headers: dict[str, str] | None = None) -> Any:
    return api.post("/api/v1/oauth/revoke", data=form, headers=headers or {})


def _introspect(
    api: TestClient, form: dict[str, str], headers: dict[str, str] | None = None
) -> Any:
    return api.post("/api/v1/oauth/introspect", data=form, headers=headers or {})


def _assert_invalid_client(response: Any) -> None:
    assert response.status_code == 401, response.text
    assert response.json()["error"] == "invalid_client"
    assert response.headers["www-authenticate"].startswith("Basic")
    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Revocation (RFC 7009)
# ---------------------------------------------------------------------------


class TestRevocationClientAuthentication:
    def test_public_client_revokes_its_access_token(self, api, db_factory):
        response = _revoke(api, {"token": "at-a", "client_id": PUBLIC_A})
        assert response.status_code == 200
        assert _stored(db_factory, "a").is_revoked()

    def test_public_client_by_basic_with_empty_secret(self, api, db_factory):
        response = _revoke(api, {"token": "at-a"}, _basic(PUBLIC_A, ""))
        assert response.status_code == 200
        assert _stored(db_factory, "a").is_revoked()

    def test_missing_client_id_is_refused(self, api, db_factory):
        _assert_invalid_client(_revoke(api, {"token": "at-a"}))
        assert not _stored(db_factory, "a").is_revoked()

    def test_unknown_client_is_refused(self, api, db_factory):
        _assert_invalid_client(_revoke(api, {"token": "at-a", "client_id": "nope"}))
        assert not _stored(db_factory, "a").is_revoked()

    def test_token_of_another_client_is_ignored(self, api, db_factory):
        response = _revoke(api, {"token": "at-b", "client_id": PUBLIC_A})
        assert response.status_code == 200
        assert not _stored(db_factory, "b").is_revoked()
        refresh = _revoke(api, {"token": "rt-b", "client_id": PUBLIC_A})
        assert refresh.status_code == 200
        assert _stored(db_factory, "b").is_refresh_token_active()

    def test_ignored_and_unknown_tokens_answer_alike(self, api):
        ignored = _revoke(api, {"token": "at-b", "client_id": PUBLIC_A})
        unknown = _revoke(api, {"token": "no-such-token", "client_id": PUBLIC_A})
        assert ignored.status_code == unknown.status_code == 200
        assert ignored.json() == unknown.json()

    def test_refresh_revocation_also_revokes_the_access_token(self, api, db_factory):
        response = _revoke(
            api, {"token": "rt-a", "token_type_hint": "refresh_token", "client_id": PUBLIC_A}
        )
        assert response.status_code == 200
        stored = _stored(db_factory, "a")
        assert not stored.is_refresh_token_active()
        assert stored.is_revoked()

    def test_access_revocation_keeps_the_refresh_token(self, api, db_factory):
        _revoke(api, {"token": "at-a", "client_id": PUBLIC_A})
        assert _stored(db_factory, "a").is_refresh_token_active()

    def test_confidential_client_needs_its_secret(self, api, db_factory):
        _assert_invalid_client(_revoke(api, {"token": "at-c", "client_id": CONFIDENTIAL}))
        _assert_invalid_client(
            _revoke(api, {"token": "at-c", "client_id": CONFIDENTIAL, "client_secret": "wrong"})
        )
        _assert_invalid_client(_revoke(api, {"token": "at-c"}, _basic(CONFIDENTIAL, "wrong")))
        assert not _stored(db_factory, "c").is_revoked()

    def test_confidential_client_by_post(self, api, db_factory):
        response = _revoke(
            api,
            {"token": "at-c", "client_id": CONFIDENTIAL, "client_secret": CONFIDENTIAL_SECRET},
        )
        assert response.status_code == 200
        assert _stored(db_factory, "c").is_revoked()

    def test_confidential_client_by_basic(self, api, db_factory):
        response = _revoke(api, {"token": "rt-c"}, _basic(CONFIDENTIAL, CONFIDENTIAL_SECRET))
        assert response.status_code == 200
        assert _stored(db_factory, "c").is_revoked()
        assert not _stored(db_factory, "c").is_refresh_token_active()

    def test_malformed_basic_header_is_refused(self, api):
        _assert_invalid_client(
            _revoke(api, {"token": "at-a"}, {"Authorization": "Basic !!!not-base64"})
        )

    def test_two_authentication_methods_are_refused(self, api):
        response = _revoke(
            api,
            {"token": "at-c", "client_secret": CONFIDENTIAL_SECRET},
            _basic(CONFIDENTIAL, CONFIDENTIAL_SECRET),
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"

    def test_basic_and_form_client_ids_must_agree(self, api):
        response = _revoke(api, {"token": "at-a", "client_id": PUBLIC_B}, _basic(PUBLIC_A, ""))
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Introspection (RFC 7662)
# ---------------------------------------------------------------------------


class TestIntrospectionAuthentication:
    def test_unauthenticated_is_refused(self, api):
        _assert_invalid_client(_introspect(api, {"token": "at-c"}))

    def test_public_client_is_refused(self, api):
        _assert_invalid_client(_introspect(api, {"token": "at-a", "client_id": PUBLIC_A}))
        _assert_invalid_client(_introspect(api, {"token": "at-a"}, _basic(PUBLIC_A, "")))

    def test_wrong_secret_is_refused(self, api):
        _assert_invalid_client(_introspect(api, {"token": "at-c"}, _basic(CONFIDENTIAL, "x")))

    def test_own_token_is_described(self, api):
        response = _introspect(api, {"token": "at-c"}, _basic(CONFIDENTIAL, CONFIDENTIAL_SECRET))
        assert response.status_code == 200
        body = response.json()
        assert body["active"] is True
        assert body["client_id"] == CONFIDENTIAL
        assert body["scope"] == "memory:read memory:write"
        assert body["aud"] == MCP_RESOURCE

    def test_own_token_by_post(self, api):
        response = _introspect(
            api,
            {"token": "at-c", "client_id": CONFIDENTIAL, "client_secret": CONFIDENTIAL_SECRET},
        )
        assert response.status_code == 200
        assert response.json()["active"] is True

    def test_token_of_another_client_is_inactive(self, api):
        response = _introspect(api, {"token": "at-a"}, _basic(OTHER_CONFIDENTIAL, OTHER_SECRET))
        assert response.status_code == 200
        assert response.json() == {"active": False}

    def test_unknown_token_is_inactive(self, api):
        response = _introspect(
            api, {"token": "no-such-token"}, _basic(CONFIDENTIAL, CONFIDENTIAL_SECRET)
        )
        assert response.status_code == 200
        assert response.json() == {"active": False}

    def test_revoked_token_is_inactive(self, api):
        _revoke(api, {"token": "rt-c"}, _basic(CONFIDENTIAL, CONFIDENTIAL_SECRET))
        response = _introspect(api, {"token": "at-c"}, _basic(CONFIDENTIAL, CONFIDENTIAL_SECRET))
        assert response.json() == {"active": False}


# ---------------------------------------------------------------------------
# Form body bound (#2207)
# ---------------------------------------------------------------------------

_FORM_LIMIT = _OAUTH_FORM_MAX_BODY_BYTES
_ENDPOINTS = ["/api/v1/oauth/revoke", "/api/v1/oauth/introspect"]
_FORM_HEADERS = {"content-type": "application/x-www-form-urlencoded"}


def _assert_too_large(response: Any) -> None:
    assert response.status_code == 413, response.text
    assert set(response.json()) == {"error", "error_description"}
    assert response.json()["error"] == "invalid_request"
    assert response.headers["cache-control"] == "no-store"


def _oversized(fields: dict[str, str]) -> bytes:
    return urlencode(fields).encode() + b"&pad=" + b"x" * _FORM_LIMIT


class TestFormBodyBound:
    """A revocation or introspection body over 16 KiB is refused before it is
    parsed and before the client is authenticated (#2207)."""

    @pytest.mark.parametrize("path", _ENDPOINTS)
    @pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
    def test_body_over_the_limit_is_invalid_request_413(
        self, api: TestClient, path: str, chunked: bool
    ) -> None:
        body = _oversized({"token": "at-c"})
        # An iterator makes httpx send the body chunked, without Content-Length.
        content = iter([body[:4096], body[4096:]]) if chunked else body
        response = api.post(
            path,
            content=content,
            headers={**_FORM_HEADERS, **_basic(CONFIDENTIAL, CONFIDENTIAL_SECRET)},
        )
        _assert_too_large(response)

    @pytest.mark.parametrize("path", _ENDPOINTS)
    def test_bound_runs_before_client_authentication(self, api: TestClient, path: str) -> None:
        # No credentials at all: 413, not 401 invalid_client.
        response = api.post(path, content=_oversized({"token": "at-c"}), headers=_FORM_HEADERS)
        _assert_too_large(response)

    def test_oversized_revocation_with_valid_credentials_revokes_nothing(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        response = api.post(
            "/api/v1/oauth/revoke",
            content=_oversized(
                {"token": "at-c", "client_id": CONFIDENTIAL, "client_secret": CONFIDENTIAL_SECRET}
            ),
            headers=_FORM_HEADERS,
        )
        _assert_too_large(response)
        assert not _stored(db_factory, "c").is_revoked()

    @pytest.mark.parametrize("path", _ENDPOINTS)
    def test_oversized_multipart_token_part_is_413(self, api: TestClient, path: str) -> None:
        response = api.post(
            path,
            files={"token": ("token.txt", b"x" * 32768, "text/plain")},
            headers=_basic(CONFIDENTIAL, CONFIDENTIAL_SECRET),
        )
        _assert_too_large(response)

    def test_revocation_body_at_the_limit_is_answered(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        prefix = urlencode({"token": "at-a", "client_id": PUBLIC_A}).encode() + b"&pad="
        body = prefix + b"x" * (_FORM_LIMIT - len(prefix))
        assert len(body) == _FORM_LIMIT
        response = api.post("/api/v1/oauth/revoke", content=body, headers=_FORM_HEADERS)
        assert response.status_code == 200, response.text
        assert _stored(db_factory, "a").is_revoked()

    def test_introspection_body_at_the_limit_is_answered(self, api: TestClient) -> None:
        prefix = urlencode({"token": "at-c"}).encode() + b"&pad="
        body = prefix + b"x" * (_FORM_LIMIT - len(prefix))
        assert len(body) == _FORM_LIMIT
        response = api.post(
            "/api/v1/oauth/introspect",
            content=body,
            headers={**_FORM_HEADERS, **_basic(CONFIDENTIAL, CONFIDENTIAL_SECRET)},
        )
        assert response.status_code == 200, response.text
        assert response.json()["active"] is True


class TestMissingToken:
    """An absent or empty ``token`` is an RFC 6749 ``invalid_request`` (#2207)."""

    @pytest.mark.parametrize("path", _ENDPOINTS)
    def test_missing_token_is_invalid_request_400(self, api: TestClient, path: str) -> None:
        response = api.post(
            path,
            data={"client_id": CONFIDENTIAL, "client_secret": CONFIDENTIAL_SECRET},
        )
        assert response.status_code == 400, response.text
        assert set(response.json()) == {"error", "error_description"}
        assert response.json()["error"] == "invalid_request"
        assert response.headers["cache-control"] == "no-store"

    @pytest.mark.parametrize("path", _ENDPOINTS)
    def test_empty_token_is_invalid_request_400(self, api: TestClient, path: str) -> None:
        # FastAPI's ``Form(...)`` treated ``token=`` as missing (422); it stays
        # an error, now in the RFC 6749 shape.
        response = api.post(
            path,
            data={"token": "", "client_id": CONFIDENTIAL, "client_secret": CONFIDENTIAL_SECRET},
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_request"

    def test_repeated_token_is_refused(self, api: TestClient, db_factory) -> None:
        response = api.post(
            "/api/v1/oauth/revoke",
            content=urlencode([("token", "at-b"), ("token", "at-a"), ("client_id", PUBLIC_A)]),
            headers=_FORM_HEADERS,
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"
        assert not _stored(db_factory, "a").is_revoked()


class TestEmptyOptionalFields:
    """An empty optional form field counts as absent, as it did with
    ``Form()`` (#2207): it is not a second client authentication method."""

    def test_empty_client_secret_beside_basic_revokes(self, api, db_factory):
        response = _revoke(
            api,
            {"token": "at-c", "client_secret": "", "token_type_hint": ""},
            _basic(CONFIDENTIAL, CONFIDENTIAL_SECRET),
        )
        assert response.status_code == 200, response.text
        assert _stored(db_factory, "c").is_revoked()

    def test_empty_client_id_beside_basic_public_client(self, api, db_factory):
        response = _revoke(api, {"token": "at-a", "client_id": ""}, _basic(PUBLIC_A, ""))
        assert response.status_code == 200, response.text
        assert _stored(db_factory, "a").is_revoked()

    def test_empty_client_fields_beside_basic_introspect(self, api):
        response = _introspect(
            api,
            {"token": "at-c", "client_id": "", "client_secret": ""},
            _basic(CONFIDENTIAL, CONFIDENTIAL_SECRET),
        )
        assert response.status_code == 200, response.text
        assert response.json()["active"] is True


class TestRepeatedEndpointFields:
    @pytest.mark.parametrize("path", _ENDPOINTS)
    @pytest.mark.parametrize("name", ["token", "client_id", "client_secret", "token_type_hint"])
    @pytest.mark.parametrize("empty_first", [False, True])
    def test_empty_duplicate_is_refused_before_authentication(
        self, api, db_factory, path, name, empty_first
    ):
        from structlog.testing import capture_logs

        fields = {
            "token": "at-c",
            "client_id": CONFIDENTIAL,
            "client_secret": CONFIDENTIAL_SECRET,
            "token_type_hint": "access_token",
        }
        value = fields.pop(name)
        values = ["", value] if empty_first else [value, ""]
        pairs = [*fields.items(), *((name, v) for v in values)]
        with capture_logs() as logs:
            response = api.post(path, content=urlencode(pairs), headers=_FORM_HEADERS)
        assert response.status_code == 400, response.text
        assert response.json() == {
            "error": "invalid_request",
            "error_description": f"Parameter {name} is included more than once",
        }
        rejected = [entry for entry in logs if entry.get("reason") == "repeated_parameter"]
        assert len(rejected) == 1
        assert rejected[0]["param"] == name
        assert CONFIDENTIAL_SECRET not in repr(rejected)
        assert "at-c" not in repr(rejected)
        assert not _stored(db_factory, "c").is_revoked()


@pytest.mark.parametrize("name", ["a", "missing"])
@pytest.mark.parametrize("prefix", ["at", "rt"])
@pytest.mark.parametrize("hint", ["unrecognized-type", "access_token", "refresh_token"])
def test_revocation_hint_is_not_a_type_requirement(api, db_factory, name, prefix, hint):
    response = _revoke(
        api, {"token": f"{prefix}-{name}", "client_id": PUBLIC_A, "token_type_hint": hint}
    )
    assert response.status_code == 200, response.text
    if name == "a":
        assert _stored(db_factory, name).is_revoked()
