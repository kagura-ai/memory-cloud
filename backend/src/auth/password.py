"""Password hashing utilities using bcrypt.

Issue #51: Password + MFA login for initial admin.
Issue #1707: passwords longer than 72 bytes under bcrypt 5.
"""

import bcrypt

# bcrypt only ever hashes the first 72 bytes of its input. bcrypt 4 truncated
# longer passwords silently; bcrypt 5 raises ``ValueError`` from ``hashpw`` and
# ``checkpw`` instead. Hashes stored while bcrypt 4 was installed therefore
# cover only the first 72 bytes of the UTF-8 encoded password.
PASSWORD_MAX_BYTES = 72

PASSWORD_TOO_LONG_MESSAGE = (
    f"Password must be at most {PASSWORD_MAX_BYTES} bytes when UTF-8 encoded."
)


class PasswordTooLongError(ValueError):
    """Raised by ``hash_password`` for a password bcrypt cannot hash in full."""

    def __init__(self) -> None:
        super().__init__(PASSWORD_TOO_LONG_MESSAGE)


def is_password_too_long(password: str) -> bool:
    """Return whether ``password`` exceeds bcrypt's 72-byte input limit.

    Args:
        password: The plaintext password.

    Returns:
        True when its UTF-8 encoding is longer than ``PASSWORD_MAX_BYTES``.
    """
    return len(password.encode()) > PASSWORD_MAX_BYTES


def hash_password(password: str) -> str:
    """Hash a password using bcrypt with cost factor 12.

    Args:
        password: The plaintext password.

    Returns:
        The bcrypt hash as a string.

    Raises:
        PasswordTooLongError: The password is longer than 72 bytes when UTF-8
            encoded. It is refused rather than truncated so a new password never
            silently ignores what the user typed past byte 72.
    """
    if is_password_too_long(password):
        raise PasswordTooLongError()
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a bcrypt hash.

    Only the first 72 bytes of the UTF-8 encoded password are compared, as
    bcrypt 4 did. Hashes created under bcrypt 4 from a longer password keep
    verifying, and an over-long wrong password is a plain mismatch instead of
    bcrypt 5's ``ValueError``.

    Args:
        password: The plaintext password to check.
        password_hash: The stored bcrypt hash.

    Returns:
        True when the password matches the hash.
    """
    return bcrypt.checkpw(password.encode()[:PASSWORD_MAX_BYTES], password_hash.encode())
