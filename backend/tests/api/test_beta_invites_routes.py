"""HTTP wiring for ``/api/v1/beta-invites`` (Issues #1581, #1595).

The service is patched; these pin what the web UI (#1582) consumes: the kill
switch (every route a plain 404 *before* auth), the response shapes, the 409
machine codes, the public preview's 404 / 410 split and its per-IP limiter, and
that the plaintext URL appears in the ``POST`` / reissue responses and nowhere
else. #1595 adds the optional ``label`` body (a body-less ``POST`` must keep
working), ``label`` / ``redeemed_email`` / ``active`` / ``redeemed`` on the
summary, and ``POST /{id}/reissue``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import structlog
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_session_auth
from config.settings import get_settings
from db.base import get_db
from models.beta_invite import BetaInvite
from services.beta_invite_service import BetaInviteSummary, MintedBetaInvite
from utils.exceptions import (
    BetaInviteAlreadyRedeemedError,
    BetaInviteAlreadyRevokedError,
    BetaInviteGoneError,
    BetaInviteQuotaExceededError,
    NotFoundException,
)

SERVICE = "api.routes.beta_invites.BetaInviteService"
TOKEN = "t" * 43
INVITE_ID = uuid4()

INVITER_ROUTES = [
    ("GET", "/api/v1/beta-invites/me"),
    ("POST", "/api/v1/beta-invites"),
    ("DELETE", f"/api/v1/beta-invites/{INVITE_ID}"),
    ("POST", f"/api/v1/beta-invites/{INVITE_ID}/reissue"),
]
ROUTES = [*INVITER_ROUTES, ("GET", f"/api/v1/beta-invites/{TOKEN}/preview")]


def _invite(**overrides) -> BetaInvite:
    fields = {
        "id": INVITE_ID,
        "token_hash": "a" * 64,
        "inviter_user_id": "user_1",
        "created_at": datetime(2026, 9, 1, 12, 0, 0),
        "expires_at": datetime(2099, 9, 8, 12, 0, 0),
        "redeemed_at": None,
        "redeemed_allowlist_entry_id": uuid4(),
        "revoked_at": None,
        "label": None,
    }
    fields.update(overrides)
    return BetaInvite(**fields)


def _summary(**overrides) -> BetaInviteSummary:
    fields = {
        "quota": 4,
        "used": 1,
        "active": 1,
        "redeemed": 0,
        "remaining": 3,
        "invites": [_invite()],
        "redeemed_emails": {},
    }
    fields.update(overrides)
    return BetaInviteSummary(**fields)


async def _mock_db():
    yield MagicMock()


@pytest.fixture
def client():
    async def mock_session():
        return {"user_id": "user_1", "email": "user@test.example", "role": "user"}

    app.dependency_overrides[require_session_auth] = mock_session
    app.dependency_overrides[get_db] = _mock_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def anonymous_client():
    app.dependency_overrides[get_db] = _mock_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_beta_invites", True)


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    """The preview limiter counts in Redis; keep these tests hermetic."""
    counter = AsyncMock(return_value=1)
    monkeypatch.setattr("api.routes.beta_invites.incrby_counter", counter)
    return counter


class TestKillSwitch:
    @pytest.mark.parametrize(("method", "path"), ROUTES)
    def test_every_route_404s_when_disabled(self, client, monkeypatch, method, path) -> None:
        monkeypatch.setattr(get_settings(), "enable_beta_invites", False)
        assert client.request(method, path).status_code == 404

    @pytest.mark.parametrize(("method", "path"), ROUTES)
    def test_disabled_is_404_before_auth_and_looks_like_no_route_at_all(
        self, anonymous_client, monkeypatch, no_redis, method, path
    ) -> None:
        """An unauthenticated caller must get 404, not 401 — a 401/404 split would
        advertise the endpoint. The body is the one an unknown path produces."""
        monkeypatch.setattr(get_settings(), "enable_beta_invites", False)

        response = anonymous_client.request(method, path)

        assert response.status_code == 404
        assert response.json() == anonymous_client.get("/api/v1/no-such-route").json()
        no_redis.assert_not_awaited()  # inert: not even the limiter runs

    def test_default_is_disabled(self) -> None:
        assert type(get_settings()).model_fields["enable_beta_invites"].default is False

    @pytest.mark.parametrize(("method", "path"), INVITER_ROUTES)
    def test_enabled_inviter_routes_require_a_session(
        self, anonymous_client, enabled, method, path
    ) -> None:
        assert anonymous_client.request(method, path).status_code == 401

    @pytest.mark.parametrize(("method", "path"), INVITER_ROUTES)
    def test_inviter_routes_reject_an_api_key(
        self, anonymous_client, enabled, monkeypatch, method, path
    ) -> None:
        """Session-only: a Bearer credential is refused outright (#252), and the
        service is never reached — a key that could mint or reissue links would
        be a scriptable account-creation endpoint."""
        called = AsyncMock()
        for name in ("get_summary", "create", "revoke", "reissue"):
            monkeypatch.setattr(f"{SERVICE}.{name}", called)

        response = anonymous_client.request(
            method, path, headers={"Authorization": "Bearer kagura_not_a_real_key"}
        )

        assert response.status_code == 403
        called.assert_not_awaited()


class TestSummary:
    def test_shape_for_a_capped_user(self, client, enabled, monkeypatch) -> None:
        redeemed_id = uuid4()
        redeemed = _invite(
            id=redeemed_id, redeemed_at=datetime(2026, 9, 2, 8, 30, 0), label="Bob from work"
        )
        monkeypatch.setattr(
            f"{SERVICE}.get_summary",
            AsyncMock(
                return_value=_summary(
                    used=2,
                    active=1,
                    redeemed=1,
                    remaining=2,
                    invites=[_invite(), redeemed],
                    redeemed_emails={redeemed_id: "bob@invitee.example"},
                )
            ),
        )

        response = client.get("/api/v1/beta-invites/me")

        assert response.status_code == 200
        body = response.json()
        assert (body["quota"], body["used"], body["remaining"]) == (4, 2, 2)
        # #1595: the breakdown the quota is made of.
        assert (body["active"], body["redeemed"]) == (1, 1)
        assert body["invites"][0] == {
            "id": str(INVITE_ID),
            "status": "active",
            "label": None,
            "created_at": "2026-09-01T12:00:00Z",
            "expires_at": "2099-09-08T12:00:00Z",
            "redeemed_at": None,
            "revoked_at": None,
            "redeemed_email": None,
        }
        assert body["invites"][1]["status"] == "redeemed"
        assert body["invites"][1]["redeemed_at"] == "2026-09-02T08:30:00Z"
        assert body["invites"][1]["label"] == "Bob from work"
        assert body["invites"][1]["redeemed_email"] == "bob@invitee.example"

    def test_item_fields_are_the_contract_and_never_the_token(
        self, client, enabled, monkeypatch
    ) -> None:
        """Was ``test_inviter_never_sees_who_redeemed_or_the_token`` (#1581). #1595
        reverses the "status only" stance on purpose — the inviter vouched for the
        person — so ``label`` and ``redeemed_email`` join the item. Still no token,
        no hash, no URL, no allowlist id."""
        monkeypatch.setattr(f"{SERVICE}.get_summary", AsyncMock(return_value=_summary()))

        response = client.get("/api/v1/beta-invites/me")
        invite = response.json()["invites"][0]

        assert set(invite) == {
            "id",
            "status",
            "label",
            "created_at",
            "expires_at",
            "redeemed_at",
            "revoked_at",
            "redeemed_email",
        }
        assert "a" * 64 not in response.text  # the token hash

    def test_redeemed_email_is_only_ever_attached_to_a_redeemed_row(
        self, client, enabled, monkeypatch
    ) -> None:
        """Defence in depth at the serialisation edge: whatever the service maps,
        a row that is not ``redeemed`` renders ``null``."""
        revoked_id = uuid4()
        revoked = _invite(
            id=revoked_id,
            redeemed_at=datetime(2026, 9, 2, 8, 30, 0),
            revoked_at=datetime(2026, 9, 3, 8, 30, 0),
        )
        monkeypatch.setattr(
            f"{SERVICE}.get_summary",
            AsyncMock(
                return_value=_summary(
                    invites=[revoked], redeemed_emails={revoked_id: "x@invitee.example"}
                )
            ),
        )

        invite = client.get("/api/v1/beta-invites/me").json()["invites"][0]

        assert invite["status"] == "revoked"
        assert invite["redeemed_email"] is None

    def test_admin_is_unlimited_as_nulls(self, client, enabled, monkeypatch) -> None:
        monkeypatch.setattr(
            f"{SERVICE}.get_summary",
            AsyncMock(
                return_value=_summary(
                    quota=None, used=9, active=7, redeemed=2, remaining=None, invites=[]
                )
            ),
        )

        body = client.get("/api/v1/beta-invites/me").json()

        assert body == {
            "quota": None,
            "used": 9,
            "active": 7,
            "redeemed": 2,
            "remaining": None,
            "invites": [],
        }


class TestCreate:
    def test_returns_the_url_once(self, client, enabled, monkeypatch) -> None:
        create = AsyncMock(
            return_value=MintedBetaInvite(
                invite=_invite(), url=f"https://app.example.test/join/{TOKEN}"
            )
        )
        monkeypatch.setattr(f"{SERVICE}.create", create)

        # No body at all — what every pre-#1595 client sends.
        response = client.post("/api/v1/beta-invites")

        assert response.status_code == 201
        assert response.json() == {
            "id": str(INVITE_ID),
            "url": f"https://app.example.test/join/{TOKEN}",
            "expires_at": "2099-09-08T12:00:00Z",
            "label": None,
        }
        assert create.await_args.kwargs["user_id"] == "user_1"
        assert create.await_args.kwargs["user_email"] == "user@test.example"
        assert create.await_args.kwargs["label"] is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({}, id="no-body-no-content-type"),
            # The web client always sends the JSON content type, body or not.
            pytest.param(
                {"headers": {"Content-Type": "application/json"}}, id="json-content-type-empty"
            ),
            pytest.param({"json": {}}, id="empty-object"),
            pytest.param({"json": {"label": None}}, id="null-label"),
            pytest.param({"json": {"label": "   "}}, id="blank-label"),
        ],
    )
    def test_label_is_optional_in_every_spelling(
        self, client, enabled, monkeypatch, kwargs
    ) -> None:
        create = AsyncMock(
            return_value=MintedBetaInvite(
                invite=_invite(), url=f"https://app.example.test/join/{TOKEN}"
            )
        )
        monkeypatch.setattr(f"{SERVICE}.create", create)

        response = client.post("/api/v1/beta-invites", **kwargs)

        assert response.status_code == 201
        assert create.await_args.kwargs["label"] is None

    def test_label_is_trimmed_and_returned(self, client, enabled, monkeypatch) -> None:
        create = AsyncMock(
            return_value=MintedBetaInvite(
                invite=_invite(label="Alice"), url=f"https://app.example.test/join/{TOKEN}"
            )
        )
        monkeypatch.setattr(f"{SERVICE}.create", create)

        response = client.post("/api/v1/beta-invites", json={"label": "  Alice \n"})

        assert response.status_code == 201
        assert create.await_args.kwargs["label"] == "Alice"
        assert response.json()["label"] == "Alice"

    @pytest.mark.parametrize(
        "label",
        ["x" * 101, "Ali\x00ce", "two\nlines", "tab\there", "del\x7f", 123, ["Alice"]],
    )
    def test_bad_label_is_a_422_that_never_echoes_it(
        self, client, enabled, monkeypatch, label
    ) -> None:
        create = AsyncMock()
        monkeypatch.setattr(f"{SERVICE}.create", create)

        response = client.post("/api/v1/beta-invites", json={"label": label})

        assert response.status_code == 422
        assert response.json()["error"] == "VAL-001"
        create.assert_not_awaited()
        if isinstance(label, str):
            assert label not in response.text
            assert label[:8] not in response.json()["details"]["errors"][0]["msg"]

    def test_quota_exceeded_is_a_409_with_the_contract_code(
        self, client, enabled, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            f"{SERVICE}.create", AsyncMock(side_effect=BetaInviteQuotaExceededError(quota=4))
        )

        response = client.post("/api/v1/beta-invites")

        assert response.status_code == 409
        assert response.json()["error"] == "BETA-INVITE-001"
        assert response.json()["details"] == {"reason": "quota_exceeded", "quota": 4}


class TestRevoke:
    def test_revokes_own_invite(self, client, enabled, monkeypatch) -> None:
        revoke = AsyncMock(return_value=None)
        monkeypatch.setattr(f"{SERVICE}.revoke", revoke)

        response = client.delete(f"/api/v1/beta-invites/{INVITE_ID}")

        assert response.status_code == 204
        assert response.content == b""
        assert revoke.await_args.kwargs["user_id"] == "user_1"
        assert revoke.await_args.kwargs["invite_id"] == INVITE_ID

    def test_unknown_or_someone_elses_is_404(self, client, enabled, monkeypatch) -> None:
        monkeypatch.setattr(
            f"{SERVICE}.revoke", AsyncMock(side_effect=NotFoundException("Beta invite"))
        )

        response = client.delete(f"/api/v1/beta-invites/{INVITE_ID}")

        assert response.status_code == 404

    def test_already_redeemed_is_a_409_with_the_contract_code(
        self, client, enabled, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            f"{SERVICE}.revoke", AsyncMock(side_effect=BetaInviteAlreadyRedeemedError())
        )

        response = client.delete(f"/api/v1/beta-invites/{INVITE_ID}")

        assert response.status_code == 409
        assert response.json()["error"] == "BETA-INVITE-002"
        assert response.json()["details"] == {"reason": "already_redeemed"}

    def test_non_uuid_id_is_rejected_before_the_service(self, client, enabled, monkeypatch) -> None:
        revoke = AsyncMock()
        monkeypatch.setattr(f"{SERVICE}.revoke", revoke)

        response = client.delete("/api/v1/beta-invites/not-a-uuid")

        assert response.status_code == 422
        revoke.assert_not_awaited()


class TestReissue:
    PATH = f"/api/v1/beta-invites/{INVITE_ID}/reissue"

    def test_returns_exactly_the_create_payload(self, client, enabled, monkeypatch) -> None:
        new_id = uuid4()
        reissue = AsyncMock(
            return_value=MintedBetaInvite(
                invite=_invite(id=new_id, label="Alice"),
                url=f"https://app.example.test/join/{TOKEN}",
            )
        )
        monkeypatch.setattr(f"{SERVICE}.reissue", reissue)

        response = client.post(self.PATH)

        assert response.status_code == 201
        assert response.json() == {
            "id": str(new_id),
            "url": f"https://app.example.test/join/{TOKEN}",
            "expires_at": "2099-09-08T12:00:00Z",
            "label": "Alice",
        }
        assert reissue.await_args.kwargs["user_id"] == "user_1"
        assert reissue.await_args.kwargs["invite_id"] == INVITE_ID
        assert reissue.await_args.kwargs["user_email"] == "user@test.example"

    def test_unknown_or_someone_elses_is_404(self, client, enabled, monkeypatch) -> None:
        monkeypatch.setattr(
            f"{SERVICE}.reissue", AsyncMock(side_effect=NotFoundException("Beta invite"))
        )

        assert client.post(self.PATH).status_code == 404

    @pytest.mark.parametrize(
        ("error", "code", "details"),
        [
            (BetaInviteAlreadyRedeemedError(), "BETA-INVITE-002", {"reason": "already_redeemed"}),
            (BetaInviteAlreadyRevokedError(), "BETA-INVITE-003", {"reason": "already_revoked"}),
            (
                BetaInviteQuotaExceededError(quota=4),
                "BETA-INVITE-001",
                {"reason": "quota_exceeded", "quota": 4},
            ),
        ],
    )
    def test_conflicts_are_409s_with_the_contract_codes(
        self, client, enabled, monkeypatch, error, code, details
    ) -> None:
        monkeypatch.setattr(f"{SERVICE}.reissue", AsyncMock(side_effect=error))

        response = client.post(self.PATH)

        assert response.status_code == 409
        assert response.json()["error"] == code
        assert response.json()["details"] == details

    def test_non_uuid_id_is_rejected_before_the_service(self, client, enabled, monkeypatch) -> None:
        reissue = AsyncMock()
        monkeypatch.setattr(f"{SERVICE}.reissue", reissue)

        assert client.post("/api/v1/beta-invites/not-a-uuid/reissue").status_code == 422
        reissue.assert_not_awaited()


class TestNothingPrivateIsLogged:
    """#1595: the label and the admitted e-mail are response-only. No route on
    this surface may put either into a log line."""

    LABEL = "Carol <carol@friend.example>"
    EMAIL = "carol@invitee.example"

    def test_create_reissue_and_list_log_neither(self, client, enabled, monkeypatch) -> None:
        redeemed_id = uuid4()
        minted = MintedBetaInvite(
            invite=_invite(label=self.LABEL), url=f"https://app.example.test/join/{TOKEN}"
        )
        monkeypatch.setattr(f"{SERVICE}.create", AsyncMock(return_value=minted))
        monkeypatch.setattr(f"{SERVICE}.reissue", AsyncMock(return_value=minted))
        monkeypatch.setattr(
            f"{SERVICE}.get_summary",
            AsyncMock(
                return_value=_summary(
                    invites=[
                        _invite(
                            id=redeemed_id,
                            label=self.LABEL,
                            redeemed_at=datetime(2026, 9, 2, 8, 30, 0),
                        )
                    ],
                    redeemed_emails={redeemed_id: self.EMAIL},
                )
            ),
        )

        with structlog.testing.capture_logs() as logs:
            created = client.post("/api/v1/beta-invites", json={"label": self.LABEL})
            rejected = client.post("/api/v1/beta-invites", json={"label": self.LABEL + "\x00"})
            reissued = client.post(f"/api/v1/beta-invites/{INVITE_ID}/reissue")
            listed = client.get("/api/v1/beta-invites/me")

        assert (created.status_code, rejected.status_code) == (201, 422)
        assert (reissued.status_code, listed.status_code) == (201, 200)
        assert listed.json()["invites"][0]["redeemed_email"] == self.EMAIL
        rendered = repr(logs)
        for private in (self.LABEL, "Carol", "friend.example", self.EMAIL, TOKEN):
            assert private not in rendered


class TestPreview:
    def test_valid_token_needs_no_session(self, anonymous_client, enabled, monkeypatch) -> None:
        preview = AsyncMock(return_value=_invite())
        monkeypatch.setattr(f"{SERVICE}.preview", preview)

        response = anonymous_client.get(f"/api/v1/beta-invites/{TOKEN}/preview")

        assert response.status_code == 200
        assert response.json() == {"valid": True, "expires_at": "2099-09-08T12:00:00Z"}
        preview.assert_awaited_once_with(TOKEN)

    def test_unknown_or_revoked_is_404(self, anonymous_client, enabled, monkeypatch) -> None:
        monkeypatch.setattr(
            f"{SERVICE}.preview", AsyncMock(side_effect=NotFoundException("Beta invite"))
        )
        assert anonymous_client.get(f"/api/v1/beta-invites/{TOKEN}/preview").status_code == 404

    def test_expired_or_redeemed_is_410(self, anonymous_client, enabled, monkeypatch) -> None:
        monkeypatch.setattr(f"{SERVICE}.preview", AsyncMock(side_effect=BetaInviteGoneError()))
        assert anonymous_client.get(f"/api/v1/beta-invites/{TOKEN}/preview").status_code == 410

    @pytest.mark.parametrize("token", ["short", "x" * 129, "has.dots.and~tilde" + "x" * 20])
    def test_malformed_token_is_404_without_a_lookup(
        self, anonymous_client, enabled, monkeypatch, token
    ) -> None:
        preview = AsyncMock()
        monkeypatch.setattr(f"{SERVICE}.preview", preview)

        assert anonymous_client.get(f"/api/v1/beta-invites/{token}/preview").status_code == 404
        preview.assert_not_awaited()

    def test_error_bodies_never_echo_the_token(
        self, anonymous_client, enabled, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            f"{SERVICE}.preview", AsyncMock(side_effect=NotFoundException("Beta invite"))
        )
        response = anonymous_client.get(f"/api/v1/beta-invites/{TOKEN}/preview")
        assert TOKEN not in response.text

    def test_per_ip_rate_limit(self, anonymous_client, enabled, monkeypatch, no_redis) -> None:
        from api.routes.beta_invites import PREVIEW_RATE_LIMIT_PER_MINUTE

        preview = AsyncMock(return_value=_invite())
        monkeypatch.setattr(f"{SERVICE}.preview", preview)
        no_redis.return_value = PREVIEW_RATE_LIMIT_PER_MINUTE + 1

        response = anonymous_client.get(f"/api/v1/beta-invites/{TOKEN}/preview")

        assert response.status_code == 429
        assert response.json()["error"] == "RATE-001"
        preview.assert_not_awaited()
        # Keyed on the caller's IP — never on the token.
        key = no_redis.await_args.args[0]
        assert key.startswith("beta_invite_preview:")
        assert TOKEN not in key

    def test_limiter_fails_open_when_redis_is_down(
        self, anonymous_client, enabled, monkeypatch, no_redis
    ) -> None:
        from utils.exceptions import RedisError

        monkeypatch.setattr(f"{SERVICE}.preview", AsyncMock(return_value=_invite()))
        no_redis.side_effect = RedisError("down")

        assert anonymous_client.get(f"/api/v1/beta-invites/{TOKEN}/preview").status_code == 200
