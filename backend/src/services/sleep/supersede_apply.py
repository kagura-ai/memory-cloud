"""Sleep Phase 4.5: apply judge-verified supersede suggestions (#3020).

When a memory is written the server may store a ``supersede_candidate`` on it
(the earlier memory it seems to replace), and the deployment's judge may
verify the pair. This phase turns a verified suggestion into the
``supersedes`` link an acceptance would make, newer -> older, and clears the
suggestion. It makes no model call. It runs only when the deployment switch
``sleep_supersede_apply_enabled`` is on, and only where the run's user may
write edges in the context.

What it applies: a stored verdict ``supersedes`` at or above the read path's
floor (``SUPERSEDE_VERIFY_MIN_CONFIDENCE``), still about the texts the judge
saw (each memory's summary and the head of its content), on an older memory
no longer than those heads (the verdict says nothing about text past them),
between two of the run user's own memories that dedup may
compare (``dedup_eligible``: live, not pinned, not a tool guardrail, not a
time memory, not owned by the resource indexer), the older one carrying no
exempt tag and its text not written after the newer one's (dedup's recency:
the later of creation and the last logged text edit, #3019; a pair the
revision log cannot order is left alone), with no ``supersedes`` or
``contradicts`` edge on the pair (either direction, any author) and no
declared edge of the run user from the newer to the older. A dismissed,
``parallel``, ``unclear``, unverified or stale suggestion is never applied,
and neither is one a rollback restored (``SLEEP_APPLY_REVERTED_KEY``).

Each pair is decided again under both rows' locks and written in a savepoint
of its own: the edge (metadata ``{source, report_id}``), the cleared
suggestion and one ``apply_supersede`` action land together or not at all.
The carriers are read in ``(created_at, id)`` order from the partition's
stored position (``services.sleep.coverage``), at most the phase's memory
allowance per run. ``undo_apply_supersede`` reverses one action in
``rollback_sleep_run``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import Text, and_, delete, func, null, or_, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import (
    EDGE_ORIGIN_DECLARED,
    EDGE_ORIGIN_SEMANTIC,
    EDGE_SOURCE_SLEEP_SUPERSEDE_APPLY,
    EDGE_TYPE_CONTRADICTS,
    EDGE_TYPE_SUPERSEDES,
    SLEEP_APPLY_REVERTED_KEY,
    Memory,
    NeuralMemoryEdge,
)
from repositories.neural_edge import NeuralEdgeRepository, sleep_applied_supersedes
from services.exempt_tags import carries_exempt_tag, exempt_tags, not_exempt_tagged_predicate
from services.memory_versions import (
    TextEditEvent,
    revision_log_began_query,
    text_edit_event,
    text_edit_events_query,
    text_edit_times,
)
from services.sleep.coverage import (
    FAILURE,
    PASSED,
    SETTLED,
    ApplyCoverage,
    LaneEntry,
    after_sweep_position,
)
from services.sleep.dedup_merge import dedup_eligible
from services.sleep.pair_write import (
    edited_version,
    locked_refusals,
    recency_key,
    recency_unknowable,
    write_sleep_supersedes_edge,
)
from services.sleep.reporter import PhaseResult, SleepBudget, SleepReporter, phase_outcome
from services.supersede_verify import (
    CONTENT_HEAD_CHARS,
    SUMMARY_CHARS,
    VERDICT_SUPERSEDES,
    is_verified,
    min_confidence_from_env,
    verdict_current,
)
from utils.datetime import utcnow
from utils.logger import get_logger

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig

logger = get_logger(__name__)

PHASE_NAME = "supersede_apply"
ACTION_APPLY_SUPERSEDE = "apply_supersede"
# The orchestrator's skip when the run already grades ``failed`` (every judge
# call raised): ``rollback_sleep_run`` does not take such a run.
SKIP_RUN_FAILED = "run_failed"

# Why a carrier read was not applied, in the order the checks run (the first
# that holds is counted).
REASON_MALFORMED = "malformed_candidate"
REASON_BELOW_FLOOR = "below_floor"
REASON_TARGET_INELIGIBLE = "target_ineligible"
REASON_NEWER_TARGET = "newer_target"
# dedup's keep-both reason for the same rule (#3019).
REASON_AMBIGUOUS_RECENCY = "ambiguous_recency"
REASON_VERDICT_STALE = "verdict_stale"
REASON_OLDER_PAST_JUDGED_HEAD = "older_past_judged_head"
REASON_ALREADY_LINKED = "already_linked"
REASON_CONTRADICTS = "contradicts"
REASON_DECLARED_EDGE_CONFLICT = "declared_edge_conflict"
REASON_CHANGED_SINCE_READ = "changed_since_read"
REASON_WRITE_FAILED = "write_failed"


class _PairRefused(Exception):
    """A pair refused inside its savepoint; the savepoint discards its writes."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def carrier_predicate() -> Any:
    """A stored verdict ``supersedes`` on a suggestion no rollback restored.

    The column is ``json`` (not ``jsonb``): path extraction only. A dismissal
    tombstone, a JSON ``null``, an unverified suggestion and the ``parallel``
    / ``unclear`` verdicts all fail it.
    """
    column = Memory.supersede_candidate
    return and_(
        column[("verification", "verdict")].as_string() == VERDICT_SUPERSEDES,
        column[SLEEP_APPLY_REVERTED_KEY].as_string().is_(None),
    )


