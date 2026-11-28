"""Builders and the committed-test cleanup of the #3020 DB tests (not collected).

Flush-only tests drive the supersede-apply phase, the orchestrator or the
undo handler on ``db_session``, which rolls back. A test that commits (the
rollback handler, recall) uses :func:`committed_cleanup` through a fixture of
its own: it records the test's users, workspaces and contexts, deletes what
they own in foreign-key order after the test, commits, and then asserts that
no row of any table names one of those ids, so a leak fails here instead of
moving failures between CI shards.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from sqlalchemy import select, text

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory, NeuralMemoryEdge
from models.sleep import SleepReport
from services.supersede_verify import pair_fingerprint, text_fingerprint, verification_record

# ------------------------------------------------------------------ cleanup


@dataclass
class CommittedIds:
    """The ids a committed test creates; the cleanup deletes by them."""

    db: Any
    users: set[str] = field(default_factory=set)
    workspaces: set[UUID] = field(default_factory=set)
    contexts: set[UUID] = field(default_factory=set)


# Child tables first. Each entry: (table, WHERE over :users / :wss / :ctxs).
_DELETES: tuple[tuple[str, str], ...] = (
    ("neural_memory_edges", "context_id = ANY(:ctxs) OR user_id = ANY(:users)"),
    ("sleep_related_indexes", "context_id = ANY(:ctxs) OR workspace_id = ANY(:wss)"),
    ("sleep_reports", "context_id = ANY(:ctxs) OR user_id = ANY(:users)"),
    ("memory_access_events", "context_id = ANY(:ctxs) OR user_id = ANY(:users)"),
    ("retrieval_feedback", "context_id = ANY(:ctxs) OR user_id = ANY(:users)"),
    ("context_read_attributions", "context_id = ANY(:ctxs) OR user_id = ANY(:users)"),
    (
        "usage_stats",
        "context_id = ANY(:ctxs) OR user_id = ANY(:users) OR workspace_id = ANY(:wss)",
    ),
    (
        "workspace_operation_history",
        "context_id = ANY(:ctxs) OR workspace_id = ANY(:wss) OR actor_user_id = ANY(:users)",
    ),
    ("llm_call_log", "context_id = ANY(:ctxs) OR user_id = ANY(:users)"),
    ("memory_revisions", "context_id = ANY(:ctxs) OR actor_user_id = ANY(:users)"),
    ("graph_memory", "user_id = ANY(:users)"),
    ("memories", "context_id = ANY(:ctxs) OR user_id = ANY(:users)"),
    ("context_search_configs", "context_id = ANY(:ctxs)"),
    ("hub_tag_cache", "context_id = ANY(:ctxs)"),
    ("bm25_idf_drift_log", "context_id = ANY(:ctxs)"),
    ("context_members", "context_id = ANY(:ctxs) OR user_id = ANY(:users)"),
    ("contexts", "id = ANY(:ctxs)"),
    ("workspace_members", "workspace_id = ANY(:wss) OR user_id = ANY(:users)"),
    ("workspace_slot_states", "owner_user_id = ANY(:users)"),
    ("workspaces", "id = ANY(:wss)"),
    ("users", "user_id = ANY(:users)"),
)

_ID_COLUMNS = (
    "user_id",
    "workspace_id",
    "context_id",
    "created_by",
    "owner_user_id",
    "actor_user_id",
)


async def _leftovers(db, ids: CommittedIds) -> list[str]:
    """``table.column`` of every row still naming one of the test's ids."""
    values = [*ids.users, *(str(v) for v in ids.workspaces), *(str(v) for v in ids.contexts)]
    if not values:
        return []
    columns = (
        await db.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = ANY(:names) "
                "ORDER BY table_name, column_name"
            ),
            {"names": list(_ID_COLUMNS)},
        )
    ).all()
    left: list[str] = []
    for table, column in columns:
        # Identifiers from the catalog, quoted; the values are bound.
        count = (
            await db.execute(
                text(f'SELECT count(*) FROM "{table}" WHERE CAST("{column}" AS TEXT) = ANY(:v)'),
                {"v": values},
            )
        ).scalar_one()
        if count:
            left.append(f"{table}.{column}={count}")
    return left


