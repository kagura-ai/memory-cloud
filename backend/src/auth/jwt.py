"""JWT token generation and validation utilities.

Provides JWT access token creation and verification for API authentication.
Uses authlib.jose for JWT operations (migrated from python-jose in #183).
"""

import base64
import binascii
import json
from datetime import UTC, datetime, timedelta

from authlib.jose import jwt
from authlib.jose.errors import ExpiredTokenError, JoseError

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

    header = {"alg": settings.jwt_algorithm}
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "iat": now,
        "exp": expire,
    }

    token_bytes = jwt.encode(header, payload, settings.jwt_secret)
    return token_bytes.decode("utf-8")


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
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            claims_options={"iss": {"essential": False}},
        )
        # Enforce configured algorithm — reject tokens signed with unexpected alg
        token_alg = claims.header.get("alg")
        if token_alg != settings.jwt_algorithm:
            raise AuthenticationError(
                f"JWT algorithm mismatch: expected {settings.jwt_algorithm}, got {token_alg}"
            )
        claims.validate()
        return dict(claims)

    except ExpiredTokenError as e:
        raise TokenExpiredError("JWT token has expired") from e

    except JoseError as e:
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
        if not isinstance(token, str) or not token:
            return None
        # Manually decode the payload segment (no signature verification)
        parts = token.split(".")
        if len(parts) != 3 or not parts[1]:
            return None
        # Add only the missing padding required for base64url decoding
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        decoded = json.loads(payload_bytes)
        if not isinstance(decoded, dict):
            return None
        return decoded
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
        return None
