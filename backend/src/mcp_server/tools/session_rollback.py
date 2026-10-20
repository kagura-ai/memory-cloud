"""MCP tool handler: ``rollback_session`` (#2044, window mode #2121).

The tool face of ``services.session_rollback``: authorization (context editor;
a session is addressed only with its own principal, a context owner may name
any), the dry-run plan, and the ``rollback_sleep_run``-shaped report —
``partial_rollback`` with per-item causes when an item failed, a refusal once
the session's rollback is final.

Exactly one mode per call: ``session_id``, or ``since`` (+ ``until``) for the
caller's own writes in a time window — for clients that have no session id
(the stateless MCP transport). The window is always the caller's own
principal (verified sub + the credential's agent), never a context owner's
override; it is at most ``MAX_ROLLBACK_WINDOW`` wide, and an omitted ``until``
(or one later than now) continues the caller's unfinished rollback of a
window from the same ``since`` before it defaults to now. "Now" is the later
of the database clock and this server's clock: ``memory_revisions.at`` comes
from both (a created event takes the database's ``now()``, an edit or a
forget the server's ``utcnow()``), so neither clock alone may cut off a
write that has already happened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._constants import get_tool_timeout
from mcp_server.tools._errors import classify_cause, new_correlation_id
from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _context_response_fields,
    _ContextNotFoundError,
    _error_response,
    _log_tool_usage,
    _resolve_context,
    _resolve_context_id,
    _success_response,
    execute_with_timeout,
)
from utils.datetime import parse_iso8601_to_aware, to_naive_utc, to_utc_iso, utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

#: Share of the tool timeout one call may spend starting items; the rest is
#: headroom for the item in flight, the commit and the post-commit steps.
_TIME_BUDGET_SHARE = 0.5

_SESSION_ID_HELP = (
    "session_id is the session your writes were recorded under — remember, "
    "update_memory and forget return it: the gen_ai.conversation.id / session.id "
    "OTel baggage of the request, else the MCP session id (Mcp-Session-Id)."
)
_NOT_FOUND_HELP = (
    "Only your own sessions are visible (the same user and the same agent "
    "credential that wrote them); a context owner may name any session in the "
    "context. changes_since(context_id, since, session_id) lists what a session wrote."
)
_MODE_HELP = (
    "Pass session_id (a session a write returned) or since (ISO 8601) to roll back "
    "your own writes in a time window."
)
_NO_WRITES_HELP = (
    "Only your own writes count: the same user and the same credential — an agent "
    "key's writes are not your user credential's, and two clients on one user "
    "credential share them. changes_since(context_id, since, by_me=true) lists what "
    "you wrote and when."
)
_CONTINUE_HELP = "Call again with the same since and until to continue."
_DRY_RUN_WINDOW_HELP = (
    "Call with the same since and until and dry_run=false to act on exactly this window."
)
_PARTIAL_HELP = (
    "Every item not listed in summary.errors was handled and saved. Call "
    "rollback_session again to retry the failed items (they are planned again "
    "from the memory as it is then; nothing already reversed is reversed again). "
    "Quote the correlation_id when reporting a failure."
)


@dataclass(frozen=True)
class _Args:
    """The parsed arguments: one session, or a window (naive UTC)."""

    session_id: str | None
    since: datetime | None
    # Explicit until (not yet compared with now); None in session mode or when omitted.
    until: datetime | None
    dry_run: bool


def _window_too_wide(since: datetime, until: datetime) -> list[TextContent] | None:
    from services.session_rollback import MAX_ROLLBACK_WINDOW

    if until - since <= MAX_ROLLBACK_WINDOW:
        return None
    return _error_response(
        "validation_error",
        f"The window may be at most {MAX_ROLLBACK_WINDOW.days} days; split it.",
        since=to_utc_iso(since),
        until=to_utc_iso(until),
    )


def _parse_window(
    since_arg: Any, until_arg: Any
) -> tuple[datetime, datetime | None] | list[TextContent]:
    """``since`` / ``until`` → naive UTC. Comparing them with now needs the database (``_window_now``)."""
    if not isinstance(since_arg, str) or (until_arg is not None and not isinstance(until_arg, str)):
        return _error_response("validation_error", "since and until must be ISO 8601 strings.")
    try:
        since = to_naive_utc(parse_iso8601_to_aware(since_arg, "since"))
        until = (
            to_naive_utc(parse_iso8601_to_aware(until_arg, "until"))
            if until_arg is not None
            else None
        )
    except (ValueError, OverflowError) as e:
        # OverflowError: a year-1 / year-9999 value with an offset leaves the
        # datetime range when it is moved to UTC.
        return _error_response("validation_error", f"since / until: {e}")
    if until is not None and until <= since:
        return _error_response("validation_error", "until must be later than since.")
    return since, until


async def _window_now(db: Any) -> datetime:
    """Now for a window (naive UTC): the later of the database and the server clock.

    ``memory_revisions.at`` is stamped by both — the database's ``now()`` on a
    created event, the server's ``utcnow()`` on an edit or a forget.
    """
    from sqlalchemy import func, select

    db_now = to_naive_utc(await db.scalar(select(func.clock_timestamp())))
    return max(db_now, utcnow())


async def _resolve_window(
    db: Any, context_id: UUID, principal: Any, since: datetime, until: datetime | None
) -> tuple[datetime, datetime, bool] | list[TextContent]:
    """The effective window, or the refusal.

    ``since`` must be before now. An ``until`` later than now is treated like
    an omitted one, so a retry with the same arguments continues the same
    record instead of opening a new window at a later now: it becomes the
    ``until`` of the caller's unfinished rollback of a window from ``since``,
    else now.
    """
    from services.session_rollback import unfinished_window_until

    now = await _window_now(db)
    if since >= now:
        return _error_response("validation_error", "since must be earlier than now.")
    continued = False
    if until is None or until > now:
        unfinished = await unfinished_window_until(db, context_id, principal, since)
        continued = unfinished is not None
        until = unfinished or now
    too_wide = _window_too_wide(since, until)
    if too_wide is not None:
        return too_wide
    return since, until, continued


def _parse_args(args: dict[str, Any]) -> _Args | list[TextContent]:
    from api.correlation import validate_correlation_token

    session_id = args.get("session_id")
    if session_id == "":
        session_id = None
    since_arg = args.get("since")
    until_arg = args.get("until")
    if session_id is not None and since_arg is not None:
        return _error_response(
            "validation_error", "Pass session_id or since, not both.", help=_MODE_HELP
        )
    if until_arg is not None and since_arg is None:
        return _error_response("validation_error", "until needs since.", help=_MODE_HELP)
    if session_id is None and since_arg is None:
        return _error_response(
            "missing_fields", "Missing required field: session_id or since", help=_MODE_HELP
        )
    since: datetime | None = None
    until: datetime | None = None
    if session_id is not None:
        if not isinstance(session_id, str) or validate_correlation_token(session_id) is None:
            return _error_response(
                "validation_error",
                "session_id must be 1-128 characters of A-Z, a-z, 0-9, '.', '_' or '-'.",
                help=_SESSION_ID_HELP,
            )
    else:
        window = _parse_window(since_arg, until_arg)
        if isinstance(window, list):
            return window
        since, until = window
    dry_run = args.get("dry_run", True)
    if not isinstance(dry_run, bool):
        return _error_response("validation_error", "dry_run must be a boolean.")
    return _Args(session_id=session_id, since=since, until=until, dry_run=dry_run)


async def handle_rollback_session(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Plan or roll back what one session — or the caller in a window — wrote in a context."""
    from auth.workspace_roles import ContextRole
    from db.base import get_db
    from services.session_rollback import (
        AlreadyRolledBackError,
        NoWritesInWindowError,
        SessionNotFoundError,
        SessionScope,
        addressable_principal,
        context_role,
        finish,
        lock_window_principal,
        rollback_session,
    )
    from services.write_provenance import caller_principal_linked

    parsed = _parse_args(args)
    if isinstance(parsed, list):
        return parsed
    session_id, since, until, dry_run = (
        parsed.session_id,
        parsed.since,
        parsed.until,
        parsed.dry_run,
    )
    # The mode's echo in every reply: the session, or the effective window.
    mode: dict[str, Any] = {"session_id": session_id}
    start = time.time()
    async for db in get_db():
        context_id: UUID | None = None
        try:
            context_id = _resolve_context_id(args["context_id"])
            perm_error = await _check_viewer_permission(
                db, user_id, workspace_id, "roll back sessions"
            )
            if perm_error:
                return perm_error
            # The write path: workspace-key scope and the agent-binding write
            # gate, uniform context_not_found on a deny.
            context = await _resolve_context(db, user_id, context_id)
            role = await context_role(db, user_id, context_id)
            if role not in (ContextRole.OWNER, ContextRole.EDITOR):
                return _error_response(
                    "permission_denied",
                    "Cannot roll back sessions: you need the editor or owner role on this context.",
                    your_role=role.value if role is not None else None,
                    required_role="editor",
                    help="Ask a context owner for the editor role.",
                )
            if since is None:
                scope = SessionScope(
                    context_id=context_id,
                    session_id=session_id,
                    principal=await addressable_principal(db, user_id, role),
                )
            else:
                # #2121: always the caller's own principal — the owner
                # override is session-mode only. #2114: an account linked
                # to the caller's counts as the caller's own.
                principal = await caller_principal_linked(db, user_id)
                if not dry_run:
                    # Resolve an omitted until under the principal's window
                    # lock, so concurrent first calls share one record.
                    await lock_window_principal(db, context_id, principal)
                window = await _resolve_window(db, context_id, principal, since, until)
                if isinstance(window, list):
                    await db.rollback()
                    return window
                since, until, continued = window
                mode = {"since": to_utc_iso(since), "until": to_utc_iso(until)}
                if continued:
                    # The echoed until is an earlier call's: say so, and how
                    # to reach writes made after it.
                    mode["until_from"] = "unfinished_rollback"
                scope = SessionScope(
                    context_id=context_id,
                    session_id=None,
                    principal=principal,
                    since=since,
                    until=until,
                )
            timeout = get_tool_timeout("rollback_session")
            result = await execute_with_timeout(
                rollback_session(
                    db,
                    scope=scope,
                    workspace_id=context.workspace_id,
                    requested_by=user_id,
                    dry_run=dry_run,
                    classify=classify_cause,
                    new_correlation_id=new_correlation_id,
                    # Half the tool timeout: no item starts after it, so the
                    # call commits what it finished instead of timing out and
                    # replaying the same batch (v0.113.0 review F2).
                    time_budget=timeout * _TIME_BUDGET_SHARE,
                ),
                timeout=timeout,
                operation_name="rollback_session",
            )
            failed = bool(result.errors)
            await _log_tool_usage(
                db,
                user_id,
                "rollback_session",
                start,
                500 if failed else 200,
                context_id,
                workspace_id,
            )
            if dry_run:
                await db.rollback()  # a dry run writes nothing
            else:
                await db.commit()
                await finish(result, db)
        except SessionNotFoundError:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "rollback_session", start, 404, context_id, workspace_id
            )
            return _error_response(
                "session_not_found",
                "No session with this id that you can roll back exists in this context.",
                session_id=session_id,
                help=_NOT_FOUND_HELP,
            )
        except NoWritesInWindowError:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "rollback_session", start, 404, context_id, workspace_id
            )
            return _error_response(
                "no_writes_in_window",
                "You wrote nothing in this context in this window that can be rolled back.",
                retryable=False,
                **mode,
                help=_NO_WRITES_HELP,
            )
        except AlreadyRolledBackError as e:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "rollback_session", start, 409, context_id, workspace_id
            )
            return _error_response(
                "already_rolled_back",
                "This session's rollback in this context is complete and it wrote nothing since."
                if since is None
                else "Your writes in this window are rolled back and nothing new was written in it.",
                retryable=False,
                **mode,
                **({"rollback_id": e.rollback_id} if e.rollback_id is not None else {}),
                rollback_status=e.status,
                help="changes_since(context_id, since, session_id) shows what the session wrote."
                if since is None
                else "changes_since(context_id, since, by_me=true) shows what you wrote.",
            )
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "rollback_session", start, 500, context_id, workspace_id
            )
            raise

        from services.session_rollback import plan_counts

        common: dict[str, Any] = {
            **mode,
            "events": result.events,
            "remaining": result.remaining,
            **({"rollback_id": result.rollback_id} if result.rollback_id is not None else {}),
            **_context_response_fields(context),
        }
        items = result.listed_items()
        listing: dict[str, Any] = {"items": items}
        if len(items) < len(result.items):
            listing["items_truncated"] = True
        if dry_run:
            return _success_response(
                status="dry_run",
                dry_run=True,
                plan=plan_counts(result.items),
                **listing,
                **common,
                **({"help": _DRY_RUN_WINDOW_HELP} if since is not None else {}),
            )
        if failed:
            return _error_response(
                "partial_rollback",
                f"Rollback finished this call with {len(result.errors)} failed item(s); "
                "summary shows what was reversed.",
                help=f"{_PARTIAL_HELP} {result.remaining} event(s) remain."
                + (f" {_CONTINUE_HELP}" if since is not None else ""),
                retryable=True,
                rollback_status=result.status,
                summary=result.summary,
                totals=result.totals,
                **({"correlation_id": result.correlation_id} if result.correlation_id else {}),
                **common,
            )
        return _success_response(
            status="in_progress" if result.remaining else "rolled_back",
            dry_run=False,
            rollback_status=result.status,
            summary=result.summary,
            totals=result.totals,
            **common,
            **({"help": _CONTINUE_HELP} if since is not None and result.remaining else {}),
        )

    return _error_response("db_error", "Database unavailable.")