@asynccontextmanager
async def committed_cleanup(db) -> AsyncIterator[CommittedIds]:
    """Yield the ids to record; afterwards delete their rows and assert none is left."""
    ids = CommittedIds(db=db)
    try:
        yield ids
    finally:
        await db.rollback()
        existing = {
            row[0]
            for row in (
                await db.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).all()
        }
        params = {
            "users": sorted(ids.users),
            "wss": sorted(ids.workspaces, key=str),
            "ctxs": sorted(ids.contexts, key=str),
        }
        for table, where in _DELETES:
            if table in existing:
                await db.execute(text(f"DELETE FROM {table} WHERE {where}"), params)
        await db.commit()
        left = await _leftovers(db, ids)
        await db.rollback()
        assert left == [], f"rows left behind by the test: {left}"


# ----------------------------------------------------------------- builders


@dataclass
class Scope:
    """A workspace (plan ``pro``) owned by ``owner``, and a context in it."""

    owner: str
    ws: Workspace
    ctx: Context

    @property
    def ws_id(self) -> str:
        return str(self.ws.id)

    @property
    def ctx_id(self) -> str:
        return str(self.ctx.id)


async def add_user(db, prefix: str = "sa", ids: CommittedIds | None = None) -> str:
    user_id = f"{prefix}_{uuid4().hex[:10]}"
    db.add(User(email=f"{user_id}@test.example", user_id=user_id, name=user_id, role="user"))
    await db.flush()
    if ids is not None:
        ids.users.add(user_id)
    return user_id


async def scope(db, *, ids: CommittedIds | None = None, sleep_mode: str = "full") -> Scope:
    owner = await add_user(db, ids=ids)
    ws = Workspace(
        id=uuid4(),
        name=f"sa-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"sa-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
        sleep_mode=sleep_mode,
    )
    db.add(ctx)
    await db.flush()
    if ids is not None:
        ids.workspaces.add(ws.id)
        ids.contexts.add(ctx.id)
    return Scope(owner=owner, ws=ws, ctx=ctx)


def memory(s: Scope, *, user_id: str | None = None, **fields) -> Memory:
    values = {
        "summary": f"memory {uuid4().hex[:8]}",
        "content": "body",
        "type": "fact",
        "importance": 0.5,
        "client": "pytest",
        "scope": "persistent",
        "embedding_status": "success",
        **fields,
    }
    return Memory(
        id=uuid4(),
        workspace_id=s.ws.id,
        context_id=s.ctx.id,
        user_id=user_id or s.owner,
        **values,
    )


def verified_candidate(
    older: Memory,
    *,
    newer_summary: str,
    newer_content: str,
    verdict: str = "supersedes",
    confidence: float = 0.9,
    similarity: float = 0.91,
) -> dict[str, Any]:
    """A stored suggestion as detection and the judge leave it, about these texts."""
    return {
        "memory_id": str(older.id),
        "similarity": similarity,
        "detected_at": "2026-01-01T00:00:00Z",
        "verification": verification_record(
            verdict,
            confidence,
            "the newer memory states the current value",
            model="judge-model",
            min_confidence=0.7,
            fingerprint=pair_fingerprint(
                SimpleNamespace(summary=older.summary, content=older.content),
                SimpleNamespace(summary=newer_summary, content=newer_content),
            ),
            old_fingerprint=text_fingerprint(older.summary, older.content),
            at="2026-01-01T00:00:00Z",
        ),
    }


async def verified_pair(
    db,
    s: Scope,
    *,
    created_at: datetime,
    older_fields: dict[str, Any] | None = None,
    older_user: str | None = None,
    older_after: bool = False,
    **candidate: Any,
) -> tuple[Memory, Memory]:
    """An older memory and a newer one carrying a verified suggestion naming it."""
    tag = uuid4().hex[:8]
    older_created = (
        created_at + timedelta(minutes=1) if older_after else created_at - timedelta(days=1)
    )
    older = memory(
        s,
        user_id=older_user,
        **{
            "summary": f"the office is on floor 3 ({tag})",
            "content": f"floor 3 since spring ({tag})",
            "created_at": older_created,
            # A field given here wins over the defaults above.
            **(older_fields or {}),
        },
    )
    newer_summary = f"the office moved to floor 5 ({tag})"
    newer_content = f"floor 5 since autumn ({tag})"
    newer = memory(
        s,
        summary=newer_summary,
        content=newer_content,
        created_at=created_at,
        supersede_candidate=verified_candidate(
            older, newer_summary=newer_summary, newer_content=newer_content, **candidate
        ),
    )
    db.add_all([older, newer])
    await db.flush()
    return newer, older


