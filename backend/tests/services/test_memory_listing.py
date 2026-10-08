"""``list`` and ``changes_since`` against real Postgres (#1852).

Deterministic lanes: every match, exact filters, stable pages; the change log
in time order with a keyset cursor. Seeds its own workspace / contexts / rows.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from models.auth import Context, IdentityLink, User, Workspace
from models.memory import EDGE_TYPE_SUPERSEDES, Memory, NeuralMemoryEdge
from models.schemas import ForgetRequest
from services.memory_listing import (
    Change,
    change_item,
    changes_since,
    compile_memory_filters,
    decode_change_cursor,
    list_memories,
)
from services.memory_service import MemoryService
from utils.datetime import utcnow


async def _scope(
    db, owner: str, *, private: bool, trust_tier: str = "trusted"
) -> tuple[UUID, UUID]:
    ws = Workspace(
        id=uuid4(),
        name=f"ml-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ml-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=private,
        trust_tier=trust_tier,
    )
    db.add(ctx)
    await db.flush()
    return ws.id, ctx.id


async def _row(db, user_id: str, ws: UUID, ctx: UUID, **over) -> Memory:
    base = {
        "id": uuid4(),
        "user_id": user_id,
        "workspace_id": ws,
        "context_id": ctx,
        "summary": over.pop("summary", "row"),
        "content": "content",
        "type": "note",
        "importance": 0.5,
        "confidence": 1.0,
        "client": "pytest",
        "source": "manual",
        "source_type": "manual",
        "created_at": utcnow(),
        "embedding_status": "success",
    }
    base.update(over)
    mem = Memory(**base)
    db.add(mem)
    await db.flush()
    return mem


@pytest.fixture
async def seeded(db_session):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner, private=False)
    t0 = utcnow() - timedelta(days=3)
    rows = {}
    rows["task_open"] = await _row(
        db_session,
        owner,
        ws,
        ctx,
        summary="open task",
        type="task",
        tags=["Project-X", "todo"],
        importance=0.9,
        details={"status": "open", "prio": 2},
        created_at=t0,
    )
    rows["task_done"] = await _row(
        db_session,
        owner,
        ws,
        ctx,
        summary="done task",
        type="task",
        tags=["project_x"],
        importance=0.4,
        details={"status": "done"},
        created_at=t0 + timedelta(hours=1),
        updated_at=t0 + timedelta(days=1),
    )
    rows["note"] = await _row(
        db_session,
        owner,
        ws,
        ctx,
        summary="a note",
        type="note",
        tags=["todo", "misc"],
        importance=0.6,
        source_type="vault",
        source_uri="vault://v/n.md",
        created_at=t0 + timedelta(hours=2),
    )
    rows["gone"] = await _row(
        db_session,
        owner,
        ws,
        ctx,
        summary="forgotten",
        type="note",
        created_at=t0 + timedelta(hours=3),
        deleted_at=t0 + timedelta(days=2),
    )
    return owner, ws, ctx, rows, t0


# ---------------------------------------------------------------------- list


@pytest.mark.asyncio
async def test_list_returns_every_live_row_newest_update_first(db_session, seeded):
    owner, ws, ctx, rows, _ = seeded
    page = await list_memories(db_session, context_id=ctx, owner_user_id=None, filters=None)
    ids = [m.id for m in page.rows]
    assert rows["gone"].id not in ids  # soft-deleted rows are not listed
    assert page.total == 3 and not page.has_more
    assert ids[0] == rows["task_done"].id  # the only edited row sorts first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"type": "task"}, {"task_open", "task_done"}),
        ({"type": ["task", "note"], "importance": {"gte": 0.6}}, {"task_open", "note"}),
        ({"tags": ["todo"]}, {"task_open", "note"}),
        ({"tags": ["todo", "misc"], "tags_match": "all"}, {"note"}),
        ({"tags": ["project x"], "tags_normalize": True}, {"task_open", "task_done"}),
        ({"details.status": "open"}, {"task_open"}),
        ({"details.prio": 2}, {"task_open"}),
        ({"details.prio": "2"}, set()),  # a string filter never matches a stored number
        ({"details.status": True}, set()),
        ({"source_type": "vault", "source_uri_prefix": "vault://v/"}, {"note"}),
        ({"source_uri_prefix": "vault://v/n.m"}, {"note"}),
        ({"source_uri_prefix": "vault://v\\"}, set()),  # a backslash is literal, not an escape
        ({"trust_tier": "trusted"}, {"task_open", "task_done", "note"}),
    ],
)
async def test_list_filters_return_the_complete_match_set(db_session, seeded, filters, expected):
    owner, ws, ctx, rows, _ = seeded
    page = await list_memories(db_session, context_id=ctx, owner_user_id=None, filters=filters)
    assert {m.summary for m in page.rows} == {rows[k].summary for k in expected}
    assert page.total == len(expected)


@pytest.mark.asyncio
async def test_list_time_windows_are_after_inclusive_before_exclusive(db_session, seeded):
    owner, ws, ctx, rows, t0 = seeded
    one_hour = (t0 + timedelta(hours=1)).isoformat()
    after = await list_memories(
        db_session, context_id=ctx, owner_user_id=None, filters={"created_after": one_hour}
    )
    before = await list_memories(
        db_session, context_id=ctx, owner_user_id=None, filters={"created_before": one_hour}
    )
    assert {m.summary for m in after.rows} == {"done task", "a note"}
    assert {m.summary for m in before.rows} == {"open task"}
    updated = await list_memories(
        db_session, context_id=ctx, owner_user_id=None, filters={"updated_after": t0.isoformat()}
    )
    assert [m.summary for m in updated.rows] == ["done task"]


@pytest.mark.asyncio
async def test_list_pages_are_stable_at_the_boundary(db_session, seeded):
    owner, ws, ctx, rows, t0 = seeded
    # five rows sharing one created_at: the id tiebreak must make pages disjoint
    same = t0 + timedelta(days=1)
    for i in range(5):
        await _row(db_session, owner, ws, ctx, summary=f"same-{i}", type="batch", created_at=same)
    seen: list[UUID] = []
    offset = 0
    while True:
        page = await list_memories(
            db_session,
            context_id=ctx,
            owner_user_id=None,
            filters={"type": "batch"},
            order_by="created_at",
            direction="asc",
            offset=offset,
            limit=2,
        )
        seen += [m.id for m in page.rows]
        assert page.total == 5
        if not page.has_more:
            break
        offset += 2
    assert len(seen) == 5 and len(set(seen)) == 5


@pytest.mark.asyncio
async def test_private_context_shows_the_owner_and_linked_accounts_only(db_session):
    a, b, c = (f"{p}-{uuid4().hex[:6]}" for p in "abc")
    for acct in (a, b, c):
        db_session.add(User(email=f"{acct}@test.example", user_id=acct, role="user"))
    await db_session.flush()
    group = uuid4()
    db_session.add(IdentityLink(group_id=group, user_id=a, linked_by=a))
    db_session.add(IdentityLink(group_id=group, user_id=b, linked_by=a))
    await db_session.flush()
    ws, ctx = await _scope(db_session, a, private=True)
    for acct in (a, b, c):
        await _row(db_session, acct, ws, ctx, summary=f"by {acct}")
    page = await list_memories(db_session, context_id=ctx, owner_user_id=a, filters=None)
    assert {m.user_id for m in page.rows} == {a, b}
    assert page.total == 2


@pytest.mark.parametrize(
    "filters",
    [
        {"near": {"lat": 1, "lon": 2}},
        {"bogus": 1},
        {"details.bad key": "x"},
        {"importance": {"eq": 1}},
        {"tags_match": "some"},
        {"trust_tier": "external"},
        {"created_after": 5},
    ],
)
def test_compile_rejects_unsupported_or_malformed_filters(filters):
    with pytest.raises(ValueError):
        compile_memory_filters(filters)


@pytest.mark.asyncio
async def test_list_rejects_a_bad_order(db_session, seeded):
    owner, ws, ctx, rows, _ = seeded
    with pytest.raises(ValueError):
        await list_memories(
            db_session, context_id=ctx, owner_user_id=None, filters=None, order_by="summary"
        )


# ------------------------------------------------------------- changes_since


@pytest.mark.asyncio
async def test_changes_since_lists_every_kind_in_time_order(db_session, seeded):
    owner, ws, ctx, rows, t0 = seeded
    newer = await _row(
        db_session, owner, ws, ctx, summary="newer fact", created_at=t0 + timedelta(days=2, hours=1)
    )
    db_session.add(
        NeuralMemoryEdge(
            user_id=owner,
            src_id=newer.id,
            dst_id=rows["note"].id,
            workspace_id=ws,
            context_id=ctx,
            edge_type=EDGE_TYPE_SUPERSEDES,
            weight=1.0,
            confidence=1.0,
            origin="declared",
            created_at=t0 + timedelta(days=2, hours=2),
        )
    )
    await db_session.flush()
    page = await changes_since(
        db_session, context_id=ctx, owner_user_id=None, since=t0 - timedelta(seconds=1), until=None
    )
    kinds = [(c.kind, c.summary) for c in page.changes]
    assert kinds == [
        ("created", "open task"),
        ("created", "done task"),
        ("created", "a note"),
        ("created", None),  # the forgotten row: the event stays, its summary does not
        ("updated", "done task"),
        ("forgotten", None),
        ("created", "newer fact"),
        ("superseded", "a note"),
    ]
    sup = next(c for c in page.changes if c.kind == "superseded")
    assert sup.superseded_by == newer.id
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_changes_since_window_is_since_inclusive_until_exclusive(db_session, seeded):
    owner, ws, ctx, rows, t0 = seeded
    page = await changes_since(
        db_session,
        context_id=ctx,
        owner_user_id=None,
        since=t0 + timedelta(hours=1),
        until=t0 + timedelta(hours=2),
        kinds=("created",),
    )
    assert [c.summary for c in page.changes] == ["done task"]
    with pytest.raises(ValueError):
        await changes_since(db_session, context_id=ctx, owner_user_id=None, since=t0, until=t0)


@pytest.mark.asyncio
async def test_changes_since_keyset_cursor_walks_without_gaps_or_repeats(db_session, seeded):
    owner, ws, ctx, rows, t0 = seeded
    same = t0 + timedelta(days=1, hours=5)
    for i in range(5):
        await _row(db_session, owner, ws, ctx, summary=f"burst-{i}", created_at=same)
    seen: list[tuple[str, UUID]] = []
    cursor = None
    while True:
        page = await changes_since(
            db_session,
            context_id=ctx,
            owner_user_id=None,
            since=t0 - timedelta(seconds=1),
            until=None,
            kinds=("created",),
            cursor=cursor,
            limit=3,
        )
        seen += [(c.kind, c.memory_id) for c in page.changes]
        if page.next_cursor is None:
            break
        decode_change_cursor(page.next_cursor)  # round-trips
        cursor = page.next_cursor
    assert len(seen) == 9 and len(set(seen)) == 9  # 4 seeded + 5 burst, each once


def test_change_cursor_rejects_garbage():
    with pytest.raises(ValueError):
        decode_change_cursor("not-a-cursor")
    with pytest.raises(ValueError):
        decode_change_cursor("")


@pytest.mark.asyncio
async def test_small_pages_walk_the_full_list_and_log_without_loss(db_session, seeded):
    """The handler continues a cut page from offset + kept; emulate it with limit=1."""
    owner, ws, ctx, rows, _ = seeded
    seen = []
    offset = 0
    while True:
        page = await list_memories(
            db_session, context_id=ctx, owner_user_id=None, filters=None, offset=offset, limit=1
        )
        seen += [m.id for m in page.rows]
        if not page.has_more:
            break
        offset += 1
    assert len(seen) == 3 and len(set(seen)) == 3


# ------------------------------------------------------------- /code-review (#1857)


@pytest.mark.asyncio
async def test_tags_normalize_folds_both_sides_the_same_way(db_session, seeded):
    """``lower()`` on both sides: a tag with ``ß`` matches its exact spelling (casefold
    would have expanded the needle to ``ss`` and missed the stored ``ß``)."""
    owner, ws, ctx, _, _ = seeded
    row = await _row(db_session, owner, ws, ctx, summary="street", tags=["Straße-Plan"])
    await db_session.commit()
    page = await list_memories(
        db_session,
        context_id=ctx,
        owner_user_id=None,
        filters={"tags": ["straße plan"], "tags_normalize": True},
    )
    assert [m.id for m in page.rows] == [row.id]


@pytest.mark.parametrize(
    "filters",
    [
        {"importance": {"gte": True}},  # bool is not a number
        {"tags_normalize": True},  # nothing to normalise
        {"tags_match": "all"},  # nothing to match
    ],
)
def test_filter_options_that_would_silently_misapply_are_refused(filters):
    with pytest.raises(ValueError):
        compile_memory_filters(filters)


@pytest.mark.asyncio
async def test_list_accepts_precompiled_predicates(db_session, seeded):
    owner, ws, ctx, rows, _ = seeded
    predicates = compile_memory_filters({"type": "task"})
    page = await list_memories(
        db_session, context_id=ctx, owner_user_id=None, predicates=predicates
    )
    assert {m.id for m in page.rows} == {rows["task_open"].id, rows["task_done"].id}


@pytest.mark.asyncio
async def test_changes_since_trusted_only_drops_an_external_tier_context(db_session):
    """The ``trusted_only`` switch the session bootstrap (#1851) passes."""
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner, private=False, trust_tier="external")
    await _row(db_session, owner, ws, ctx, summary="from an external-tier context")
    await db_session.commit()
    since = utcnow() - timedelta(hours=1)
    everything = await changes_since(
        db_session, context_id=ctx, owner_user_id=None, since=since, until=None
    )
    assert len(everything.changes) == 1
    trusted = await changes_since(
        db_session, context_id=ctx, owner_user_id=None, since=since, until=None, trusted_only=True
    )
    assert trusted.changes == []


