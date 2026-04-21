"""Tests for the GitHub username → user_id resolver (Issue #358)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from utils.github_user import GitHubUserNotFound, resolve_github_user_id


def _fake_response(status_code: int, json_body=None) -> MagicMock:
    """Build a fake httpx.Response-like object for patching AsyncClient.get."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body or {})
    if status_code >= 400:
        request = MagicMock(spec=httpx.Request)
        response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                f"HTTP {status_code}", request=request, response=response
            )
        )
    else:
        response.raise_for_status = MagicMock()
    return response


class TestResolveGitHubUserId:
    @pytest.mark.asyncio
    async def test_resolves_username_to_numeric_id(self):
        """Happy path: GitHub returns 200 with {id, login}."""
        fake = _fake_response(200, {"id": 583231, "login": "octocat"})
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake)):
            user_id, canonical = await resolve_github_user_id("octocat")
        assert user_id == "583231"
        assert canonical == "octocat"

    @pytest.mark.asyncio
    async def test_canonicalizes_login_case(self):
        """GitHub returns canonical login even if query was mis-cased."""
        fake = _fake_response(200, {"id": 1, "login": "octocat"})
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake)):
            _, canonical = await resolve_github_user_id("OctoCat")
        assert canonical == "octocat"

    @pytest.mark.asyncio
    async def test_raises_not_found_on_404(self):
        fake = _fake_response(404)
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake)):
            with pytest.raises(GitHubUserNotFound):
                await resolve_github_user_id("no-such-user-xyz-abc")

    @pytest.mark.asyncio
    async def test_propagates_http_error_on_rate_limit(self):
        """403 (rate limit or other) surfaces as HTTPStatusError, not NotFound."""
        fake = _fake_response(403)
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake)):
            with pytest.raises(httpx.HTTPStatusError):
                await resolve_github_user_id("octocat")
