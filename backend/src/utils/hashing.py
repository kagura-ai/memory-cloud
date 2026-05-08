"""Shared hashing primitives.

Centralized so that API-key hashing, audit-log pseudonymization, and any
future "hash this string" need go through one place. Lets us swap the
underlying primitive (SHA256 → SHA3, BLAKE2, …) in a single edit.
"""

from __future__ import annotations

import hashlib
import hmac

# Single source of truth for the on-wire sha256 hex shape — used by Pydantic
# Field(pattern=...) on the REST request models and by the service-layer
# regex check in FileStorageService.reserve_upload (which precompiles this
# pattern and rejects any input that doesn't fullmatch).
SHA256_HEX_PATTERN = r"^[0-9a-fA-F]{64}$"


def sha256_hex(value: str, *, salt: str = "") -> str:
    """Return the SHA256 hex digest of ``value``, optionally salted.

    Args:
        value: Input string. Must be UTF-8 encodable.
        salt: Optional salt prepended before hashing. Use a stable
            per-deployment string when the goal is irreversible
            pseudonymization with cross-row correlation; leave empty
            for plain content hashing (e.g. API keys).

    Returns:
        64-character lowercase hex digest.
    """
    return hashlib.sha256(f"{salt}{value}".encode()).hexdigest()


def hmac_sha256_hex(value: str, key: str) -> str:
    """Return the HMAC-SHA256 hex digest of ``value`` keyed by ``key``.

    Use this for audit-log columns where a plain salted SHA256 is too weak —
    e.g. ``audit_logs.old_value_hash`` / ``new_value_hash`` storing email
    address hashes (Issue #481). The keyed primitive resists rainbow-table
    attacks against small-domain inputs (email local-part ≤ 64 chars) that
    a public salt cannot, because the attacker would need to obtain ``key``.

    Args:
        value: Input string to hash. Must be UTF-8 encodable.
        key: HMAC key (server secret). Must be UTF-8 encodable. Pass the
            ``Settings.audit_hmac_key`` value, never a hard-coded constant.

    Returns:
        64-character lowercase hex digest. Same shape as ``sha256_hex`` —
        fits the existing ``String(64)`` audit-log columns without DDL.
    """
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()
