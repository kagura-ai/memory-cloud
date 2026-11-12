"""The unsubscribe link in announcement emails (Issue #2761).

A token that can do one thing: turn one account's announcement-email setting
off. It is a Fernet token (AES-CBC + HMAC-SHA256, MAC checked before anything
else) whose plaintext names the account row::

    {"p": "au1", "i": <users.id>, "u": "<users.user_id>"}

- Key: for every entry of the ``API_KEY_SECRET`` ring (the current secret and
  ``API_KEY_SECRET_PREVIOUS``), the purpose key is
  ``HMAC-SHA256(key=<that entry's Fernet key>, msg=PURPOSE)``. So no other
  Fernet value of this deployment (stored provider keys, TOTP secrets) opens
  as a token, and a token opens nowhere else. A link keeps working through a
  rotation while the old secret stays in ``API_KEY_SECRET_PREVIOUS``.
- No expiry: an opt-out link in an old email must keep working. It can only
  turn the setting off, and the secret rotation bounds its life.
- Bound to the account row by ``users.id`` (an identity that is never reused)
  AND ``user_id``: a link minted for an erased account cannot touch a later
  account that reuses the same OAuth subject.
- URL-safe base64 without ``=`` padding, so it fits a path segment and a URL
  fragment unchanged.

Reading never raises and never logs: a public endpoint feeds it arbitrary
input. ``utils.encryption.APIKeyEncryption.decrypt`` logs every failure at
ERROR, which is why this module builds its own ``MultiFernet``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from config.settings import get_settings
from utils.encryption import derive_fernet_key

PURPOSE: Final = b"kagura/announcement-unsubscribe/v1"
_PAYLOAD_TAG: Final = "au1"
# A Fernet token of a 255-character user_id is ~500 characters; anything far
# longer is not one of ours.
MAX_TOKEN_LENGTH: Final = 2048
TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,2048}")

UNSUBSCRIBE_API_PATH: Final = "/api/v1/announcement-emails/unsubscribe"
UNSUBSCRIBE_PAGE_PATH: Final = "/email/unsubscribe"


@dataclass(frozen=True)
class UnsubscribeClaim:
    """The account a valid token names (``users.id`` and ``users.user_id``)."""

    account_id: int
    user_id: str


def _purpose_keys(secrets: tuple[str, ...]) -> list[bytes]:
    """The purpose-bound Fernet key for each ring entry, current first."""
    keys: list[bytes] = []
    seen: set[str] = set()
    for secret in secrets:
        secret = secret.strip()
        if not secret:
            continue
        derived = derive_fernet_key(secret)
        if derived in seen:
            continue
        seen.add(derived)
        base_key = base64.urlsafe_b64decode(derived.encode())
        purpose_key = hmac.new(base_key, PURPOSE, hashlib.sha256).digest()
        keys.append(base64.urlsafe_b64encode(purpose_key))
    return keys


@lru_cache(maxsize=4)
def _ring(secrets: tuple[str, ...]) -> MultiFernet:
    keys = _purpose_keys(secrets)
    if not keys:
        msg = "API_KEY_SECRET is empty. Required for announcement unsubscribe links."
        raise ValueError(msg)
    return MultiFernet([Fernet(key) for key in keys])


def _current_ring() -> MultiFernet:
    settings = get_settings()
    secrets = (settings.api_key_secret, *settings.api_key_secret_previous_keys)
    return _ring(tuple(secrets))


def issue_unsubscribe_token(*, account_id: int, user_id: str) -> str:
    """A token that turns announcement emails off for this account row."""
    payload = json.dumps(
        {"p": _PAYLOAD_TAG, "i": account_id, "u": user_id}, separators=(",", ":")
    ).encode()
    return _current_ring().encrypt(payload).decode().rstrip("=")


def read_unsubscribe_token(token: object) -> UnsubscribeClaim | None:
    """The account a token names, or ``None`` for anything that is not ours.

    CPU only: no database, no Redis. Never raises, never logs the token.
    """
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return None
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = _current_ring().decrypt(padded.encode("ascii"), ttl=None)
    except (InvalidToken, ValueError, TypeError, binascii.Error):
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("p") != _PAYLOAD_TAG:
        return None
    account_id = payload.get("i")
    user_id = payload.get("u")
    if type(account_id) is not int or account_id <= 0:
        return None
    if not isinstance(user_id, str) or not user_id or len(user_id) > 255:
        return None
    return UnsubscribeClaim(account_id=account_id, user_id=user_id)


def _frontend_base() -> str:
    return get_settings().frontend_url.rstrip("/")


def build_unsubscribe_headers(*, account_id: int, user_id: str) -> dict[str, str]:
    """``List-Unsubscribe`` and ``List-Unsubscribe-Post`` (RFC 2369 / RFC 8058).

    The one-click target is ``FRONTEND_URL`` + ``/api/v1/...``: every user link
    is built from ``FRONTEND_URL`` and the shipped proxy routes ``/api/*`` on
    that origin to the API.
    """
    token = issue_unsubscribe_token(account_id=account_id, user_id=user_id)
    return {
        "List-Unsubscribe": f"<{_frontend_base()}{UNSUBSCRIBE_API_PATH}/{token}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def unsubscribe_page_url(*, account_id: int, user_id: str) -> str:
    """The visible link for the email body: the token rides in the fragment,
    which never reaches a server log."""
    token = issue_unsubscribe_token(account_id=account_id, user_id=user_id)
    return f"{_frontend_base()}{UNSUBSCRIBE_PAGE_PATH}#{token}"
