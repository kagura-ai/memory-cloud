"""#3019, on Postgres: dedup's newer-wins compares creation and the last text edit.

A memory that was never edited is compared by its creation time, not by
``updated_at``: Sleep and other writes that change no text stamp that column
too (importance re-evaluation did, before it kept its own clock), so an old
stamped memory used to outrank a newer duplicate and the newer fact was the
one retired. An edit's time comes from the revision log. When the log cannot
tell whether a stamp is an edit that would make the losing row the newer one
(it began after the other row's recency, or nothing tells when it began),
both rows stay (owner decision 2026-11-27). The test database's log start is
set per run (``_Scope.run(log_began=...)``) unless a test reads the real one.

The vector store, the embedding service and the judge are faked; the window,
the eligibility checks, the revision read, the winner rules and the merge run
on Postgres. Tests that commit (the edit, the session rollback and the context
merge commit) delete every row they wrote: a later file in the same CI shard
reads whole tables. Rows are dated explicitly; ``now`` is taken inside each
test.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import DateTime, delete, literal, select

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import (
    DELETED_BY_SLEEP_MERGE,
    EDGE_TYPE_SUPERSEDES,
    Memory,
    MemoryRevision,
    NeuralMemoryEdge,
)
from models.operation_history import OperationHistory
from models.schemas import UpdateMemoryRequest
from neural.config import NeuralMemoryConfig
from services.memory_versions import text_edit_event, text_edit_events_query
from services.sleep import dedup_merge
from services.sleep.dedup_merge import DedupMergePhase
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow

# (owner, workspace id) of every scope the running test created.
_created: list[tuple[str, UUID]] = []

# ``_Scope.run``: read the deployment's real revision log start.
_READ: Any = object()


def _log_start_query(at: datetime | None):
    """A stand-in for ``revision_log_began_query`` answering ``at`` on Postgres."""
    return lambda: select(literal(at, DateTime()))


@pytest.fixture(autouse=True)
async def _delete_committed_rows(db_session):
    """Delete every row the test committed, by its own owners and workspaces."""
    _created.clear()
    yield
    owners = [owner for owner, _ in _created]
    workspaces = [ws_id for _, ws_id in _created]
    await db_session.rollback()
    if workspaces:
        await db_session.execute(
            delete(OperationHistory).where(OperationHistory.workspace_id.in_(workspaces))
        )
        # memory_revisions, edges and session rollback rows go with their
        # memory / context (FK cascade)
        await db_session.execute(delete(Memory).where(Memory.workspace_id.in_(workspaces)))
        await db_session.execute(delete(Context).where(Context.workspace_id.in_(workspaces)))
        await db_session.execute(
            delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(workspaces))
        )
        await db_session.execute(delete(Workspace).where(Workspace.id.in_(workspaces)))
        await db_session.execute(delete(User).where(User.user_id.in_(owners)))
        await db_session.commit()
    _created.clear()


class _Scope:
    """One user's context, its memories with one stored vector each, and a
    dedup run over it with the vector store and the judge faked."""

    def __init__(self, db, owner: str, ws: Workspace, ctx: Context, dims: int = 16) -> None:
        self.db, self.owner, self.ws, self.ctx, self.dims = db, owner, ws, ctx, dims
        self.vectors: dict[str, list[float]] = {}
        self.prompts: list[str] = []

    @classmethod
    async def create(cls, db) -> _Scope:
        owner = f"win_{uuid4().hex[:8]}"
        db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
        await db.flush()
        ws = Workspace(
            id=uuid4(),
            name=f"win-ws-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=5000,
            weekly_api_limit=25000,
        )
        db.add(ws)
        await db.flush()
        _created.append((owner, ws.id))
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
        scope = cls(db, owner, ws, await cls._context(db, owner, ws))
        return scope

    @staticmethod
    async def _context(db, owner: str, ws: Workspace) -> Context:
        ctx = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"win-ctx-{uuid4().hex[:8]}",
            created_by=owner,
            is_private=False,
            trust_tier="trusted",
        )
        db.add(ctx)
        await db.flush()
        return ctx

    def axis(self, index: int) -> list[float]:
        vector = [0.0] * self.dims
        vector[index] = 1.0
        return vector

    async def add(
        self, *, created_at: datetime, vector: list[float], ctx: Context | None = None, **fields
    ) -> Memory:
        values = {
            "summary": f"memory {uuid4().hex[:8]}",
            "content": "c",
            "type": "note",
            "importance": 0.5,
            "client": "pytest",
            "scope": "working",
            "embedding_status": "success",
            **fields,
        }
        row = Memory(
            id=uuid4(),
            workspace_id=self.ws.id,
            context_id=(ctx or self.ctx).id,
            user_id=self.owner,
            created_at=created_at,
            **values,
        )
        self.db.add(row)
        await self.db.flush()
        self.vectors[str(row.id)] = vector
        return row

    async def _embedding_service(self) -> MagicMock:
        rows = (
            await self.db.execute(
                select(Memory.id, Memory.summary).where(Memory.context_id == self.ctx.id)
            )
        ).all()
        by_summary = {
            summary: self.vectors[str(memory_id)]
            for memory_id, summary in rows
            if str(memory_id) in self.vectors
        }
        service = MagicMock()
        service.provider = "test"
        service.model = "test"
        service.embed_with_usage = AsyncMock(side_effect=lambda text, **_: (by_summary[text], 1))
        return service

    async def _search(self, *, query_vector, limit, filters, **_):
        hits = []
        for point_id, vector in self.vectors.items():
            score = sum(a * b for a, b in zip(query_vector, vector, strict=True))
            if score >= filters["score_threshold"]:
                hits.append({"id": point_id, "score": score})
        return sorted(hits, key=lambda h: -h["score"])[:limit]

    async def _stored_vectors(self, collection, point_ids):
        return {pid: self.vectors[pid] for pid in point_ids if pid in self.vectors}

    def _judge(self, pick: Memory) -> MagicMock:
        """A judge that calls every pair a duplicate and picks ``pick``."""

        async def answer(*, prompt: str, **_: Any) -> MagicMock:
            self.prompts.append(prompt)
            # one block per label: from its header to the next header
            heads = sorted(
                (prompt.index(f"[{label}] type="), label)
                for label in "ABCDE"
                if f"[{label}] type=" in prompt
            )
            ends = [start for start, _ in heads[1:]] + [len(prompt)]
            blocks = {
                label: prompt[start:end] for (start, label), end in zip(heads, ends, strict=True)
            }
            labels = list(blocks)
            winner = next(label for label in labels if pick.summary in blocks[label])
            loser = next(label for label in labels if label != winner)
            response = MagicMock()
            response.parsed = {
                "judgments": [{"pair": [winner, loser], "verdict": "merge", "winner": winner}]
            }
            response.total_tokens = 10
            response.input_tokens = 6
            response.output_tokens = 4
            response.cached_input_tokens = 0
            response.cache_write_tokens = 0
            response.provider = "openai"
            response.model = "m"
            response.tokenizer_version = "v1"
            return response

        service = MagicMock()
        service.complete_json = AsyncMock(side_effect=answer)
        return service

    async def run(
        self,
        *,
        judge_picks: Memory | None = None,
        shadow: bool = False,
        log_began: Any = _READ,
    ):
        """One dedup run over the context: the judge picks ``judge_picks``, or
        the rule path (no model) when it is None. ``log_began`` is when the
        revision log began (None: it has no row); left out, the real start is
        read from ``memory_revisions``."""
        config = NeuralMemoryConfig(
            sleep_llm_provider="openai" if judge_picks is not None else "",
            sleep_llm_model="m",
            sleep_dedup_enabled=True,
            sleep_max_memories_per_run=50,
            sleep_dedup_similarity_threshold=0.92,
            sleep_dedup_supersede_enabled=shadow,
        )
        llm = self._judge(judge_picks) if judge_picks is not None else MagicMock()
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    dedup_merge, "EmbeddingService", return_value=await self._embedding_service()
                )
            )
            stack.enter_context(patch.object(dedup_merge, "search_memories_qdrant", self._search))
            stack.enter_context(
                patch.object(dedup_merge, "retrieve_dense_vectors", self._stored_vectors)
            )
            if log_began is not _READ:
                stack.enter_context(
                    patch.object(
                        dedup_merge, "revision_log_began_query", _log_start_query(log_began)
                    )
                )
            phase = DedupMergePhase(self.db, llm)
            result = await phase.execute(
                config, self.owner, str(self.ws.id), str(self.ctx.id), SleepBudget()
            )
        await self.db.flush()
        return result, phase

    async def state(self, *rows: Memory) -> dict[UUID, Any]:
        found = (
            await self.db.execute(
                select(Memory.id, Memory.deleted_at, Memory.deleted_by).where(
                    Memory.id.in_([row.id for row in rows])
                )
            )
        ).all()
        return {row.id: row for row in found}


async def _old_stamped_and_newer(scope: _Scope, now: datetime) -> tuple[Memory, Memory]:
    """A: six months old, stamped last week without an edit (version 1). B:
    written two months ago, never stamped (``updated_at`` NULL)."""
    a = await scope.add(
        created_at=now - timedelta(days=180),
        updated_at=now - timedelta(days=7),
        vector=scope.axis(0),
        summary="the deploy target is blue",
    )
    b = await scope.add(
        created_at=now - timedelta(days=60),
        vector=scope.axis(0),
        summary="the deploy target is green",
    )
    return a, b


@pytest.mark.asyncio
@pytest.mark.parametrize("judged", [True, False], ids=["judge", "rule_path"])
async def test_a_row_stamped_without_an_edit_loses_to_a_newer_duplicate(db_session, judged):
    """The owner decision's scenario, in remove mode: the judge picks the old
    stamped row (or the rule path decides at equal importance); the newer row
    stays live and the old one is the Sleep tombstone."""
    now = utcnow()
    scope = await _Scope.create(db_session)
    a, b = await _old_stamped_and_newer(scope, now)
    assert a.version == 1 and b.version == 1 and b.updated_at is None

    # The log began before B was written: an edit of A after B would be in it.
    result, phase = await scope.run(
        judge_picks=a if judged else None, log_began=now - timedelta(days=90)
    )

    assert result.details["merged"] == 1
    state = await scope.state(a, b)
    assert state[b.id].deleted_at is None
    assert state[a.id].deleted_by == DELETED_BY_SLEEP_MERGE
    if judged:
        assert result.details["winner_overrides"] == 1
        assert f"last_updated={a.created_at:%Y-%m-%d %H:%M}," in scope.prompts[0]
    assert phase._text_edited_at == {}


@pytest.mark.asyncio
async def test_shadow_mode_records_the_newer_row_as_the_successor(db_session):
    """Shadow mode follows the same decision: B supersedes A, both stay live."""
    now = utcnow()
    scope = await _Scope.create(db_session)
    a, b = await _old_stamped_and_newer(scope, now)

    result, _ = await scope.run(judge_picks=a, shadow=True, log_began=now - timedelta(days=90))

    assert result.details["merged"] == 1
    edges = (
        await db_session.execute(
            select(NeuralMemoryEdge.src_id, NeuralMemoryEdge.dst_id).where(
                NeuralMemoryEdge.user_id == scope.owner,
                NeuralMemoryEdge.edge_type == EDGE_TYPE_SUPERSEDES,
            )
        )
    ).all()
    assert [(row.src_id, row.dst_id) for row in edges] == [(b.id, a.id)]
    state = await scope.state(a, b)
    assert state[a.id].deleted_at is None and state[b.id].deleted_at is None


def _service(db):
    from services.memory_service import MemoryService

    svc = MemoryService(db)
    svc._ensure_capacity = AsyncMock()  # type: ignore[method-assign]
    svc._lint_write = AsyncMock(return_value=[])  # type: ignore[method-assign]
    svc._update_sync_qdrant_payload = AsyncMock()  # type: ignore[method-assign]
    return svc


async def _edit(scope: _Scope, memory: Memory, **fields: Any) -> None:
    """``update_memory`` in place, as the API runs it (it commits)."""
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ),
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
    ):
        await _service(scope.db).update_memory(
            UpdateMemoryRequest(memory_id=memory.id, **fields),
            scope.owner,
            current_context_id=scope.ctx.id,
            current_workspace_id=scope.ws.id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fields", "a_wins"),
    [
        ({"summary": "the deploy target is blue, checked again"}, True),
        ({"content": "the body was rewritten"}, True),
        ({"importance": 0.6}, False),
        ({"tags": ["checked"]}, False),
    ],
    ids=["summary", "content", "importance", "tags"],
)
async def test_a_real_edit_of_the_text_makes_the_old_row_win(db_session, fields, a_wins):
    """An ``update_memory`` that changes the text is the memory's recency, so
    the old row wins over a duplicate written before the edit even when the
    judge picks the duplicate; an edit of metadata only leaves the creation
    time, and the duplicate wins even when the judge picks the old row."""
    now = utcnow()
    scope = await _Scope.create(db_session)
    a, b = await _old_stamped_and_newer(scope, now)
    await db_session.commit()

    await _edit(scope, a, **fields)
    await db_session.refresh(a)
    assert a.version == 2
    # the edit's stamp; the merge winner's tag union stamps the row again
    edit_stamp = a.updated_at

    result, phase = await scope.run(
        judge_picks=b if a_wins else a, log_began=now - timedelta(days=90)
    )

    assert result.details["merged"] == 1
    assert result.details["winner_overrides"] == 1
    state = await scope.state(a, b)
    winner, loser = (a, b) if a_wins else (b, a)
    assert state[winner.id].deleted_at is None
    assert state[loser.id].deleted_by == DELETED_BY_SLEEP_MERGE
    edited_at = phase._text_edited_at[a.id]
    if a_wins:
        assert edited_at == edit_stamp
    else:
        assert edited_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("log_began_days_ago", "merged"),
    [(90, True), (30, False)],
    ids=["log_began_before_later", "log_began_after_later"],
)
async def test_a_context_merge_copy_loses_to_a_row_created_later_in_the_target(
    db_session, log_began_days_ago, merged
):
    """``merge_contexts`` copies a row with its ``created_at`` and a fresh
    ``updated_at`` at version 1: the copy is as old as the row it copies. When
    the log began before the later row, the copy loses (the documented limit:
    an edit made in the source before the merge is not seen); when it began
    after, the copy's stamp could be an edit and both stay."""
    from services.context_service import ContextService

    now = utcnow()
    scope = await _Scope.create(db_session)
    source = await _Scope._context(db_session, scope.owner, scope.ws)
    original = await scope.add(
        created_at=now - timedelta(days=180),
        vector=scope.axis(0),
        ctx=source,
        summary="the deploy target is blue",
    )
    later = await scope.add(
        created_at=now - timedelta(days=60),
        vector=scope.axis(0),
        summary="the deploy target is green",
    )
    await db_session.commit()

    with (
        patch(
            "services.permission_service.PermissionService.check_context_owner",
            AsyncMock(),
        ),
        patch(
            "db.qdrant.copy_context_points",
            AsyncMock(side_effect=lambda **kw: set(kw["memory_id_mapping"].values())),
        ),
    ):
        await ContextService(db_session).merge_contexts(scope.owner, source.id, scope.ctx.id)
    copy = (
        await db_session.execute(
            select(Memory).where(
                Memory.context_id == scope.ctx.id, Memory.summary == original.summary
            )
        )
    ).scalar_one()
    assert copy.version == 1
    assert copy.created_at == original.created_at
    assert copy.updated_at is not None and copy.updated_at > later.created_at
    scope.vectors[str(copy.id)] = scope.vectors.pop(str(original.id))

    result, _ = await scope.run(
        judge_picks=copy, log_began=now - timedelta(days=log_began_days_ago)
    )

    state = await scope.state(copy, later)
    assert state[later.id].deleted_at is None
    if merged:
        assert result.details["merged"] == 1
        assert state[copy.id].deleted_by == DELETED_BY_SLEEP_MERGE
    else:
        assert result.details["merged"] == 0
        assert result.details["no_action_reasons"] == {"ambiguous_recency": 1}
        assert state[copy.id].deleted_at is None


