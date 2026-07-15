"""Writer for memory_access_events (RFC-0002 P0-5, Issue #1278).

Hot-path-safe and **fail-open** (design F3 D24): runs on an INDEPENDENT db
session (so the audit write neither joins nor poisons the caller's
transaction, and a caller rollback never loses the audit row), swallows DB
failures with a structured warning that logs ``error_type`` — never
``str(exc)`` (the credential-leak guard, ``llm_call_log_writer`` precedent).
Validation errors (bad enum) RAISE. A dropped event degrades observability,
not security — P0 policy decisions are advisory.

**Audited population**: only requests carrying **verified agent identity** are
written (D34). :func:`emit_memory_access_event` early-returns for every
non-agent request, so legacy (unbound) traffic gains no hot-path write — the
RFC-0002 backward-compatibility contract. The P1 unbound-traffic extension is
setting-gated and separately reviewed.
"""

from __future__ import annotations

from contextlib import aclosing
from typing import Any
from uuid import UUID

from utils.logger import get_logger

logger = get_logger(__name__)

# Max recall result memory_ids stored in event_metadata (identifiers only,
# content never; under the 4 KB cap).
MAX_METADATA_MEMORY_IDS = 32


def _validate_enums(
    *, operation: str, outcome: str, surface: str, principal_type: str, policy_decision: str | None
) -> None:
    from models.memory_access_event import (
        MAE_OPERATIONS,
        MAE_OUTCOMES,
        MAE_POLICY_DECISIONS,
        MAE_PRINCIPAL_TYPES,
        MAE_SURFACES,
    )

    if operation not in MAE_OPERATIONS:
        raise ValueError(f"invalid memory-access operation: {operation!r}")
    if outcome not in MAE_OUTCOMES:
        raise ValueError(f"invalid memory-access outcome: {outcome!r}")
    if surface not in MAE_SURFACES:
        raise ValueError(f"invalid memory-access surface: {surface!r}")
    if principal_type not in MAE_PRINCIPAL_TYPES:
        raise ValueError(f"invalid memory-access principal_type: {principal_type!r}")
    if policy_decision is not None and policy_decision not in MAE_POLICY_DECISIONS:
        raise ValueError(f"invalid memory-access policy_decision: {policy_decision!r}")


async def record_memory_access_event(
    *,
    workspace_id: UUID,
    user_id: str,
    principal_type: str,
    operation: str,
    outcome: str,
    surface: str,
    agent_id: UUID | None = None,
    context_id: UUID | None = None,
    api_key_prefix: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    policy_decision: str | None = None,
    memory_id: UUID | None = None,
    result_count: int | None = None,
    latency_ms: int | None = None,
    query_hash: str | None = None,
    event_metadata: dict[str, Any] | None = None,
) -> None:
    """Write one audit row on an independent session; fail-open.

    Validation (enum) raises; a DB failure logs ``error_type`` and returns.
    """
    _validate_enums(
        operation=operation,
        outcome=outcome,
        surface=surface,
        principal_type=principal_type,
        policy_decision=policy_decision,
    )

    from db.base import get_db
    from models.memory_access_event import MemoryAccessEvent

    try:
        # aclosing() forces get_db()'s post-yield teardown (session.close) to run
        # synchronously HERE, inside this try/except. A bare early `return`/`break`
        # out of `async for` defers async-generator finalization to GC, so a
        # teardown error would escape the fail-open guard and surface as an
        # unretrieved finalizer exception instead (Copilot review, #1278).
        async with aclosing(get_db()) as gen:
            async for db in gen:
                db.add(
                    MemoryAccessEvent(
                        workspace_id=workspace_id,
                        context_id=context_id,
                        user_id=user_id,
                        principal_type=principal_type,
                        api_key_prefix=api_key_prefix,
                        agent_id=agent_id,
                        session_id=session_id,
                        run_id=run_id,
                        trace_id=trace_id,
                        span_id=span_id,
                        surface=surface,
                        operation=operation,
                        outcome=outcome,
                        policy_decision=policy_decision,
                        memory_id=memory_id,
                        result_count=result_count,
                        latency_ms=latency_ms,
                        query_hash=query_hash,
                        event_metadata=event_metadata,
                    )
                )
                await db.commit()
                break
    except Exception as exc:  # fail-open — never break the caller's path
        logger.warning(
            "memory_access_event_write_failed",
            operation=operation,
            outcome=outcome,
            surface=surface,
            error_type=type(exc).__name__,
        )


async def emit_memory_access_event(
    *,
    operation: str,
    outcome: str,
    workspace_id: UUID | None,
    user_id: str | None,
    context_id: UUID | None = None,
    memory_id: UUID | None = None,
    result_count: int | None = None,
    latency_ms: int | None = None,
    query: str | None = None,
    query_hash: str | None = None,
    policy_decision: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    """Chokepoint entry point — emits only for the verified-agent population.

    Gathers agent identity from the per-request AgentScope (#1275) and
    correlation identifiers from the per-request CorrelationContext (#1277).
    Early-returns (no write, one contextvar read) when the request carries no
    verified agent identity — the backward-compat no-hot-path-write guarantee
    for legacy traffic. Fail-open end to end.

    ``query``: raw query text — hashed here (HMAC-SHA256, dedicated audit key)
    into ``query_hash``; the raw text is NEVER stored. Passing a pre-hashed
    ``query_hash`` instead is also honored (``query`` wins if both are given).
    """
    from auth.agent_scope import get_agent_scope

    scope = get_agent_scope()
    # P0 audited population: credential-bound agent identity. (The P0-4 verified
    # baggage-claim population is stamped by the caller passing agent_id +
    # policy_decision='unbound'; when only the scope is set we use it.)
    if scope is None:
        return
    if workspace_id is None or not user_id:
        return

    from api.correlation import get_correlation

    corr = get_correlation()
    surface = corr.surface if corr else "rest"

    # Hash the raw query with the dedicated audit key; never store it verbatim.
    if query:
        from config.settings import get_settings
        from utils.hashing import hmac_sha256_hex

        query_hash = hmac_sha256_hex(query, get_settings().audit_hmac_key)

    metadata = dict(extra_metadata) if extra_metadata else None

    await record_memory_access_event(
        workspace_id=workspace_id,
        user_id=user_id,
        # Agent workloads authenticate with member API keys (P0-2); the audited
        # population is API-key-authenticated by construction.
        principal_type="api_key",
        operation=operation,
        outcome=outcome,
        surface=surface,
        agent_id=scope.agent_id,
        context_id=context_id,
        session_id=corr.session_id if corr else None,
        run_id=corr.run_id if corr else None,
        trace_id=corr.trace_id if corr else None,
        span_id=corr.span_id if corr else None,
        policy_decision=policy_decision,
        memory_id=memory_id,
        result_count=result_count,
        latency_ms=latency_ms,
        query_hash=query_hash,
        event_metadata=metadata,
    )
