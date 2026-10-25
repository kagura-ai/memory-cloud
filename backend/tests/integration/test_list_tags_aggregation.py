"""Integration tests for ``ContextService.aggregate_tags``.

Exercise the raw-SQL CTE against a real Postgres so the load-bearing
correctness properties are pinned:

1. Intra-array duplicates (``tags=["python","python"]``) collapse to 1 count.
2. Soft-deleted memories (``deleted_at IS NOT NULL``) are excluded.
3. Rows of other scopes do NOT leak in; a cross-workspace row with the
   caller's ``context_id`` is refused by the schema (#1963).
4. Empty / untagged context returns ``[]`` (200-equivalent).
5. ``min_count`` filter (HAVING clause).
6. ``prefix`` filter treats ``%`` / ``_`` as literal characters.
7. Sort modes: ``count`` / ``recent`` / ``alpha``.
8. ``last_used_at`` reflects ``MAX(GREATEST(created_at, updated_at))``
   across distinct memories carrying the tag.

The mock-only unit tests at ``tests/api/test_context_tags.py`` and
``tests/mcp_server/test_list_tags.py`` cover the route / handler wiring;
this file covers the SQL itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, Workspace, WorkspaceMember
from models.memory import Memory
from services.context_service import ContextService


@pytest.fixture
def now() -> datetime:
    """Anchor for deterministic timestamps."""
    return datetime(2026, 5, 12, 3, 0, 0)


async def _agg_rows(db, user_id, ctx_id, **kwargs):
    """Call aggregate_tags and unwrap the ``rows`` list for assertion convenience."""
    result = await ContextService(db).aggregate_tags(user_id, ctx_id, **kwargs)
    return result["rows"]


async def _seed_workspace_context(
    db: AsyncSession,
    *,
    user_id: str,
    is_private: bool = False,
) -> tuple[Workspace, Context]:
    """Mint a workspace + workspace_member + context owned by ``user_id``.

    All UUIDs are random so each test run is isolated within the shared
    session-scoped ``db_session`` fixture.

    Flush between workspace and its children: ``WorkspaceMember.workspace_id``
    and ``Context.workspace_id`` are column-level FKs with no ``relationship()``
    to drive insert order. Without the intermediate flush SQLAlchemy may emit
    the child INSERTs before the parent, deterministically violating the
    workspace FK.
    """
    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=user_id,
        daily_api_limit=50_000,
        weekly_api_limit=250_000,
    )
    db.add(ws)
    await db.flush()

    member = WorkspaceMember(
        workspace_id=ws.id,
        user_id=user_id,
        role=WorkspaceRole.OWNER,
    )
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=user_id,
        is_private=is_private,
    )
    db.add_all([member, ctx])
    await db.flush()
    return ws, ctx


def _memory(
    *,
    ws_id,
    ctx_id,
    user_id: str,
    tags: list[str] | None,
    created_at: datetime,
    updated_at: datetime | None = None,
    deleted_at: datetime | None = None,
    summary: str | None = None,
) -> Memory:
    return Memory(
        id=uuid4(),
        user_id=user_id,
        workspace_id=ws_id,
        context_id=ctx_id,
        summary=summary if summary is not None else f"mem-{uuid4().hex[:6]}",
        content="x",
        type="note",
        client="test",
        tags=tags,
        created_at=created_at,
        updated_at=updated_at or created_at,
        deleted_at=deleted_at,
    )


@pytest.mark.asyncio
class TestAggregateTagsCTE:
    async def test_empty_context_returns_empty_list(self, db_session, now):
        user_id = f"u_empty_{uuid4().hex[:6]}"
        _, ctx = await _seed_workspace_context(db_session, user_id=user_id)

        rows = await _agg_rows(db_session, user_id, ctx.id)

        assert rows == []

    async def test_intra_array_duplicates_count_once(self, db_session, now):
        """`tags=['python','python','backend']` → python:1, backend:1 (not 2:1)."""
        user_id = f"u_dup_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        db_session.add(
            _memory(
                ws_id=ws.id,
                ctx_id=ctx.id,
                user_id=user_id,
                tags=["python", "python", "backend"],
                created_at=now,
            )
        )
        await db_session.flush()

        rows = await _agg_rows(db_session, user_id, ctx.id)

        counts = {r["tag"]: r["count"] for r in rows}
        assert counts == {"python": 1, "backend": 1}

    async def test_q_facets_tags_to_matching_summaries(self, db_session, now):
        """#618: ``q`` filters the scope by summary substring, so only tags on
        matching memories are aggregated. Blank / absent q → all tags."""
        user_id = f"u_q_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        db_session.add_all(
            [
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["ops"],
                    created_at=now,
                    summary="deploy runbook",
                ),
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["security"],
                    created_at=now,
                    summary="auth flow",
                ),
            ]
        )
        await db_session.flush()

        # q matches only "deploy runbook" → just its tag.
        facet = {r["tag"] for r in await _agg_rows(db_session, user_id, ctx.id, q="deploy")}
        assert facet == {"ops"}
        # No q → all tags in the context.
        all_tags = {r["tag"] for r in await _agg_rows(db_session, user_id, ctx.id)}
        assert all_tags == {"ops", "security"}
        # Whitespace-only q is treated as no filter (no accidental empty set).
        blank = {r["tag"] for r in await _agg_rows(db_session, user_id, ctx.id, q="   ")}
        assert blank == {"ops", "security"}

    async def test_soft_deleted_memory_excluded(self, db_session, now):
        user_id = f"u_softdel_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        db_session.add_all(
            [
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["live"],
                    created_at=now,
                ),
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["ghost"],
                    created_at=now - timedelta(days=1),
                    deleted_at=now,
                ),
            ]
        )
        await db_session.flush()

        rows = await _agg_rows(db_session, user_id, ctx.id)

        tags = {r["tag"] for r in rows}
        assert tags == {"live"}

    async def test_schema_refuses_a_cross_workspace_row(self, db_session, now):
        """#1963: a memory in (ws_b, ctx_a) cannot be stored at all.

        ``fk_memories_workspace_context`` ties ``memories (workspace_id,
        context_id)`` to ``contexts (workspace_id, id)``, so the leak the CTE's
        ``WHERE workspace_id = :workspace_id`` used to be the only guard
        against is now refused by the schema.
        """
        user_a = f"u_wsfk_a_{uuid4().hex[:6]}"
        user_b = f"u_wsfk_b_{uuid4().hex[:6]}"
        _ws_a, ctx_a = await _seed_workspace_context(db_session, user_id=user_a)
        ws_b, _ctx_b = await _seed_workspace_context(db_session, user_id=user_b)

        with pytest.raises(IntegrityError, match="fk_memories_workspace_context"):
            async with db_session.begin_nested():
                db_session.add(
                    _memory(
                        ws_id=ws_b.id,
                        ctx_id=ctx_a.id,
                        user_id=user_b,
                        tags=["leaked"],
                        created_at=now,
                    )
                )
                await db_session.flush()

    async def test_scope_filter_keeps_other_rows_out(self, db_session, now):
        """Only the caller's (workspace, context) rows are aggregated.

        Setup: caller in ws_a queries ctx_a. We seed:
          (1) a legitimate memory in (ws_a, ctx_a) tagged 'legit'
          (2) a memory in ws_a without a context tagged 'no-context'
          (3) an unrelated memory in (ws_b, ctx_b) tagged 'unrelated'
        Only 'legit' should be returned.
        """
        user_a = f"u_wsfilt_a_{uuid4().hex[:6]}"
        user_b = f"u_wsfilt_b_{uuid4().hex[:6]}"
        ws_a, ctx_a = await _seed_workspace_context(db_session, user_id=user_a)
        ws_b, ctx_b = await _seed_workspace_context(db_session, user_id=user_b)

        db_session.add_all(
            [
                _memory(
                    ws_id=ws_a.id,
                    ctx_id=ctx_a.id,
                    user_id=user_a,
                    tags=["legit"],
                    created_at=now,
                ),
                _memory(
                    ws_id=ws_a.id,
                    ctx_id=None,
                    user_id=user_a,
                    tags=["no-context"],
                    created_at=now,
                ),
                _memory(
                    ws_id=ws_b.id,
                    ctx_id=ctx_b.id,
                    user_id=user_b,
                    tags=["unrelated"],
                    created_at=now,
                ),
            ]
        )
        await db_session.flush()

        rows = await _agg_rows(db_session, user_a, ctx_a.id)

        tags = {r["tag"] for r in rows}
        assert tags == {"legit"}, (
            "scope filter failed — 'no-context' or 'unrelated' appeared in caller's view"
        )

    async def test_min_count_filter(self, db_session, now):
        user_id = f"u_minc_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        for _ in range(3):
            db_session.add(
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["popular"],
                    created_at=now,
                )
            )
        db_session.add(
            _memory(
                ws_id=ws.id,
                ctx_id=ctx.id,
                user_id=user_id,
                tags=["once"],
                created_at=now,
            )
        )
        await db_session.flush()

        rows_default = await _agg_rows(db_session, user_id, ctx.id, min_count=1)
        rows_filtered = await _agg_rows(db_session, user_id, ctx.id, min_count=3)

        assert {r["tag"] for r in rows_default} == {"popular", "once"}
        assert {r["tag"] for r in rows_filtered} == {"popular"}

    async def test_prefix_filter_treats_wildcards_as_literals(self, db_session, now):
        """`prefix='100_'` matches the literal '100_', not the '100<any>' glob."""
        user_id = f"u_pfx_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        db_session.add_all(
            [
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["100_percent"],
                    created_at=now,
                ),
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["100xpercent"],  # would match '100_' if _ were a glob
                    created_at=now,
                ),
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["alphabet"],
                    created_at=now,
                ),
            ]
        )
        await db_session.flush()

        rows = await _agg_rows(db_session, user_id, ctx.id, prefix="100_")

        tags = {r["tag"] for r in rows}
        # '100_' is treated literally: only '100_percent' matches, NOT '100xpercent'.
        assert tags == {"100_percent"}

    async def test_sort_count_descending_then_alpha(self, db_session, now):
        user_id = f"u_sortc_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        for tag, n in [("zeta", 2), ("alpha", 2), ("beta", 5)]:
            for _ in range(n):
                db_session.add(
                    _memory(
                        ws_id=ws.id,
                        ctx_id=ctx.id,
                        user_id=user_id,
                        tags=[tag],
                        created_at=now,
                    )
                )
        await db_session.flush()

        rows = await _agg_rows(db_session, user_id, ctx.id, sort="count")

        ordered = [r["tag"] for r in rows]
        # beta (5) first, then alpha and zeta tied at 2 → ascending alpha tiebreak.
        assert ordered == ["beta", "alpha", "zeta"]

    async def test_sort_recent_uses_greatest_created_updated(self, db_session, now):
        """`recent` ordering uses MAX(GREATEST(created_at, updated_at))."""
        user_id = f"u_sortr_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        # "old": created earlier, never updated.
        db_session.add(
            _memory(
                ws_id=ws.id,
                ctx_id=ctx.id,
                user_id=user_id,
                tags=["old"],
                created_at=now - timedelta(days=10),
            )
        )
        # "tagged_late": created early but tags-only update brings it to "now".
        db_session.add(
            _memory(
                ws_id=ws.id,
                ctx_id=ctx.id,
                user_id=user_id,
                tags=["tagged_late"],
                created_at=now - timedelta(days=20),
                updated_at=now,
            )
        )
        await db_session.flush()

        rows = await _agg_rows(db_session, user_id, ctx.id, sort="recent")

        ordered = [r["tag"] for r in rows]
        # tagged_late wins because updated_at=now > old.created_at=now-10d.
        assert ordered == ["tagged_late", "old"]

    async def test_sort_alpha_case_insensitive(self, db_session, now):
        user_id = f"u_sorta_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        for tag in ["Banana", "apple", "Cherry"]:
            db_session.add(
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=[tag],
                    created_at=now,
                )
            )
        await db_session.flush()

        rows = await _agg_rows(db_session, user_id, ctx.id, sort="alpha")

        ordered = [r["tag"] for r in rows]
        assert ordered == ["apple", "Banana", "Cherry"]

    async def test_limit_cap(self, db_session, now):
        user_id = f"u_lim_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        for i in range(7):
            db_session.add(
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=[f"tag-{i:02d}"],
                    created_at=now,
                )
            )
        await db_session.flush()

        rows = await _agg_rows(db_session, user_id, ctx.id, limit=3)

        assert len(rows) == 3

    async def test_last_used_at_takes_max_across_memories(self, db_session, now):
        """For a tag spanning multiple memories, last_used_at is the MAX."""
        user_id = f"u_lua_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        early = now - timedelta(days=5)
        late = now
        db_session.add_all(
            [
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["shared"],
                    created_at=early,
                ),
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["shared"],
                    created_at=late,
                ),
            ]
        )
        await db_session.flush()

        rows = await _agg_rows(db_session, user_id, ctx.id)

        assert len(rows) == 1
        assert rows[0]["tag"] == "shared"
        assert rows[0]["count"] == 2
        assert rows[0]["last_used_at"] == late

    async def test_invalid_sort_raises_validation_error(self, db_session):
        from utils.exceptions import ValidationError

        user_id = f"u_badsort_{uuid4().hex[:6]}"
        _, ctx = await _seed_workspace_context(db_session, user_id=user_id)

        with pytest.raises(ValidationError):
            await _agg_rows(db_session, user_id, ctx.id, sort="bogus")


@pytest.mark.asyncio
class TestAggregateTagsWithTags:
    """#830: ``with_tags`` multi-tag AND drill-down on the tag cloud."""

    async def _seed_cooccurrence(self, db_session, now):
        """3 memories so co-occurrence is non-trivial:

        m1: {python, backend, api}
        m2: {python, backend, db}
        m3: {python, frontend}
        """
        user_id = f"u_wt_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        db_session.add_all(
            [
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["python", "backend", "api"],
                    created_at=now,
                ),
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["python", "backend", "db"],
                    created_at=now,
                ),
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["python", "frontend"],
                    created_at=now,
                ),
            ]
        )
        await db_session.flush()
        return user_id, ctx

    async def test_with_tags_facets_to_cooccurring_and_self_excludes(self, db_session, now):
        user_id, ctx = await self._seed_cooccurrence(db_session, now)

        # Drill into "backend": only m1+m2 qualify (both hold backend).
        # Their other tags are {python, api, db}; "backend" itself is excluded.
        rows = await _agg_rows(db_session, user_id, ctx.id, with_tags=["backend"])
        by_tag = {r["tag"]: r["count"] for r in rows}
        assert set(by_tag) == {"python", "api", "db"}
        assert "backend" not in by_tag  # self-exclusion
        assert "frontend" not in by_tag  # m3 lacks backend
        # Counts reflect the faceted subset: python on both m1,m2 → 2; api/db → 1.
        assert by_tag == {"python": 2, "api": 1, "db": 1}

    async def test_with_tags_and_semantics_multi(self, db_session, now):
        user_id, ctx = await self._seed_cooccurrence(db_session, now)

        # AND of python+backend → m1,m2 → remaining {api, db} (python+backend excluded).
        rows = await _agg_rows(db_session, user_id, ctx.id, with_tags=["python", "backend"])
        assert {r["tag"] for r in rows} == {"api", "db"}

    async def test_with_tags_empty_matches_618_behavior(self, db_session, now):
        user_id, ctx = await self._seed_cooccurrence(db_session, now)

        baseline = {r["tag"] for r in await _agg_rows(db_session, user_id, ctx.id)}
        empty = {r["tag"] for r in await _agg_rows(db_session, user_id, ctx.id, with_tags=[])}
        assert empty == baseline == {"python", "backend", "api", "db", "frontend"}

    async def test_with_tags_no_cooccurrence_returns_empty(self, db_session, now):
        user_id, ctx = await self._seed_cooccurrence(db_session, now)

        # api and frontend never co-occur (api∈m1, frontend∈m3) → empty cloud.
        rows = await _agg_rows(db_session, user_id, ctx.id, with_tags=["api", "frontend"])
        assert rows == []

    async def test_with_tags_combines_with_q(self, db_session, now):
        """with_tags AND q both narrow the same memory set."""
        user_id = f"u_wtq_{uuid4().hex[:6]}"
        ws, ctx = await _seed_workspace_context(db_session, user_id=user_id)
        db_session.add_all(
            [
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["python", "api"],
                    created_at=now,
                    summary="deploy api",
                ),
                _memory(
                    ws_id=ws.id,
                    ctx_id=ctx.id,
                    user_id=user_id,
                    tags=["python", "db"],
                    created_at=now,
                    summary="schema notes",
                ),
            ]
        )
        await db_session.flush()

        # with_tags=python narrows to both; q=deploy further narrows to m1 only.
        rows = await _agg_rows(db_session, user_id, ctx.id, with_tags=["python"], q="deploy")
        assert {r["tag"] for r in rows} == {"api"}

    async def test_with_tags_over_limit_raises(self, db_session, now):
        from utils.exceptions import ValidationError

        user_id, ctx = await self._seed_cooccurrence(db_session, now)
        with pytest.raises(ValidationError):
            await _agg_rows(db_session, user_id, ctx.id, with_tags=[f"t{i}" for i in range(51)])

    async def test_with_tags_trims_whitespace(self, db_session, now):
        """`?with_tags=%20backend%20` binds 'backend', not ' backend ' (PR #833
        Copilot review). Surrounding whitespace must not break the @> match or
        the self-exclusion."""
        user_id, ctx = await self._seed_cooccurrence(db_session, now)

        rows = await _agg_rows(db_session, user_id, ctx.id, with_tags=["  backend  "])
        by_tag = {r["tag"] for r in rows}
        assert by_tag == {"python", "api", "db"}
        assert "backend" not in by_tag  # self-excluded despite the whitespace

    async def test_mcp_list_tags_with_tags_filters(self, db_session, now):
        """#1669: MCP ``list_tags(with_tags=...)`` drills down end to end.

        Before #1669 the handler dropped ``with_tags`` and returned the whole
        cloud; this runs the real handler over the seeded rows.
        """
        import json
        from unittest.mock import AsyncMock, patch

        from mcp_server.tools.context import handle_list_tags

        user_id, ctx = await self._seed_cooccurrence(db_session, now)

        async def _get_db():
            yield db_session

        with (
            patch("db.base.get_db", _get_db),
            patch("mcp_server.tools.context._log_tool_usage", AsyncMock()),
        ):
            result = await handle_list_tags(
                {"context_id": str(ctx.id), "with_tags": ["python", "backend"]},
                user_id,
                ctx.workspace_id,
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "success"
        assert payload["context_name"] == ctx.name
        assert {t["tag"]: t["count"] for t in payload["tags"]} == {"api": 1, "db": 1}
