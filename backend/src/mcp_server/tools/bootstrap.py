"""``bootstrap`` — the interactive session's one-call start (#1851).

Context block + guardrails + pinned + upcoming + changes since a time, each
bounded and filtered like its standalone tool, fail-soft per component. The
agent-side sibling is ``get_agent_bootstrap``; this one takes the user's own
credential and a ``context_id``.

``format="index"`` (#2126) returns pointer rows instead of the full shape; it
is the default under ``max_chars`` 10,000, so every call that was valid before
(``max_chars`` ≥ 10,000) keeps the full shape.
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
from utils.datetime import parse_iso8601_to_aware, to_naive_utc, utcnow
from utils.response_budget import (
    MIN_MAX_CHARS,
    BudgetArgumentError,
    json_chars,
    parse_max_chars,
)

DEFAULT_SINCE_DAYS = 7
MAX_SINCE_DAYS = 999
_RELATIVE = re.compile(r"^(\d{1,3})d$", re.IGNORECASE)
# The relative form with more digits than the range holds: reported as out of
# range instead of being handed to the ISO 8601 parser.
_RELATIVE_TOO_LONG = re.compile(r"^\d{4,}d$", re.IGNORECASE)
# Every ``since`` error ends with this; the schema description states the same
# range (#1883).
SINCE_FORMS = f"ISO 8601 or '<N>d' (days back, '0d' to '{MAX_SINCE_DAYS}d', e.g. '7d')"
# #2126: bootstrap's own max_chars floor — the index format fits a small
# budget; every other tool keeps the shared MIN_MAX_CHARS floor.
BOOTSTRAP_INDEX_MIN_CHARS = 2_000
# Under this budget an absent format means "index", and "full" is refused: it
# is the floor every call had to respect before the index format existed.
FULL_FORMAT_MIN_CHARS = MIN_MAX_CHARS


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


def parse_format(raw: Any) -> str | None:
    """``format``: absent → ``None`` (chosen from ``max_chars``), else ``"full"`` or ``"index"``."""
    from services.session_bootstrap_service import FORMATS

    if raw is None:
        return None
    if raw not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    return raw


def resolve_format(requested: str | None, max_chars: int) -> str:
    """The reply's format: ``index`` iff absent and ``max_chars`` < 10,000, else ``full``.

    Raises:
        ValueError: ``format="full"`` with a budget below 10,000.
    """
    from services.session_bootstrap_service import FORMAT_FULL, FORMAT_INDEX

    if requested is None:
        return FORMAT_INDEX if max_chars < FULL_FORMAT_MIN_CHARS else FORMAT_FULL
    if requested == FORMAT_FULL and max_chars < FULL_FORMAT_MIN_CHARS:
        raise ValueError(
            f'format="full" needs max_chars of at least {FULL_FORMAT_MIN_CHARS}; '
            'pass format="index" (or omit format) under it.'
        )
    return requested


def envelope_budget(max_chars: int, reply_format: str, context_fields: dict[str, Any]) -> int:
    """The budget the service fits the envelope to.

    The handler adds the ``context_*`` fields after the fit; in index mode
    their room (about 200 characters, a tenth of the smallest budget) comes off
    first, so the reply holds ``max_chars``. The full shape keeps its v0.114.0
    fit unchanged.
    """
    from services.session_bootstrap_service import FORMAT_INDEX

    if reply_format != FORMAT_INDEX:
        return max_chars
    return max_chars - json_chars(context_fields)


_FULL_TOO_SMALL_HELP = (
    'format="index" returns one short row per memory (an 80-character summary head, '
    "summary_cut when cut; pinned keeps its text) and no instructions; read a row "
    "with reference(memory_id)."
)


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
        max_chars = parse_max_chars(args.get("max_chars"), minimum=BOOTSTRAP_INDEX_MIN_CHARS)
        requested_format = parse_format(args.get("format"))
    except BudgetArgumentError as e:
        return _error_response("validation_error", e.message, received=e.received)
    except ValueError as e:
        return _error_response("validation_error", str(e))
    try:
        reply_format = resolve_format(requested_format, max_chars)
    except ValueError as e:
        return _error_response("validation_error", str(e), help=_FULL_TOO_SMALL_HELP)
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
                    max_chars=envelope_budget(max_chars, reply_format, context_fields),
                    guardrails_provider=partial(_guardrails_field, db, user_id, context),
                    format=reply_format,
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
