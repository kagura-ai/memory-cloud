"""The ``connector:{id}`` API-key name contract (#1944).

A connector's KMC write key is a workspace-scoped ``APIKey`` named after the
connector it serves. ``connector_provisioning`` mints and rotates it under this
name and deletes it by this name; ``APIKeyManager.verify_key`` parses the name
back to the connector so the key stops with the connector's seats (#1939), and
the user-facing create endpoints refuse the prefix.

Leaf module — depends on ``uuid`` only — so the parser and the formatter are
one source for every importer (auth, routes, schemas, provisioning).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

CONNECTOR_KEY_PREFIX: Final = "connector:"
CONNECTOR_KEY_PREFIX_RESERVED_MESSAGE: Final = (
    f"names starting with '{CONNECTOR_KEY_PREFIX}' are reserved for connector keys"
)


def connector_key_name(connector_id: UUID) -> str:
    """The name of the write key minted for ``connector_id``."""
    return f"{CONNECTOR_KEY_PREFIX}{connector_id}"


def connector_id_from_key_name(name: str) -> UUID | None:
    """The connector id a ``connector:{uuid}`` key name carries, else ``None``.

    Args:
        name: ``APIKey.name``.

    Returns:
        The UUID after the prefix; ``None`` for any other name, including a
        prefix followed by something that is not the canonical UUID spelling.
    """
    if not name.startswith(CONNECTOR_KEY_PREFIX):
        return None
    suffix = name[len(CONNECTOR_KEY_PREFIX) :]
    try:
        connector_id = UUID(suffix)
    except ValueError:
        return None
    # Only the canonical spelling the formatter produces — ``UUID()`` would
    # also accept braces, ``urn:uuid:`` and the dashless form.
    return connector_id if str(connector_id) == suffix else None


def reject_reserved_key_name(name: str) -> str:
    """Pydantic ``field_validator`` body for user-supplied key names.

    Raises:
        ValueError: when ``name`` carries the connector prefix (→ 422).
    """
    if name.startswith(CONNECTOR_KEY_PREFIX):
        raise ValueError(CONNECTOR_KEY_PREFIX_RESERVED_MESSAGE)
    return name