@pytest.mark.asyncio
async def test_a_retyped_edge_is_dated_when_it_became_supersedes(db_session, seeded):
    """k-NN seeding linked the pair a week ago; the supersession declared today
    must show up in today's window, not be hidden in last week's."""
    from repositories.neural_edge import NeuralEdgeRepository

    owner, ws, ctx, rows, _ = seeded
    old, new = rows["task_done"], rows["task_open"]
    week_ago = utcnow() - timedelta(days=7)
    db_session.add(
        NeuralMemoryEdge(
            user_id=owner,
            src_id=new.id,
            dst_id=old.id,
            edge_type="neural_association",
            weight=0.3,
            confidence=1.0,
            workspace_id=ws,
            context_id=ctx,
            origin="semantic",
            created_at=week_ago,
            last_updated=week_ago,
        )
    )
    await db_session.flush()
    await NeuralEdgeRepository(db_session).create_or_update_edge(
        owner,
        new.id,
        old.id,
        edge_type=EDGE_TYPE_SUPERSEDES,
        weight=1.0,
        workspace_id=str(ws),
        context_id=str(ctx),
        origin="declared",
    )
    await db_session.commit()
    page = await changes_since(
        db_session,
        context_id=ctx,
        owner_user_id=None,
        since=utcnow() - timedelta(hours=1),
        until=None,
        kinds=("superseded",),
    )
    assert [(c.memory_id, c.superseded_by) for c in page.changes] == [(old.id, new.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("kinds", [("updated",), ("forgotten",), ("superseded", "forgotten")])
async def test_changes_since_works_when_created_is_not_the_first_kind(db_session, seeded, kinds):
    """The union used to take its column names from the ``created`` part only."""
    owner, ws, ctx, rows, t0 = seeded
    page = await changes_since(
        db_session,
        context_id=ctx,
        owner_user_id=None,
        since=t0 - timedelta(days=1),
        until=None,
        kinds=kinds,
    )
    assert all(c.kind in kinds for c in page.changes)
    assert any(c.kind == "updated" for c in page.changes) is ("updated" in kinds)


# ------------------------------------------------------------------ #1876


@pytest.mark.asyncio
async def test_a_forgotten_memory_keeps_its_events_but_not_its_summary(db_session, seeded):
    """Edited, superseded, then forgotten: all four events stay in the log with
    their kind, time and cursor position; none of them carries the summary."""
    owner, ws, ctx, rows, t0 = seeded
    secret = await _row(
        db_session,
        owner,
        ws,
        ctx,
        summary="stored by mistake",
        created_at=t0 + timedelta(hours=4),
        updated_at=t0 + timedelta(hours=5),
        deleted_at=t0 + timedelta(hours=7),
    )
    newer = await _row(
        db_session, owner, ws, ctx, summary="the correction", created_at=t0 + timedelta(hours=6)
    )
    db_session.add(
        NeuralMemoryEdge(
            user_id=owner,
            src_id=newer.id,
            dst_id=secret.id,
            workspace_id=ws,
            context_id=ctx,
            edge_type=EDGE_TYPE_SUPERSEDES,
            weight=1.0,
            confidence=1.0,
            origin="declared",
            created_at=t0 + timedelta(hours=6, minutes=30),
        )
    )
    await db_session.flush()
    window = {
        "context_id": ctx,
        "owner_user_id": None,
        "since": t0 + timedelta(hours=4),
        "until": t0 + timedelta(hours=8),
    }
    page = await changes_since(db_session, **window)
    assert [(c.kind, c.memory_id, c.summary, c.at) for c in page.changes] == [
        ("created", secret.id, None, t0 + timedelta(hours=4)),
        ("updated", secret.id, None, t0 + timedelta(hours=5)),
        ("created", newer.id, "the correction", t0 + timedelta(hours=6)),
        ("superseded", secret.id, None, t0 + timedelta(hours=6, minutes=30)),
        ("forgotten", secret.id, None, t0 + timedelta(hours=7)),
    ]
    assert page.changes[3].superseded_by == newer.id
    # The keyset cursor walks the same sequence one event at a time.
    walked = []
    cursor = None
    while True:
        one = await changes_since(db_session, **window, cursor=cursor, limit=1)
        walked += one.changes
        if one.next_cursor is None:
            break
        cursor = one.next_cursor
    assert walked == page.changes
    for kind in ("created", "updated", "superseded", "forgotten"):  # each part of the union
        only = await changes_since(db_session, **window, kinds=(kind,))
        assert [c.summary for c in only.changes if c.memory_id == secret.id] == [None]


def test_change_item_omits_the_summary_of_a_forgotten_memory():
    at = utcnow()
    live = Change(memory_id=uuid4(), kind="created", at=at, summary="kept", superseded_by=None)
    gone = Change(memory_id=uuid4(), kind="forgotten", at=at, summary=None, superseded_by=None)
    assert change_item(live)["summary"] == "kept"
    assert set(change_item(gone)) == {"memory_id", "kind", "at"}
    newer = uuid4()
    replaced = Change(
        memory_id=uuid4(), kind="superseded", at=at, summary=None, superseded_by=newer
    )
    assert set(change_item(replaced)) == {"memory_id", "kind", "at", "superseded_by"}
    assert change_item(replaced)["superseded_by"] == str(newer)


@pytest.mark.asyncio
async def test_the_handler_renders_a_forgotten_memory_without_a_summary_key(db_session, seeded):
    """``handle_changes_since`` end to end on the seeded rows: the forgotten row's
    ``created`` and ``forgotten`` items have no ``summary`` key; live rows keep it."""
    import json
    from unittest.mock import AsyncMock, patch

    from mcp_server.tools.listing import handle_changes_since

    owner, ws, ctx, rows, t0 = seeded
    context = await db_session.get(Context, ctx)

    async def one_session():
        yield db_session

    with (
        patch("db.base.get_db", new=one_session),
        patch(
            "mcp_server.tools.listing._resolve_context_for_read", AsyncMock(return_value=context)
        ),
        patch("mcp_server.tools.listing._log_tool_usage", AsyncMock()),
    ):
        (block,) = await handle_changes_since(
            {"context_id": str(ctx), "since": (t0 - timedelta(seconds=1)).isoformat()}, owner, ws
        )
    payload = json.loads(block.text)
    assert payload["status"] == "success" and payload["count"] == 6
    gone = [c for c in payload["changes"] if c["memory_id"] == str(rows["gone"].id)]
    assert [c["kind"] for c in gone] == ["created", "forgotten"]
    assert all(set(c) == {"memory_id", "kind", "at"} for c in gone)
    assert "forgotten" not in block.text.replace('"kind":"forgotten"', "")  # the seeded summary
    live = [c for c in payload["changes"] if c["memory_id"] != str(rows["gone"].id)]
    assert [c["summary"] for c in live] == ["open task", "done task", "a note", "done task"]


@pytest.fixture
async def typed_details(db_session):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner, private=False)
    for name, details in (
        ("flag string", {"flag": "true"}),
        ("flag boolean", {"flag": True}),
        ("n int", {"n": 2}),
        ("n float", {"n": 2.0}),
        ("n string", {"n": "2"}),
        ("n null", {"n": None}),
        ("n object", {"n": {"v": 2}}),
        ("text", {"note": 'say "hé" \\ ok'}),
        ("no details", None),
    ):
        await _row(db_session, owner, ws, ctx, summary=name, details=details)
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"details.flag": True}, {"flag boolean"}),
        ({"details.flag": "true"}, {"flag string"}),
        ({"details.flag": False}, set()),
        ({"details.n": 2}, {"n int"}),
        ({"details.n": 2.0}, {"n float"}),
        ({"details.n": "2"}, {"n string"}),
        ({"details.n": "2.0"}, set()),
        ({"details.n": 1}, set()),
        ({"details.n": "null"}, set()),
        ({"details.note": 'say "hé" \\ ok'}, {"text"}),  # strings compare unescaped
        ({"details.absent": "x"}, set()),
    ],
)
async def test_details_filter_matches_json_type_and_spelling(
    db_session, typed_details, filters, expected
):
    """A string matches a stored string only, a boolean a stored boolean only, and a
    number a stored number written the same way (``2`` is not ``2.0``)."""
    page = await list_memories(
        db_session, context_id=typed_details, owner_user_id=None, filters=filters
    )
    assert {m.summary for m in page.rows} == expected


