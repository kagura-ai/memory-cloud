"""Run Sleep and undo a merge for a target user, on real Postgres (#2838).

Over HTTP against the real routes, as a session admin and as a service
operator (``require_admin`` is overridden with each principal it builds).
Only the background job, the vector store and the embedder are replaced.

Run (``POST /api/v1/admin/users/{user_id}/sleep/run``):

- the report belongs to the target user, and the report and its
  ``sleep_run_trigger`` audit row are both committed when the handler returns;
- 409 ``SLEEP-002`` while the target has a running report, and not while only
  the caller has one;
- 404 ``SLEEP-003`` for a soft-deleted context, ``sleep_mode = 'skip'``, a
  context with only another user's live memory, with only a soft-deleted
  memory of the target, and for an unknown context; 404 without that code for
  an unknown user. A refusal writes nothing.

Undo (``POST /api/v1/admin/sleep-reports/{report_id}/actions/{action_id}/undo-merge``):

- the loser is live again, ``undone_by`` is ``operator`` for an operator and
  the caller's id for a session admin, and a ``sleep_merge_undo`` audit row
  names the caller, the report, the action and the memories, on the tombstone
  path and on the shadow path;
- an action of another report is 404 and restores nothing; a second undo is
  409 and leaves one audit row;
- the own-account undo keeps its caller filter and writes no audit row.

No answer and no audit row holds memory text, tags, a user's e-mail or name,
or a context's name.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import memory_cloud_exception_handler
from api.routes import admin_sleep
from auth.dependencies import require_admin
from auth.system_admin_mode import build_operator_principal
from db.base import get_db
from models.auth import (
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    AuditLog,
    Context,
    User,
    Workspace,
)
from models.memory import (
    DELETED_BY_SLEEP_MERGE,
    EDGE_ORIGIN_SEMANTIC,
    EDGE_TYPE_SUPERSEDES,
    Memory,
    NeuralMemoryEdge,
)
from models.operation_history import OperationHistory
from models.sleep import SleepAction, SleepReport
from services.capacity_lock import capacity_lock_state
from utils.datetime import utcnow
from utils.exceptions import MemoryCloudException

pytestmark = pytest.mark.asyncio(loop_scope="session")

OPERATOR_ADDRESS = "ops@example.com"
OPERATOR = build_operator_principal(OPERATOR_ADDRESS)
AUDIT_ACTIONS = ("sleep_run_trigger", "sleep_merge_undo")


def _token(label: str) -> str:
    return f"{label}-{secrets.token_hex(8)}"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


def _client(maker, principal: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.include_router(admin_sleep.router, prefix="/api/v1")
    app.include_router(admin_sleep.target_router, prefix="/api/v1")

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: dict(principal)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def jobs(monkeypatch) -> list[tuple[list, str]]:
    """The batches a run handler scheduled; the phases never run here."""
    scheduled: list[tuple[list, str]] = []

    async def fake_batch(batch: list, user_id: str) -> None:
        scheduled.append((batch, user_id))

    monkeypatch.setattr(admin_sleep, "_run_sleep_batch", fake_batch)
    return scheduled


@pytest.fixture
def no_vector_store():
    """The restore's point write and embedder, replaced."""
    with (
        patch(
            "services.sleep.undo.re_embed_memory_to_qdrant", new=AsyncMock(return_value=None)
        ) as re_embed,
        patch("services.embedding_service.EmbeddingService", MagicMock()),
    ):
        yield re_embed


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """A target user U, another user V and a session admin A, with contexts of
    every eligibility shape and reports that recorded merges."""
    tag = uuid4().hex[:8]
    sentinels = SimpleNamespace(
        email=f"{_token('target-mail')}@sleep-target.example",
        name=_token("Target Name"),
        context_name=_token("ctx-name"),
        context_display=_token("Context Display"),
        content=_token("memory-content"),
        summary=_token("memory-summary"),
        tag=_token("memory-tag"),
        details=_token("memory-details"),
        action_summary=_token("merged-summary"),
    )
    target, other, admin = f"sleep-u-{tag}", f"sleep-v-{tag}", f"sleep-a-{tag}"
    db_session.add(User(email=sentinels.email, user_id=target, name=sentinels.name, role="user"))
    db_session.add(User(email=f"{other}@test.example", user_id=other, name=other, role="user"))
    db_session.add(User(email=f"{admin}@test.example", user_id=admin, name=admin, role="admin"))
    await db_session.flush()

    def workspace() -> Workspace:
        return Workspace(
            id=uuid4(),
            name=f"sleep-target-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=target,
            daily_api_limit=5000,
            weekly_api_limit=25000,
        )

    ws = workspace()
    db_session.add(ws)
    await db_session.flush()

    def context(label: str, **cols) -> Context:
        values = {"sleep_mode": "full", "name": f"{label}-{tag}"} | cols
        return Context(id=uuid4(), workspace_id=ws.id, created_by=other, **values)

    contexts = {
        "ok": context("ok", name=sentinels.context_name, display_name=sentinels.context_display),
        "deleted": context("deleted", deleted_at=utcnow()),
        "skip": context("skip", sleep_mode="skip"),
        "other_only": context("otheronly"),
        "dead_memory": context("deadmemory"),
        "no_memory": context("nomemory"),
        "admin_own": context("adminown"),
    }
    db_session.add_all(contexts.values())
    await db_session.flush()

    def memory(key: str, user_id: str, **cols) -> Memory:
        values = {
            "summary": sentinels.summary,
            "content": sentinels.content,
            "tags": [sentinels.tag],
            "details": {"note": sentinels.details},
            "workspace_id": ws.id,
        } | cols
        return Memory(
            id=uuid4(),
            context_id=contexts[key].id,
            user_id=user_id,
            type="note",
            scope="working",
            client="test",
            embedding_status="success",
            **values,
        )

    tombstone = {
        "deleted_at": utcnow() - timedelta(hours=1),
        "deleted_by": DELETED_BY_SLEEP_MERGE,
    }
    memories = {
        "winner": memory("ok", target),
        "loser": memory("ok", target, **tombstone),
        "shadow_winner": memory("ok", target),
        "shadow_loser": memory("ok", target),
        "deleted_ctx": memory("deleted", target),
        "skip_ctx": memory("skip", target),
        "other_only": memory("other_only", other),
        "dead": memory("dead_memory", target, deleted_at=utcnow(), deleted_by="user_delete"),
        "other_loser": memory("ok", other, **tombstone),
        "admin_loser": memory("admin_own", admin, **tombstone),
    }
    db_session.add_all(memories.values())
    await db_session.flush()
    db_session.add(
        NeuralMemoryEdge(
            user_id=target,
            src_id=memories["shadow_winner"].id,
            dst_id=memories["shadow_loser"].id,
            workspace_id=ws.id,
            context_id=contexts["ok"].id,
            edge_type=EDGE_TYPE_SUPERSEDES,
            weight=0.9,
            confidence=0.9,
            origin=EDGE_ORIGIN_SEMANTIC,
        )
    )

    def report(user_id: str, key: str) -> SleepReport:
        return SleepReport(
            id=uuid4(),
            user_id=user_id,
            workspace_id=ws.id,
            context_id=contexts[key].id,
            status="completed",
        )

    reports = {
        "target": report(target, "ok"),
        "other": report(other, "ok"),
        "admin": report(admin, "admin_own"),
    }
    db_session.add_all(reports.values())
    await db_session.flush()

    def merge(report_key: str, winner: str | None, loser: str, **details) -> SleepAction:
        return SleepAction(
            report_id=reports[report_key].id,
            phase="dedup_merge",
            action_type="merge",
            memory_id=memories[winner].id if winner else None,
            target_id=memories[loser].id,
            details={"loser_summary": sentinels.action_summary, "tags": [sentinels.tag]} | details,
        )

    actions = {
        "merge": merge("target", "winner", "loser"),
        "shadow": merge("target", "shadow_winner", "shadow_loser", mode="shadow", prior_edge=None),
        "other_merge": merge("other", None, "other_loser"),
        "admin_merge": merge("admin", None, "admin_loser"),
        "edge": SleepAction(
            report_id=reports["target"].id,
            phase="edge_discovery",
            action_type="create_edge",
            memory_id=memories["winner"].id,
            target_id=memories["shadow_winner"].id,
            details={"edge_type": "related_to"},
        ),
    }
    db_session.add_all(actions.values())
    await db_session.commit()

    names = SimpleNamespace(
        target=target,
        other=other,
        admin=admin,
        admin_principal={"user_id": admin, "email": f"{admin}@test.example", "role": "admin"},
        ws=ws.id,
        contexts={key: value.id for key, value in contexts.items()},
        memories={key: value.id for key, value in memories.items()},
        reports={key: value.id for key, value in reports.items()},
        actions={key: value.id for key, value in actions.items()},
        sentinels=list(vars(sentinels).values()),
    )
    workspace_ids = [ws.id]
    users = [target, other, admin]
    try:
        yield names
    finally:
        await db_session.rollback()
        await db_session.execute(delete(AuditLog).where(AuditLog.action.in_(AUDIT_ACTIONS)))
        await db_session.execute(
            delete(OperationHistory).where(OperationHistory.workspace_id.in_(workspace_ids))
        )
        await db_session.execute(
            delete(NeuralMemoryEdge).where(NeuralMemoryEdge.user_id.in_(users))
        )
        await db_session.execute(delete(SleepReport).where(SleepReport.user_id.in_(users)))
        await db_session.execute(delete(Memory).where(Memory.workspace_id.in_(workspace_ids)))
        await db_session.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
        await db_session.execute(delete(User).where(User.user_id.in_(users)))
        await db_session.commit()


