"""Password hashing utilities using bcrypt.

Issue #51: Password + MFA login for initial admin.
"""

import bcrypt


def hash_password(password: str) -> str:
    """Hash a password using bcrypt with cost factor 12."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a bcrypt hash."""
    return bcrypt.checkpw(password.encode(), password_hash.encode())
