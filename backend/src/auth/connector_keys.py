"""The ``connector:{id}`` API-key name convention (#1944, #1952).

A connector's KMC write key is a workspace-scoped ``APIKey`` linked to the
connector by ``api_keys.connector_id`` (#1952, ``ON DELETE CASCADE``) — the
link ``verify_key``, ``delete_connector`` and ``rotate_kmc_key`` read. The
name ``connector:{id}`` is display only (the UI and the worker config reply
show it) plus a reserved prefix: the user-facing create endpoints refuse it,
so no new user key can look like a connector key.

Leaf module — depends on ``uuid`` only — so the formatter and the reserved
prefix are one source for every importer (routes, schemas, provisioning).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

CONNECTOR_KEY_PREFIX: Final = "connector:"
CONNECTOR_KEY_PREFIX_RESERVED_MESSAGE: Final = (
    f"names starting with '{CONNECTOR_KEY_PREFIX}' are reserved for connector keys"
)

# #1952: a connector key's secret lives Fernet-encrypted on its connector, so a
# plain regenerate would revoke the row the connector still holds and mint a
# replacement the connector never receives.
CONNECTOR_KEY_REGENERATE_MESSAGE: Final = (
    "A connector's write key cannot be regenerated here; "
    "rotate it through its connector instead "
    "(POST /api/v1/workspace-connectors/{connector_id}/rotate-kmc-key)"
)


def connector_key_name(connector_id: UUID) -> str:
    """The name of the write key minted for ``connector_id``."""
    return f"{CONNECTOR_KEY_PREFIX}{connector_id}"


def reject_reserved_key_name(name: str) -> str:
    """Pydantic ``field_validator`` body for user-supplied key names.

    Raises:
        ValueError: when ``name`` carries the connector prefix (→ 422).
    """
    if name.startswith(CONNECTOR_KEY_PREFIX):
        raise ValueError(CONNECTOR_KEY_PREFIX_RESERVED_MESSAGE)
    return name