# ------------------------------------------------------------------ #1924


@pytest.mark.asyncio
async def test_forget_adds_no_updated_event_to_the_log(db_session):
    """remember -> forget: the log lists exactly created and forgotten. The
    soft delete is dated by deleted_at; it must not stamp updated_at and so
    read as an edit made after the memory was forgotten."""
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner, private=False)
    since = utcnow() - timedelta(seconds=1)
    mem = await _row(db_session, owner, ws, ctx, summary="to forget")
    service = MemoryService(db_session)
    with (
        patch.object(db_session, "commit", AsyncMock()),
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ),
        patch("services.memory_service.resolve_collection_name", AsyncMock(return_value="c")),
        patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()),
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
    ):
        res = await service.forget(ForgetRequest(memory_id=mem.id), owner)
    assert res.deleted_count == 1
    await db_session.refresh(mem)
    assert mem.deleted_at is not None
    assert mem.updated_at is None
    page = await changes_since(
        db_session, context_id=ctx, owner_user_id=None, since=since, until=None
    )
    assert [(c.kind, c.memory_id) for c in page.changes] == [
        ("created", mem.id),
        ("forgotten", mem.id),
    ]


@pytest.mark.asyncio
async def test_a_tombstone_stamped_by_an_older_forget_lists_no_updated_event(db_session, seeded):
    """Rows forgotten before #1924 carry forget's own updated_at a few ms after
    deleted_at: that is not an edit. An edit before the delete still is."""
    owner, ws, ctx, rows, t0 = seeded
    deleted = t0 + timedelta(hours=10)
    legacy = await _row(
        db_session,
        owner,
        ws,
        ctx,
        summary="legacy tombstone",
        created_at=t0 + timedelta(hours=9),
        updated_at=deleted + timedelta(milliseconds=2),
        deleted_at=deleted,
    )
    page = await changes_since(
        db_session,
        context_id=ctx,
        owner_user_id=None,
        since=t0 + timedelta(hours=9),
        until=t0 + timedelta(hours=11),
    )
    assert [(c.kind, c.memory_id) for c in page.changes] == [
        ("created", legacy.id),
        ("forgotten", legacy.id),
    ]
