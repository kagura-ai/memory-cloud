"""Validation of a user's UI language and time zone.

Shared by ``PUT /users/profile`` and the OAuth sign-up carrier that seeds a new
account with the sign-in page's language and time zone (#2700), and the
language negotiation of the OIDC consent page (#2949).
"""

from __future__ import annotations

import math
import re
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
    return frozenset(zoneinfo.available_timezones()) - {"localtime", "Factory"}


def is_valid_locale(value: object) -> TypeGuard[str]:
    """Whether ``value`` is one of :data:`SUPPORTED_LOCALES`."""
    return isinstance(value, str) and value in SUPPORTED_LOCALES


# A language tag as OIDC ``ui_locales`` and ``Accept-Language`` carry it
# (BCP 47 shape: a 1-8 letter primary subtag, then 1-8 alphanumeric subtags).
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*")
# Longer values are ignored whole: no client needs more than a few tags.
UI_LOCALES_MAX_LENGTH = 255
ACCEPT_LANGUAGE_MAX_LENGTH = 1024


def _supported_primary(tag: str) -> str | None:
    """The supported locale a well-formed tag names (``ja-JP`` -> ``ja``)."""
    if not _LANGUAGE_TAG.fullmatch(tag):
        return None
    primary = tag.split("-", 1)[0].lower()
    # The constant, not the input's own string: only a supported code leaves.
    return next((code for code in SUPPORTED_LOCALES if code == primary), None)


def _from_ui_locales(value: str | None) -> str | None:
    """The first supported tag of a space-separated preference list."""
    if not value or len(value) > UI_LOCALES_MAX_LENGTH:
        return None
    for tag in value.split():
        found = _supported_primary(tag)
        if found is not None:
            return found
    return None


def _from_accept_language(value: str | None) -> str | None:
    """The supported locale with the highest q-value (first on a tie).

    Malformed entries and ``q=0`` (not acceptable) are skipped; ``*`` names no
    language, so it never selects one.
    """
    if not value or len(value) > ACCEPT_LANGUAGE_MAX_LENGTH:
        return None
    ranked: list[tuple[float, int, str]] = []
    for position, entry in enumerate(value.split(",")):
        tag, *params = (part.strip() for part in entry.split(";"))
        quality = 1.0
        for param in params:
            name, _, raw = param.partition("=")
            if name.strip().lower() != "q":
                continue
            try:
                quality = float(raw.strip())
            except ValueError:
                quality = -1.0
            if not math.isfinite(quality) or not 0.0 <= quality <= 1.0:
                quality = -1.0
        if quality <= 0.0:
            continue
        found = _supported_primary(tag)
        if found is not None:
            ranked.append((-quality, position, found))
    return min(ranked)[2] if ranked else None


def negotiate_locale(
    *,
    ui_locales: str | None = None,
    account_locale: str | None = None,
    accept_language: str | None = None,
) -> str:
    """The page language: ``ui_locales``, the account's locale, ``Accept-Language``, ``en``.

    Always one of :data:`SUPPORTED_LOCALES`; request input never comes out.
    Unsupported or malformed values are skipped and the next source answers.
    """
    from_request = _from_ui_locales(ui_locales)
    if from_request is not None:
        return from_request
    if is_valid_locale(account_locale):
        return account_locale
    from_header = _from_accept_language(accept_language)
    if from_header is not None:
        return from_header
    return "en"


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