async def new_report(db, s: Scope, started_at: datetime) -> SleepReport:
    report = SleepReport(
        user_id=s.owner,
        workspace_id=s.ws.id,
        context_id=s.ctx.id,
        status="running",
        started_at=started_at,
    )
    db.add(report)
    await db.flush()
    return report


async def run_phase(db, s: Scope, report: SleepReport, *, max_memories: int = 200, coverage=None):
    """The phase alone, as the orchestrator runs it, without the orchestrator."""
    from services.sleep.coverage import LaneEntry
    from services.sleep.reporter import SleepBudget, SleepReporter
    from services.sleep.supersede_apply import SupersedeApplyPhase

    phase = SupersedeApplyPhase(db, coverage=coverage or LaneEntry())
    result = await phase.execute(
        None,
        s.owner,
        s.ws_id,
        s.ctx_id,
        SleepBudget(max_memories=max_memories),
        reporter=SleepReporter(db),
        report_id=report.id,
    )
    await db.flush()
    return phase, result


async def run_sleep(
    db,
    s: Scope,
    *,
    started_at: datetime,
    enabled: bool = True,
    max_memories: int = 200,
) -> SleepReport:
    """One Sleep run of the supersede-apply phase alone, through the orchestrator."""
    from neural.config import NeuralMemoryConfig
    from services.sleep.orchestrator import SleepOrchestrator
    from services.sleep.reporter import PhaseResult

    report = await new_report(db, s, started_at)
    config = NeuralMemoryConfig(
        sleep_supersede_apply_enabled=enabled,
        sleep_max_memories_per_run=max_memories,
        sleep_llm_provider="",
    )
    with ExitStack() as stack:
        stack.enter_context(patch("services.sleep.orchestrator.FULL_PHASES", {"supersede_apply"}))
        stack.enter_context(
            patch("services.sleep.orchestrator.sleep_suspended", return_value=False)
        )
        stack.enter_context(
            patch("services.sleep.orchestrator.slot_suspended", AsyncMock(return_value=False))
        )
        reindex = stack.enter_context(patch("services.sleep.orchestrator.ReindexPhase"))
        reindex.return_value.execute = AsyncMock(return_value=PhaseResult(phase_name="reindex"))
        await SleepOrchestrator(db).run(s.owner, s.ws_id, s.ctx_id, config=config, report=report)
    await db.flush()
    return report


async def add_edge(
    db,
    s: Scope,
    src: Memory,
    dst: Memory,
    edge_type: str = "supersedes",
    *,
    origin: str = "semantic",
    user_id: str | None = None,
    weight: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        NeuralMemoryEdge(
            user_id=user_id or s.owner,
            src_id=src.id,
            dst_id=dst.id,
            edge_type=edge_type,
            origin=origin,
            weight=weight,
            confidence=1.0,
            edge_metadata=metadata,
            workspace_id=s.ws.id,
            context_id=s.ctx.id,
        )
    )
    await db.flush()


async def edge_row(db, user_id: str, src_id: UUID, dst_id: UUID):
    """The ``(user, src, dst)`` edge row as columns, or None."""
    return (
        await db.execute(
            select(
                NeuralMemoryEdge.edge_type,
                NeuralMemoryEdge.origin,
                NeuralMemoryEdge.weight,
                NeuralMemoryEdge.confidence,
                NeuralMemoryEdge.edge_metadata,
                NeuralMemoryEdge.created_at,
            ).where(
                NeuralMemoryEdge.user_id == user_id,
                NeuralMemoryEdge.src_id == src_id,
                NeuralMemoryEdge.dst_id == dst_id,
            )
        )
    ).one_or_none()


async def candidate_column(db, memory_id: UUID):
    """``(is SQL NULL, value)`` of a memory's ``supersede_candidate``."""
    row = (
        await db.execute(
            text(
                "SELECT supersede_candidate IS NULL AS is_null, "
                "CAST(supersede_candidate AS TEXT) AS raw FROM memories WHERE id = :id"
            ),
            {"id": memory_id},
        )
    ).one()
    value = (
        await db.execute(select(Memory.supersede_candidate).where(Memory.id == memory_id))
    ).scalar_one()
    return row.is_null, row.raw, value


async def apply_actions(db, report_id: UUID) -> list[Any]:
    from models.sleep import SleepAction

    return list(
        (
            await db.execute(
                select(SleepAction)
                .where(
                    SleepAction.report_id == report_id,
                    SleepAction.action_type == "apply_supersede",
                )
                .order_by(SleepAction.id)
            )
        )
        .scalars()
        .all()
    )
