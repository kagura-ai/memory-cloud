"""Log fields for a search or delete query without its text (#1721).

Directory policy 1.D: a connector must not collect extraneous conversation
data, even for logging. A recall / forget / public-search query is what the
user or model typed, so INFO logs carry only its length and a keyed hash. The
hash is the one ``memory_access_events.query_hash`` stores (HMAC-SHA256 under
``Settings.audit_hmac_key``, see ``emit_memory_access_event``), so a log line
still joins to its access-event row.
"""

from __future__ import annotations

from typing import Any

from utils.hashing import hmac_sha256_hex


def query_log_fields(query: str | None) -> dict[str, Any]:
    """Return ``query_len`` and ``query_hash`` for a log line, never the text.

    Args:
        query: The raw query, or ``None`` / ``""`` when there is none.

    Returns:
        ``{"query_len": int, "query_hash": str | None}``; the hash is ``None``
        for an empty query, as ``memory_access_events.query_hash`` is.
    """
    if not query:
        return {"query_len": 0, "query_hash": None}
    from config.settings import get_settings

    return {
        "query_len": len(query),
        "query_hash": hmac_sha256_hex(query, get_settings().audit_hmac_key),
    }
