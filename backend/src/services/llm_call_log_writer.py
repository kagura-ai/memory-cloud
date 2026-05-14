"""Writer service for the comprehensive LLM call ledger (#474).

Issue #474: Inserts one ``llm_call_log`` row per LLM/embedding/rerank
API call from non-Sleep paths (recall reranking, recall embedding via
#475 PR-3, future ``ask()``, admin actions). Sleep Maintenance never
emits here — Sleep cost stays in ``sleep_reports`` + ``sleep_report_llm_usage``
(canonical pattern, memory ``9db43e36`` / 2026-05-14 gate1 decision).

Design contract (mirrors ``services/sleep/reporter.py:SleepReporter``):

- ``__init__(db: AsyncSession)`` — caller injects the session, writer
  uses it directly. No internal session acquisition / commit / rollback.
- ``record()`` performs ``self.db.add(row)`` + ``await self.db.flush()``
  so the caller can read back ``row.id`` immediately. ``commit()`` is
  the orchestrator's responsibility.
- Errors propagate to the caller (no swallow). The recall-path call
  site decides whether a writer failure should degrade silently — the
  writer never makes that policy call.

Cost computation is **write-time snapshot**: for each non-None positive
usage column, ``LLMPricingService.compute_cost_usd`` is called with
``occurred_at`` as the ``started_at`` and the result is summed into
``cost_usd``. A lookup miss on any axis stores ``cost_usd = 0`` and
sets ``call_metadata.pricing_miss = true`` so the row stays queryable
and the dashboard surfaces the gap. Lazy/read-time computation was
rejected at gate1 yellow #1 — it ties historical reports to the
current ``llm_pricing`` state and breaks reproducibility across price
changes.

The ``caller`` value ``'sleep'`` is permitted by the table CHECK
(forward-compat) but the writer refuses it — emitting Sleep cost twice
would force a reconciliation contract this PR explicitly avoids.

The nullability matrix per ``caller`` is asserted Python-side as
defense-in-depth on top of the schema. See ``models/llm_call_log.py``
module docstring for the canonical table.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from models.llm_call_log import (
    LLM_CALL_LOG_CALL_TYPES,
    LLM_CALL_LOG_CALLERS,
    LLM_CALL_LOG_PAID_BY_VALUES,
    LLMCallLog,
)
from services.llm_pricing_service import LLMPricingService
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


# Caller values the writer refuses to emit. Sleep is included for the
# canonical-store reason; future "forbidden" callers can be added here
# without touching the CHECK constraint (the CHECK is a superset).
_FORBIDDEN_CALLERS: frozenset[str] = frozenset({"sleep"})

# Callers that require full (user_id, workspace_id, context_id) identity.
# ``admin`` is intentionally excluded — backfill / recalibration jobs
# may run without a user binding. The table CHECK is permissive (all
# three columns nullable); this Python-side assert enforces the
# operational contract.
_FULL_IDENTITY_CALLERS: frozenset[str] = frozenset({"recall", "rerank", "ask"})

# Column-name → llm_pricing.unit_type axis. ``cached_input_tokens`` maps
# to the ``cache_read_tokens`` pricing axis — naming differs because
# providers (Anthropic) return ``cached_input_tokens`` in their usage
# payload but pricing tables consistently use ``cache_read_tokens`` for
# the dimension. Same convention as ``sleep_report_llm_usage``.
_USAGE_TO_UNIT_TYPE: dict[str, str] = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cached_input_tokens": "cache_read_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "embedding_tokens": "embedding_tokens",
    "rerank_tokens": "rerank_tokens",
    "rerank_search_units": "rerank_search_units",
}

# Import-time guard: if anyone renames a usage column on ``LLMCallLog``
# (e.g., ``cached_input_tokens`` → ``cache_read_input_tokens`` to match
# the pricing axis), this assertion turns the silent miss into a hard
# AssertionError at module load — the writer would otherwise drop the
# axis silently because the ``_USAGE_TO_UNIT_TYPE`` key would no longer
# match any column and the loop in ``record()`` iterates the same
# hardcoded keys.
_LLM_CALL_LOG_COLUMN_KEYS: frozenset[str] = frozenset(LLMCallLog.__table__.columns.keys())
assert set(_USAGE_TO_UNIT_TYPE.keys()) <= _LLM_CALL_LOG_COLUMN_KEYS, (
    "_USAGE_TO_UNIT_TYPE keys must all be columns on LLMCallLog; "
    f"missing: {set(_USAGE_TO_UNIT_TYPE.keys()) - _LLM_CALL_LOG_COLUMN_KEYS}"
)


def _coerce_uuid(value: UUID | str | None) -> UUID | None:
    """Accept either ``UUID`` or ``str`` from callers; return ``UUID | None``.

    Recall/rerank call sites already hold ``UUID`` objects (from auth
    dependencies), but admin / MCP entry points may pass strings. Keep
    the writer's surface loose so callers don't have to coerce.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(value)


