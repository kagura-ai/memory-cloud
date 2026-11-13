"""The opt-in operations probe: one minimal call per platform lane (Issue #2768).

Run by the scheduler leader every ``OPERATIONS_PROBE_INTERVAL_SECONDS`` (off
by default: the calls are paid). Each distinct (kind, provider, model) of the
configured lanes is called once:

- completion: ``Return the JSON object {}.`` with ``max_tokens=16``; any
  answer, an empty one included, is ``ok``;
- embedding: a short fixed text straight to the provider (the embedding
  cache is bypassed, so a provider outage is seen);
- rerank: one fixed document.

Credentials come only from :mod:`services.platform_credentials` (settings
and env); this module never takes a workspace, a context or a user and never
reaches a workspace's own key. The answer is discarded; only the outcome, the
latency and the exception CLASS name are kept. No database connection is held
across a provider call: the lane set is read and its session closed before the
first call, and each result is recorded in its own short session.

Completion and embedding calls are entered in the cost ledger like any
platform call (``llm_call_log``: ``caller='admin'``, ``paid_by='platform'``,
no identity, ``call_metadata.stage='operations_probe'``). Rerank calls are
not (reranks are not ledgered, and the self-hosted rerankers report no usage).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, update

from models.operations_status import OperationsLlmLane
from services.llm_providers import LLMProvider, Usage
from services.operations import job_status
from services.operations.llm_lanes import LaneSnapshot, ProbeKind
from services.platform_credentials import (
    platform_embedding_client,
    platform_llm_credential,
    platform_llm_provider,
    platform_reranker,
)
from tasks.job_runs import sanitize_error_class
from utils.logger import get_logger

if TYPE_CHECKING:
    from config.settings import Settings

logger = get_logger(__name__)

PROBE_TEXT = "operations status probe"
PROBE_PROMPT = "Return the JSON object {}."
PROBE_MAX_TOKENS = 16
PROBE_TIMEOUT_SECONDS = 20.0
PROBE_RUN_BUDGET_MAX_SECONDS = 120.0
LEDGER_STAGE = "operations_probe"


class ProbeCredentialMissing(Exception):
    """The leader holds no platform credential for the target."""


@dataclass(frozen=True)
class ProbeTarget:
    """One distinct call the probe makes, and the lanes that share it."""

    kind: ProbeKind
    provider: str
    model: str
    lanes: tuple[str, ...]


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of one probe call (the answer itself is never kept)."""

    target: ProbeTarget
    ok: bool
    latency_ms: int
    error_class: str | None
    at: datetime
    completion_usage: Usage | None = None
    embedding_tokens: int | None = None


def probe_targets(lanes: Sequence[LaneSnapshot]) -> list[ProbeTarget]:
    """The distinct (kind, provider, model) of ``lanes``, in first-lane order."""
    grouped: dict[tuple[str, str, str], list[str]] = {}
    kinds: dict[tuple[str, str, str], ProbeKind] = {}
    for lane in lanes:
        key = (lane.probe_kind, lane.provider, lane.model)
        grouped.setdefault(key, []).append(lane.lane)
        kinds[key] = lane.probe_kind
    return [
        ProbeTarget(kind=kinds[key], provider=key[1], model=key[2], lanes=tuple(names))
        for key, names in grouped.items()
    ]


def run_budget_seconds(interval_seconds: int) -> float:
    """The whole run's time budget: ``min(interval / 2, 120 s)``."""
    return min(interval_seconds / 2, PROBE_RUN_BUDGET_MAX_SECONDS)


