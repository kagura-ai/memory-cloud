"""#2026: a Python ``None`` written to a nullable JSON column is stored as SQL NULL.

``models/json_types.py`` gives every nullable JSON / JSONB column
``none_as_null=True``. The ORM reads SQL NULL and JSON ``null`` back as the same
``None``, so every check here reads the stored value with raw SQL
(``col IS NULL`` and ``col::text``). One representative column per model is
written through an ORM INSERT and, where the table allows UPDATE, cleared back
to ``None`` through an ORM UPDATE. The two default-bearing exceptions keep their
behaviour: an explicit ``None`` is JSON ``null``, an omitted value is ``{}``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from models.analysis import MemoryAnalysis
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.bm25_drift import Bm25IdfDriftLog
from models.erasure import REASON_USER_REQUEST_VIA_SUPPORT, ErasureRequest
from models.llm_call_log import LLMCallLog
from models.measurement import Measurement
from models.memory import Memory, NeuralMemoryEdge
from models.memory_access_event import MemoryAccessEvent
from models.resource import IndexerState, Resource, ResourceEvent, WorkspaceConnector
from models.schemas import RememberRequest
from models.sleep import SleepAction, SleepReport
from services.memory_service import MemoryService
from services.quota_service import DailyReservation
from services.secret_store_service import SecretStoreService
from utils.datetime import utcnow


@dataclass
class Scope:
    owner: str
    ws: Workspace
    ctx: Context
    resource: Resource
    report: SleepReport
    # one per connector: ``resource_pk`` is unique on workspace_connectors
    spare_resources: list[Resource]


async def _scope(db) -> Scope:
    owner = f"jn-{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"jn-ws-{uuid4().hex[:8]}",
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
        name=f"jn-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    resource, *spare = (
        Resource(id=uuid4(), workspace_id=ws.id, resource_id=f"jn-res-{uuid4().hex[:8]}")
        for _ in range(3)
    )
    report = SleepReport(id=uuid4(), user_id=owner, context_id=ctx.id)
    db.add_all([resource, *spare, report])
    await db.flush()
    return Scope(owner, ws, ctx, resource, report, spare)


def _memory(s: Scope, **col: Any) -> Memory:
    return Memory(
        user_id=s.owner,
        workspace_id=s.ws.id,
        context_id=s.ctx.id,
        summary="a fact",
        content="content",
        type="note",
        client="pytest",
        **col,
    )


# (table, column, attribute, factory, the table accepts UPDATE)
Factory = Callable[[Scope, dict[str, Any]], Any]
CASES: list[tuple[str, str, str, Factory, bool]] = [
    ("memories", "details", "details", lambda s, c: _memory(s, **c), True),
    ("memories", "context", "context", lambda s, c: _memory(s, **c), True),
    (
        "sleep_reports",
        "dedup_result",
        "dedup_result",
        lambda s, c: SleepReport(user_id=s.owner, context_id=s.ctx.id, **c),
        True,
    ),
    (
        "sleep_actions",
        "details",
        "details",
        lambda s, c: SleepAction(report_id=s.report.id, phase="dedup", action_type="merge", **c),
        True,
    ),
    (
        "measurements",
        "details",
        "details",
        lambda s, c: Measurement(
            context_id=s.ctx.id, metric="weight", measured_at=utcnow(), value=Decimal("1"), **c
        ),
        True,
    ),
    (
        "memory_analyses",
        "quality",
        "quality",
        lambda s, c: MemoryAnalysis(
            workspace_id=s.ws.id,
            context_id=s.ctx.id,
            triggered_by=s.owner,
            model_snapshot={},
            embedding_model="test-embedding",
            params={},
            input_count=0,
            status="succeeded",
            **c,
        ),
        True,
    ),
    (
        "bm25_idf_drift_log",
        "top_divergent_terms",
        "top_divergent_terms",
        lambda s, c: Bm25IdfDriftLog(
            context_id=s.ctx.id,
            psi_status="insufficient_data",
            m_memory_points=0,
            r_resource_points=0,
            num_terms=0,
            **c,
        ),
        True,
    ),
    (
        "erasure_requests",
        "deleted_data_summary",
        "deleted_data_summary",
        lambda s, c: ErasureRequest(
            user_id=f"gone-{uuid4().hex[:8]}",
            user_email_hash="0" * 64,
            initiated_by="admin-x",
            is_self_service=False,
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            **c,
        ),
        True,
    ),
    (
        "workspace_connectors",
        "runtime_config",
        "runtime_config",
        lambda s, c: WorkspaceConnector(
            resource_pk=s.spare_resources.pop().id,
            workspace_id=s.ws.id,
            connector_type="slack",
            **c,
        ),
        True,
    ),
    (
        "audit_logs",
        "user_metadata",
        "user_metadata",
        lambda s, c: AuditLog(
            user_email=f"{s.owner}@test.example",
            user_id=s.owner,
            action="test",
            resource="test",
            **c,
        ),
        True,
    ),
    (
        "llm_call_log",
        "call_metadata",
        "call_metadata",
        lambda s, c: LLMCallLog(
            occurred_at=utcnow(),
            caller="sleep",
            call_type="completion",
            provider="test",
            model="test-model",
            **c,
        ),
        True,
    ),
    (
        "neural_memory_edges",
        "metadata",
        "edge_metadata",
        lambda s, c: NeuralMemoryEdge(
            user_id=s.owner,
            workspace_id=s.ws.id,
            context_id=s.ctx.id,
            src_id=uuid4(),
            dst_id=uuid4(),
            **c,
        ),
        True,
    ),
    (
        "resource_events",
        "payload",
        "payload",
        lambda s, c: ResourceEvent(
            resource_id=s.resource.resource_id,
            resource_pk=s.resource.id,
            op="delete",
            doc_id=f"doc-{uuid4().hex[:8]}",
            **c,
        ),
        True,
    ),
    # Append-only in production (a trigger blocks UPDATE): INSERT only.
    (
        "memory_access_events",
        "event_metadata",
        "event_metadata",
        lambda s, c: MemoryAccessEvent(
            workspace_id=s.ws.id,
            user_id=s.owner,
            principal_type="session",
            surface="rest",
            operation="recall",
            outcome="success",
            **c,
        ),
        False,
    ),
]


async def _stored(db, table: str, column: str, row_id: Any) -> tuple[bool, str | None]:
    """(``IS NULL``, ``::text``) of the stored column, bypassing the ORM."""
    row = (
        await db.execute(
            text(
                f'SELECT "{column}" IS NULL AS sql_null, "{column}"::text AS raw '
                f"FROM {table} WHERE id = :id"
            ),
            {"id": row_id},
        )
    ).one()
    return row.sql_null, row.raw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "column", "attr", "factory", "updatable"),
    CASES,
    ids=[f"{c[0]}.{c[1]}" for c in CASES],
)
async def test_none_is_stored_as_sql_null(db_session, table, column, attr, factory, updatable):
    scope = await _scope(db_session)

    explicit = factory(scope, {attr: None})
    omitted = factory(scope, {})
    db_session.add_all([explicit, omitted])
    await db_session.flush()

    assert await _stored(db_session, table, column, explicit.id) == (True, None)
    assert await _stored(db_session, table, column, omitted.id) == (True, None)

    if not updatable:
        await db_session.rollback()
        return
    setattr(explicit, attr, {"k": 1})
    await db_session.flush()
    sql_null, raw = await _stored(db_session, table, column, explicit.id)
    assert sql_null is False and raw is not None and '"k"' in raw

    setattr(explicit, attr, None)
    await db_session.flush()
    assert await _stored(db_session, table, column, explicit.id) == (True, None)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_resource_event_metadata_keeps_its_default_exception(db_session):
    """``event_metadata`` has ``server_default '{}'``: an explicit ``None`` stays
    JSON ``null`` (what the ingest API stored and returns), an omitted value
    gets the default."""
    scope = await _scope(db_session)

    def event(**col: Any) -> ResourceEvent:
        return ResourceEvent(
            resource_id=scope.resource.resource_id,
            resource_pk=scope.resource.id,
            op="upsert",
            doc_id=f"doc-{uuid4().hex[:8]}",
            payload={"a": 1},
            **col,
        )

    explicit, omitted = event(event_metadata=None), event()
    db_session.add_all([explicit, omitted])
    await db_session.flush()

    assert await _stored(db_session, "resource_events", "event_metadata", explicit.id) == (
        False,
        "null",
    )
    assert await _stored(db_session, "resource_events", "event_metadata", omitted.id) == (
        False,
        "{}",
    )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_indexer_metrics_keeps_its_default_exception(db_session):
    scope = await _scope(db_session)
    state = IndexerState(
        resource_id=scope.resource.resource_id,
        resource_pk=scope.resource.id,
        context_id=scope.ctx.id,
    )
    db_session.add(state)
    await db_session.flush()

    assert await _stored(db_session, "indexer_state", "metrics", state.id) == (False, "{}")
    await db_session.rollback()


@pytest.mark.asyncio
async def test_secret_audit_entry_stores_sql_null_and_the_chain_verifies(db_session):
    """``req_meta=None`` is SQL NULL, and the hash chain (which hashes
    ``req_meta or {}``) still verifies across both forms."""
    workspace_id = uuid4()
    db_session.add(Workspace(id=workspace_id, name=f"jn-sec-{workspace_id}", owner_user_id="o-1"))
    await db_session.flush()
    svc = SecretStoreService(db_session)

    bare = await svc._append_audit(
        workspace_id=workspace_id, actor_user_id="o-1", action="get", result="ok"
    )
    with_meta = await svc._append_audit(
        workspace_id=workspace_id,
        actor_user_id="o-1",
        action="get",
        result="ok",
        req_meta={"tool": "pytest"},
    )

    assert await _stored(db_session, "secret_access_log", "req_meta", bare.id) == (True, None)
    assert (await _stored(db_session, "secret_access_log", "req_meta", with_meta.id))[0] is False
    assert (await svc.verify_audit_chain(workspace_id=workspace_id))["valid"] is True
    await db_session.rollback()


# --- remember(): the main writer of memories.details / memories.context ------


@pytest.fixture
def quiet_write():
    """No embedding task, no Redis quota: the parts of remember() not under test."""
    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(
            return_value=DailyReservation(key="quota:test:memories:day", count=1)
        ),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.QuotaService", return_value=quota),
    ):
        yield quota


async def _remember(db, scope: Scope, **fields: Any) -> UUID:
    request = RememberRequest(
        summary=f"a remembered fact {uuid4().hex[:6]}",
        content="content",
        type="note",
        importance=0.5,
        **fields,
    )
    result = await MemoryService(db).remember(
        request,
        user_id=scope.owner,
        client="pytest",
        current_context_id=scope.ctx.id,
        current_workspace_id=scope.ws.id,
    )
    return result.memory_id


@pytest.mark.asyncio
async def test_remember_without_details_or_context_stores_sql_null(db_session, quiet_write):
    scope = await _scope(db_session)
    await db_session.commit()

    memory_id = await _remember(db_session, scope)

    assert await _stored(db_session, "memories", "details", memory_id) == (True, None)
    assert await _stored(db_session, "memories", "context", memory_id) == (True, None)


@pytest.mark.asyncio
async def test_remember_with_details_and_context_stores_the_objects(db_session, quiet_write):
    scope = await _scope(db_session)
    await db_session.commit()

    memory_id = await _remember(
        db_session, scope, details={"source": "pytest"}, context={"where": "here"}
    )

    details = await _stored(db_session, "memories", "details", memory_id)
    context = await _stored(db_session, "memories", "context", memory_id)
    assert details[0] is False and '"source"' in (details[1] or "")
    assert context[0] is False and '"where"' in (context[1] or "")


def test_cases_cover_every_switched_model() -> None:
    """One representative column per model with a switched column (the secret
    log has its own test above)."""
    from tests.models.test_json_none_as_null import NULLABLE

    tables = {c.table.name for c in NULLABLE} - {"secret_access_log"}
    assert {case[0] for case in CASES} == tables


# --- the erasure summary merge (account_erasure_service._merged_summary) ------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "expected_keys"),
    [
        ("null", {"postgres"}),  # legacy JSON null: merged like SQL NULL
        (None, {"postgres"}),
        ('{"retry": {"attempts": 1}}', {"retry", "postgres"}),
    ],
    ids=["json-null", "sql-null", "object"],
)
async def test_erasure_summary_merge_treats_json_null_as_empty(db_session, stored, expected_keys):
    """``'null'::jsonb || {...}`` is an array, which would hide ``postgres``
    from ``has_key``; the merge reads a JSON-null summary as empty (#2026)."""
    from sqlalchemy import select, update

    from services.account_erasure_service import _merged_summary
    from services.erasure_details import _ran_in_postgres

    request = ErasureRequest(
        user_id=f"gone-{uuid4().hex[:8]}",
        user_email_hash="0" * 64,
        initiated_by="admin-x",
        is_self_service=False,
        reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
    )
    db_session.add(request)
    await db_session.flush()
    await db_session.execute(
        text("UPDATE erasure_requests SET deleted_data_summary = CAST(:v AS jsonb) WHERE id = :id"),
        {"v": stored, "id": request.id},
    )

    await db_session.execute(
        update(ErasureRequest)
        .where(ErasureRequest.id == request.id)
        .values(deleted_data_summary=_merged_summary({"postgres": {"users": 1}}))
        .execution_options(synchronize_session=False)
    )

    kind, keys = (
        await db_session.execute(
            text(
                "SELECT jsonb_typeof(deleted_data_summary), "
                "ARRAY(SELECT jsonb_object_keys(deleted_data_summary)) "
                "FROM erasure_requests WHERE id = :id"
            ),
            {"id": request.id},
        )
    ).one()
    assert kind == "object"
    assert set(keys) == expected_keys
    ran = (
        await db_session.execute(select(_ran_in_postgres()).where(ErasureRequest.id == request.id))
    ).scalar_one()
    assert ran is True
    await db_session.rollback()
