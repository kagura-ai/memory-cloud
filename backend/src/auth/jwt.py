"""JWT token generation and validation utilities.

Provides JWT access token creation and verification for API authentication.
"""

from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

from config.settings import get_settings
from utils.exceptions import AuthenticationError, TokenExpiredError


def create_access_token(user_id: str, email: str, role: str) -> str:
    """Create JWT access token.

    Args:
        user_id: OAuth2 sub (user identifier)
        email: User email
        role: User role (admin/user/read_only)

    Returns:
        Encoded JWT token

    Example:
        >>> token = create_access_token("google|123", "user@example.com", "user")
        >>> token
        'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...'
    """
    settings = get_settings()

    now = datetime.now(UTC)
    expire = now + timedelta(minutes=settings.jwt_expire_minutes)

    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "iat": now,
        "exp": expire,
    }

    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token


def verify_access_token(token: str) -> dict:
    """Verify and decode JWT access token.

    Args:
        token: JWT token string

    Returns:
        Decoded payload dict with sub, email, role

    Raises:
        TokenExpiredError: If token has expired
        AuthenticationError: If token is invalid

    Example:
        >>> payload = verify_access_token(token)
        >>> payload["sub"]
        'google|123'
        >>> payload["role"]
        'user'
    """
    settings = get_settings()

    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return payload

    except jwt.ExpiredSignatureError as e:
        raise TokenExpiredError("JWT token has expired") from e

    except JWTError as e:
        raise AuthenticationError(f"Invalid JWT token: {e}") from e


def decode_token_without_verification(token: str) -> dict | None:
    """Decode JWT token without verification (for debugging).

    Args:
        token: JWT token string

    Returns:
        Decoded payload or None if invalid

    Warning:
        Do NOT use for authentication! Use verify_access_token() instead.
    """
    try:
        return jwt.get_unverified_claims(token)
    except JWTError:
        return None