def _principal(world, mode: str) -> dict:
    return OPERATOR if mode == "service" else world.admin_principal


def _run_url(user_id: str) -> str:
    return f"/api/v1/admin/users/{user_id}/sleep/run"


def _undo_url(report_id: UUID, action_id: int) -> str:
    return f"/api/v1/admin/sleep-reports/{report_id}/actions/{action_id}/undo-merge"


async def _audit_rows(db: AsyncSession, action: str) -> list[AuditLog]:
    rows = list(
        (
            await db.execute(
                select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
            )
        ).scalars()
    )
    return rows


async def _reports_of(db: AsyncSession, user_id: str, status: str = "running") -> list[SleepReport]:
    rows = list(
        (
            await db.execute(
                select(SleepReport).where(
                    SleepReport.user_id == user_id, SleepReport.status == status
                )
            )
        ).scalars()
    )
    return rows


def _assert_no_sentinel(world, *texts: str) -> None:
    for text in texts:
        for sentinel in world.sentinels:
            assert sentinel not in text


# --------------------------------------------------------------------- run


@pytest.mark.parametrize("mode", ["service", "session"])
async def test_a_run_for_the_target_user_commits_the_report_and_its_audit_row(
    maker, world, jobs, mode
):
    principal = _principal(world, mode)
    context_id = world.contexts["ok"]
    async with _client(maker, principal) as client:
        response = await client.post(_run_url(world.target), json={"context_id": str(context_id)})

    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"report_ids"} and len(body["report_ids"]) == 1
    report_id = UUID(body["report_ids"][0])
    # Read through a second session: both rows were committed by the handler.
    async with maker() as second:
        report = await second.get(SleepReport, report_id)
        assert report is not None
        assert report.user_id == world.target
        assert report.user_id != principal["user_id"]
        assert (report.workspace_id, report.context_id) == (world.ws, context_id)
        assert report.status == "running"
        [audit] = await _audit_rows(second, "sleep_run_trigger")
    assert audit.user_id == principal["user_id"]
    assert audit.user_email == principal["email"]
    assert audit.resource == f"context:{context_id}"
    assert audit.user_metadata == {
        "target_user_id": world.target,
        "workspace_id": str(world.ws),
        "context_id": str(context_id),
        "report_id": str(report_id),
    }
    assert audit.ip_address is None and audit.user_agent is None
    # The job runs for the target user.
    await asyncio.sleep(0)
    assert jobs == [([(report_id, str(world.ws), str(context_id))], world.target)]
    _assert_no_sentinel(world, response.text, json.dumps(audit.user_metadata), audit.resource)