def _target_id(candidate: Any, carrier_id: UUID) -> UUID | None:
    """The suggestion's older memory, or None when the suggestion is malformed.

    Malformed: not a dict, no parseable top-level ``memory_id``, no numeric
    ``similarity`` (the recall resolver's own requirement), or the carrier
    itself.
    """
    if not isinstance(candidate, dict):
        return None
    raw = candidate.get("memory_id")
    similarity = candidate.get("similarity")
    if not raw or isinstance(similarity, bool) or not isinstance(similarity, (int, float)):
        return None
    try:
        target_id = UUID(str(raw))
    except (ValueError, TypeError):
        return None
    return None if target_id == carrier_id else target_id


def _past_judged_head(row: Any) -> bool:
    """The older memory's text goes beyond what the judge saw.

    The judge is shown a memory's summary up to ``SUMMARY_CHARS`` and its
    content up to ``CONTENT_HEAD_CHARS``, and the verdict's fingerprints cover
    the same heads, so a correction appended past them leaves the verdict
    "current". An older memory that long is not linked on that verdict.
    """
    return len(row.summary or "") > SUMMARY_CHARS or (row.content_chars or 0) > CONTENT_HEAD_CHARS


def _edge_refusal(
    edges: Iterable[Any], *, user_id: str, newer_id: UUID, older_id: UUID
) -> str | None:
    """Rules 6-8 on the pair's edges (either direction, any author).

    A ``supersedes`` edge settles the pair (``already_linked``); a
    ``contradicts`` edge says neither is known to be current; the run user's
    own declared row newer -> older of another type is a person's link, which
    is never retyped. Any other row (a Hebbian association, a discovered
    ``related_to``, a k-NN seed) is retyped by the write and snapshotted.
    """
    pair = {newer_id, older_id}
    on_pair = [edge for edge in edges if {edge.src_id, edge.dst_id} == pair]
    if any(edge.edge_type == EDGE_TYPE_SUPERSEDES for edge in on_pair):
        return REASON_ALREADY_LINKED
    if any(edge.edge_type == EDGE_TYPE_CONTRADICTS for edge in on_pair):
        return REASON_CONTRADICTS
    if any(
        edge.user_id == user_id
        and edge.src_id == newer_id
        and edge.dst_id == older_id
        and edge.origin == EDGE_ORIGIN_DECLARED
        for edge in on_pair
    ):
        return REASON_DECLARED_EDGE_CONFLICT
    return None


