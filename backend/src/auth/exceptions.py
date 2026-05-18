"""Authentication-related exceptions.

Issue #13: OAuth2 authentication
Issue #31: Frontend integration
"""


class AuthenticationError(Exception):
    """Base authentication error."""

    pass


class InvalidCredentialsError(AuthenticationError):
    """Invalid credentials provided."""

    pass


class NotAuthenticatedError(AuthenticationError):
    """User is not authenticated."""

    pass


class TokenRefreshError(AuthenticationError):
    """Failed to refresh access token."""

    def __init__(self, provider: str, *, reason: str | None = None) -> None:
        self.provider = provider
        self.reason = reason
        msg = f"Token refresh failed for {provider}"
        if reason:
            msg = f"{msg}: {reason}"
        super().__init__(msg)


class SessionExpiredError(AuthenticationError):
    """Session has expired."""

    pass


class InvalidSessionError(AuthenticationError):
    """Invalid session ID or session not found."""

    pass
