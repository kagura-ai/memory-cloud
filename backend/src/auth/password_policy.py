"""Password policy shared by the admin CLIs and the self-service endpoints.

Issue #1678: the rules used to live inline in ``cli/create_admin.py`` and
``cli/reset_password.py``. Regular users can now set, change and reset a
password through the API, so every entry point validates against this one
module.

Rules:
    - at least ``PASSWORD_MIN_LENGTH`` (12) characters
    - an upper-case letter, a lower-case letter, a digit and a special
      (non-alphanumeric) character
    - valid Unicode text (UTF-8 encodable, #1718)
    - at most bcrypt's 72 bytes when UTF-8 encoded (#1707)

Messages never echo the password.
"""

from __future__ import annotations

from auth.password import (
    PASSWORD_MAX_BYTES,
    PASSWORD_NOT_ENCODABLE_MESSAGE,
    PASSWORD_TOO_LONG_MESSAGE,
    is_password_too_long,
)
from utils.utf8 import is_utf8_encodable

PASSWORD_MIN_LENGTH = 12

# The requirements banner the CLIs print before prompting.
PASSWORD_REQUIREMENT_LINES: tuple[str, ...] = (
    f"Minimum {PASSWORD_MIN_LENGTH} characters",
    "At least 1 uppercase letter (A-Z)",
    "At least 1 lowercase letter (a-z)",
    "At least 1 digit (0-9)",
    "At least 1 special character (!@#$%^&*...)",
    f"Maximum {PASSWORD_MAX_BYTES} bytes when UTF-8 encoded",
)


class PasswordPolicyError(ValueError):
    """Raised by ``validate_password_policy`` for a password that breaks a rule.

    ``str(error)`` is a user-facing message that names the broken rules and
    never contains the password.
    """

    def __init__(self, message: str) -> None:
        # The message is an argument so pickle / copy can rebuild the error.
        super().__init__(message)


def missing_password_requirements(password: str) -> list[str]:
    """Return the composition rules ``password`` does not meet.

    Covers length and character classes only; encodability and the 72-byte
    limit are separate checks (see ``validate_password_policy``).

    Args:
        password: The candidate plaintext password.

    Returns:
        Short rule descriptions (``"1 digit"``, ...), empty when all are met.
    """
    missing: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        missing.append(f"at least {PASSWORD_MIN_LENGTH} characters")
    if not any(c.isupper() for c in password):
        missing.append("1 uppercase letter")
    if not any(c.islower() for c in password):
        missing.append("1 lowercase letter")
    if not any(c.isdigit() for c in password):
        missing.append("1 digit")
    if not any(not c.isalnum() for c in password):
        missing.append("1 special character")
    return missing


def validate_password_policy(password: str) -> None:
    """Validate a new password against every rule.

    Args:
        password: The candidate plaintext password.

    Raises:
        PasswordPolicyError: The password breaks a rule. The message is safe
            to return to the client.
    """
    missing = missing_password_requirements(password)
    if missing:
        raise PasswordPolicyError(f"Password must contain: {', '.join(missing)}.")
    if not is_utf8_encodable(password):
        raise PasswordPolicyError(PASSWORD_NOT_ENCODABLE_MESSAGE)
    if is_password_too_long(password):
        raise PasswordPolicyError(PASSWORD_TOO_LONG_MESSAGE)