class SupersedeApplyPhase:
    """Turn judge-verified supersede suggestions into ``supersedes`` links."""

    def __init__(self, db: AsyncSession, *, coverage: LaneEntry | None = None) -> None:
        self.db = db
        self.edge_repo = NeuralEdgeRepository(db)
        # The partition's sweep position as the last run left it; this run's
        # rows with their outcomes, which the orchestrator turns into the next.
        self._entry = coverage or LaneEntry()
        self.coverage_outcome: ApplyCoverage | None = None
        # #3019's revision reads, as dedup keeps them: the last logged text
        # edit of each edited memory read (None when no logged edit wrote its
        # text), the memories whose read failed, the doubt a session rollback
        # naming no block left, and the log start, read once a run if needed.
        self._text_edited_at: dict[UUID, datetime | None] = {}
        self._text_edit_unread: set[UUID] = set()
        self._text_edit_unsure: dict[UUID, datetime] = {}
        self._log_began_at: datetime | None = None
        self._log_began_read = False

    async def execute(
        self,
        config: NeuralMemoryConfig | Any,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        budget: SleepBudget,
        *,
        reporter: SleepReporter | None = None,
        report_id: UUID | None = None,
    ) -> PhaseResult:
        """Apply the verified suggestions of one partition, within the allowance."""
        result = PhaseResult(phase_name=PHASE_NAME)
        if not workspace_id or not context_id:
            result.skipped, result.skip_reason = True, "no_context"
            return result
        if report_id is None:
            raise ValueError("supersede_apply needs the run's report_id")
        reporter = reporter or SleepReporter(self.db)

        # Write access first, before any memory is read.
        if not await self._writable(user_id, workspace_id, context_id):
            result.skipped, result.skip_reason = True, "not_writable"
            return result

        floor = min_confidence_from_env()
        allowance = budget.max_memories - budget.memories_used
        if allowance <= 0:
            result.details = {"applied": 0, "floor": floor, **phase_outcome(0, budget_cut=True)}
            return result

        eligible = dedup_eligible(user_id, workspace_id, context_id)
        carriers = await self._read_carriers(eligible, allowance)
        more = len(carriers) > allowance
        carriers = carriers[:allowance]
        budget.consume(memories=len(carriers))
        outcomes: dict[UUID, str] = dict.fromkeys((row.id for row in carriers), FAILURE)
        self.coverage_outcome = ApplyCoverage(
            keys=[(row.created_at, row.id) for row in carriers],
            exhausted=not more,
            outcomes=outcomes,
        )

        exempt = exempt_tags()
        targets_of = {row.id: _target_id(row.supersede_candidate, row.id) for row in carriers}
        targets = await self._read_targets(
            {tid for tid in targets_of.values() if tid is not None}, eligible, exempt
        )
        edges = await self._read_pair_edges({row.id for row in carriers} | set(targets))
        await self._load_text_edit_times([*carriers, *targets.values()])

        reasons: dict[str, int] = {}
        failed: list[UUID] = []
        applied = 0
        for carrier in carriers:
            candidate = carrier.supersede_candidate
            target_id = targets_of[carrier.id]
            target = targets.get(target_id) if target_id is not None else None
            reason = await self._refusal(
                carrier, candidate, target_id, target, edges, user_id=user_id, floor=floor
            )
            if reason is None and target is not None:
                try:
                    await self._apply_pair(
                        carrier,
                        target,
                        candidate,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        context_id=context_id,
                        exempt=exempt,
                        reporter=reporter,
                        report_id=report_id,
                    )
                except _PairRefused as refused:
                    reason = refused.reason
                except Exception as e:
                    logger.warning(
                        "sleep_supersede_apply_write_failed",
                        newer_id=str(carrier.id),
                        older_id=str(target.id),
                        error_type=type(e).__name__,
                    )
                    reason = REASON_WRITE_FAILED
                    failed.append(carrier.id)
            if reason is None:
                applied += 1
            else:
                reasons[reason] = reasons.get(reason, 0) + 1
            if reason != REASON_WRITE_FAILED:
                outcomes[carrier.id] = SETTLED
        # A failure of its own while the same kind of work succeeded moves on;
        # otherwise the position stops at it (the stall rule passes it).
        for carrier_id in failed:
            outcomes[carrier_id] = PASSED if applied else FAILURE

        result.memories_processed = len(carriers)
        result.operation_failures = len(failed)
        if carriers and len(failed) == len(carriers):
            result.success = False
            result.error = "every supersede write failed"
        result.details = {
            "applied": applied,
            "floor": floor,
            **phase_outcome(len(carriers), reasons, budget_cut=more),
        }
        return result

    async def _writable(self, user_id: str, workspace_id: str, context_id: str) -> bool:
        """The accept path's write probe; a suspended workspace is not writable."""
        from services.supersede_acceptance import can_write_edges
        from utils.exceptions import WorkspaceSlotSuspendedError

        try:
            return await can_write_edges(
                self.db, user_id=user_id, workspace_id=UUID(workspace_id), context_id=context_id
            )
        except WorkspaceSlotSuspendedError:
            return False

    async def _read_carriers(self, eligible: Any, allowance: int) -> list[Any]:
        """The next ``allowance + 1`` carriers after the stored position.

        Column selects only: the session may hold the same ``Memory``
        instances from earlier phases, whose attributes can be older than the
        row.
        """
        stmt = select(
            Memory.id,
            Memory.user_id,
            Memory.workspace_id,
            Memory.context_id,
            Memory.created_at,
            Memory.version,
            Memory.summary,
            func.substr(Memory.content, 1, CONTENT_HEAD_CHARS).label("content_head"),
            Memory.supersede_candidate,
        ).where(eligible, carrier_predicate())
        position = self._entry.position
        if position is not None:
            stmt = stmt.where(after_sweep_position(position))
        stmt = stmt.order_by(Memory.created_at.asc(), Memory.id.asc()).limit(allowance + 1)
        return list((await self.db.execute(stmt)).all())

    async def _read_targets(
        self, ids: set[UUID], eligible: Any, exempt: frozenset[str]
    ) -> dict[UUID, Any]:
        """The suggestions' older memories that pass the same rule, and no exempt tag.

        A target not returned is ``target_ineligible``: gone, another user's
        or context's, pinned, a tool guardrail, a time memory, owned by the
        resource indexer or exempt-tagged.
        """
        if not ids:
            return {}
        rows = await self.db.execute(
            select(
                Memory.id,
                Memory.user_id,
                Memory.context_id,
                Memory.created_at,
                # Only tells whether an edit the revision log does not show
                # could be later than the newer memory (``recency_unknowable``).
                Memory.updated_at,
                Memory.version,
                Memory.summary,
                func.substr(Memory.content, 1, CONTENT_HEAD_CHARS).label("content_head"),
                func.char_length(Memory.content).label("content_chars"),
                Memory.tags,
            ).where(Memory.id.in_(ids), eligible, not_exempt_tagged_predicate(exempt))
        )
        return {row.id: row for row in rows.all()}

    async def _read_pair_edges(self, ids: set[UUID]) -> list[Any]:
        """Every edge between two of ``ids``, any author, any scope.

        By the memories alone, as recall's shadowing reads ``supersedes``
        edges: an edge a moved memory left in another workspace or context
        still links the pair.
        """
        if len(ids) < 2:
            return []
        rows = await self.db.execute(
            select(
                NeuralMemoryEdge.user_id,
                NeuralMemoryEdge.src_id,
                NeuralMemoryEdge.dst_id,
                NeuralMemoryEdge.edge_type,
                NeuralMemoryEdge.origin,
            ).where(
                NeuralMemoryEdge.src_id.in_(ids),
                NeuralMemoryEdge.dst_id.in_(ids),
            )
        )
        return list(rows.all())

    async def _load_text_edit_times(self, rows: Iterable[Any]) -> None:
        """Read when each edited memory of the page last had its text written.

        Dedup's read (#3019): a memory at version 1 was never edited through a
        versioned path, so its creation time is its recency and nothing is
        read; the others' ``updated`` revisions are read in one statement and
        walked by ``text_edit_times``. A read that fails is rolled back to its
        savepoint and leaves those memories at their creation time, marked
        unread (``recency_unknowable`` then leaves a pair alone when an older
        one's stamp says an edit could make it the newer one).
        """
        ids = [
            row.id
            for row in rows
            if edited_version(row.version) and row.id not in self._text_edited_at
        ]
        if not ids:
            return
        try:
            async with self.db.begin_nested():
                found = (await self.db.execute(text_edit_events_query(ids))).all()
        except Exception as e:
            logger.warning(
                "sleep_supersede_apply_text_edit_times_unavailable",
                count=len(ids),
                error_type=type(e).__name__,
            )
            found = []
            self._text_edit_unread.update(ids)
        events: dict[UUID, list[TextEditEvent]] = {}
        for row in found:
            events.setdefault(row.memory_id, []).append(text_edit_event(row))
        for memory_id in ids:
            times = text_edit_times(events.get(memory_id, ()))
            self._text_edited_at[memory_id] = times.last
            if times.unsure_until is not None:
                self._text_edit_unsure[memory_id] = times.unsure_until

    async def _revision_log_began(self) -> datetime | None:
        """When the deployment's revision log began, read once a run (#3019).

        Made only when a pair needs it. A log with no row yet, or a read that
        fails, is None: nothing tells when logging began, so it orders no pair.
        """
        if not self._log_began_read:
            self._log_began_read = True
            value: object = None
            try:
                async with self.db.begin_nested():
                    value = (await self.db.execute(revision_log_began_query())).scalar()
            except Exception as e:
                logger.warning(
                    "sleep_supersede_apply_revision_log_start_unavailable",
                    error_type=type(e).__name__,
                )
            self._log_began_at = value if isinstance(value, datetime) else None
        return self._log_began_at

    async def _direction_refusal(self, carrier: Any, target: Any) -> str | None:
        """Rule 4: the older memory's text is not newer than the newer one's.

        Recency is dedup's (``recency_key``): the later of creation and the
        last logged text edit. An older memory edited after the newer one was
        written is ``newer_target``, at any precision (equal times pass, as on
        the accept path); one whose recency the revision log cannot tell
        under dedup's keep-both conditions is ``ambiguous_recency``.
        """
        newer = recency_key(carrier.created_at, self._text_edited_at.get(carrier.id))
        older = recency_key(target.created_at, self._text_edited_at.get(target.id))
        if newer is None or older is None or older > newer:
            return REASON_NEWER_TARGET
        if await recency_unknowable(
            newer,
            loser_text_edited_at=self._text_edited_at.get(target.id),
            loser_unsure_until=self._text_edit_unsure.get(target.id),
            loser_updated_at=target.updated_at,
            loser_unread=target.id in self._text_edit_unread,
            log_began=self._revision_log_began,
        ):
            return REASON_AMBIGUOUS_RECENCY
        return None

    async def _refusal(
        self,
        carrier: Any,
        candidate: Any,
        target_id: UUID | None,
        target: Any,
        edges: Iterable[Any],
        *,
        user_id: str,
        floor: float,
    ) -> str | None:
        """The rules on the values read, in order; None when the pair may be written."""
        if target_id is None:
            return REASON_MALFORMED
        if not is_verified(candidate, min_confidence=floor):
            return REASON_BELOW_FLOOR
        if target is None:
            return REASON_TARGET_INELIGIBLE
        direction = await self._direction_refusal(carrier, target)
        if direction is not None:
            return direction
        if not verdict_current(
            candidate,
            old_summary=target.summary,
            old_content_head=target.content_head,
            new_summary=carrier.summary,
            new_content_head=carrier.content_head,
        ):
            return REASON_VERDICT_STALE
        if _past_judged_head(target):
            return REASON_OLDER_PAST_JUDGED_HEAD
        return _edge_refusal(edges, user_id=user_id, newer_id=carrier.id, older_id=target.id)

    async def _apply_pair(
        self,
        carrier: Any,
        target: Any,
        candidate: Mapping[str, Any],
        *,
        user_id: str,
        workspace_id: str,
        context_id: str,
        exempt: frozenset[str],
        reporter: SleepReporter,
        report_id: UUID,
    ) -> None:
        """Decide once more under both rows' locks, then write, in one savepoint.

        Raises:
            _PairRefused: the locked state refused the pair; nothing is kept.
        """
        async with self.db.begin_nested():
            reasons, locked = await locked_refusals(
                self.db, (("newer", carrier), ("older", target))
            )
            if reasons:
                raise _PairRefused(REASON_CHANGED_SINCE_READ)
            newer, older = locked[carrier.id], locked[target.id]
            # A dismissal, a re-detection, an accept or a re-judgement since
            # the read; an exempt tag added since; a text edited without a
            # version move (the verdict is checked again on the locked texts),
            # or the older memory grown past the judged heads.
            if (
                newer.supersede_candidate != candidate
                or carries_exempt_tag(older.tags, exempt)
                or _past_judged_head(older)
                or not verdict_current(
                    newer.supersede_candidate,
                    old_summary=older.summary,
                    old_content_head=older.content_head,
                    new_summary=newer.summary,
                    new_content_head=newer.content_head,
                )
            ):
                raise _PairRefused(REASON_CHANGED_SINCE_READ)
            # Both rows stay locked until the run commits (through the
            # reindex), but an edge write takes no memory lock: an edge another
            # account writes on the pair after this read is not read again (a
            # person's older -> newer link has another unique key and can then
            # stand next to this one). The run user's own newer -> older row is
            # guarded by the upsert (``protect_declared_link``).
            edge_reason = _edge_refusal(
                await self._read_pair_edges({carrier.id, target.id}),
                user_id=user_id,
                newer_id=carrier.id,
                older_id=target.id,
            )
            if edge_reason is not None:
                raise _PairRefused(edge_reason)

            prior_edge, edge = await write_sleep_supersedes_edge(
                self.edge_repo,
                user_id=user_id,
                newer_id=carrier.id,
                older_id=target.id,
                workspace_id=workspace_id,
                context_id=context_id,
                edge_metadata={
                    "source": EDGE_SOURCE_SLEEP_SUPERSEDE_APPLY,
                    "report_id": str(report_id),
                },
                protect_declared_link=True,
                prior_in_any_scope=True,
            )
            if edge.edge_type != EDGE_TYPE_SUPERSEDES or edge.origin != EDGE_ORIGIN_SEMANTIC:
                # A declared row appeared since the re-check and the upsert's
                # guard kept its type and origin (it still overwrote weight and
                # metadata): the savepoint discards the write.
                self.db.expire(edge)
                raise _PairRefused(
                    REASON_ALREADY_LINKED
                    if edge.edge_type == EDGE_TYPE_SUPERSEDES
                    else REASON_DECLARED_EDGE_CONFLICT
                )

            # Clearing a suggestion is not an edit: neither updated_at nor
            # version moves (as the accept path's clear).
            await self.db.execute(
                update(Memory).where(Memory.id == carrier.id).values(supersede_candidate=null())
            )
            verification = candidate.get("verification") or {}
            await reporter.add_action(
                report_id=report_id,
                phase=PHASE_NAME,
                action_type=ACTION_APPLY_SUPERSEDE,
                memory_id=carrier.id,
                target_id=target.id,
                details={
                    "prior_candidate": newer.supersede_candidate,
                    "prior_edge": prior_edge,
                    "verdict_confidence": float(verification.get("confidence")),
                    "similarity": float(candidate["similarity"]),
                },
            )
        logger.info(
            "sleep_supersede_applied",
            newer_id=str(carrier.id),
            older_id=str(target.id),
            retyped_prior_edge=prior_edge is not None,
        )


