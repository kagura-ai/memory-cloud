"""``bootstrap`` — the interactive session's one-call start (#1851).

Context block + guardrails + pinned + upcoming + changes since a time, each
bounded and filtered like its standalone tool, fail-soft per component. The
agent-side sibling is ``get_agent_bootstrap``; this one takes the user's own
credential and a ``context_id``.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._errors import _tool_exception_response, is_caller_value_error
from mcp_server.tools._helpers import (
    _context_response_fields,
    _ContextNotFoundError,
    _dumps,
    _error_response,
    _log_tool_usage,
    _resolve_context_for_read,
    _resolve_context_id,
    execute_with_timeout,
)
from services.memory_listing import to_naive_utc
from utils.datetime import parse_iso8601_to_aware, utcnow
from utils.response_budget import BudgetArgumentError, parse_max_chars

DEFAULT_SINCE_DAYS = 7
MAX_SINCE_DAYS = 999
_RELATIVE = re.compile(r"^(\d{1,3})d$", re.IGNORECASE)
# The relative form with more digits than the range holds: reported as out of
# range instead of being handed to the ISO 8601 parser.
_RELATIVE_TOO_LONG = re.compile(r"^\d{4,}d$", re.IGNORECASE)
# Every ``since`` error ends with this; the schema description states the same
# range (#1883).
SINCE_FORMS = f"ISO 8601 or '<N>d' (days back, '0d' to '{MAX_SINCE_DAYS}d', e.g. '7d')"


def parse_since(raw: Any) -> datetime:
    """``since``: ISO 8601 (naive = UTC), ``"<N>d"`` (0-999 days back), or absent → 7 days back.

    Returns naive UTC, the convention of the ``memories`` timestamps, through
    the same conversion ``changes_since`` applies to its ``since``.
    """
    now = utcnow()
    if raw is None or raw == "":
        return now - timedelta(days=DEFAULT_SINCE_DAYS)
    if not isinstance(raw, str):
        raise ValueError(f"since must be a string: {SINCE_FORMS}")
    text = raw.strip()
    m = _RELATIVE.match(text)
    if m:
        return now - timedelta(days=int(m.group(1)))
    if _RELATIVE_TOO_LONG.match(text):
        raise ValueError(
            f"since '{text}' is not accepted — pass {SINCE_FORMS}; "
            "an ISO 8601 timestamp reaches further back"
        )
    try:
        return to_naive_utc(parse_iso8601_to_aware(raw, "since"))
    except ValueError as e:
        raise ValueError(f"{e} — pass {SINCE_FORMS}") from e


def parse_include(raw: Any) -> tuple[str, ...]:
    """``include``: absent → every component; else a non-empty subset, deduplicated in order."""
    from services.session_bootstrap_service import COMPONENTS

    if raw is None:
        return COMPONENTS
    if not isinstance(raw, list) or not raw or not all(r in COMPONENTS for r in raw):
        raise ValueError(f"include must be a non-empty subset of {', '.join(COMPONENTS)}")
    return tuple(dict.fromkeys(raw))


async def handle_bootstrap(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Rehydrate an interactive session on one context in one call (#1851)."""
    from db.base import get_db
    from mcp_server.tools.context import _guardrails_field
    from services.session_bootstrap_service import SessionBootstrapService

    if "context_id" not in args:
        return _error_response("missing_fields", "Missing required field: context_id")
    try:
        since = parse_since(args.get("since"))
        include = parse_include(args.get("include"))
        max_chars = parse_max_chars(args.get("max_chars"))
    except BudgetArgumentError as e:
        return _error_response("validation_error", e.message, received=e.received)
    except ValueError as e:
        return _error_response("validation_error", str(e))
    start = time.time()
    async for db in get_db():
        context_id: UUID | None = None
        try:
            context_id = _resolve_context_id(args["context_id"])
            context = await _resolve_context_for_read(
                db, user_id, context_id, operation="bootstrap"
            )
            # Read before any lane runs: the guardrails read and the lanes roll
            # the session back when they fail, which expires this instance.
            context_fields = _context_response_fields(context)
            envelope = await execute_with_timeout(
                SessionBootstrapService(db).build(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    context=context,
                    since=since,
                    include=include,
                    max_chars=max_chars,
                    guardrails_provider=partial(_guardrails_field, db, user_id, context),
                ),
                operation_name="bootstrap",
            )
            await _log_tool_usage(db, user_id, "bootstrap", start, 200, context_id, workspace_id)
            envelope.update(context_fields)
            return [TextContent(type="text", text=_dumps(envelope))]
        except _ContextNotFoundError as e:
            await _log_tool_usage(db, user_id, "bootstrap", start, 404, context_id, workspace_id)
            return e.to_response()
        except ValueError as e:
            if not is_caller_value_error(e):
                await _log_tool_usage(
                    db, user_id, "bootstrap", start, 500, context_id, workspace_id
                )
                return _tool_exception_response("bootstrap", e)
            await _log_tool_usage(db, user_id, "bootstrap", start, 422, context_id, workspace_id)
            return _error_response("validation_error", str(e))
    return _error_response("internal_error", "Database session unavailable")
