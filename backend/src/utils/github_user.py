"""Resolve a GitHub username to its immutable numeric user ID (Issue #358).

The signup allowlist keys on ``github_user_id`` (numeric, never changes)
rather than username (which GitHub lets users rename), so the admin UI
accepts a username and this helper resolves it to the canonical pair at
add-time.

Uses the public GitHub REST API ``GET /users/{username}`` unauthenticated.
The 60 req/hr rate limit is sufficient for Phase 1's manual admin flow;
Phase 2's bulk Sponsors sync will authenticate with the admin-configured
token and get a 5000 req/hr budget.
"""

from typing import NamedTuple

import httpx

_GITHUB_API = "https://api.github.com"
_TIMEOUT_SECONDS = 10.0


class GitHubUser(NamedTuple):
    """Canonical GitHub user identity returned by the resolver."""

    user_id: str
    login: str


class GitHubUserNotFound(Exception):
    """Raised when the GitHub username does not resolve to an existing user."""


async def resolve_github_user_id(username: str) -> GitHubUser:
    """Resolve ``username`` to its immutable numeric ID and canonical login.

    Args:
        username: GitHub handle (case-insensitive per GitHub).

    Returns:
        ``GitHubUser(user_id, login)`` — numeric ID as string, canonical login.

    Raises:
        GitHubUserNotFound: The username does not exist (HTTP 404).
        httpx.HTTPStatusError: Any other HTTP error (rate limit, 5xx, etc.).
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.get(
            f"{_GITHUB_API}/users/{username}",
            headers={"Accept": "application/vnd.github+json"},
        )
    if response.status_code == 404:
        raise GitHubUserNotFound(username)
    response.raise_for_status()
    data = response.json()
    return GitHubUser(user_id=str(data["id"]), login=data["login"])