async def test_a_second_run_while_the_target_has_a_running_report_is_409(maker, world, jobs):
    body = {"context_id": str(world.contexts["ok"])}
    async with _client(maker, OPERATOR) as client:
        first = await client.post(_run_url(world.target), json=body)
        second = await client.post(_run_url(world.target), json=body)

    assert first.status_code == 202, first.text
    assert second.status_code == 409, second.text
    answer = second.json()
    assert answer["error"] == "SLEEP-002"
    assert set(answer["details"]) == {"running_report_id", "started_at"}
    assert answer["details"]["running_report_id"] == first.json()["report_ids"][0]
    async with maker() as check:
        assert len(await _reports_of(check, world.target)) == 1
        assert len(await _audit_rows(check, "sleep_run_trigger")) == 1
    await asyncio.sleep(0)
    assert len(jobs) == 1
    _assert_no_sentinel(world, second.text)


async def test_the_callers_own_running_report_is_no_conflict(
    maker, world, jobs, db_session: AsyncSession
):
    db_session.add(
        SleepReport(
            id=uuid4(),
            user_id=world.admin,
            workspace_id=world.ws,
            context_id=world.contexts["admin_own"],
            status="running",
        )
    )
    await db_session.commit()

    async with _client(maker, world.admin_principal) as client:
        response = await client.post(
            _run_url(world.target), json={"context_id": str(world.contexts["ok"])}
        )

    assert response.status_code == 202, response.text
    async with maker() as check:
        [report] = await _reports_of(check, world.target)
    assert str(report.id) == response.json()["report_ids"][0]