async def _log_edit(
    scope: _Scope, memory: Memory, at: datetime, session_id: str, **fields: Any
) -> None:
    """An API edit by session ``session_id``, written as the API writes it."""
    prior = {name: getattr(memory, name) for name in fields}
    for name, value in fields.items():
        setattr(memory, name, value)
    memory.version += 1
    memory.updated_at = at
    memory.session_id = session_id
    scope.db.add(
        MemoryRevision(
            memory_id=memory.id,
            context_id=memory.context_id,
            version=memory.version,
            kind="updated",
            writer="api",
            actor_user_id=scope.owner,
            session_id=session_id,
            prior=json.loads(json.dumps(prior)),
            at=at,
        )
    )
    await scope.db.flush()


async def _rollback(scope: _Scope, session_id: str) -> None:
    """``rollback_session`` for one session, as the tool runs it, committed."""
    from services import session_rollback as sr
    from services.write_provenance import SessionPrincipal

    with (
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ),
        patch("db.qdrant.delete_points_from_qdrant", AsyncMock()),
        patch("services.sleep.undo.re_embed_memory_to_qdrant", AsyncMock(return_value=None)),
        patch("services.embedding_service.EmbeddingService", MagicMock()),
        patch("db.qdrant.update_memory_payload_in_qdrant", AsyncMock()),
    ):
        principal = SessionPrincipal(scope.owner, None)
        await sr.rollback_session(
            scope.db,
            scope=sr.SessionScope(scope.ctx.id, session_id, principal),
            workspace_id=scope.ws.id,
            requested_by=scope.owner,
            dry_run=False,
            classify=lambda _e: "internal_error",
            new_correlation_id=lambda: "corr-winner",
        )
    await scope.db.commit()


