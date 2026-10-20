"""Connection default context (#2125): ``?context=<uuid>`` on the MCP URL.

A client that already knows its context (a plugin option, a desktop entry
configured for one project) used to spend the first turn of every session on
``list_contexts``. The URL can now name the context once; a context-scoped
tool called without ``context_id`` then runs as if the caller had passed it.
An agent-bound key whose URL names no context falls back to the agent's
default (or sole) binding.

#2129: a user API key and an OAuth grant can carry a default of their own
(``api_keys.default_context_id``, ``oauth_grant_settings``), set in
Credentials & Integrations or on the consent page. The auth step stores it per
request (``_helpers.set_mcp_credential_default_context``); it sits below the
URL and above the agent binding (an agent-bound key never has one).

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

import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

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


ContextSource = Literal["url", "credential", "binding"]


@dataclass(frozen=True)
class DefaultContext:
    """The context a call without ``context_id`` runs in, and where it came from."""

    context_id: UUID
    source: ContextSource


async def resolve_default_context(
    selection: DefaultContextSelection | None,
) -> DefaultContext | None:
    """The connection default for this request, or ``None``.

    ``url`` → its UUID (no I/O). No ``?context=`` and a credential default
    (a user API key's or the OAuth grant's, #2129) → that UUID (no I/O; the
    auth step read it). Neither, and an agent-bound key → the agent's default
    binding, or its sole binding (one query). Anything else → no default, and
    the call fails ``context_id_required`` as before. The caller handles
    ``invalid`` before asking; an invalid URL value never falls through to
    the credential default.

    An agent with no binding and one with several but no default are
    indistinguishable here (no existence oracle, RFC-0002 F2), and a database
    error is "no default" (fail closed — never a guessed context).
    """
    if selection is not None and selection.mode == "url" and selection.context_id is not None:
        return DefaultContext(selection.context_id, "url")
    if selection is not None and selection.mode != "none":
        return None
    from mcp_server.tools._helpers import get_mcp_credential_default_context

    credential_default = get_mcp_credential_default_context()
    if credential_default is not None:
        return DefaultContext(credential_default, "credential")
    from auth.agent_scope import get_agent_scope

    scope = get_agent_scope()
    if scope is None:
        return None
    try:
        context_id = await _agent_default_binding(scope.agent_id)
    except Exception as e:  # noqa: BLE001 - fail closed to "no default"
        logger.warning("mcp_default_context_binding_failed: reason=%s", type(e).__name__)
        return None
    return DefaultContext(context_id, "binding") if context_id is not None else None


async def _agent_default_binding(agent_id: UUID) -> UUID | None:
    """The context of the agent's default (or sole) binding, else ``None``."""
    from db.base import get_db

    async with contextlib.aclosing(get_db()) as sessions:
        async for db in sessions:
            return await default_binding_context_id(db, agent_id)
    return None  # pragma: no cover - get_db always yields


async def default_binding_context_id(db: AsyncSession, agent_id: UUID) -> UUID | None:
    """The one rule for "an agent's default context" (#1276, F2).

    The ``is_default`` binding, else the agent's only binding; no binding and
    several without a default both answer ``None`` and are indistinguishable
    to the caller. Shared by the tool default and the guardrail digest.
    """
    from services.agent_binding_service import AgentBindingService

    binding, outcome = await AgentBindingService(db).resolve_default_binding(agent_id)
    if binding is None or outcome not in ("default", "sole"):
        logger.debug("mcp_default_binding_none: outcome=%s", outcome)
        return None
    return binding.context_id