@pytest.mark.parametrize(
    "case", ["deleted", "skip", "other_only", "dead_memory", "no_memory", "unknown"]
)
@pytest.mark.parametrize("mode", ["service", "session"])
async def test_a_context_that_is_not_eligible_is_404_sleep_003(maker, world, jobs, mode, case):
    context_id = uuid4() if case == "unknown" else world.contexts[case]
    async with _client(maker, _principal(world, mode)) as client:
        response = await client.post(_run_url(world.target), json={"context_id": str(context_id)})

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["error"] == "SLEEP-003"
    assert body["details"] == {"context_id": str(context_id)}
    async with maker() as check:
        assert await _reports_of(check, world.target) == []
        assert await _audit_rows(check, "sleep_run_trigger") == []
    await asyncio.sleep(0)
    assert jobs == []
    _assert_no_sentinel(world, response.text)


async def test_every_ineligible_cause_answers_the_same_message(maker, world, jobs):
    messages = set()
    async with _client(maker, OPERATOR) as client:
        for case in ("deleted", "skip", "other_only", "dead_memory", "no_memory"):
            response = await client.post(
                _run_url(world.target), json={"context_id": str(world.contexts[case])}
            )
            assert response.status_code == 404, response.text
            messages.add(response.json()["message"])
        unknown = await client.post(_run_url(world.target), json={"context_id": str(uuid4())})
        messages.add(unknown.json()["message"])

    assert len(messages) == 1


async def test_the_other_users_memory_makes_the_context_eligible_for_that_user_only(
    maker, world, jobs
):
    """The memory check is keyed on the target: the same context runs for the
    user who has a live memory in it."""
    body = {"context_id": str(world.contexts["other_only"])}
    async with _client(maker, OPERATOR) as client:
        for_target = await client.post(_run_url(world.target), json=body)
        for_other = await client.post(_run_url(world.other), json=body)

    assert for_target.status_code == 404
    assert for_other.status_code == 202, for_other.text
    async with maker() as check:
        [report] = await _reports_of(check, world.other)
    assert report.context_id == world.contexts["other_only"]


@pytest.mark.parametrize("mode", ["service", "session"])
async def test_an_unknown_user_is_404_without_the_sleep_code(maker, world, jobs, mode):
    unknown = f"no-such-user-{uuid4().hex[:8]}"
    async with _client(maker, _principal(world, mode)) as client:
        response = await client.post(
            _run_url(unknown), json={"context_id": str(world.contexts["ok"])}
        )

    assert response.status_code == 404, response.text
    assert response.json() == {"error": "RES-001", "message": "User not found", "details": {}}
    async with maker() as check:
        assert await _reports_of(check, unknown) == []
        assert await _audit_rows(check, "sleep_run_trigger") == []
    await asyncio.sleep(0)
    assert jobs == []


async def test_a_missing_context_id_is_422(maker, world, jobs):
    async with _client(maker, OPERATOR) as client:
        for body in ({}, {"context_id": None}):
            response = await client.post(_run_url(world.target), json=body)
            assert response.status_code == 422, response.text

    async with maker() as check:
        assert await _reports_of(check, world.target) == []
        assert await _audit_rows(check, "sleep_run_trigger") == []


