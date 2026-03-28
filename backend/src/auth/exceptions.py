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

    pass


class SessionExpiredError(AuthenticationError):
    """Session has expired."""

    pass


class InvalidSessionError(AuthenticationError):
    """Invalid session ID or session not found."""

    pass
