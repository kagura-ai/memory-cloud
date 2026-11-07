"""Validation of a user's UI language and time zone.

Shared by ``PUT /users/profile`` and the OAuth sign-up carrier that seeds a new
account with the sign-in page's language and time zone (#2700).
"""

from __future__ import annotations

import zoneinfo
from functools import cache
from typing import TypeGuard

# The UI languages (frontend ``src/i18n/config.ts`` ``locales``).
SUPPORTED_LOCALES: tuple[str, ...] = ("en", "ja")
LOCALE_PATTERN = f"^({'|'.join(SUPPORTED_LOCALES)})$"

# ``users.timezone`` is ``String(50)``.
TIMEZONE_MAX_LENGTH = 50


@cache
def _available_timezones() -> frozenset[str]:
    """The IANA names this interpreter's tz database knows, read once."""
    return frozenset(zoneinfo.available_timezones())


def is_valid_locale(value: object) -> TypeGuard[str]:
    """Whether ``value`` is one of :data:`SUPPORTED_LOCALES`."""
    return isinstance(value, str) and value in SUPPORTED_LOCALES


def is_valid_timezone(value: object) -> TypeGuard[str]:
    """Whether ``value`` is an IANA time zone name such as ``Asia/Tokyo``.

    A set lookup, never ``ZoneInfo(value)``: ZoneInfo opens a file named by the
    input and raises ``ValueError`` (not ``ZoneInfoNotFoundError``) for ``""``,
    absolute paths and ``..`` segments.
    """
    return (
        isinstance(value, str)
        and len(value) <= TIMEZONE_MAX_LENGTH
        and value in _available_timezones()
    )