# -------------------------------------------------------------------- undo


async def _memory(db: AsyncSession, memory_id: UUID) -> Memory:
    row = (await db.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()
    return row


async def _undo_actions(db: AsyncSession, report_id: UUID) -> list[SleepAction]:
    rows = list(
        (
            await db.execute(
                select(SleepAction)
                .where(SleepAction.report_id == report_id, SleepAction.action_type == "undo_merge")
                .order_by(SleepAction.id)
            )
        ).scalars()
    )
    return rows


@pytest.mark.parametrize("mode", ["service", "session"])
async def test_an_undo_of_the_target_users_merge_restores_and_audits(
    maker, world, no_vector_store, mode
):
    principal = _principal(world, mode)
    report_id, action_id = world.reports["target"], world.actions["merge"]
    async with _client(maker, principal) as client:
        response = await client.post(_undo_url(report_id, action_id))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "restored_memory_id": str(world.memories["loser"]),
        "winner_id": str(world.memories["winner"]),
        "report_id": str(report_id),
        "undone_action_id": action_id,
    }
    no_vector_store.assert_awaited_once()
    async with maker() as check:
        loser = await _memory(check, world.memories["loser"])
        assert loser.deleted_at is None and loser.deleted_by is None
        [undo_action] = await _undo_actions(check, report_id)
        [audit] = await _audit_rows(check, "sleep_merge_undo")
    expected_undone_by = "operator" if mode == "service" else world.admin
    assert undo_action.details == {"undone_action_id": action_id, "undone_by": expected_undone_by}
    assert OPERATOR_ADDRESS not in json.dumps(undo_action.details)
    assert audit.user_id == principal["user_id"]
    assert audit.user_email == principal["email"]
    assert audit.resource == f"sleep_report:{report_id}"
    assert audit.user_metadata == {
        "target_user_id": world.target,
        "workspace_id": str(world.ws),
        "context_id": str(world.contexts["ok"]),
        "report_id": str(report_id),
        "action_id": action_id,
        "restored_memory_id": str(world.memories["loser"]),
        "winner_id": str(world.memories["winner"]),
    }
    _assert_no_sentinel(world, response.text, json.dumps(audit.user_metadata), audit.resource)


async def test_a_shadow_merge_undo_reverts_the_edge_and_audits_too(maker, world, no_vector_store):
    report_id, action_id = world.reports["target"], world.actions["shadow"]
    async with _client(maker, OPERATOR) as client:
        response = await client.post(_undo_url(report_id, action_id))

    assert response.status_code == 200, response.text
    assert response.json()["restored_memory_id"] == str(world.memories["shadow_loser"])
    no_vector_store.assert_not_awaited()
    async with maker() as check:
        edges = await check.scalar(
            select(func.count())
            .select_from(NeuralMemoryEdge)
            .where(
                NeuralMemoryEdge.src_id == world.memories["shadow_winner"],
                NeuralMemoryEdge.dst_id == world.memories["shadow_loser"],
            )
        )
        await check.rollback()
        [undo_action] = await _undo_actions(check, report_id)
        [audit] = await _audit_rows(check, "sleep_merge_undo")
    assert edges == 0
    assert undo_action.details == {
        "undone_action_id": action_id,
        "undone_by": "operator",
        "mode": "shadow",
    }
    assert audit.user_id == OPERATOR["user_id"]
    assert audit.user_metadata["action_id"] == action_id
    assert audit.user_metadata["restored_memory_id"] == str(world.memories["shadow_loser"])
    assert audit.user_metadata["winner_id"] == str(world.memories["shadow_winner"])
    assert audit.user_metadata["target_user_id"] == world.target
    _assert_no_sentinel(world, response.text, json.dumps(audit.user_metadata))


