"""``GET /api/v1/admin/embedding/failures`` against Postgres (#2832).

Over HTTP (the real query parsing), as a session admin and as a service
operator (``require_admin`` is overridden with each principal it builds):

- for a ``workspace_id`` / ``context_id`` pair the sum of ``failed`` is the
  ``reset_count`` of ``POST /api/v1/admin/embedding/retry-failed`` with the
  same ids, and ``failed + erased`` is it with ``include_erased=true``;
- ``stalled``, ``capped``, ``configuration``, ``other`` and ``erased`` are the
  sweep's diagnostic and ``get_unsearchable_memory_stats`` on the same rows;
- soft-deleted memories are not counted, and a memory without a context is
  counted in a group with ``context_id`` null;
- groups come by ``failed`` descending, then workspace, then context, the
  same on every page, and ``total`` counts groups;
- the answer holds counts, ids of workspaces and contexts, their names and
  two times: no memory id, text, tag, author id or stored error.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes import admin as admin_routes
from auth.dependencies import require_admin
from auth.system_admin_mode import build_operator_principal
from config.constants import MAX_EMBEDDING_RETRIES
from db.base import get_db
from models.auth import AuditLog, Context, Workspace, WorkspaceMember
from models.memory import Memory
from services.workspace_service import WorkspaceService
from tasks import embedding_tasks

pytestmark = pytest.mark.asyncio(loop_scope="session")

PRINCIPALS = {
    "session": {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"},
    "service": build_operator_principal("ops@example.com"),
}
URL = "/api/v1/admin/embedding/failures"
RETRY = "/api/v1/admin/embedding/retry-failed"
GROUP_KEYS = {
    "workspace_id",
    "workspace_name",
    "context_id",
    "context_name",
    "failed",
    "stalled",
    "capped",
    "configuration",
    "other",
    "erased",
    "oldest_attempt_at",
    "newest_attempt_at",
}
COUNTS = ("failed", "stalled", "capped", "configuration", "other", "erased")
T0 = datetime(2026, 11, 1, 3, 0, 0)


def _sentinel(label: str) -> str:
    return f"SENTINEL-{label}-{secrets.token_hex(8)}"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


def _client(maker, principal: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/api/v1")

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: principal
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """Failed embeddings of every class in two workspaces, rebuilt for each test.

    ===========  ======  =======  ======  =============  =====  ======
    group        failed  stalled  capped  configuration  other  erased
    ===========  ======  =======  ======  =============  =====  ======
    big          6       2        1       2              3      2
    small        1       0        1       0              0      0
    no context   2       1        0       0              2      0
    erased only  0       0        0       0              0      1
    ===========  ======  =======  ======  =============  =====  ======
    """
    tag = uuid4().hex[:8]
    owner, author = f"owner-{tag}", f"author-{tag}"
    sentinels = {
        key: _sentinel(key) for key in ("error", "summary", "content", "tag", "details", "author")
    }
    author = f"{author}-{sentinels['author']}"

    first = Workspace(id=uuid4(), name=f"first-{tag}", owner_user_id=owner)
    second = Workspace(id=uuid4(), name=f"second-{tag}", owner_user_id=owner)
    db_session.add_all([first, second])
    await db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=first.id, user_id=owner, role="owner"))
    big = Context(
        id=uuid4(), workspace_id=first.id, name=f"big-{tag}", created_by=owner, is_private=False
    )
    small = Context(
        id=uuid4(), workspace_id=first.id, name=f"small-{tag}", created_by=owner, is_private=True
    )
    healthy = Context(
        id=uuid4(), workspace_id=first.id, name=f"healthy-{tag}", created_by=owner, is_private=False
    )
    erased_only = Context(
        id=uuid4(), workspace_id=second.id, name=f"erased-{tag}", created_by=owner, is_private=False
    )
    db_session.add_all([big, small, healthy, erased_only])
    await db_session.flush()
    # A workspace with no row: its failed memories have no context either.
    orphan_workspace = uuid4()
    memory_ids: list[UUID] = []

    def memory(
        workspace_id: UUID,
        context: Context | None,
        *,
        status: str = "failed",
        error: str | None = None,
        retries: int = 0,
        attempted: datetime | None = None,
        suppressed: bool = False,
        deleted: bool = False,
    ) -> None:
        row = Memory(
            id=uuid4(),
            user_id=author,
            workspace_id=workspace_id,
            context_id=context.id if context is not None else None,
            summary=f"{sentinels['summary']} summary",
            content=sentinels["content"],
            tags=[sentinels["tag"]],
            details={"note": sentinels["details"]},
            type="note",
            client="pytest",
            embedding_status=status,
            embedding_error=error,
            embedding_retry_count=retries,
            embedding_attempted_at=attempted,
            embedding_suppressed_at=datetime.now(UTC) if suppressed else None,
            deleted_at=T0 if deleted else None,
        )
        memory_ids.append(row.id)
        db_session.add(row)

    provider_error = f"provider said {sentinels['error']}"
    # big: 6 failed (2 stalled; 1 capped, 2 configuration, 3 other) + 2 erased.
    memory(first.id, big, error=provider_error, retries=1, attempted=T0)
    memory(first.id, big, error=provider_error, retries=MAX_EMBEDDING_RETRIES)
    memory(first.id, big, error=None, retries=MAX_EMBEDDING_RETRIES + 1, attempted=T0)
    memory(first.id, big, error=f"QUOTA-002: cap {sentinels['error']}", attempted=T0)
    memory(first.id, big, error=f"CFG-001: no key {sentinels['error']}")
    memory(first.id, big, error="FEAT-003: plan", attempted=T0 + timedelta(days=13))
    memory(first.id, big, error="erased_author: dequeued", suppressed=True, attempted=T0)
    # An erased row whose error was overwritten by a later class: still erased only.
    memory(
        first.id,
        big,
        error="QUOTA-002: cap",
        retries=MAX_EMBEDDING_RETRIES,
        suppressed=True,
        attempted=T0 - timedelta(days=2),
    )
    # Not counted: soft-deleted, and every status but failed.
    memory(first.id, big, error=provider_error, deleted=True)
    memory(first.id, big, error="CFG-001: gone", suppressed=True, deleted=True)
    memory(first.id, big, status="pending")
    memory(first.id, big, status="processing")
    memory(first.id, big, status="success")
    memory(first.id, healthy, status="success")
    # small: one capped row.
    memory(first.id, small, error="QUOTA-002: cap")
    # No context: its own group, in a workspace that has no row.
    memory(orphan_workspace, None, error="code-less failure", retries=MAX_EMBEDDING_RETRIES)
    memory(orphan_workspace, None, error=" CFG-001: a leading space is not the prefix")
    # second workspace: erased rows only. Its error reads as a configuration
    # failure, and the row is still erased only.
    memory(second.id, erased_only, error="CFG-001: no key", suppressed=True)
    await db_session.commit()

    workspace_ids = [first.id, second.id]
    yield SimpleNamespace(
        tag=tag,
        first=first.id,
        first_name=first.name,
        second=second.id,
        orphan=orphan_workspace,
        owner=owner,
        author=author,
        big=big.id,
        big_name=big.name,
        small=small.id,
        healthy=healthy.id,
        erased_only=erased_only.id,
        contexts=[big, small, healthy],
        sentinels=sentinels,
        memory_ids=memory_ids,
    )

    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.id.in_(memory_ids)))
    await db_session.execute(delete(Context).where(Context.workspace_id.in_(workspace_ids)))
    await db_session.execute(
        delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(workspace_ids))
    )
    await db_session.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
    await db_session.commit()


async def _groups(client: httpx.AsyncClient, **params) -> dict:
    response = await client.get(URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _by_key(body: dict) -> dict[tuple[str, str | None], dict]:
    return {(group["workspace_id"], group["context_id"]): group for group in body["groups"]}


def _counts(group: dict) -> dict[str, int]:
    return {name: group[name] for name in COUNTS}


def _mine(world, body: dict) -> list[dict]:
    """The groups of this test's rows (other tests may have left failed rows)."""
    mine = {str(world.first), str(world.second), str(world.orphan)}
    return [group for group in body["groups"] if group["workspace_id"] in mine]


