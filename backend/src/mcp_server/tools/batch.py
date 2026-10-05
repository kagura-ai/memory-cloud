"""``remember_batch`` — several memories in one call (#1853).

Each item carries ``remember``'s arguments (no ``context_id``: the batch's
applies; ``dedupe`` / ``tags_normalize`` / ``verbose`` are set on the batch)
and is validated and limited like a single ``remember`` — an undeclared key is
refused per item (#1873). Without ``atomic`` the items are written
independently and reported per item; with ``atomic=true`` the service writes
them in one transaction and a single failure — a ``dedupe="check"`` candidate
included — rolls everything back.
"""

from __future__ import annotations

import difflib
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent
from pydantic import ValidationError

from mcp_server.tools._arg_coercion import (
    _MAX_UNKNOWN_SHOWN,
    _TOOL_SCHEMAS,
    _shown_name,
    coerce_mcp_arguments,
)
from mcp_server.tools._errors import is_caller_value_error
from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _context_response_fields,
    _ContextNotFoundError,
    _dumps,
    _error_response,
    _format_validation_error,
    _lint_response_field,
    _log_tool_usage,
    _persistence_response_field,
    _resolve_context,
    _resolve_context_id,
    _touch_context_last_used,
    execute_with_timeout,
)
from mcp_server.tools.memory import parse_write_options, remember_request_from_args
from services.memory_service import (
    MAX_BATCH_ITEMS,
    BatchCommittedError,
    BatchItemError,
    DedupeUnavailableError,
    DuplicateCandidateError,
)
from utils.exceptions import AuthorizationError, NotFoundException, QuotaExceededError
from utils.logger import get_logger

logger = get_logger(__name__)

_REQUIRED = ("summary", "content", "type")
# remember arguments that are set once, on the batch, and not per item.
_BATCH_LEVEL = ("dedupe", "tags_normalize", "verbose")
# What an item may carry: remember's declared arguments minus the batch-level
# ones. context_id stays in (accepted when it repeats the batch's).
_ITEM_KEYS = frozenset(_TOOL_SCHEMAS["remember"]) - frozenset(_BATCH_LEVEL)


def _undeclared_keys(item: dict[str, Any]) -> dict[str, Any] | None:
    """The per-item ``invalid_argument`` for keys an item may not carry (#1873).

    A single ``remember`` refuses unknown arguments (#1742); an item that
    dropped ``importnace`` silently and reported ``success`` did not. The
    echoed names are client-controlled, hence bounded as in
    ``find_unknown_arguments``.
    """
    unknown = sorted(str(key) for key in item if key not in _ITEM_KEYS)
    if not unknown:
        return None
    allowed = sorted(_ITEM_KEYS - {"context_id"})
    parts: list[str] = []
    for name in unknown[:_MAX_UNKNOWN_SHOWN]:
        shown = _shown_name(name)
        if name in _BATCH_LEVEL:
            parts.append(f"{shown} (batch-level)")
            continue
        close = difflib.get_close_matches(name, allowed, n=1, cutoff=0.6)
        parts.append(f"{shown} (did you mean '{close[0]}'?)" if close else shown)
    more = len(unknown) - _MAX_UNKNOWN_SHOWN
    listed = ", ".join(parts) + (f" and {more} more" if more > 0 else "")
    return {
        "error": "invalid_argument",
        "message": (
            f"item does not accept {listed}. {', '.join(_BATCH_LEVEL)} are batch-level "
            "arguments: set them on remember_batch itself, not on an item. "
            f"An item accepts: {', '.join(allowed)}."
        ),
    }


def _request_from_item(item: Any, batch_context_id: str) -> Any:
    """A ``RememberRequest`` for one item, or a per-item error dict."""
    if not isinstance(item, dict):
        return {"error": "validation_error", "message": "item must be an object"}
    undeclared = _undeclared_keys(item)
    if undeclared:
        return undeclared
    # #1873: the JSON-string coercion a single remember gets at dispatch
    # (tags sent as '["a"]', details as '{"k": 1}').
    item = coerce_mcp_arguments("remember", item)
    if "context_id" in item and item["context_id"] != batch_context_id:
        return {
            "error": "validation_error",
            "message": "items carry no context_id of their own; the batch's applies",
        }
    missing = [f for f in _REQUIRED if f not in item]
    if missing:
        return {"error": "missing_fields", "message": f"Missing: {', '.join(missing)}"}
    try:
        return remember_request_from_args(item)
    except ValidationError as e:
        return {"error": "validation_error", "message": _format_validation_error(e)}