@pytest.mark.parametrize("case", ["other_reports_action", "unknown_report", "unknown_action"])
async def test_an_action_that_is_not_in_the_report_is_404_and_restores_nothing(
    maker, world, no_vector_store, case
):
    report_id, action_id = {
        # A real merge action, asked for under another report's id.
        "other_reports_action": (world.reports["target"], world.actions["other_merge"]),
        "unknown_report": (uuid4(), world.actions["merge"]),
        "unknown_action": (world.reports["target"], 2**40),
    }[case]
    async with _client(maker, OPERATOR) as client:
        response = await client.post(_undo_url(report_id, action_id))

    assert response.status_code == 404, response.text
    assert response.json() == {
        "error": "SLEEP-UNDO-action_not_found",
        "message": f"Sleep action {action_id} not found in sleep report {report_id}.",
        "details": {},
    }
    no_vector_store.assert_not_awaited()
    async with maker() as check:
        for key in ("loser", "other_loser"):
            assert (await _memory(check, world.memories[key])).deleted_at is not None
        assert await _audit_rows(check, "sleep_merge_undo") == []
        for key in ("target", "other"):
            assert await _undo_actions(check, world.reports[key]) == []
    _assert_no_sentinel(world, response.text)


async def test_a_second_undo_is_409_and_leaves_one_audit_row(maker, world, no_vector_store):
    url = _undo_url(world.reports["target"], world.actions["merge"])
    async with _client(maker, OPERATOR) as client:
        first = await client.post(url)
        second = await client.post(url)

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"] == "SLEEP-UNDO-already_restored"
    assert second.json()["details"] == {}
    async with maker() as check:
        assert len(await _audit_rows(check, "sleep_merge_undo")) == 1
        assert len(await _undo_actions(check, world.reports["target"])) == 1
    _assert_no_sentinel(world, first.text, second.text)


async def test_an_action_that_is_not_a_merge_is_400_and_writes_nothing(
    maker, world, no_vector_store
):
    async with _client(maker, OPERATOR) as client:
        response = await client.post(_undo_url(world.reports["target"], world.actions["edge"]))

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "SLEEP-UNDO-not_a_merge"
    async with maker() as check:
        assert await _audit_rows(check, "sleep_merge_undo") == []
        assert await _undo_actions(check, world.reports["target"]) == []
    _assert_no_sentinel(world, response.text)


# ------------------------------------------------------ own-account routes


async def test_the_own_account_undo_keeps_its_caller_filter_and_writes_no_audit_row(
    maker, world, no_vector_store
):
    async with _client(maker, world.admin_principal) as client:
        another_users = await client.post(
            f"/api/v1/admin/sleep/actions/{world.actions['merge']}/undo-merge"
        )
        own = await client.post(
            f"/api/v1/admin/sleep/actions/{world.actions['admin_merge']}/undo-merge"
        )

    assert another_users.status_code == 404, another_users.text
    assert another_users.json() == {
        "error": "SLEEP-UNDO-action_not_found",
        "message": f"Sleep action {world.actions['merge']} not found or not owned by you.",
        "details": {},
    }
    assert own.status_code == 200, own.text
    async with maker() as check:
        # The other user's loser stays merged away; the caller's own is back.
        assert (await _memory(check, world.memories["loser"])).deleted_at is not None
        assert (await _memory(check, world.memories["admin_loser"])).deleted_at is None
        [undo_action] = await _undo_actions(check, world.reports["admin"])
        assert await _audit_rows(check, "sleep_merge_undo") == []
    assert undo_action.details == {
        "undone_action_id": world.actions["admin_merge"],
        "undone_by": world.admin,
    }


async def test_the_own_account_run_still_runs_for_the_caller_and_writes_no_audit_row(
    maker, world, jobs, db_session: AsyncSession
):
    # The own-account route picks contexts the caller created.
    own_context = Context(
        id=uuid4(),
        workspace_id=world.ws,
        name=f"own-{uuid4().hex[:8]}",
        created_by=world.admin,
        sleep_mode="full",
    )
    db_session.add(own_context)
    await db_session.commit()
    own_context_id = own_context.id

    async with _client(maker, world.admin_principal) as client:
        response = await client.post(
            "/api/v1/admin/sleep/run", json={"context_id": str(own_context_id)}
        )

    assert response.status_code == 202, response.text
    async with maker() as check:
        [report] = await _reports_of(check, world.admin)
        assert await _audit_rows(check, "sleep_run_trigger") == []
    assert str(report.id) == response.json()["report_ids"][0]
    assert report.context_id == own_context_id
    await asyncio.sleep(0)
    assert jobs[0][1] == world.admin