@pytest.mark.parametrize("mode", list(PRINCIPALS))
class TestEmbeddingFailures:
    async def test_each_group_carries_its_counts_names_and_times(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _groups(client, limit=500)

        groups = _by_key(body)
        big = groups[(str(world.first), str(world.big))]
        assert big == {
            "workspace_id": str(world.first),
            "workspace_name": world.first_name,
            "context_id": str(world.big),
            "context_name": world.big_name,
            "failed": 6,
            "stalled": 2,
            "capped": 1,
            "configuration": 2,
            "other": 3,
            "erased": 2,
            # Over the group's rows, the erased ones included.
            "oldest_attempt_at": "2026-10-30T03:00:00Z",
            "newest_attempt_at": "2026-11-14T03:00:00Z",
        }
        assert _counts(groups[(str(world.first), str(world.small))]) == {
            "failed": 1,
            "stalled": 0,
            "capped": 1,
            "configuration": 0,
            "other": 0,
            "erased": 0,
        }
        assert _counts(groups[(str(world.second), str(world.erased_only))]) == {
            "failed": 0,
            "stalled": 0,
            "capped": 0,
            "configuration": 0,
            "other": 0,
            "erased": 1,
        }
        # A context with no failed row has no group.
        assert (str(world.first), str(world.healthy)) not in groups
        for group in body["groups"]:
            assert set(group) == GROUP_KEYS
            assert group["failed"] == group["capped"] + group["configuration"] + group["other"]
            assert group["stalled"] <= group["failed"]

    async def test_a_memory_without_a_context_is_its_own_group_with_null_names(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _groups(client, workspace_id=str(world.orphan))

        [group] = body["groups"]
        assert body["total"] == 1
        assert group == {
            "workspace_id": str(world.orphan),
            # The workspace has no row: no name.
            "workspace_name": None,
            "context_id": None,
            "context_name": None,
            "failed": 2,
            "stalled": 1,
            "capped": 0,
            "configuration": 0,
            "other": 2,
            "erased": 0,
            "oldest_attempt_at": None,
            "newest_attempt_at": None,
        }

    async def test_soft_deleted_memories_and_other_statuses_are_not_counted(
        self, world, maker, db_session, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _groups(client, workspace_id=str(world.first))

        listed = sum(group["failed"] + group["erased"] for group in body["groups"])
        rows = await db_session.scalar(
            select(func.count()).select_from(Memory).where(Memory.workspace_id == world.first)
        )
        assert (listed, rows) == (9, 15)

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            ({"workspace_id": "first"}, [("first", "big"), ("first", "small")]),
            ({"context_id": "big"}, [("first", "big")]),
            ({"workspace_id": "first", "context_id": "small"}, [("first", "small")]),
            ({"workspace_id": "second"}, [("second", "erased_only")]),
            ({"workspace_id": "second", "context_id": "big"}, []),
            ({"context_id": "healthy"}, []),
        ],
    )
    async def test_the_filters_are_anded(self, world, maker, mode, filters, expected) -> None:
        params = {key: str(getattr(world, value)) for key, value in filters.items()}
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _groups(client, **params)

        assert [(g["workspace_id"], g["context_id"]) for g in body["groups"]] == [
            (str(getattr(world, ws)), str(getattr(world, ctx))) for ws, ctx in expected
        ]
        assert body["total"] == len(expected)

    async def test_groups_come_by_failed_descending_then_workspace_then_context(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _groups(client, limit=500)

        order = [
            (-group["failed"], group["workspace_id"], group["context_id"] or "~")
            for group in body["groups"]
        ]
        assert order == sorted(order)
        mine = [(g["workspace_id"], g["context_id"]) for g in _mine(world, body)]
        assert mine == [
            (str(world.first), str(world.big)),
            (str(world.orphan), None),
            (str(world.first), str(world.small)),
            (str(world.second), str(world.erased_only)),
        ]

    async def test_paging_is_stable_and_total_counts_groups(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            whole = await _groups(client, limit=500)
            total = whole["total"]
            pages = [await _groups(client, limit=1, offset=offset) for offset in range(total + 1)]

        assert total == len(whole["groups"]) >= 4
        assert [len(page["groups"]) for page in pages] == [1] * total + [0]
        assert {page["total"] for page in pages} == {total}
        assert [(page["limit"], page["offset"]) for page in pages[:2]] == [(1, 0), (1, 1)]
        assert [group for page in pages for group in page["groups"]] == whole["groups"]

    @pytest.mark.parametrize(
        "params",
        [
            {"workspace_id": "not-a-uuid"},
            {"context_id": "not-a-uuid"},
            {"limit": 0},
            {"limit": 501},
            {"offset": -1},
        ],
    )
    async def test_a_bad_value_is_a_422(self, world, maker, mode, params) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            response = await client.get(URL, params=params)

        assert response.status_code == 422

    @pytest.mark.parametrize(
        "pair",
        [
            {"workspace_id": "first"},
            {"context_id": "big"},
            {"workspace_id": "first", "context_id": "big"},
            {"workspace_id": "first", "context_id": "small"},
            {"workspace_id": "second"},
            {"workspace_id": "orphan"},
            {"context_id": "healthy"},
        ],
    )
    @pytest.mark.parametrize("include_erased", [False, True])
    async def test_the_sums_are_what_the_retry_resets(
        self, world, maker, mode, pair, include_erased
    ) -> None:
        params = {key: str(getattr(world, value)) for key, value in pair.items()}
        async with _client(maker, PRINCIPALS[mode]) as client:
            # Read first: the retry empties the groups it resets.
            body = await _groups(client, limit=500, **params)
            failed = sum(group["failed"] for group in body["groups"])
            erased = sum(group["erased"] for group in body["groups"])
            retry = await client.post(
                RETRY, params={**params, "include_erased": str(include_erased).lower()}
            )
            after = await _groups(client, limit=500, **params)

        assert retry.status_code == 200, retry.text
        assert retry.json()["reset_count"] == (failed + erased if include_erased else failed)
        # What the retry did not reset is still listed: the erased rows.
        assert sum(group["failed"] for group in after["groups"]) == 0
        assert sum(group["erased"] for group in after["groups"]) == (
            0 if include_erased else erased
        )

    async def test_the_classes_are_the_sweeps_diagnostic_on_the_same_rows(
        self, world, maker, db_session, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _groups(client, limit=500)
        assert body["total"] == len(body["groups"])
        summed = {name: sum(group[name] for group in body["groups"]) for name in COUNTS}

        await db_session.rollback()
        with patch.object(embedding_tasks, "logger") as log:
            await embedding_tasks._log_unsearchable_backlog(db_session)
        [line] = [
            call
            for call in log.warning.call_args_list
            if call.args[0] == "embedding_unsearchable_backlog"
        ]
        assert line.kwargs == {
            "unsearchable": summed["failed"],
            "stalled": summed["stalled"],
            "capped": summed["capped"],
            "configuration": summed["configuration"],
            "erased": summed["erased"],
            "other": summed["other"],
        }

    async def test_failed_stalled_and_erased_are_the_workspace_stats_per_context(
        self, world, maker, db_session, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = await _groups(client, workspace_id=str(world.first))
        groups = _by_key(body)

        await db_session.rollback()
        service = WorkspaceService(db_session)
        unsearchable = await service.get_unsearchable_memory_stats(
            world.owner, world.contexts, is_workspace_owner=True
        )
        erased = await service.get_erased_memory_stats(
            world.owner, world.contexts, is_workspace_owner=True
        )

        assert unsearchable == {str(world.big): (6, 2), str(world.small): (1, 0)}
        for context_id, (failed, stalled) in unsearchable.items():
            group = groups[(str(world.first), context_id)]
            assert (group["failed"], group["stalled"]) == (failed, stalled)
            assert group["erased"] == erased.get(context_id, 0)
        assert set(unsearchable) == {context_id for _, context_id in groups}

    async def test_no_memory_id_text_tag_author_or_stored_error_is_answered(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            responses = [
                await client.get(URL, params={"limit": 500}),
                await client.get(URL, params={"workspace_id": str(world.first)}),
                await client.get(URL, params={"context_id": str(world.big)}),
            ]

        for response in responses:
            assert response.status_code == 200
            raw = response.text
            assert str(world.big) in raw
            for label, sentinel in world.sentinels.items():
                assert sentinel not in raw, label
            for memory_id in world.memory_ids:
                assert str(memory_id) not in raw
            for text in ("provider said", "QUOTA-002", "CFG-001", "FEAT-003", "erased_author"):
                assert text not in raw, text
            assert world.author not in raw
            assert world.owner not in raw
            for group in response.json()["groups"]:
                assert set(group) == GROUP_KEYS


@pytest.mark.parametrize("mode", list(PRINCIPALS))
async def test_the_read_writes_no_audit_row_and_changes_no_row(
    world, maker, db_session, mode
) -> None:
    async def _state() -> tuple[int, list[tuple]]:
        await db_session.rollback()
        audits = await db_session.scalar(select(func.count()).select_from(AuditLog))
        rows = (
            await db_session.execute(
                select(
                    Memory.id,
                    Memory.embedding_status,
                    Memory.embedding_retry_count,
                    Memory.embedding_error,
                    Memory.embedding_suppressed_at,
                )
                .where(Memory.id.in_(world.memory_ids))
                .order_by(Memory.id)
            )
        ).all()
        return audits, [tuple(row) for row in rows]

    before = await _state()
    async with _client(maker, PRINCIPALS[mode]) as client:
        await _groups(client, limit=500)
        await _groups(client, workspace_id=str(world.first), context_id=str(world.big))
    assert await _state() == before
