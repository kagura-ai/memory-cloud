"""API tests for admin signup-gate endpoints (Issue #358 Phase 1).

Patches service-layer methods so these tests stay pure HTTP/serialization
checks — the service behavior is covered by
``tests/services/test_signup_gate_service.py``. Auth and DB are mocked via
dependency_overrides.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin
from db.base import get_db
from utils.github_user import GitHubUserNotFound


def _mock_admin_user() -> dict:
    return {"user_id": "admin_user_1", "email": "admin@test.com", "role": "admin"}


@pytest.fixture
def client():
    async def mock_admin():
        return _mock_admin_user()

    async def mock_db():
        yield MagicMock()

    app.dependency_overrides[require_admin] = mock_admin
    app.dependency_overrides[get_db] = mock_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _mock_config(enabled: bool = False, mode: str = "manual") -> Any:
    return MagicMock(
        enabled=enabled,
        mode=mode,
        github_sponsors_grace_period_days=30,
    )


def _mock_entry(
    username: str = "octocat",
    user_id: str = "583231",
    *,
    provider: str = "github",
    subject_id: str | None = None,
    subject_label: str | None = None,
) -> Any:
    """Build a provider-aware MagicMock entry (#655).

    Defaults mimic a pre-#655 GitHub row so existing tests need no
    changes. Pass ``provider="google"`` + ``subject_id``/``subject_label``
    to mock a Google row.
    """
    if provider == "github":
        resolved_sub = subject_id or user_id
        resolved_label = subject_label or username
        legacy_uid = user_id
        legacy_username = username
    else:
        assert subject_id is not None, "google mock requires subject_id"
        resolved_sub = subject_id
        resolved_label = subject_label or username
        legacy_uid = f"{provider}:{subject_id}"
        legacy_username = resolved_label
    return MagicMock(
        id=uuid4(),
        provider=provider,
        subject_id=resolved_sub,
        subject_label=resolved_label,
        github_user_id=legacy_uid,
        github_username=legacy_username,
        source="manual",
        state="active",
        added_by_user_id="admin_user_1",
        created_at=datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC),
    )


class TestGetConfig:
    def test_returns_config_shape(self, client, monkeypatch):
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.get_config",
            AsyncMock(return_value=_mock_config(enabled=False, mode="manual")),
        )
        response = client.get("/api/v1/admin/signup-gate/config")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert body["mode"] == "manual"
        assert body["github_sponsors_grace_period_days"] == 30


class TestUpdateConfig:
    def test_updates_manual_mode(self, client, monkeypatch):
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.update_config",
            AsyncMock(return_value=_mock_config(enabled=True, mode="manual")),
        )
        response = client.put(
            "/api/v1/admin/signup-gate/config",
            json={"enabled": True, "mode": "manual"},
        )
        assert response.status_code == 200
        assert response.json()["enabled"] is True

    def test_rejects_sponsors_mode_with_422(self, client):
        # Phase 2 modes are narrowed out of the update schema, so Pydantic
        # rejects them at parse time with 422 Unprocessable Entity.
        response = client.put(
            "/api/v1/admin/signup-gate/config",
            json={"enabled": True, "mode": "github_sponsors"},
        )
        assert response.status_code == 422

    def test_rejects_both_mode_with_422(self, client):
        response = client.put(
            "/api/v1/admin/signup-gate/config",
            json={"enabled": True, "mode": "both"},
        )
        assert response.status_code == 422


class TestListAllowlist:
    def test_empty_list(self, client, monkeypatch):
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.list_allowlist",
            AsyncMock(return_value=[]),
        )
        response = client.get("/api/v1/admin/signup-gate/allowlist")
        assert response.status_code == 200
        assert response.json() == []

    def test_returns_entries(self, client, monkeypatch):
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.list_allowlist",
            AsyncMock(return_value=[_mock_entry("octocat"), _mock_entry("hubot", "1")]),
        )
        response = client.get("/api/v1/admin/signup-gate/allowlist")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2
        assert body[0]["github_username"] == "octocat"


class TestAddToAllowlist:
    def test_201_on_success(self, client, monkeypatch):
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.add_to_allowlist",
            AsyncMock(return_value=_mock_entry("octocat")),
        )
        response = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"github_username": "octocat"},
        )
        assert response.status_code == 201
        assert response.json()["github_username"] == "octocat"

    def test_404_when_github_user_not_found(self, client, monkeypatch):
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.add_to_allowlist",
            AsyncMock(side_effect=GitHubUserNotFound("ghost")),
        )
        response = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"github_username": "ghost"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_409_on_duplicate(self, client, monkeypatch):
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.add_to_allowlist",
            AsyncMock(side_effect=ValueError("already on the manual allowlist")),
        )
        response = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"github_username": "octocat"},
        )
        assert response.status_code == 409

    def test_502_on_github_api_error(self, client, monkeypatch):
        """GitHub API rate limit / network error surfaces as 502."""
        request = httpx.Request("GET", "https://api.github.com/users/octocat")
        response = httpx.Response(403, request=request)
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.add_to_allowlist",
            AsyncMock(
                side_effect=httpx.HTTPStatusError("rate limit", request=request, response=response)
            ),
        )
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"github_username": "octocat"},
        )
        assert resp.status_code == 502
        assert "GitHub" in resp.json()["detail"]

    def test_422_on_empty_username(self, client):
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"github_username": ""},
        )
        assert resp.status_code == 422

    def test_422_on_overlong_username(self, client):
        # GitHub caps usernames at 39 chars — anything longer is rejected
        # before we spend a network call on it.
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"github_username": "a" * 40},
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "bad_username",
        [
            "-leading-hyphen",
            "trailing-hyphen-",
            "has space",
            "has_underscore",
            "dot.name",
            "has!bang",
        ],
    )
    def test_422_on_malformed_username(self, client, bad_username):
        """GitHub usernames reject hyphen-endpoints + non-alphanumeric chars."""
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"github_username": bad_username},
        )
        assert resp.status_code == 422

    # ---- #655: provider-aware add ---------------------------------------

    def test_legacy_payload_still_works(self, client, monkeypatch):
        """Pre-#655 payload ``{github_username}`` (no provider field) still
        routes to the GitHub path (provider defaults to 'github')."""
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.add_to_allowlist",
            AsyncMock(return_value=_mock_entry("octocat")),
        )
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"github_username": "octocat"},
        )
        assert resp.status_code == 201
        body = resp.json()
        # Response shape now includes the provider-aware fields too.
        assert body["provider"] == "github"
        assert body["subject_id"] == "583231"
        assert body["subject_label"] == "octocat"
        assert body["github_username"] == "octocat"

    def test_201_on_google_success(self, client, monkeypatch):
        """provider='google' resolves sub via the local users table and
        creates an entry with the OIDC sub as subject_id."""
        google_sub = "108276939729829363"
        monkeypatch.setattr(
            "api.routes.admin_signup_gate.resolve_google_sub_by_email",
            AsyncMock(return_value=(google_sub, "alice@example.com")),
        )
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.add_to_allowlist_entry",
            AsyncMock(
                return_value=_mock_entry(
                    provider="google",
                    subject_id=google_sub,
                    subject_label="alice@example.com",
                )
            ),
        )
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"provider": "google", "email": "alice@example.com"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["provider"] == "google"
        assert body["subject_id"] == google_sub
        assert body["subject_label"] == "alice@example.com"

    def test_google_pending_row_when_user_not_in_users_table(self, client, monkeypatch):
        """Phase 2 (#655 follow-up): admin can pre-allowlist a Google user
        by email before they've OAuth'd. The handler creates a pending row
        with a sentinel ``subject_id='pending:<email>'`` that the OAuth
        callback gate promotes to the real OIDC sub on first sign-in.
        """
        from utils.google_user import GoogleUserNotFound

        monkeypatch.setattr(
            "api.routes.admin_signup_gate.resolve_google_sub_by_email",
            AsyncMock(side_effect=GoogleUserNotFound("user must OAuth first")),
        )
        add_mock = AsyncMock(
            return_value=_mock_entry(
                provider="google",
                subject_id="pending:stranger@example.com",
                subject_label="stranger@example.com",
            )
        )
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.add_to_allowlist_entry",
            add_mock,
        )

        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"provider": "google", "email": "Stranger@Example.com"},
        )
        assert resp.status_code == 201

        # Route normalized the email to lower-case and built the sentinel
        # subject_id format the OAuth callback gate looks for.
        add_mock.assert_awaited_once()
        kwargs = add_mock.await_args.kwargs
        assert kwargs["provider"] == "google"
        assert kwargs["subject_id"] == "pending:stranger@example.com"
        assert kwargs["subject_label"] == "stranger@example.com"

        body = resp.json()
        assert body["provider"] == "google"
        assert body["subject_id"] == "pending:stranger@example.com"
        assert body["subject_label"] == "stranger@example.com"

    def test_google_422_when_email_missing(self, client):
        """provider='google' requires the email field."""
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"provider": "google"},
        )
        assert resp.status_code == 422

    def test_github_422_when_username_missing(self, client):
        """Explicit provider='github' still needs github_username."""
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"provider": "github"},
        )
        assert resp.status_code == 422

    def test_422_on_unknown_provider(self, client):
        """provider values outside the Literal allow-list reject at parse time."""
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"provider": "microsoft", "email": "alice@example.com"},
        )
        assert resp.status_code == 422

    def test_422_when_github_payload_has_email(self, client):
        """PR #657 review #3: provider=github MUST NOT accept an email field.

        Without the model_validator, the extra field would be silently
        ignored and the request would succeed with confusing data — admins
        would not see their typo. The validator rejects with 422 so the
        mismatch is explicit.
        """
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={
                "provider": "github",
                "github_username": "octocat",
                "email": "alice@example.com",
            },
        )
        assert resp.status_code == 422
        assert "email must not be set" in str(resp.json())

    def test_422_when_google_payload_has_github_username(self, client):
        """Symmetric: provider=google MUST NOT accept github_username."""
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={
                "provider": "google",
                "email": "alice@example.com",
                "github_username": "octocat",
            },
        )
        assert resp.status_code == 422
        assert "github_username must not be set" in str(resp.json())

    def test_422_on_malformed_google_email(self, client):
        """The email regex pattern (Field(pattern=...)) rejects non-RFC strings
        before we hit the DB. We deliberately use a regex rather than
        pydantic.EmailStr to avoid pulling in email-validator as a new
        dependency — see the AllowlistAddRequest.email comment in
        admin_signup_gate.py."""
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"provider": "google", "email": "not-an-email"},
        )
        assert resp.status_code == 422

    def test_409_on_duplicate_google_entry(self, client, monkeypatch):
        """Duplicate Google entry surfaces the standard 409 conflict."""
        monkeypatch.setattr(
            "api.routes.admin_signup_gate.resolve_google_sub_by_email",
            AsyncMock(return_value=("9999", "dup@b.com")),
        )
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.add_to_allowlist_entry",
            AsyncMock(side_effect=ValueError("already on the manual allowlist")),
        )
        resp = client.post(
            "/api/v1/admin/signup-gate/allowlist",
            json={"provider": "google", "email": "dup@b.com"},
        )
        assert resp.status_code == 409


class TestRemoveFromAllowlist:
    def test_204_on_success(self, client, monkeypatch):
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.remove_from_allowlist",
            AsyncMock(return_value=None),
        )
        response = client.delete(f"/api/v1/admin/signup-gate/allowlist/{uuid4()}")
        assert response.status_code == 204

    def test_404_when_entry_missing(self, client, monkeypatch):
        monkeypatch.setattr(
            "services.signup_gate_service.SignupGateService.remove_from_allowlist",
            AsyncMock(side_effect=ValueError("Allowlist entry ... not found")),
        )
        response = client.delete(f"/api/v1/admin/signup-gate/allowlist/{uuid4()}")
        assert response.status_code == 404