async def test_the_own_account_routes_refuse_an_operator_and_write_nothing(
    maker, world, jobs, no_vector_store
):
    async with _client(maker, OPERATOR) as client:
        run = await client.post(
            "/api/v1/admin/sleep/run", json={"context_id": str(world.contexts["ok"])}
        )
        undo = await client.post(f"/api/v1/admin/sleep/actions/{world.actions['merge']}/undo-merge")

    assert (run.status_code, undo.status_code) == (403, 403)
    assert run.json()["error"] == undo.json()["error"] == "SYSADMIN-004"
    no_vector_store.assert_not_awaited()
    async with maker() as check:
        assert (await _memory(check, world.memories["loser"])).deleted_at is not None
        assert await _undo_actions(check, world.reports["target"]) == []
        for action in AUDIT_ACTIONS:
            assert await _audit_rows(check, action) == []
        running = await check.scalar(
            select(func.count()).select_from(SleepReport).where(SleepReport.status == "running")
        )
    assert running == 0
    await asyncio.sleep(0)
    assert jobs == []


# ------------------------------------------- one commit, on the real rows


def _unwritable_actor(world) -> dict:
    """A session admin whose address does not fit ``audit_logs.user_email``:
    the audit row's INSERT fails, and with it the commit it belongs to."""
    return {"user_id": world.admin, "email": f"{'a' * 300}@test.example", "role": "admin"}


async def test_a_run_whose_audit_row_cannot_be_written_commits_no_report(maker, world, jobs):
    """The report and its audit row share one commit: no report without the row."""
    async with _client(maker, _unwritable_actor(world)) as client:
        with pytest.raises(DBAPIError):
            await client.post(
                _run_url(world.target), json={"context_id": str(world.contexts["ok"])}
            )

    async with maker() as check:
        assert await _reports_of(check, world.target) == []
        assert await _audit_rows(check, "sleep_run_trigger") == []
    await asyncio.sleep(0)
    assert jobs == []


@pytest.mark.parametrize("path", ["tombstone", "shadow"])
async def test_an_undo_whose_audit_row_cannot_be_written_restores_nothing(
    maker, world, no_vector_store, path
):
    """The restore and its audit row share one commit, on both success paths."""
    action_id = world.actions["merge" if path == "tombstone" else "shadow"]
    async with _client(maker, _unwritable_actor(world)) as client:
        with pytest.raises(DBAPIError):
            await client.post(_undo_url(world.reports["target"], action_id))

    async with maker() as check:
        loser = await _memory(check, world.memories["loser"])
        edges = await check.scalar(
            select(func.count())
            .select_from(NeuralMemoryEdge)
            .where(
                NeuralMemoryEdge.src_id == world.memories["shadow_winner"],
                NeuralMemoryEdge.dst_id == world.memories["shadow_loser"],
            )
        )
        assert await _undo_actions(check, world.reports["target"]) == []
        assert await _audit_rows(check, "sleep_merge_undo") == []
        history = await check.scalar(
            select(func.count())
            .select_from(OperationHistory)
            .where(OperationHistory.workspace_id == world.ws)
        )
    # Still merged away, and the shadow edge still there.
    assert loser.deleted_at is not None and loser.deleted_by == DELETED_BY_SLEEP_MERGE
    assert edges == 1
    assert history == 0


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [("not_merge_deleted", 409, "not_merge_deleted"), ("purged", 410, "memory_purged")],
)
async def test_a_refused_undo_writes_no_audit_row_and_restores_nothing(
    maker, world, no_vector_store, db_session: AsyncSession, case, status, code
):
    # A merge whose loser was deleted by something else, or is gone.
    loser_id = world.memories["dead"] if case == "not_merge_deleted" else uuid4()
    action = SleepAction(
        report_id=world.reports["target"],
        phase="dedup_merge",
        action_type="merge",
        memory_id=world.memories["winner"],
        target_id=loser_id,
        details={},
    )
    db_session.add(action)
    await db_session.commit()

    async with _client(maker, OPERATOR) as client:
        response = await client.post(_undo_url(world.reports["target"], action.id))

    assert response.status_code == status, response.text
    assert response.json()["error"] == f"SLEEP-UNDO-{code}"
    no_vector_store.assert_not_awaited()
    async with maker() as check:
        assert await _audit_rows(check, "sleep_merge_undo") == []
        assert await _undo_actions(check, world.reports["target"]) == []
        assert (await _memory(check, world.memories["dead"])).deleted_by == "user_delete"
    _assert_no_sentinel(world, response.text)