def item_error(exc: BaseException) -> dict[str, Any]:
    """Map a write failure onto the per-item ``{error, message}`` the batch reports."""
    if isinstance(exc, DuplicateCandidateError):
        return {"status": "duplicate_candidate", "candidate": exc.candidate}
    if isinstance(exc, DedupeUnavailableError):
        return {"error": "dedupe_unavailable", "message": "duplicate check unavailable"}
    if isinstance(exc, QuotaExceededError):
        return {"error": "quota_exceeded", "message": str(exc)}
    if isinstance(exc, AuthorizationError):
        return {
            "error": "forbidden",
            "message": "marking a memory as a tool guardrail needs the context editor role",
        }
    if isinstance(exc, NotFoundException):
        return {"error": "not_found", "message": str(exc)}
    if isinstance(exc, ValueError) and is_caller_value_error(exc):
        return {"error": "validation_error", "message": str(exc)}
    logger.error("remember_batch_item_failed", error=str(exc), exc_info=exc)
    return {"error": "internal_error", "message": "memory write failed"}


def _success_item(result: Any, *, verbose: bool) -> dict[str, Any]:
    return {
        "status": "success",
        "memory_id": str(result.memory_id),
        "scope": result.scope,
        **_persistence_response_field(result.persistence, verbose=verbose),
        **_lint_response_field(result.lint),
    }


def _finish(index: int, body: dict[str, Any]) -> dict[str, Any]:
    status = body.get("status") or ("error" if "error" in body else "success")
    return {"index": index, "status": status, **{k: v for k, v in body.items() if k != "status"}}


def _envelope(
    results: list[dict[str, Any]], context_fields: dict[str, Any], **extra: Any
) -> list[TextContent]:
    """``success`` (every item written), ``partial`` (some written), or
    ``duplicate_candidate`` (nothing written and no item failed: the refusals
    are candidates — a decision, not an error, as for ``remember``); an error
    envelope through ``_error_response`` (so the transport marks it) only when
    nothing was written and something failed.

    ``skipped`` items — the rest of a rolled-back atomic batch — are counted
    on their own (#1873): they did not fail, so an atomic batch whose only
    refusal is a candidate is ``duplicate_candidate`` too."""
    succeeded = sum(1 for r in results if r["status"] == "success")
    candidates = sum(1 for r in results if r["status"] == "duplicate_candidate")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed = len(results) - succeeded - candidates - skipped
    body = {
        "results": results,
        "count": len(results),
        "succeeded": succeeded,
        "candidates": candidates,
        "failed": failed,
        "skipped": skipped,
        **context_fields,
        **extra,
    }
    if succeeded == 0 and failed:
        return _error_response("batch_failed", "No item was written; see results.", **body)
    if succeeded == 0:
        status = "duplicate_candidate"
    else:
        status = "success" if failed == 0 and candidates == 0 else "partial"
    return [TextContent(type="text", text=_dumps({"status": status, **body}))]


def usage_status(results: list[dict[str, Any]]) -> int:
    """The HTTP-style status the usage log records for the batch, as the single
    ``remember`` would for the same outcome: 200 when something was written or
    every refusal was a candidate; otherwise the error class that dominated."""
    if any(r["status"] in ("success", "duplicate_candidate") for r in results):
        return 200
    errors = [r.get("error") for r in results if r["status"] == "error"]
    if errors and all(e == "quota_exceeded" for e in errors):
        return 429
    if errors and all(
        e in ("validation_error", "missing_fields", "invalid_argument") for e in errors
    ):
        return 422
    if errors and all(e == "dedupe_unavailable" for e in errors):
        return 503  # as remember's (#1873)
    if errors and all(e in ("forbidden", "not_found") for e in errors):
        return 403
    return 500


