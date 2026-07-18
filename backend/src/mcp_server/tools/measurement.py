"""MCP tools for the HOW-MUCH measurement lane (Issue #1333).

``record_measurement`` / ``recall_series`` over the dedicated ``measurements``
table — an append-only numeric time-series lane that is structurally excluded
from ``recall()`` and untouchable by Sleep consolidation.

Access control composes the same proven helpers as the state lane (#889):
``_resolve_context_for_read`` verifies the caller can reach the context at all
(uniform ``context_not_found`` on deny — CWE-639), and, for the write tool,
``_check_viewer_permission`` blocks read-only viewers plus the #1275
subtractive agent-binding WRITE gate.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _ContextNotFoundError,
    _error_response,
    _resolve_context_for_read,
    _resolve_context_id,
    _success_response,
)

# Matches the measurements.metric column length (VARCHAR(64)). Enforced in the
# handlers too so an overlong metric returns a structured error, not a DB 500.
from services.measurement_service import METRIC_MAX_LEN as _METRIC_MAX_LEN
from utils.datetime import to_utc_iso


def _validate_metric_arg(metric: Any) -> list[TextContent] | None:
    """Return an error response if ``metric`` is not a valid series name."""
    if not isinstance(metric, str) or not metric:
        return _error_response("validation_error", "'metric' must be a non-empty string")
    if len(metric) > _METRIC_MAX_LEN:
        return _error_response(
            "validation_error", f"'metric' must be at most {_METRIC_MAX_LEN} characters"
        )
    return None


def _parse_iso_arg(raw: Any, field: str) -> datetime:
    """Parse an ISO-8601 argument (``Z`` suffix accepted).

    Raises:
        ValueError: When the value is not a parseable ISO-8601 string.
    """
    if not isinstance(raw, str):
        raise ValueError(f"'{field}' must be an ISO 8601 datetime string")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"'{field}' is not a valid ISO 8601 datetime: {raw!r}") from None


async def handle_record_measurement(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Append one numeric observation to a metric's series (plain INSERT)."""
    if "metric" not in args or "value" not in args:
        return _error_response("missing_fields", "Missing required fields: metric, value")
    # These handlers have no pydantic request model, so validate the loosely
    # typed args explicitly rather than letting bad values reach SQL.
    metric = args["metric"]
    metric_error = _validate_metric_arg(metric)
    if metric_error:
        return metric_error
    value = args["value"]
    # #1322 parity: some MCP clients JSON-stringify scalar args, and
    # _arg_coercion leaves numbers to pydantic — which this handler doesn't
    # have. Coerce numeric strings here so the tool behaves like every
    # pydantic-backed tool would.
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return _error_response("validation_error", "'value' must be a number")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _error_response("validation_error", "'value' must be a number")
    measured_at: datetime | None = None
    if args.get("measured_at") is not None:
        try:
            measured_at = _parse_iso_arg(args["measured_at"], "measured_at")
        except ValueError as exc:
            return _error_response("validation_error", str(exc))
    unit = args.get("unit")
    details = args.get("details")

    from db.base import get_db
    from services.measurement_service import MeasurementService

    async for db in get_db():
        # Defense-in-depth: the dispatcher already validates context_id for
        # non-exempt tools, but guard here too so a direct handler call returns
        # a structured error instead of raising KeyError/ValueError.
        raw_ctx = args.get("context_id")
        if not raw_ctx:
            return _error_response("missing_fields", "Missing required field: context_id")
        try:
            context_id = _resolve_context_id(str(raw_ctx))
        except ValueError as exc:
            return _error_response("invalid_context_id_format", str(exc))
        try:
            # Verify the caller can reach the context (IDOR guard) ...
            context = await _resolve_context_for_read(db, user_id, context_id)
        except _ContextNotFoundError as exc:
            return exc.to_response()
        # ... and is not a read-only viewer (write gate, mirrors set_state).
        # workspace_id is often None under OAuth2 / session-cookie MCP auth —
        # fall back to the resolved context's workspace so the gate always
        # fires against the authoritative workspace.
        effective_workspace_id = workspace_id or context.workspace_id
        perm_error = await _check_viewer_permission(
            db, user_id, effective_workspace_id, "record measurement"
        )
        if perm_error:
            return perm_error

        # Issue #1275: recording is a WRITE against the context — apply the
        # subtractive binding gate so a read-only-bound agent (can_read=true,
        # write_policy='deny') cannot append measurements in a context whose
        # binding forbids writes. _resolve_context_for_read above only checks
        # the READ side. No-op for non-agent credentials.
        from services.agent_binding_service import ACCESS_WRITE, agent_binding_permits

        if not await agent_binding_permits(db, context_id, ACCESS_WRITE):
            return _ContextNotFoundError(context_id, "Context not found.").to_response()

        try:
            row = await MeasurementService(db).record(
                context_id,
                metric,
                value,
                measured_at=measured_at,
                unit=unit,
                details=details,
            )
        except ValueError as exc:
            return _error_response("validation_error", str(exc))
        return _success_response(
            measurement_id=str(row.id),
            metric=row.metric,
            measured_at=to_utc_iso(row.measured_at),
            value=float(row.value),
            unit=row.unit,
        )

    return _error_response("internal_error", "Database session unavailable")


async def handle_recall_series(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Read one metric's series bucketed by period with an aggregate."""
    if "metric" not in args:
        return _error_response("missing_fields", "Missing required field: metric")
    metric = args["metric"]
    metric_error = _validate_metric_arg(metric)
    if metric_error:
        return metric_error
    period = args.get("period", "day")
    agg = args.get("agg", "avg")
    start: datetime | None = None
    end: datetime | None = None
    try:
        if args.get("start") is not None:
            start = _parse_iso_arg(args["start"], "start")
        if args.get("end") is not None:
            end = _parse_iso_arg(args["end"], "end")
    except ValueError as exc:
        return _error_response("validation_error", str(exc))

    from db.base import get_db
    from services.measurement_service import MeasurementService

    async for db in get_db():
        # Defense-in-depth context_id guard (see handle_record_measurement).
        raw_ctx = args.get("context_id")
        if not raw_ctx:
            return _error_response("missing_fields", "Missing required field: context_id")
        try:
            context_id = _resolve_context_id(str(raw_ctx))
        except ValueError as exc:
            return _error_response("invalid_context_id_format", str(exc))
        try:
            await _resolve_context_for_read(db, user_id, context_id)
        except _ContextNotFoundError as exc:
            return exc.to_response()

        try:
            series = await MeasurementService(db).recall_series(
                context_id,
                metric,
                period=period,
                agg=agg,
                start=start,
                end=end,
            )
        except ValueError as exc:
            return _error_response("validation_error", str(exc))
        return _success_response(
            metric=metric,
            period=period,
            agg=agg,
            series=[
                {
                    "bucket": to_utc_iso(item["bucket"]),
                    "value": item["value"],
                    "count": item["count"],
                }
                for item in series
            ],
            count=len(series),
        )

    return _error_response("internal_error", "Database session unavailable")