async def _events(db, memory: Memory) -> list[Any]:
    rows = (await db.execute(text_edit_events_query([memory.id]))).all()
    return [text_edit_event(row) for row in rows]


@pytest.mark.asyncio
async def test_the_revision_read_and_the_rollback_walk_on_real_rows(db_session):
    """The key tests and ``restored_from`` against rows ``record_edit`` and
    ``rollback_session`` wrote: a text edit counts until a rollback reverts it,
    a metadata edit never counts, a text edit after the rollback counts."""
    now = utcnow()
    scope = await _Scope.create(db_session)
    a = await scope.add(created_at=now - timedelta(days=180), vector=scope.axis(0))
    other = await scope.add(created_at=now - timedelta(days=60), vector=scope.axis(1))
    await _log_edit(scope, a, now - timedelta(days=30), "keep", importance=0.7)
    await _log_edit(scope, a, now - timedelta(days=20), "undo", summary="a rewritten summary")
    await db_session.commit()

    events = await _events(db_session, a)
    assert [(e.version, e.writer, e.text, e.restored_from) for e in events] == [
        (2, "api", False, None),
        (3, "api", True, None),
    ]
    phase = DedupMergePhase(db_session, MagicMock())
    await phase._load_text_edit_times([a, other])
    assert phase._recency_key(a) == now - timedelta(days=20)

    await _rollback(scope, "undo")
    await db_session.refresh(a)
    assert a.version == 4 and a.summary != "a rewritten summary"
    events = await _events(db_session, a)
    assert [(e.version, e.writer, e.text, e.restored_from) for e in events][-1] == (
        4,
        "rollback",
        True,
        3,
    )
    phase = DedupMergePhase(db_session, MagicMock())
    await phase._load_text_edit_times([a, other])
    assert phase._recency_key(a) == a.created_at

    await _log_edit(scope, a, now - timedelta(days=2), "later", content="a new body")
    await db_session.commit()
    phase = DedupMergePhase(db_session, MagicMock())
    await phase._load_text_edit_times([a, other])
    assert phase._recency_key(a) == now - timedelta(days=2)