async def handle_remember_batch(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Store up to ``MAX_BATCH_ITEMS`` memories in one call (#1853)."""
    from db.base import get_db
    from services.memory_service import MemoryService

    if "context_id" not in args:
        return _error_response("missing_fields", "Missing required field: context_id")
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return _error_response("validation_error", "items must be a non-empty array of objects.")
    if len(items) > MAX_BATCH_ITEMS:
        return _error_response(
            "validation_error", f"items holds {len(items)} items; the limit is {MAX_BATCH_ITEMS}."
        )
    atomic = args.get("atomic", False)
    verbose = args.get("verbose", False)
    if not isinstance(atomic, bool) or not isinstance(verbose, bool):
        return _error_response("validation_error", "atomic and verbose must be booleans.")
    try:
        tags_normalize, dedupe = parse_write_options(args)
    except ValueError as e:
        return _error_response("validation_error", str(e))

    parsed = [_request_from_item(item, str(args["context_id"])) for item in items]
    invalid = {i: p for i, p in enumerate(parsed) if isinstance(p, dict)}
    if atomic and invalid:
        # Nothing touches the database: the batch is refused whole.
        results = [
            _finish(i, invalid[i])
            if i in invalid
            else {
                "index": i,
                "status": "skipped",
                "message": "batch refused: another item is invalid",
            }
            for i in range(len(parsed))
        ]
        return _error_response(
            "batch_refused",
            f"{len(invalid)} of {len(parsed)} items are invalid; atomic=true writes nothing.",
            results=results,
            count=len(results),
            succeeded=0,
            candidates=0,
            failed=len(invalid),
            skipped=len(results) - len(invalid),
        )

    start = time.time()
    async for db in get_db():
        context_id: UUID | None = None
        try:
            context_id = _resolve_context_id(args["context_id"])
            perm_error = await _check_viewer_permission(
                db, user_id, workspace_id, "create memories"
            )
            if perm_error:
                return perm_error
            # operation="remember": the items are remembers, and the memory-access
            # audit vocabulary (MAE_OPERATIONS) has no batch entry.
            context = await _resolve_context(db, user_id, context_id, operation="remember")
            # Read now: a failing item rolls the session back, which expires
            # this instance (reading it afterwards would be a lazy load outside
            # the async greenlet).
            context_fields = _context_response_fields(context)
            rolled_back = False
            service = MemoryService(db)
            write = {
                "user_id": user_id,
                "client": "mcp",
                "current_context_id": context_id,
                "current_workspace_id": workspace_id,
                "tags_normalize": tags_normalize,
                "dedupe": dedupe,
            }
            results: list[dict[str, Any]] = []
            note: dict[str, Any] = {}
            if atomic:
                try:
                    responses = await execute_with_timeout(
                        service.remember_many(parsed, **write), operation_name="remember_batch"
                    )
                except BatchCommittedError as e:
                    # #1873: the time limit passed AFTER the commit. The rows
                    # exist and have their embedding tasks; saying "rolled
                    # back" would make the caller resend and duplicate them.
                    # A cancelled statement may have left the session mid-flight.
                    await db.rollback()
                    rolled_back = True
                    results = [
                        _finish(i, _success_item(r, verbose=verbose))
                        for i, r in enumerate(e.responses)
                    ]
                    note = {
                        "committed_after_timeout": True,
                        "message": (
                            "The call passed its time limit after the batch was committed: "
                            "every item is stored (memory_id per item). Do NOT send the batch "
                            "again. Post-commit steps were cut short, so declared links "
                            "(supersedes, linked_memory_ids) of some items may be missing — "
                            "check with reference()."
                        ),
                    }
                except QuotaExceededError as e:
                    # #1873: the batch's one daily reservation did not fit —
                    # nothing was reserved or written, so a smaller batch may
                    # still fit. Same envelope and details as remember's.
                    await db.rollback()
                    await _log_tool_usage(
                        db, user_id, "remember_batch", start, 429, context_id, workspace_id
                    )
                    return _error_response(
                        "quota_exceeded",
                        e.message,
                        **{
                            **{k: v for k, v in e.details.items() if v is not None},
                            "count": len(parsed),
                            **context_fields,
                        },
                    )
                except TimeoutError:
                    # Cancelled before the commit (it is shielded): remember_many
                    # rolled back and released its quota reservation.
                    await _log_tool_usage(
                        db, user_id, "remember_batch", start, 504, context_id, workspace_id
                    )
                    return _error_response(
                        "timeout",
                        "The atomic batch did not finish in time and was rolled back; "
                        "nothing was written. Send fewer items, or drop atomic.",
                        count=len(parsed),
                        **context_fields,
                    )
                except BatchItemError as e:
                    rolled_back = True
                    results = [
                        _finish(i, item_error(e.cause))
                        if i == e.index
                        else {
                            "index": i,
                            "status": "skipped",
                            "message": f"batch rolled back: item {e.index} failed",
                        }
                        for i in range(len(parsed))
                    ]
                else:
                    results = [
                        _finish(i, _success_item(r, verbose=verbose))
                        for i, r in enumerate(responses)
                    ]
            else:
                # One fold map for the whole batch, so item 2's 'dev_env' lands on
                # item 1's 'Dev-Env' even before the vocabulary cache sees it.
                canonical = (
                    await service.tag_canonical_map(
                        workspace_id=context.workspace_id, context_id=context_id, user_id=user_id
                    )
                    if tags_normalize
                    else None
                )
                for i, request in enumerate(parsed):
                    if isinstance(request, dict):
                        results.append(_finish(i, request))
                        continue
                    try:
                        result = await execute_with_timeout(
                            service.remember(request, **write, tag_canonical=canonical),
                            operation_name="remember",
                        )
                    except Exception as exc:  # per item: the batch goes on
                        await db.rollback()
                        rolled_back = True
                        results.append(_finish(i, item_error(exc)))
                    else:
                        results.append(_finish(i, _success_item(result, verbose=verbose)))
            await _log_tool_usage(
                db,
                user_id,
                "remember_batch",
                start,
                usage_status(results),
                context_id,
                workspace_id,
            )
            if any(r["status"] == "success" for r in results):
                if rolled_back:
                    await db.refresh(context)  # expired by the rollback; one indexed read
                await _touch_context_last_used(db, context)
                await db.commit()
            return _envelope(results, context_fields, **note)
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
    return _error_response("internal_error", "Database session unavailable")