def _applied_link(user_id: str, action: Any) -> Any:
    """The edge an ``apply_supersede`` action wrote, while it still carries the
    run's marker: the run user's ``newer -> older`` ``supersedes`` row, origin
    ``semantic``, metadata naming this phase and this report. A row a person
    re-declared since (origin ``declared``) or any writer rewrote without the
    marker no longer matches."""
    metadata = sql_cast(NeuralMemoryEdge.edge_metadata, JSONB)
    return and_(
        NeuralMemoryEdge.user_id == user_id,
        NeuralMemoryEdge.src_id == action.memory_id,
        NeuralMemoryEdge.dst_id == action.target_id,
        sleep_applied_supersedes(),
        metadata["report_id"].astext == str(action.report_id),
    )


def _rowcount(result: Any) -> int:
    # DML through AsyncSession.execute() is a CursorResult at runtime (#1442).
    return cast(CursorResult[Any], result).rowcount or 0


async def undo_apply_supersede(
    db: AsyncSession, action: Any, ctx: Any, summary: dict[str, Any]
) -> None:
    """Reverse one ``apply_supersede`` action in ``rollback_sleep_run``.

    The link is removed (or the edge it retyped put back from
    ``details.prior_edge``) only while it is the one this run wrote. Then the
    suggestion is restored from ``details.prior_candidate``, with
    ``SLEEP_APPLY_REVERTED_KEY`` set to this report, only into an empty
    column: a suggestion detected or dismissed since, or a memory forgotten
    since, is left as it is. A link changed since is a person's or another
    writer's decision and stands (``supersede_links_kept``); that is not an
    error. Counters are added only when used, so a run without such actions
    returns the summary it returned before #3020.
    """
    details = action.details or {}
    marker = _applied_link(ctx.user_id, action)
    prior_edge = details.get("prior_edge")
    if isinstance(prior_edge, dict) and prior_edge:
        result = await db.execute(
            update(NeuralMemoryEdge)
            .where(marker)
            .values(
                edge_type=prior_edge.get("edge_type") or "neural_association",
                origin=prior_edge.get("origin") or "hebbian",
                weight=prior_edge.get("weight", 0.0),
                confidence=prior_edge.get("confidence", 1.0),
                edge_metadata=prior_edge.get("edge_metadata"),
                last_updated=utcnow(),
            )
        )
    else:
        result = await db.execute(delete(NeuralMemoryEdge).where(marker))
    if not _rowcount(result):
        summary["supersede_links_kept"] = summary.get("supersede_links_kept", 0) + 1
        return
    summary["supersede_links_reversed"] = summary.get("supersede_links_reversed", 0) + 1

    prior_candidate = details.get("prior_candidate")
    if not isinstance(prior_candidate, dict):
        return
    column = Memory.supersede_candidate
    restored = await db.execute(
        update(Memory)
        .where(
            Memory.id == action.memory_id,
            Memory.user_id == ctx.user_id,
            Memory.deleted_at.is_(None),
            # #2022: no suggestion is SQL NULL or the JSON null.
            or_(column.is_(None), sql_cast(column, Text) == "null"),
        )
        .values(
            supersede_candidate={
                **prior_candidate,
                SLEEP_APPLY_REVERTED_KEY: str(action.report_id),
            }
        )
    )
    if _rowcount(restored):
        summary["supersede_candidates_restored"] = (
            summary.get("supersede_candidates_restored", 0) + 1
        )
