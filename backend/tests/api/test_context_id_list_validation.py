"""Request-supplied context-id lists are validated as UUIDs (#1693 item 1).

``POST /api/v1/workspaces/{ws}/invitations`` and
``PUT /api/v1/workspaces/{ws}/members/{user}/context-access`` take an
``allowed_context_ids`` list. Each entry used to reach an unguarded
``UUID(ctx_id)`` in the route, so a value that is not a UUID answered 500.
Both request models now type the list as ``list[UUID]``: a bad entry is a 422
in the canonical ``{error, message, details}`` envelope (VAL-001, via the
``RequestValidationError`` handler), and any spelling ``uuid.UUID`` accepts
(uppercase, braced, dashless) is the same id as its canonical form.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_user_from_api_key_or_session
from db.base import get_db

_WS = uuid.uuid4()
_CTX = uuid.UUID("0f8fad5b-d9cb-469f-a165-70867728950e")
_BAD = "not-a-context-id"

INVITE_URL = f"/api/v1/workspaces/{_WS}/invitations"
ACCESS_URL = f"/api/v1/workspaces/{_WS}/members/member-1/context-access"


def _session_user() -> dict:
    return {"user_id": "admin-1", "sub": "admin-1", "email": "a@test", "role": "user"}


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _override_db(db: MagicMock) -> None:
    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db


def _assert_canonical_422(resp, field: str) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert set(body) == {"error", "message", "details"}
    assert body["error"] == "VAL-001"
    assert body["message"] == "Request validation failed"
    locs = [e["loc"] for e in body["details"]["errors"]]
    assert ["body", field, 0] in locs
    # The rejected value is never reflected back.
    assert _BAD not in resp.text


# ---------------------------------------------------------------------------
# POST /workspaces/{ws}/invitations
# ---------------------------------------------------------------------------


class TestInvitationContextIds:
    def test_non_uuid_entry_is_422_not_500(self, client):
        async def _user():
            return _session_user()

        app.dependency_overrides[get_user_from_api_key_or_session] = _user
        resp = client.post(
            INVITE_URL,
            json={"email": "x@example.com", "role": "member", "allowed_context_ids": [_BAD]},
        )
        _assert_canonical_422(resp, "allowed_context_ids")

    @pytest.mark.parametrize(
        "spelling",
        [str(_CTX).upper(), "{" + str(_CTX) + "}", _CTX.hex],
        ids=["uppercase", "braced", "dashless"],
    )
    def test_valid_spellings_reach_the_service_as_the_same_uuid(self, client, spelling):
        async def _user():
            return _session_user()

        app.dependency_overrides[get_user_from_api_key_or_session] = _user

        workspace = MagicMock(plan_name="enterprise")
        ws_result = MagicMock()
        ws_result.scalar_one.return_value = workspace
        db = MagicMock()
        db.execute = AsyncMock(return_value=ws_result)
        db.commit = AsyncMock()
        _override_db(db)

        invitation = MagicMock()
        invitation.id = 7
        invitation.workspace_id = _WS
        invitation.token = "t" * 32
        invitation.email = "x@example.com"
        invitation.role = "member"
        invitation.invited_by = "admin-1"
        invitation.expires_at = None
        invitation.accepted_at = None
        invitation.accepted_by = None
        invitation.created_at = __import__("datetime").datetime(2026, 1, 1)
        invitation.is_expired.return_value = False
        invitation.is_accepted.return_value = False
        invitation.allowed_context_ids = [_CTX]
        svc = MagicMock()
        svc.create_invitation = AsyncMock(return_value=invitation)

        quota = MagicMock()
        quota.check_member_quota = AsyncMock()

        with (
            patch(
                "api.routes.invitations.authorize_workspace_management",
                AsyncMock(return_value=MagicMock(kind="session")),
            ),
            patch("api.routes.invitations.audit_programmatic_workspace_action", AsyncMock()),
            patch("api.routes.invitations.has_feature", return_value=True),
            patch("api.routes.invitations.InvitationService", return_value=svc),
            patch("services.quota_service.QuotaService", return_value=quota),
        ):
            resp = client.post(
                INVITE_URL,
                json={
                    "email": "x@example.com",
                    "role": "member",
                    "allowed_context_ids": [spelling],
                },
            )

        assert resp.status_code == 200, resp.text
        assert svc.create_invitation.await_args.kwargs["allowed_context_ids"] == [_CTX]
        assert resp.json()["allowed_context_ids"] == [str(_CTX)]


# ---------------------------------------------------------------------------
# PUT /workspaces/{ws}/members/{user}/context-access
# ---------------------------------------------------------------------------


@pytest.fixture
def access_route(monkeypatch):
    """Session admin passes the gate; DB knows exactly one live context (_CTX)."""
    from api.routes import workspaces as mod

    monkeypatch.setattr(mod, "get_current_user", AsyncMock(return_value=_session_user()))
    perm = MagicMock()
    perm.check_workspace_admin = AsyncMock()
    monkeypatch.setattr(mod, "PermissionService", lambda db: perm)

    result = MagicMock()
    result.all.return_value = [(_CTX,)]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    _override_db(db)

    svc = MagicMock()

    async def _update(*, workspace_id, user_id, allowed_context_ids):
        return MagicMock(user_id=user_id, allowed_context_ids=allowed_context_ids)

    svc.update_member_context_access = AsyncMock(side_effect=_update)
    monkeypatch.setattr(mod, "WorkspaceService", lambda db: svc)
    return svc


class TestMemberContextAccessIds:
    def test_non_uuid_entry_is_422_not_500(self, client):
        resp = client.put(ACCESS_URL, json={"allowed_context_ids": [_BAD]})
        _assert_canonical_422(resp, "allowed_context_ids")

    def test_mixed_valid_and_invalid_entries_is_422(self, client):
        resp = client.put(ACCESS_URL, json={"allowed_context_ids": [str(_CTX), _BAD]})
        assert resp.status_code == 422, resp.text
        locs = [e["loc"] for e in resp.json()["details"]["errors"]]
        assert locs == [["body", "allowed_context_ids", 1]]

    @pytest.mark.parametrize(
        "spelling",
        [str(_CTX).upper(), "{" + str(_CTX) + "}", _CTX.hex],
        ids=["uppercase", "braced", "dashless"],
    )
    def test_other_spellings_of_a_valid_id_are_not_reported_invalid(
        self, client, access_route, spelling
    ):
        # The old code compared the raw strings with str(<db uuid>), so an
        # uppercase / braced spelling of a context that exists was answered
        # "Invalid context IDs". Ids are now compared as UUIDs.
        resp = client.put(ACCESS_URL, json={"allowed_context_ids": [spelling]})
        assert resp.status_code == 200, resp.text
        kwargs = access_route.update_member_context_access.await_args.kwargs
        assert kwargs["allowed_context_ids"] == [_CTX]
        assert resp.json()["allowed_context_ids"] == [str(_CTX)]

    def test_unknown_uuid_is_400_naming_the_canonical_id(self, client, access_route):
        unknown = uuid.uuid4()
        resp = client.put(ACCESS_URL, json={"allowed_context_ids": [str(unknown).upper()]})
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == f"Invalid context IDs: ['{unknown}']"
        access_route.update_member_context_access.assert_not_called()

    def test_null_and_empty_keep_their_meaning(self, client, access_route):
        resp = client.put(ACCESS_URL, json={"allowed_context_ids": None})
        assert resp.status_code == 200, resp.text
        assert (
            access_route.update_member_context_access.await_args.kwargs["allowed_context_ids"]
            is None
        )

        resp = client.put(ACCESS_URL, json={"allowed_context_ids": []})
        assert resp.status_code == 200, resp.text
        assert (
            access_route.update_member_context_access.await_args.kwargs["allowed_context_ids"] == []
        )
