"""Tests for _github_get_user_info — verified-primary-only contract.

Issue #481: ``_github_get_user_info`` always fetches ``/user/emails`` and
returns the primary verified address with ``email_verified=True``. The
public-profile ``email`` field on ``/user`` is intentionally ignored —
trusting it would let a user surface an unverified address as their
sync target on every OAuth login.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routes.auth import _github_get_user_info


def _mock_response(json_payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_payload)
    return resp


def _patch_httpx(user_payload, emails_payload):
    """Yield a context that mocks httpx.AsyncClient returning the two payloads in order."""
    client_instance = AsyncMock()
    client_instance.__aenter__.return_value = client_instance
    client_instance.__aexit__.return_value = None
    client_instance.get = AsyncMock(
        side_effect=[_mock_response(user_payload), _mock_response(emails_payload)]
    )
    return patch("api.routes.auth.httpx.AsyncClient", return_value=client_instance)


class TestGithubGetUserInfo:
    @pytest.mark.asyncio
    async def test_returns_verified_primary_email(self):
        """Picks the address that is BOTH primary AND verified, ignoring /user.email."""
        user = {"id": 12345, "login": "alice", "name": "Alice", "email": "public@untrusted.io"}
        emails = [
            {"email": "secondary@example.com", "primary": False, "verified": True},
            {"email": "primary@example.com", "primary": True, "verified": True},
            {"email": "unverified@example.com", "primary": False, "verified": False},
        ]
        with _patch_httpx(user, emails):
            info = await _github_get_user_info("tok")

        assert info["email"] == "primary@example.com"
        assert info["email_verified"] is True

    @pytest.mark.asyncio
    async def test_ignores_public_profile_email_even_when_present(self):
        """The legacy bug: /user.email was trusted without verification.

        After #481 the public profile email must NEVER be the chosen address.
        Even when /user.email matches a verified row, the function still selects
        based on /user/emails (which is what we're verifying via the unrelated
        /user.email value below).
        """
        user = {"id": 1, "login": "u", "name": "U", "email": "tampered@attacker.io"}
        emails = [
            {"email": "real-primary@example.com", "primary": True, "verified": True},
        ]
        with _patch_httpx(user, emails):
            info = await _github_get_user_info("tok")

        assert info["email"] == "real-primary@example.com"
        assert info["email"] != user["email"]

    @pytest.mark.asyncio
    async def test_raises_when_no_verified_primary(self):
        """No primary+verified entry → ValueError (callback fails before DB write)."""
        user = {"id": 1, "login": "u", "email": None}
        emails = [
            {"email": "primary@example.com", "primary": True, "verified": False},
            {"email": "verified@example.com", "primary": False, "verified": True},
        ]
        with _patch_httpx(user, emails):
            with pytest.raises(ValueError, match="no verified primary email"):
                await _github_get_user_info("tok")

    @pytest.mark.asyncio
    async def test_raises_on_empty_email_list(self):
        user = {"id": 1, "login": "u"}
        emails = []
        with _patch_httpx(user, emails):
            with pytest.raises(ValueError, match="no verified primary email"):
                await _github_get_user_info("tok")

    @pytest.mark.asyncio
    async def test_sub_is_stringified_int_id(self):
        user = {"id": 999, "login": "u", "name": "U"}
        emails = [{"email": "u@example.com", "primary": True, "verified": True}]
        with _patch_httpx(user, emails):
            info = await _github_get_user_info("tok")
        assert info["sub"] == "999"

    @pytest.mark.asyncio
    async def test_name_falls_back_to_login_when_missing(self):
        user = {"id": 1, "login": "alice", "name": None}
        emails = [{"email": "a@example.com", "primary": True, "verified": True}]
        with _patch_httpx(user, emails):
            info = await _github_get_user_info("tok")
        assert info["name"] == "alice"