class LlmCallLogWriter:
    """Inserts one ``llm_call_log`` row per provider call.

    Stateless from the caller's perspective; the only mutable state is
    the injected ``AsyncSession``. Reuse one instance per request /
    operation context — each ``record()`` call performs at most
    (1 + number_of_priced_unit_types) DB round trips: one or more
    ``llm_pricing`` SELECTs followed by a single INSERT via ``flush()``.
    """

    def __init__(self, db: AsyncSession, pricing: LLMPricingService | None = None) -> None:
        """Initialize the writer.

        Args:
            db: Async SQLAlchemy session. Same session pattern as every
                other service in this layer (compare ``SleepReporter``,
                ``LLMPricingService``).
            pricing: Optional pre-built pricing service — useful for
                tests that mock pricing without mocking the DB. When
                omitted, the writer constructs one from ``db``.
        """
        self.db = db
        self.pricing = pricing or LLMPricingService(db)

    async def record(
        self,
        *,
        caller: str,
        call_type: str,
        provider: str,
        model: str,
        occurred_at: datetime | None = None,
        user_id: str | None = None,
        workspace_id: UUID | str | None = None,
        context_id: UUID | str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        embedding_tokens: int | None = None,
        rerank_tokens: int | None = None,
        rerank_search_units: int | None = None,
        paid_by: str = "platform",
        call_metadata: dict[str, Any] | None = None,
    ) -> LLMCallLog:
        """Insert one llm_call_log row with a write-time cost snapshot.

        Returns the inserted ``LLMCallLog`` (post-flush, so ``id`` is
        populated). The caller is responsible for the surrounding
        transaction lifecycle.

        Raises:
            ValueError: ``caller`` / ``call_type`` / ``paid_by`` is
                outside its allowed set, OR ``caller`` is in
                ``_FORBIDDEN_CALLERS`` (today: 'sleep'), OR the
                nullability matrix is violated for the chosen caller.
        """
        self._validate_inputs(
            caller=caller,
            call_type=call_type,
            paid_by=paid_by,
            user_id=user_id,
            workspace_id=workspace_id,
            context_id=context_id,
        )

        resolved_occurred_at: datetime = occurred_at if occurred_at is not None else utcnow()

        # Per-axis usage list (only positive counts contribute to cost;
        # zeros and Nones are skipped — saves a pricing round trip per
        # unused column). Negative values are an error: silent drop
        # would let bad data into the row with cost_usd=0, which would
        # under-count cost without any signal at the writer boundary.
        usage_pairs: list[tuple[str, int]] = []
        for column_name, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("cached_input_tokens", cached_input_tokens),
            ("cache_write_tokens", cache_write_tokens),
            ("embedding_tokens", embedding_tokens),
            ("rerank_tokens", rerank_tokens),
            ("rerank_search_units", rerank_search_units),
        ):
            if value is None:
                continue
            if value < 0:
                raise ValueError(f"{column_name}={value} must be >= 0")
            if value > 0:
                usage_pairs.append((column_name, value))

        cost_usd, pricing_miss = await self._compute_cost_usd(
            provider=provider,
            model=model,
            occurred_at=resolved_occurred_at,
            usage_pairs=usage_pairs,
        )

        final_metadata: dict[str, Any] | None = dict(call_metadata) if call_metadata else None
        if pricing_miss:
            final_metadata = final_metadata or {}
            final_metadata["pricing_miss"] = True
            logger.warning(
                "llm_call_log_pricing_miss",
                caller=caller,
                provider=provider,
                model=model,
                call_type=call_type,
            )

        row = LLMCallLog(
            occurred_at=resolved_occurred_at,
            user_id=user_id,
            workspace_id=_coerce_uuid(workspace_id),
            context_id=_coerce_uuid(context_id),
            caller=caller,
            call_type=call_type,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_tokens=cache_write_tokens,
            embedding_tokens=embedding_tokens,
            rerank_tokens=rerank_tokens,
            rerank_search_units=rerank_search_units,
            cost_usd=cost_usd,
            paid_by=paid_by,
            call_metadata=final_metadata,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    @staticmethod
    def _validate_inputs(
        *,
        caller: str,
        call_type: str,
        paid_by: str,
        user_id: str | None,
        workspace_id: UUID | str | None,
        context_id: UUID | str | None,
    ) -> None:
        """Defense-in-depth enum + nullability checks before the DB sees them.

        Each constraint is mirrored by a CHECK constraint on the table;
        the Python-side validation just turns a future bug into a clear
        ``ValueError`` at the writer boundary instead of an
        ``IntegrityError`` from the COMMIT path.
        """
        if caller not in LLM_CALL_LOG_CALLERS:
            raise ValueError(f"invalid caller {caller!r}; must be one of {LLM_CALL_LOG_CALLERS}")
        if caller in _FORBIDDEN_CALLERS:
            raise ValueError(
                f"caller {caller!r} is not allowed in llm_call_log — "
                "Sleep Maintenance writes to sleep_reports + "
                "sleep_report_llm_usage; #472 UNION ALLs both tables"
            )
        if call_type not in LLM_CALL_LOG_CALL_TYPES:
            raise ValueError(
                f"invalid call_type {call_type!r}; must be one of {LLM_CALL_LOG_CALL_TYPES}"
            )
        if paid_by not in LLM_CALL_LOG_PAID_BY_VALUES:
            raise ValueError(
                f"invalid paid_by {paid_by!r}; must be one of {LLM_CALL_LOG_PAID_BY_VALUES}"
            )
        if caller in _FULL_IDENTITY_CALLERS:
            missing: list[str] = []
            if user_id is None:
                missing.append("user_id")
            if workspace_id is None:
                missing.append("workspace_id")
            if context_id is None:
                missing.append("context_id")
            if missing:
                raise ValueError(
                    f"caller {caller!r} requires all of "
                    f"(user_id, workspace_id, context_id); missing: {missing}"
                )

    async def _compute_cost_usd(
        self,
        *,
        provider: str,
        model: str,
        occurred_at: datetime,
        usage_pairs: list[tuple[str, int]],
    ) -> tuple[Decimal, bool]:
        """Sum cost across all priced unit_types for this call.

        Returns ``(cost_usd, pricing_miss)``. ``pricing_miss`` is True
        iff at least one ``compute_cost_usd`` lookup returned ``None``
        (caller writes ``cost_usd = 0`` and sets the metadata flag).
        An empty ``usage_pairs`` list returns ``(Decimal(0), False)`` —
        a row with no priced units is valid (e.g., a degenerate
        completion with input_tokens=0).
        """
        if not usage_pairs:
            return Decimal("0"), False

        total = Decimal("0")
        miss = False
        for column_name, units in usage_pairs:
            unit_type = _USAGE_TO_UNIT_TYPE[column_name]
            partial = await self.pricing.compute_cost_usd(
                provider=provider,
                model=model,
                unit_type=unit_type,
                started_at=occurred_at,
                units=units,
            )
            if partial is None:
                miss = True
                continue
            # ``compute_cost_usd`` returns float — convert via str to
            # preserve precision the float repr would otherwise mangle
            # (e.g. 0.05 → 0.05000000000000000277...) when crossing the
            # Decimal boundary.
            total += Decimal(str(partial))

        if miss:
            # Miss on any axis collapses the whole cost to 0; the
            # metadata flag carries the signal. Don't return a partial
            # sum — that would understate cost without being marked.
            return Decimal("0"), True
        return total, False
