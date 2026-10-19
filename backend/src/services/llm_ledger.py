"""One ``llm_call_log`` row per billed completion call (#2019, #2144).

The supersede judge (#2016) and the recall selector (#2144) both record their
provider calls the same way: through ``isolated_session`` (the #1985 recipe),
so the row lands whatever the caller does next and a failed ledger write never
leaves the caller's session needing a rollback; and fail-open, because a lost
row is an observability gap, never a reason to drop the caller's result.
``call_metadata`` carries outcomes and counts only, never memory text.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from db.base import isolated_session
from utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


async def record_completion_call(
    db: AsyncSession,
    response: Any,
    *,
    caller: str,
    call_metadata: dict[str, Any],
    paid_by: str,
    user_id: str,
    workspace_id: str,
    context_id: str | None,
    failure_event: str,
    session_factory: Callable[[AsyncSession], Any] | None = None,
    log: Any = None,
) -> None:
    """Write one ``call_type='completion'`` ledger row; never raises.

    Args:
        db: The caller's session; only its engine is used (a fresh session
            writes the row).
        response: The ``LLMResponse`` of the call, or the usage a failed call
            still spent (``LLMServiceError.usage``).
        caller: ``llm_call_log.caller`` (a value its CHECK allows).
        call_metadata: Outcome and counts; never memory text.
        paid_by: ``LLMService.resolve_paid_by()`` of the call.
        user_id: The acting user.
        workspace_id: The paying workspace.
        context_id: The context the call was about.
        failure_event: The warning event logged when the write fails.
        session_factory: The session recipe (``isolated_session`` unless a
            caller passes its own module's binding).
        log: The logger the failure warning goes to (this module's unless
            given).
    """
    from services.llm_call_log_writer import LLMCallLogWriter

    factory = session_factory or isolated_session
    try:
        async with factory(db) as session:
            row = await LLMCallLogWriter(session).record(
                caller=caller,
                call_type="completion",
                provider=response.provider,
                model=response.model,
                user_id=user_id,
                workspace_id=workspace_id,
                context_id=context_id,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cached_input_tokens=response.cached_input_tokens,
                cache_write_tokens=response.cache_write_tokens,
                paid_by=paid_by,
                call_metadata=call_metadata,
                fail_on_error=False,
            )
            if row is not None:
                await session.commit()
    except Exception as exc:  # noqa: BLE001 — a ledger failure never reaches the caller
        (log or logger).warning(
            failure_event,
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
        )