# ------------------------------------------------- operation history actor


@pytest.mark.parametrize("mode", ["service", "session"])
async def test_the_operation_history_event_never_stores_an_operator(
    maker, world, no_vector_store, mode
):
    """An operator's undo is stored without an actor; a session admin's with
    the admin's id. The operator's address is in ``audit_logs`` only."""
    principal = _principal(world, mode)
    async with _client(maker, principal) as client:
        response = await client.post(_undo_url(world.reports["target"], world.actions["merge"]))

    assert response.status_code == 200, response.text
    async with maker() as check:
        [event] = list(
            (
                await check.execute(
                    select(OperationHistory).where(OperationHistory.workspace_id == world.ws)
                )
            ).scalars()
        )
    assert event.operation == "sleep.undo"
    if mode == "service":
        assert (event.actor_user_id, event.principal_kind) == (None, "operator")
    else:
        assert event.actor_user_id == world.admin
    stored = {
        column.name: getattr(event, column.name) for column in OperationHistory.__table__.columns
    }
    assert OPERATOR_ADDRESS not in json.dumps(stored, default=str)


# ------------------------------------------- a workspace over its capacity


async def test_a_workspace_over_its_capacity_still_allows_the_run_and_the_undo(
    maker, world, jobs, no_vector_store, db_session: AsyncSession
):
    """The capacity lock is not checked here, as for the scheduled sweep and
    the own-account routes: the run applies the workspace's Sleep state itself."""
    await db_session.execute(
        update(Workspace)
        .where(Workspace.id == world.ws)
        .values(plan_name="free", entitlement_source=ENTITLEMENT_SOURCE_EXTERNAL_BILLING)
    )
    await db_session.commit()

    with patch.object(Workspace, "effective_memory_limit", property(lambda self: 1)):
        async with maker() as check:
            workspace = await check.get(Workspace, world.ws)
            lock = await capacity_lock_state(check, workspace)
        assert lock is not None and lock.over_memories > 0

        async with _client(maker, OPERATOR) as client:
            run = await client.post(
                _run_url(world.target), json={"context_id": str(world.contexts["ok"])}
            )
            undo = await client.post(_undo_url(world.reports["target"], world.actions["merge"]))

    assert run.status_code == 202, run.text
    assert undo.status_code == 200, undo.text
    async with maker() as check:
        [report] = await _reports_of(check, world.target)
        assert str(report.id) == run.json()["report_ids"][0]
        assert (await _memory(check, world.memories["loser"])).deleted_at is None
        assert len(await _audit_rows(check, "sleep_run_trigger")) == 1
        assert len(await _audit_rows(check, "sleep_merge_undo")) == 1


# ------------------------------------------- values the database refuses


async def test_a_user_id_the_database_would_refuse_is_422_and_writes_nothing(maker, world, jobs):
    body = {"context_id": str(world.contexts["ok"])}
    async with _client(maker, OPERATOR) as client:
        for user_id in (f"{world.target}%00", "%00"):
            refused = await client.post(_run_url(user_id), json=body)
            assert refused.status_code == 422, refused.text
        # Any other character is an id like another: no such user.
        unknown = await client.post(_run_url(f"{world.target}%25"), json=body)

    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"] == "RES-001"
    async with maker() as check:
        assert await _reports_of(check, world.target) == []
        assert await _audit_rows(check, "sleep_run_trigger") == []
    await asyncio.sleep(0)
    assert jobs == []


async def test_the_largest_action_id_is_looked_up_and_not_found(maker, world, no_vector_store):
    async with _client(maker, OPERATOR) as client:
        largest = await client.post(_undo_url(world.reports["target"], 2**63 - 1))
        refused = await client.post(_undo_url(world.reports["target"], 2**63))

    assert largest.status_code == 404, largest.text
    assert largest.json()["error"] == "SLEEP-UNDO-action_not_found"
    assert refused.status_code == 422, refused.text
