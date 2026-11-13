"""Platform LLM lanes: the leader's snapshot and the ledger usage (Issue #2768).

Leader side (no external call):

- :func:`configured_platform_lanes` — the platform-paid lanes this deployment
  configures, with their provider, model and whether the leader holds the
  platform credential (never the credential itself).
- :func:`write_lane_snapshot` — one row per configured lane in
  ``operations_llm_lanes``; lanes no longer configured are removed, and a
  lane whose provider or model changed loses its probe columns.

Reader side (any API process; it reads no LLM setting or credential):

- :func:`lane_usage` — the last successful call and the call counts of the
  last hour and day, from what is already recorded (``llm_call_log``,
  ``sleep_report_llm_usage``), platform-paid calls of the lane's provider and
  model only. Index-shaped: counts over the last day only, the last success as
  a newest-first ``LIMIT 1`` within a 30-day floor.

The lanes:

| lane | configured when | usage source |
|---|---|---|
| ``embedding`` | always | ``llm_call_log`` recall query embeddings |
| ``sleep_judge`` | the judge provider is set | Sleep reports' usage rows |
| ``supersede_verify`` | + ``SUPERSEDE_VERIFY_MODE=llm`` | ``llm_call_log`` judge rows |
| ``recall_selector`` | the judge provider is set | ``llm_call_log`` selector rows |
| ``analysis`` | ``MANAGED_LLM_PROVIDER`` is set | Analysis reports' usage rows |
| ``rerank`` | a self-hosted platform reranker | not ledgered (``usage_recorded: false``) |

The probe's own ledger rows are ``caller='admin'`` and never match a lane.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import and_, case, delete, func, null, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.llm_call_log import LLMCallLog
from models.operations_status import (
    LANE_MODEL_MAX_LENGTH,
    LANE_PROVIDER_MAX_LENGTH,
    LlmLane,
    OperationsLlmLane,
)
from models.sleep import SleepReport, SleepReportLLMUsage
from services.operations import job_status
from services.platform_credentials import (
    embedding_credential_present,
    embedding_lane,
    platform_llm_credential,
    rerank_lane,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from config.settings import Settings

ProbeKind = Literal["completion", "embedding", "rerank"]
UsageAttribution = Literal["platform", "includes_workspace_keys"]

# How far back the last successful call is looked for.
USAGE_LOOKBACK = timedelta(days=30)

_LOCAL_TIMEOUTS_SQL = text(
    "SELECT set_config('lock_timeout', '2000', true), set_config('statement_timeout', '5000', true)"
)

_JUDGE_LANES: tuple[LlmLane, ...] = ("sleep_judge", "supersede_verify", "recall_selector")


@dataclass(frozen=True)
class LaneSnapshot:
    """One configured platform lane as the leader sees it.

    Attributes:
        lane: The lane name.
        provider: Provider name (clipped to the column width).
        model: Model name (clipped to the column width).
        credential_present: Whether the leader holds the platform credential.
        probe_kind: What a probe of the lane calls.
    """

    lane: LlmLane
    provider: str
    model: str
    credential_present: bool
    probe_kind: ProbeKind


def _snapshot(
    lane: LlmLane, provider: str, model: str, *, credential_present: bool, kind: ProbeKind
) -> LaneSnapshot:
    return LaneSnapshot(
        lane=lane,
        provider=provider[:LANE_PROVIDER_MAX_LENGTH],
        model=model[:LANE_MODEL_MAX_LENGTH],
        credential_present=credential_present,
        probe_kind=kind,
    )


async def configured_platform_lanes(db: AsyncSession, settings: Settings) -> list[LaneSnapshot]:
    """The platform lanes this deployment configures, in lane order.

    Reads the judge pair through ``NeuralMemoryConfig.from_db`` (its
    ``neural_config`` row wins over env) and the rest from settings. Leader
    side only: it checks platform credentials.

    Args:
        db: A session for the judge config read.
        settings: The leader's settings.
    """
    from neural.config import NeuralMemoryConfig

    lanes: list[LaneSnapshot] = []
    emb = embedding_lane(settings)
    lanes.append(
        _snapshot(
            "embedding",
            emb.provider,
            emb.model,
            credential_present=embedding_credential_present(settings),
            kind="embedding",
        )
    )

    config = await NeuralMemoryConfig.from_db(db)
    judge_provider = (config.sleep_llm_provider or "").strip()
    judge_model = (config.sleep_llm_model or "").strip()
    if judge_provider:
        judge_credential = platform_llm_credential(judge_provider, settings) is not None
        for lane in _JUDGE_LANES:
            if lane == "supersede_verify" and config.supersede_verify_mode != "llm":
                continue
            lanes.append(
                _snapshot(
                    lane,
                    judge_provider,
                    judge_model,
                    credential_present=judge_credential,
                    kind="completion",
                )
            )

    managed_provider = settings.managed_llm_provider
    if managed_provider:
        lanes.append(
            _snapshot(
                "analysis",
                managed_provider,
                settings.managed_llm_model.strip(),
                credential_present=platform_llm_credential(managed_provider, settings) is not None,
                kind="completion",
            )
        )

    rerank = rerank_lane(settings)
    if rerank is not None:
        lanes.append(
            _snapshot(
                "rerank",
                rerank.provider,
                rerank.model,
                credential_present=True,
                kind="rerank",
            )
        )
    return lanes


async def read_platform_lanes(settings: Settings) -> list[LaneSnapshot]:
    """:func:`configured_platform_lanes` in its own short session (closed on return)."""
    session = job_status._new_session()
    try:
        async with session.begin():
            return await configured_platform_lanes(session, settings)
    finally:
        await session.close()


async def write_lane_snapshot(lanes: Sequence[LaneSnapshot]) -> None:
    """Replace the stored lane set with ``lanes`` (one transaction, own session).

    A lane whose provider or model changed loses its probe columns: they
    described another target. A stored lane missing from ``lanes`` is removed.

    Raises:
        Exception: Whatever the database raised.
    """
    table = OperationsLlmLane.__table__
    ordered = sorted(lanes, key=lambda s: s.lane)
    session = job_status._new_session()
    try:
        await session.execute(_LOCAL_TIMEOUTS_SQL)
        if ordered:
            stmt = pg_insert(table).values(
                [
                    {
                        "lane": s.lane,
                        "provider": s.provider,
                        "model": s.model,
                        "credential_present": s.credential_present,
                        "snapshot_at": func.now(),
                    }
                    for s in ordered
                ]
            )
            changed = or_(
                table.c.provider != stmt.excluded.provider,
                table.c.model != stmt.excluded.model,
            )

            def _reset(column: Any, empty: Any) -> Any:
                return case((changed, empty), else_=column)

            stmt = stmt.on_conflict_do_update(
                index_elements=[table.c.lane],
                set_={
                    "provider": stmt.excluded.provider,
                    "model": stmt.excluded.model,
                    "credential_present": stmt.excluded.credential_present,
                    "snapshot_at": stmt.excluded.snapshot_at,
                    "last_probe_at": _reset(table.c.last_probe_at, null()),
                    "last_probe_outcome": _reset(table.c.last_probe_outcome, null()),
                    "last_probe_latency_ms": _reset(table.c.last_probe_latency_ms, null()),
                    "last_probe_error_class": _reset(table.c.last_probe_error_class, null()),
                    "probe_consecutive_failures": _reset(table.c.probe_consecutive_failures, 0),
                    "last_probe_success_at": _reset(table.c.last_probe_success_at, null()),
                    "updated_at": func.now(),
                },
            )
            await session.execute(stmt)
            await session.execute(
                delete(table).where(table.c.lane.not_in([s.lane for s in ordered]))
            )
        else:
            await session.execute(delete(table))
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Reader side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneUsage:
    """What the ledgers say about one lane.

    Attributes:
        recorded: Whether the lane's calls are ledgered at all.
        attribution: ``platform``, or ``includes_workspace_keys`` when the
            ledger cannot tell platform-paid calls from own-key calls.
        last_success_at: The latest successful platform-paid call (30 days).
        calls_last_hour: Platform-paid calls in the last hour.
        calls_last_day: Platform-paid calls in the last day.
    """

    recorded: bool
    attribution: UsageAttribution
    last_success_at: datetime | None = None
    calls_last_hour: int | None = None
    calls_last_day: int | None = None


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _outcome() -> Any:
    return func.coalesce(func.jsonb_extract_path_text(LLMCallLog.call_metadata, "outcome"), "")


def _ledger_lane(lane: str) -> tuple[list[Any], Any] | None:
    """``llm_call_log`` predicates of a lane and its success predicate."""
    if lane == "embedding":
        return [LLMCallLog.caller == "recall", LLMCallLog.call_type == "embedding"], None
    if lane == "supersede_verify":
        return [LLMCallLog.caller == "supersede_verify"], _outcome() != "failed"
    if lane == "recall_selector":
        stage = func.jsonb_extract_path_text(LLMCallLog.call_metadata, "stage")
        return (
            [
                LLMCallLog.caller == "recall",
                LLMCallLog.call_type == "completion",
                stage == "llm_selector",
            ],
            _outcome().not_in(("timeout", "failed")),
        )
    return None


async def _ledger_usage(
    db: AsyncSession, lane: str, provider: str, model: str, now: datetime
) -> LaneUsage:
    spec = _ledger_lane(lane)
    assert spec is not None
    predicates, success = spec
    naive_now = _naive_utc(now)
    base = [
        *predicates,
        LLMCallLog.paid_by == "platform",
        LLMCallLog.provider == provider,
        LLMCallLog.model == model,
    ]
    last_success_q = (
        select(LLMCallLog.occurred_at)
        .where(*base, LLMCallLog.occurred_at >= naive_now - USAGE_LOOKBACK)
        .order_by(LLMCallLog.occurred_at.desc())
        .limit(1)
    )
    if success is not None:
        last_success_q = last_success_q.where(success)
    last_success = (await db.execute(last_success_q)).scalar_one_or_none()
    hour, day = (
        await db.execute(
            select(
                func.count().filter(LLMCallLog.occurred_at >= naive_now - timedelta(hours=1)),
                func.count(),
            ).where(*base, LLMCallLog.occurred_at >= naive_now - timedelta(days=1))
        )
    ).one()
    return LaneUsage(
        recorded=True,
        attribution="platform",
        last_success_at=_aware(last_success),
        calls_last_hour=int(hour or 0),
        calls_last_day=int(day or 0),
    )


async def _report_usage(
    db: AsyncSession,
    source: Literal["sleep", "analysis"],
    provider: str,
    model: str,
    now: datetime,
    attribution: UsageAttribution,
) -> LaneUsage:
    """Sleep / Analysis usage rows of platform-paid reports (by ``report_id``).

    A success is a usage row with calls and tokens under a report that did
    not fail; the reports are bounded by ``started_at`` (indexed).
    """
    naive_now = _naive_utc(now)
    u, r = SleepReportLLMUsage, SleepReport
    success = and_(
        u.calls > 0,
        (u.input_tokens + u.output_tokens) > 0,
        r.status != "failed",
    )
    last_success, hour, day = (
        await db.execute(
            select(
                func.max(u.created_at).filter(success),
                func.coalesce(
                    func.sum(u.calls).filter(u.created_at >= naive_now - timedelta(hours=1)), 0
                ),
                func.coalesce(
                    func.sum(u.calls).filter(u.created_at >= naive_now - timedelta(days=1)), 0
                ),
            )
            .select_from(r)
            .join(u, u.report_id == r.id)
            .where(
                r.started_at >= naive_now - USAGE_LOOKBACK,
                r.source == source,
                r.paid_by == "platform",
                u.provider == provider,
                u.model == model,
            )
        )
    ).one()
    return LaneUsage(
        recorded=True,
        attribution=attribution,
        last_success_at=_aware(last_success),
        calls_last_hour=int(hour or 0),
        calls_last_day=int(day or 0),
    )


async def lane_usage(
    db: AsyncSession,
    lane: str,
    provider: str,
    model: str,
    *,
    now: datetime,
    resolve_stored_byok_keys: bool,
) -> LaneUsage:
    """The ledger usage of one stored lane.

    Args:
        db: The request's (read-only) session.
        lane: The lane name.
        provider: The lane's provider as the leader stored it.
        model: The lane's model as the leader stored it.
        now: Timezone-aware current time.
        resolve_stored_byok_keys: ``RESOLVE_STORED_BYOK_KEYS``. Sleep records
            every run as platform-paid even when a workspace's own key served
            it, so with stored keys resolved the Sleep judge's usage includes
            own-key calls (``includes_workspace_keys``).
    """
    if lane == "rerank":
        return LaneUsage(recorded=False, attribution="platform")
    if lane == "sleep_judge":
        attribution: UsageAttribution = (
            "includes_workspace_keys" if resolve_stored_byok_keys else "platform"
        )
        return await _report_usage(db, "sleep", provider, model, now, attribution)
    if lane == "analysis":
        return await _report_usage(db, "analysis", provider, model, now, "platform")
    if _ledger_lane(lane) is None:
        return LaneUsage(recorded=False, attribution="platform")
    return await _ledger_usage(db, lane, provider, model, now)


__all__ = [
    "USAGE_LOOKBACK",
    "LaneSnapshot",
    "LaneUsage",
    "ProbeKind",
    "UsageAttribution",
    "configured_platform_lanes",
    "lane_usage",
    "read_platform_lanes",
    "write_lane_snapshot",
]