# ---------------------------------------------------------------------------
# Owner decision 2026-11-27: keep both when the log cannot tell
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["judge", "rule_path", "shadow"])
@pytest.mark.parametrize("log_began_days_ago", [None, 30], ids=["no_log_row", "log_began_after_b"])
async def test_when_the_log_cannot_tell_both_rows_stay(db_session, mode, log_began_days_ago):
    """A's stamp (last week) is later than B (two months old). With no
    revision row, or a log that began after B was written, an edit of A after
    B would not be in the log, so the stamp may be one: neither row is
    retired, shadow mode writes no edge, the judge is not asked, and both rows
    are settled for the lanes."""
    now = utcnow()
    scope = await _Scope.create(db_session)
    a, b = await _old_stamped_and_newer(scope, now)

    result, phase = await scope.run(
        judge_picks=a if mode != "rule_path" else None,
        shadow=mode == "shadow",
        log_began=None if log_began_days_ago is None else now - timedelta(days=log_began_days_ago),
    )

    assert result.details["merged"] == 0
    assert result.details["no_action_reasons"] == {"ambiguous_recency": 1}
    assert result.details["budget_cut"] is False
    assert result.llm_calls_used == 0
    assert scope.prompts == []
    state = await scope.state(a, b)
    assert state[a.id].deleted_at is None and state[b.id].deleted_at is None
    edges = (
        await db_session.execute(
            select(NeuralMemoryEdge.id).where(
                NeuralMemoryEdge.user_id == scope.owner,
                NeuralMemoryEdge.edge_type == EDGE_TYPE_SUPERSEDES,
            )
        )
    ).all()
    assert edges == []
    assert phase.coverage_outcome is not None
    assert {phase.coverage_outcome.outcomes[m.id] for m in (a, b)} == {"settled"}


@pytest.mark.asyncio
async def test_the_log_start_is_read_from_postgres(db_session):
    """The real start: the ``at`` of the lowest-id revision, never later than
    an edit the log holds."""
    now = utcnow()
    scope = await _Scope.create(db_session)
    a, _ = await _old_stamped_and_newer(scope, now)
    await db_session.commit()
    await _edit(scope, a, summary="the deploy target is blue, checked again")
    edit_at = (
        await db_session.execute(
            select(MemoryRevision.at)
            .where(MemoryRevision.memory_id == a.id, MemoryRevision.kind == "updated")
            .order_by(MemoryRevision.id.desc())
            .limit(1)
        )
    ).scalar_one()
    lowest = (
        await db_session.execute(select(MemoryRevision.at).order_by(MemoryRevision.id).limit(1))
    ).scalar_one()

    phase = DedupMergePhase(db_session, MagicMock())
    began = await phase._revision_log_began()

    assert began == lowest
    assert began is not None and began <= edit_at
    assert phase._log_began_read is True
