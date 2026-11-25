"""Display names of integration services on the workspaces page (#3010).

``INTEGRATION_SERVICE_DISPLAY_NAMES`` is a JSON **object** mapping a service
id (as the billing publisher sends it in ``services``) to the name the
profile's workspaces page shows::

    {"example-sync": "Example Service", "example-sync-eu": "Example Service"}

Each value is 1-60 characters after stripping surrounding whitespace and has
no control characters. Empty (the default) or ``{}`` maps nothing: a service
is then shown by its id. Services that map to the same name are shown as one
line. A malformed value raises ``ValueError`` naming the variable, which the
Settings validator turns into a refused start.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

ENV_NAME = "INTEGRATION_SERVICE_DISPLAY_NAMES"
MAX_DISPLAY_NAME_LENGTH = 60


def parse_integration_service_display_names(raw: str) -> dict[str, str]:
    """``{service id: display name}`` from the setting's JSON; ``{}`` when empty.

    Raises:
        ValueError: Not a JSON object, a value that is not a string, a name
            empty or longer than 60 characters after strip, or a control
            character in a name.
    """
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{ENV_NAME} is not valid JSON: {exc.msg}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{ENV_NAME} must be a JSON object of service id -> display name")
    names: dict[str, str] = {}
    for key, name in value.items():
        if not isinstance(key, str) or not isinstance(name, str):
            raise ValueError(f"{ENV_NAME}: keys and values must be strings")
        stripped = name.strip()
        if not 1 <= len(stripped) <= MAX_DISPLAY_NAME_LENGTH:
            raise ValueError(
                f"{ENV_NAME}: the name for {key!r} must be 1-{MAX_DISPLAY_NAME_LENGTH} "
                "characters after strip"
            )
        if any(unicodedata.category(ch) == "Cc" for ch in stripped):
            raise ValueError(f"{ENV_NAME}: the name for {key!r} contains a control character")
        names[key] = stripped
    return names


@dataclass(frozen=True)
class DisplayedService:
    """One line of the workspaces page: a display name and its end of access."""

    name: str
    valid_until: datetime


def display_services(
    in_force: Iterable[tuple[str, datetime]], names: Mapping[str, str]
) -> tuple[DisplayedService, ...]:
    """Collapse services in force by display name, sorted by name.

    ``in_force`` holds ``(service id, end of access)`` of the services in
    force now. A service id without a mapping is shown by its id; services with
    the same name become one line ending at the latest of their ends.
    """
    latest: dict[str, datetime] = {}
    for service_id, valid_until in in_force:
        name = names.get(service_id, service_id)
        if name not in latest or valid_until > latest[name]:
            latest[name] = valid_until
    return tuple(DisplayedService(name, latest[name]) for name in sorted(latest))
