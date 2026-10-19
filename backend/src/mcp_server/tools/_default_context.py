"""Connection default context (#2125): ``?context=<uuid>`` on the MCP URL.

A client that already knows its context (a plugin option, a desktop entry
configured for one project) used to spend the first turn of every session on
``list_contexts``. The URL can now name the context once; a context-scoped
tool called without ``context_id`` then runs as if the caller had passed it.

The default only fills an argument the caller could have sent. The
dispatcher (``execute_tool_call``) writes it into ``args["context_id"]``
before the capacity gate, the pre-dispatch check and the handler, so the
same resolvers (``_resolve_context`` / ``_resolve_context_for_read``) and the
same permission checks run as for an explicit id. Nothing here reads a
context or talks to a service on the caller's behalf.

Same parsing rule as ``?guardrails=`` (``services.guardrail_digest``):
``parse_qsl(keep_blank_values=True)``, the first value wins, never raises,
never logs the raw value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl
from uuid import UUID

DefaultContextMode = Literal["none", "url", "invalid"]


@dataclass(frozen=True)
class DefaultContextSelection:
    """What the endpoint URL says about the connection's default context.

    ``none`` — no ``context`` parameter. ``url`` — ``?context=<uuid>``.
    ``invalid`` — present but not a UUID (blank included): the connection
    stays usable, and a context-scoped call that omits ``context_id`` fails
    with ``invalid_context_id_format`` naming the URL. ``raw_length`` is the
    only thing kept of an invalid value (never the bytes).
    """

    mode: DefaultContextMode
    context_id: UUID | None = None
    raw_length: int = 0


NO_DEFAULT_CONTEXT = DefaultContextSelection("none")


def select_default_context(query_string: bytes | str | None) -> DefaultContextSelection:
    """Parse ``?context=`` from the endpoint URL (pure; first value wins)."""
    if not query_string:
        return NO_DEFAULT_CONTEXT
    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8", "replace")
    value: str | None = None
    for key, raw in parse_qsl(query_string, keep_blank_values=True):
        if key == "context":
            value = raw
            break
    if value is None:
        return NO_DEFAULT_CONTEXT
    try:
        return DefaultContextSelection("url", UUID(value.strip()))
    except ValueError:
        return DefaultContextSelection("invalid", raw_length=len(value))


ContextSource = Literal["url", "binding"]


@dataclass(frozen=True)
class DefaultContext:
    """The context a call without ``context_id`` runs in, and where it came from."""

    context_id: UUID
    source: ContextSource


async def resolve_default_context(
    selection: DefaultContextSelection | None,
) -> DefaultContext | None:
    """The connection default for this request, or ``None``.

    ``url`` → its UUID (no I/O). Anything else → no default, and the call
    fails ``context_id_required`` as before. The caller handles ``invalid``
    before asking.
    """
    if selection is not None and selection.mode == "url" and selection.context_id is not None:
        return DefaultContext(selection.context_id, "url")
    return None