class ProbeClients:
    """The clients of one probe run (one per provider, so one per base URL)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm: dict[tuple[str, str], LLMProvider | None] = {}

    def llm(self, provider: str) -> LLMProvider | None:
        key = (provider, platform_llm_credential(provider, self._settings) or "")
        if key not in self._llm:
            self._llm[key] = platform_llm_provider(provider, self._settings)
        return self._llm[key]


async def _call(target: ProbeTarget, clients: ProbeClients, settings: Settings) -> Any:
    if target.kind == "completion":
        provider = clients.llm(target.provider)
        if provider is None:
            raise ProbeCredentialMissing
        response = await provider.complete_json(
            PROBE_PROMPT, model=target.model, temperature=0.0, max_tokens=PROBE_MAX_TOKENS
        )
        return response.usage
    if target.kind == "embedding":
        emb = platform_embedding_client(settings)
        if emb is None:
            raise ProbeCredentialMissing
        try:
            response = await emb.client.embeddings.create(
                model=emb.wire_model, input=PROBE_TEXT, **emb.extra
            )
        finally:
            await emb.client.close()
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "total_tokens", None) or getattr(usage, "prompt_tokens", None)
        return int(tokens) if isinstance(tokens, int) else None
    reranker = platform_reranker(settings)
    if reranker is None:
        raise ProbeCredentialMissing
    await reranker.rerank(PROBE_TEXT, [PROBE_TEXT], top_n=1)
    return None


async def run_probe(
    target: ProbeTarget,
    clients: ProbeClients,
    settings: Settings,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Call ``target`` once within ``timeout``. Never raises (except cancellation)."""
    started = time.monotonic()
    at = datetime.now(UTC)
    try:
        usage = await asyncio.wait_for(_call(target, clients, settings), timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the outcome is the class name
        return ProbeResult(
            target=target,
            ok=False,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_class=sanitize_error_class(type(exc).__name__),
            at=at,
        )
    return ProbeResult(
        target=target,
        ok=True,
        latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        error_class=None,
        at=at,
        completion_usage=usage if isinstance(usage, Usage) else None,
        embedding_tokens=usage if isinstance(usage, int) else None,
    )


async def record_probe(result: ProbeResult) -> None:
    """Write ``result`` to every lane sharing its target (own short session).

    Only rows that still hold the probed provider and model are updated, so
    a lane reconfigured meanwhile keeps clean probe columns.

    Raises:
        Exception: Whatever the database raised.
    """
    table = OperationsLlmLane.__table__
    target = result.target
    values: dict[str, Any] = {
        "last_probe_at": result.at,
        "last_probe_outcome": "ok" if result.ok else "failed",
        "last_probe_latency_ms": result.latency_ms,
        "last_probe_error_class": result.error_class,
        "updated_at": func.now(),
    }
    if result.ok:
        values["probe_consecutive_failures"] = 0
        values["last_probe_success_at"] = result.at
    else:
        values["probe_consecutive_failures"] = table.c.probe_consecutive_failures + 1
    stmt = (
        update(table)
        .where(
            table.c.lane.in_(target.lanes),
            table.c.provider == target.provider,
            table.c.model == target.model,
            # A late write from an older run never overwrites a newer one.
            or_(table.c.last_probe_at.is_(None), table.c.last_probe_at <= result.at),
        )
        .values(**values)
    )
    session = job_status._new_session()
    try:
        await session.execute(stmt)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def ledger_probe(result: ProbeResult) -> None:
    """Enter a successful completion or embedding probe in ``llm_call_log``.

    No identity (``caller='admin'``), ``paid_by='platform'``. A failure to
    write is logged by class name and never fails the probe.
    """
    if not result.ok or result.target.kind == "rerank":
        return
    from services.llm_call_log_writer import LLMCallLogWriter

    target = result.target
    usage_kwargs: dict[str, Any]
    if target.kind == "completion":
        usage = result.completion_usage
        usage_kwargs = {
            "call_type": "completion",
            "input_tokens": usage.input if usage else None,
            "output_tokens": usage.output if usage else None,
            "cached_input_tokens": usage.cached if usage else None,
            "cache_write_tokens": usage.cache_write if usage else None,
        }
    else:
        usage_kwargs = {"call_type": "embedding", "embedding_tokens": result.embedding_tokens}
    session = job_status._new_session()
    try:
        row = await LLMCallLogWriter(session).record(
            caller="admin",
            provider=target.provider,
            model=target.model,
            occurred_at=result.at.replace(tzinfo=None),
            paid_by="platform",
            call_metadata={"stage": LEDGER_STAGE, "target": target.kind},
            fail_on_error=False,
            **usage_kwargs,
        )
        if row is not None:
            await session.commit()
        else:
            await session.rollback()
    except Exception as exc:  # noqa: BLE001 - a lost ledger row never fails the probe
        await session.rollback()
        logger.warning("operations_probe_ledger_failed", error_class=type(exc).__name__)
    finally:
        await session.close()


__all__ = [
    "LEDGER_STAGE",
    "PROBE_MAX_TOKENS",
    "PROBE_PROMPT",
    "PROBE_TEXT",
    "PROBE_TIMEOUT_SECONDS",
    "ProbeClients",
    "ProbeCredentialMissing",
    "ProbeResult",
    "ProbeTarget",
    "ledger_probe",
    "probe_targets",
    "record_probe",
    "run_budget_seconds",
    "run_probe",
]
