"""MCP tool handlers for read-only API-key binding introspection (Issue #629).

Two read-only tools deferred from #626 (PR #628):

- ``list_my_bindings`` — list the calling user's public-bound API keys.
- ``describe_binding`` — describe one binding by ``key_id`` XOR ``context_id``.

Both are **read-only**: mint/revoke stay on SDK / CLI / HTTP / dashboard only
(the #626 design decision, kagura memory ``2859a627``). No write path lives here.

Principal restriction (#629 AC): ``api_key_bound`` principals must not be able to
self-introspect. This is enforced **structurally** at the MCP auth boundary, not
in these handlers: ``mcp_server.auth._verify_api_key`` rejects any key with
``bound_context_id != None`` (``auth.dependencies.verify_api_key`` returns
``None``), so a bound key fails authentication and **never reaches a handler**.
The handler signature ``(args, user_id, workspace_id)`` carries no principal-type
discriminator, so an in-handler 403 is both impossible and unnecessary. That auth
gate is the source of truth for the restriction; a regression test
(``test_api_keys.py``) pins it so a future loosening can't silently open
introspection to bound principals.

Owner isolation: every query ANDs ``APIKey.user_id == user_id``. A caller can only
ever see their own bindings. ``describe_binding`` returns a uniform
``binding_not_found`` for both "does not exist" and "exists but not yours" so the
sequential integer ``key_id`` cannot be used as an existence oracle.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import _error_response, _log_tool_usage, _success_response
from utils.datetime import to_utc_iso

logger = logging.getLogger(__name__)


def _binding_dict(api_key: Any, display_name: str | None, name: str | None) -> dict[str, Any]:
    """Shape one binding row for the response (never includes any secret)."""
    return {
        "key_id": api_key.id,
        "name": api_key.name,
        "context_id": str(api_key.bound_context_id),
        "context_name": display_name or name,
        "created_at": to_utc_iso(api_key.created_at),
    }


async def handle_list_my_bindings(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List the calling user's active public-bound API keys (read-only).

    Returns ``bindings: [{key_id, name, context_id, context_name, created_at}]``
    for every non-revoked key the caller owns that is bound to a public context.
    ``key_prefix`` is intentionally omitted here (it is a ``describe_binding``
    detail). Secrets are never returned.
    """
    from sqlalchemy import select

    from db.base import get_db
    from models.auth import APIKey, Context

    start_time = time.time()
    async for db in get_db():
        try:
            stmt = (
                select(APIKey, Context.display_name, Context.name)
                .outerjoin(Context, Context.id == APIKey.bound_context_id)
                .where(
                    APIKey.user_id == user_id,
                    APIKey.bound_context_id.is_not(None),
                    APIKey.revoked_at.is_(None),
                )
                .order_by(APIKey.created_at.desc())
            )
            rows = (await db.execute(stmt)).all()
            bindings = [_binding_dict(key, display_name, name) for key, display_name, name in rows]

            await _log_tool_usage(
                db, user_id, "list_my_bindings", start_time, 200, None, workspace_id
            )
            return _success_response(bindings=bindings, count=len(bindings))
        except Exception as e:
            await db.rollback()
            logger.error(f"list_my_bindings_failed: {e}", exc_info=True)
            await _log_tool_usage(
                db, user_id, "list_my_bindings", start_time, 500, None, workspace_id
            )
            # Generic caller-facing message — the full exception (which may carry
            # DB schema / internal detail) stays in the server-side log only.
            return _error_response("list_my_bindings_error", "An internal error occurred.")

    return _error_response("internal_error", "Failed to acquire database session")


async def handle_describe_binding(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Describe one of the caller's bindings by ``key_id`` XOR ``context_id``.

    Exactly one selector must be supplied. The result is scoped to
    ``user_id == caller`` and to bound, non-revoked keys; a miss returns a uniform
    ``binding_not_found`` (no existence oracle on the sequential ``key_id``).
    Adds ``key_prefix`` to the ``list_my_bindings`` shape.
    """
    from sqlalchemy import select

    from db.base import get_db
    from models.auth import APIKey, Context

    raw_key_id = args.get("key_id")
    raw_context_id = args.get("context_id")

    # Exactly-one (XOR) selector validation.
    if (raw_key_id is None) == (raw_context_id is None):
        return _error_response(
            "invalid_arguments",
            "Provide exactly one of 'key_id' (integer) or 'context_id' (UUID).",
        )

    key_id: int | None = None
    context_id: UUID | None = None
    if raw_key_id is not None:
        if isinstance(raw_key_id, bool) or not isinstance(raw_key_id, int):
            return _error_response("invalid_arguments", "'key_id' must be an integer.")
        key_id = raw_key_id
    else:
        try:
            context_id = UUID(str(raw_context_id))
        except (ValueError, TypeError):
            return _error_response("invalid_arguments", "'context_id' must be a valid UUID.")

    start_time = time.time()
    async for db in get_db():
        try:
            conditions = [
                APIKey.user_id == user_id,
                APIKey.bound_context_id.is_not(None),
                APIKey.revoked_at.is_(None),
            ]
            if key_id is not None:
                conditions.append(APIKey.id == key_id)
            else:
                conditions.append(APIKey.bound_context_id == context_id)

            stmt = (
                select(APIKey, Context.display_name, Context.name)
                .outerjoin(Context, Context.id == APIKey.bound_context_id)
                .where(*conditions)
                .order_by(APIKey.created_at.desc())
            )
            rows = (await db.execute(stmt)).all()

            if not rows:
                # Uniform miss — do not distinguish "absent" from "not yours".
                await _log_tool_usage(
                    db, user_id, "describe_binding", start_time, 404, None, workspace_id
                )
                return _error_response("binding_not_found", "Binding not found.")

            api_key, display_name, name = rows[0]
            binding = _binding_dict(api_key, display_name, name)
            binding["key_prefix"] = api_key.key_prefix

            # bound_context_id is not UNIQUE on api_keys: a context can be bound by
            # several of the caller's keys. Surface that rather than silently
            # hiding the others, while still returning the most recent one.
            extra: dict[str, Any] = {}
            if context_id is not None and len(rows) > 1:
                extra["note"] = (
                    f"{len(rows)} of your keys are bound to this context; "
                    "showing the most recently created. Use key_id for a specific one."
                )

            await _log_tool_usage(
                db, user_id, "describe_binding", start_time, 200, None, workspace_id
            )
            return _success_response(binding=binding, **extra)
        except Exception as e:
            await db.rollback()
            logger.error(f"describe_binding_failed: {e}", exc_info=True)
            await _log_tool_usage(
                db, user_id, "describe_binding", start_time, 500, None, workspace_id
            )
            # Generic caller-facing message — full exception stays in the log only.
            return _error_response("describe_binding_error", "An internal error occurred.")

    return _error_response("internal_error", "Failed to acquire database session")
