"""Shared hashing primitives.

Centralized so that API-key hashing, audit-log pseudonymization, and any
future "hash this string" need go through one place. Lets us swap the
underlying primitive (SHA256 → SHA3, BLAKE2, …) in a single edit.
"""

from __future__ import annotations

import hashlib


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
