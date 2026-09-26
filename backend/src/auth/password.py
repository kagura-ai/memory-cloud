"""Password hashing utilities using bcrypt.

Issue #51: Password + MFA login for initial admin.
Issue #1707: passwords longer than 72 bytes under bcrypt 5.
Issue #1718: passwords that cannot be UTF-8 encoded (a surrogate code point).
"""

import bcrypt

from utils.utf8 import is_utf8_encodable

# bcrypt only ever hashes the first 72 bytes of its input. bcrypt 4 truncated
# longer passwords silently; bcrypt 5 raises ``ValueError`` from ``hashpw`` and
# ``checkpw`` instead. Hashes stored while bcrypt 4 was installed therefore
# cover only the first 72 bytes of the UTF-8 encoded password.
PASSWORD_MAX_BYTES = 72

PASSWORD_TOO_LONG_MESSAGE = (
    f"Password must be at most {PASSWORD_MAX_BYTES} bytes when UTF-8 encoded."
)

PASSWORD_NOT_ENCODABLE_MESSAGE = (
    "Password must be valid Unicode text: it contains a character that cannot be UTF-8 encoded."
)


class PasswordTooLongError(ValueError):
    """Raised by ``hash_password`` for a password bcrypt cannot hash in full."""

    def __init__(self, message: str = PASSWORD_TOO_LONG_MESSAGE) -> None:
        # The message is an argument so pickle / copy can rebuild the error.
        super().__init__(message)


class PasswordNotEncodableError(ValueError):
    """Raised by ``hash_password`` for a password that cannot be UTF-8 encoded.

    Such a ``str`` holds a surrogate code point: a ``"\\ud800"`` JSON escape,
    raw surrogate bytes in a JSON body, or a non-UTF-8 byte read through
    ``os.environ`` / ``getpass`` on a pipe (see ``utils.utf8``).
    """

    def __init__(self, message: str = PASSWORD_NOT_ENCODABLE_MESSAGE) -> None:
        # The message is an argument so pickle / copy can rebuild the error.
        super().__init__(message)


def is_password_too_long(password: str) -> bool:
    """Return whether ``password`` exceeds bcrypt's 72-byte input limit.

    Never raises: a surrogate code point counts as the 3 bytes ``surrogatepass``
    gives it. Check ``is_utf8_encodable`` separately (``hash_password`` does).

    Args:
        password: The plaintext password.

    Returns:
        True when its UTF-8 encoding is longer than ``PASSWORD_MAX_BYTES``.
    """
    return len(password.encode("utf-8", "surrogatepass")) > PASSWORD_MAX_BYTES


def hash_password(password: str) -> str:
    """Hash a password using bcrypt with cost factor 12.

    Args:
        password: The plaintext password.

    Returns:
        The bcrypt hash as a string.

    Raises:
        PasswordNotEncodableError: The password cannot be UTF-8 encoded.
        PasswordTooLongError: The password is longer than 72 bytes when UTF-8
            encoded. It is refused rather than truncated so a new password never
            silently ignores what the user typed past byte 72.
    """
    if not is_utf8_encodable(password):
        raise PasswordNotEncodableError()
    if is_password_too_long(password):
        raise PasswordTooLongError()
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a bcrypt hash.

    Only the first 72 bytes of the UTF-8 encoded password are compared, as
    bcrypt 4 did. Hashes created under bcrypt 4 from a longer password keep
    verifying, and an over-long wrong password is a plain mismatch instead of
    bcrypt 5's ``ValueError``.

    A password that cannot be UTF-8 encoded (#1718) is a mismatch too: no
    stored hash can have been made from one.

    Args:
        password: The plaintext password to check.
        password_hash: The stored bcrypt hash.

    Returns:
        True when the password matches the hash.
    """
    if not is_utf8_encodable(password):
        return False
    return bcrypt.checkpw(password.encode()[:PASSWORD_MAX_BYTES], password_hash.encode())
