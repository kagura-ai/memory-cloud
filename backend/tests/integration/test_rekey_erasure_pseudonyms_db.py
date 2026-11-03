"""#2598: re-keying stored erasure pseudonyms after an ``AUDIT_PSEUDO_SALT`` rotation.

Against a real Postgres: an account is erased through ``AccountErasureService``
under salt A (the shipped default, the salt a deployment that rotates has to
assume exposed), the salt is rotated to B, and
``cli.rekey_erasure_pseudonyms`` runs. The vector store is stubbed: the
erasure's own calls as in ``test_account_erasure_integration``, the re-key's
payload patch through the command module's ``erasure_scope_points`` /
``erase_point_author``.

The database is shared by the whole session, so the re-key sees every
request row other modules left; the assertions read this module's own rows,
and the "nothing to do" checks hold for the whole table (every row is under B
once an apply ran).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Text, cast, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from cli import rekey_erasure_pseudonyms as cli
from cli import reprocess_erasures
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember
from models.erasure import (
    NOTE_PSEUDONYM_REKEY_PAYLOADS_KEY,
    NOTE_PSEUDONYM_SALT_KEY,
    REASON_USER_REQUEST_VIA_SUPPORT,
    STATUS_COMPLETE,
    ErasureRequest,
)
from models.memory import Memory, MemoryRevision
from models.vector_erasure import VectorErasureJob
from services.account_erasure_service import AccountErasureService, _audit_salt
from services.erasure_details import pseudonym_salt_fingerprint
from utils.exceptions import QdrantError
from utils.hashing import sha256_hex

SALT_B = "rotated-private-salt-for-2598"
COLLECTION = "kagura_memories"


@pytest.fixture
def stores():
    """The erasure's vector store, Redis and session calls, stubbed."""
    with (
        patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(return_value={COLLECTION: 0}),
        ),
        patch("services.vector_erasure.list_memory_collections", AsyncMock(return_value=[])),
        patch("services.account_erasure_service.clear_co_activations", AsyncMock(return_value=0)),
        patch("services.account_erasure_service.clear_user_rate_limits", AsyncMock(return_value=0)),
        patch(
            "services.account_erasure_service.get_redis_client",
            new=MagicMock(return_value=MagicMock(setex=AsyncMock(), delete=AsyncMock())),
        ),
        patch("services.account_erasure_service.get_session_manager", new=lambda: None),
        patch(
            "services.security_notification_service.purge_user_notification_state",
            AsyncMock(return_value=0),
        ),
        patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()),
    ):
        yield


class _Vectors:
    """The re-key's vector calls: one point per memory in ``points``, whose
    payload author a successful patch replaces."""

    def __init__(self, points: dict[UUID, str]) -> None:
        self.points = points
        self.author = AsyncMock(side_effect=self._patch)
        self.fail = False

    async def _scope(self, collection: str, memory_id: UUID, point_id: UUID) -> list:
        author = self.points.get(memory_id)
        return [] if author is None else [(str(point_id), author)]

    async def _patch(
        self, collection: str, point_id: str, expected: str, *, replacement: str | None = None
    ) -> None:
        if self.fail:
            raise QdrantError("vector store down")
        memory_id = UUID(point_id)
        if self.points.get(memory_id) == expected and replacement is not None:
            self.points[memory_id] = replacement

    def patches(self):
        return (
            patch.object(cli, "list_memory_collections", AsyncMock(return_value=[COLLECTION])),
            patch.object(cli, "erasure_scope_points", AsyncMock(side_effect=self._scope)),
            patch.object(cli, "erase_point_author", self.author),
        )


def _salt(value: str):
    return patch("services.account_erasure_service._audit_salt", return_value=value)


async def _rekey(db: AsyncSession, vectors: _Vectors, **kwargs: Any) -> cli.RekeyResult:
    scope, points, author = vectors.patches()
    with _salt(SALT_B), scope, points, author:
        return await cli.rekey_erasure_pseudonyms(db, **kwargs)


def _user(tag: str, name: str) -> User:
    return User(
        email=f"{name}-{tag}@example.com",
        user_id=f"{name}-{tag}",
        name=name,
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
    )


@pytest_asyncio.fixture
async def subject(db_session: AsyncSession) -> AsyncIterator[dict[str, Any]]:
    """An account that owns a workspace another admin takes over, so its
    context, its memory and its edit of the admin's memory survive the
    erasure, with audit rows of its own and of the admin's naming it."""
    tag = uuid4().hex[:8]
    target = _user(tag, "subject")
    sub, email = target.user_id, target.email
    other, other_email = f"admin-{tag}", f"admin-{tag}@example.com"
    db_session.add(target)
    await db_session.flush()
    workspace = Workspace(
        id=uuid4(),
        name=f"ws-2598-{tag}",
        plan_name="free",
        owner_user_id=sub,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )
    db_session.add(workspace)
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=workspace.id, user_id=sub, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=workspace.id, user_id=other, role=WorkspaceRole.ADMIN),
        ]
    )
    context = Context(id=uuid4(), workspace_id=workspace.id, name=f"ctx-{tag}", created_by=sub)
    db_session.add(context)
    await db_session.flush()

    def memory(author: str, version: int) -> Memory:
        return Memory(
            id=uuid4(),
            user_id=author,
            workspace_id=workspace.id,
            context_id=context.id,
            summary=f"a memory of {author}",
            content="body",
            type="note",
            client="test",
            version=version,
            embedding_status="success",
        )

    authored, edited = memory(sub, 1), memory(other, 2)
    db_session.add_all([authored, edited])
    await db_session.flush()
    db_session.add(
        MemoryRevision(
            memory_id=edited.id,
            context_id=context.id,
            version=2,
            kind="updated",
            writer="api",
            actor_user_id=sub,
            prior={"summary": "before"},
        )
    )
    audit = [
        # the subject's own rows: on themselves, and one embedding the address
        AuditLog(user_email=email, user_id=sub, action="role_assign", resource=f"user:{email}"),
        AuditLog(user_email=email, user_id=sub, action="export", resource=f"export/{email}/a"),
        # the admin acting on the subject, the subject acting on the admin
        AuditLog(
            user_email=other_email, user_id=other, action="role_assign", resource=f"user:{email}"
        ),
        AuditLog(user_email=email, user_id=other, action="role_assign", resource=f"user:{other}"),
        # a subject identifier in another actor's metadata
        AuditLog(
            user_email=other_email,
            user_id=other,
            action="workspace_owner_transfer",
            resource="workspace",
            user_metadata={"target_user_id": sub},
        ),
    ]
    db_session.add_all(audit)
    await db_session.commit()
    world = {
        "sub": sub,
        "email": email,
        "workspace": workspace.id,
        "context": context.id,
        "authored": authored.id,
        "edited": edited.id,
        "audit": [row.id for row in audit],
        "requests": [],
    }
    yield world
    await _cleanup(db_session, world)


async def _cleanup(db: AsyncSession, world: dict[str, Any]) -> None:
    await db.rollback()
    salt_a = _audit_salt()
    pseudonyms = set()
    for sub in (world["sub"], *world.get("subs", ())):
        p = sha256_hex(sub, salt=salt_a)
        pseudonyms |= {sub, p, sha256_hex(p, salt=SALT_B)}
    await db.execute(
        delete(AuditLog).where(
            AuditLog.id.in_(world["audit"]) | AuditLog.user_id.in_(sorted(pseudonyms))
        )
    )
    await db.execute(delete(ErasureRequest).where(ErasureRequest.id.in_(world["requests"])))
    await db.execute(delete(Workspace).where(Workspace.id == world["workspace"]))
    # an account the test did not erase
    await db.execute(delete(User).where(User.user_id.in_([world["sub"], *world.get("subs", ())])))
    await db.commit()


async def _erase(db: AsyncSession, world: dict[str, Any], sub: str) -> UUID:
    """A self-initiated force-erase (``initiated_by`` is the subject) under salt A."""
    request = await AccountErasureService(db).admin_force_erase(
        target_user_id=sub,
        initiator_user_id=sub,
        reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
        reason_detail="integration test (#2598)",
    )
    assert request.status == STATUS_COMPLETE
    # read now: a later commit or rollback expires the instance
    request_id = request.id
    world["requests"].append(request_id)
    return request_id


async def _request(db: AsyncSession, request_id: UUID) -> ErasureRequest:
    return (
        await db.execute(
            select(ErasureRequest)
            .where(ErasureRequest.id == request_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _carriers(db: AsyncSession, value: str) -> list[str]:
    """Every re-keyed column that still carries ``value`` (whole, or inside
    ``resource``, ``user_metadata`` or a request summary)."""
    found = []
    exact = (*cli.SUB_COLUMNS, *cli.REQUEST_COLUMNS, AuditLog.user_id, AuditLog.user_email)
    for column in exact:
        count = (
            await db.execute(select(func.count()).select_from(column.class_).where(column == value))
        ).scalar_one()
        if count:
            found.append(cli._label(column))
    for name, column in (
        ("audit_logs.resource", AuditLog.resource),
        ("audit_logs.user_metadata", cast(AuditLog.user_metadata, Text)),
        ("erasure_requests.deleted_data_summary", cast(ErasureRequest.deleted_data_summary, Text)),
    ):
        model = AuditLog if name.startswith("audit") else ErasureRequest
        count = (
            await db.execute(select(func.count()).select_from(model).where(column.contains(value)))
        ).scalar_one()
        if count:
            found.append(name)
    return found


async def _snapshot(db: AsyncSession, table: str) -> dict:
    rows = (
        await db.execute(text(f"SELECT t.id, to_jsonb(t) FROM {table} AS t"))  # noqa: S608 - fixed names
    ).all()
    return {row[0]: row[1] for row in rows}


_TABLES = (
    "memories",
    "memory_revisions",
    "audit_logs",
    "contexts",
    "vector_erasure_jobs",
    "erasure_requests",
)


@pytest.mark.asyncio
async def test_after_the_rekey_the_old_salt_matches_no_row(
    db_session: AsyncSession, subject: dict[str, Any], stores: None
) -> None:
    """The issue's Done-when: erase under A, rotate to B, re-key; hashing the
    account id or the address under A then matches no row and no payload, the
    surviving memory has one author in Postgres and in its payload, the
    re-run finds nothing to do, and a second apply changes nothing."""
    salt_a = _audit_salt()
    sub, email = subject["sub"], subject["email"]
    p_a, e_a = sha256_hex(sub, salt=salt_a), sha256_hex(email, salt=salt_a)
    p_b, e_b = sha256_hex(p_a, salt=SALT_B), sha256_hex(e_a, salt=SALT_B)

    request_id = await _erase(db_session, subject, sub)

    # the live erasure stamps the salt it used
    row = await _request(db_session, request_id)
    assert row.deleted_data_summary[NOTE_PSEUDONYM_SALT_KEY] == pseudonym_salt_fingerprint(salt_a)
    assert row.user_id == row.initiated_by == p_a
    # what the re-key has to rewrite: A's pseudonyms of the sub and the address
    assert {
        "contexts.created_by",
        "memories.user_id",
        "memory_revisions.actor_user_id",
        "vector_erasure_jobs.subject_pseudonym",
        "erasure_requests.user_id",
        "erasure_requests.initiated_by",
        "audit_logs.user_id",
        "audit_logs.user_metadata",
    } <= set(await _carriers(db_session, p_a))
    assert "audit_logs.resource" in await _carriers(db_session, p_a[:16])
    assert {"audit_logs.user_email", "audit_logs.resource"} <= set(await _carriers(db_session, e_a))

    vectors = _Vectors({subject["authored"]: p_a})
    plan = await _rekey(db_session, vectors, dry_run=True)
    vectors.author.assert_not_awaited()  # the plan makes no vector-store call
    assert plan.to_rekey >= 1 and plan.payload_memories >= 1
    applied = await _rekey(db_session, vectors, dry_run=False)

    assert applied.columns == plan.columns  # the plan's counts are the apply's
    assert applied.stamped == plan.stamped
    for value in (sub, email, p_a, e_a):
        assert await _carriers(db_session, value) == [], value
    assert await _carriers(db_session, p_a[:16]) == []  # the erasure row's resource
    # no payload carries A's pseudonym; the patch was compare-and-set on it
    assert vectors.points == {subject["authored"]: p_b}
    vectors.author.assert_awaited_once_with(
        COLLECTION, str(subject["authored"]), p_a, replacement=p_b
    )
    # one author value for the surviving memory and the edit, in every column
    authored = (
        await db_session.execute(select(Memory.user_id).where(Memory.id == subject["authored"]))
    ).scalar_one()
    actor = (
        await db_session.execute(
            select(MemoryRevision.actor_user_id).where(
                MemoryRevision.memory_id == subject["edited"]
            )
        )
    ).scalar_one()
    job = (
        await db_session.execute(
            select(VectorErasureJob.subject_pseudonym)
            .where(VectorErasureJob.request_id == request_id)
            .distinct()
        )
    ).scalar_one()
    row = await _request(db_session, request_id)
    assert authored == actor == job == row.user_id == row.initiated_by == p_b
    assert row.deleted_data_summary[NOTE_PSEUDONYM_SALT_KEY] == pseudonym_salt_fingerprint(SALT_B)
    assert NOTE_PSEUDONYM_REKEY_PAYLOADS_KEY not in row.deleted_data_summary
    # the address: one re-keyed value wherever it was, the subject's rows and the admin's
    audit = {
        r.id: r
        for r in (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.id.in_(subject["audit"]))
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    own, embedded, named, acted, metadata = (audit[i] for i in subject["audit"])
    assert (own.user_id, own.user_email, own.resource) == (p_b, e_b, f"user:{e_b}")
    assert embedded.resource == f"export/{e_b}/a"
    assert named.resource == f"user:{e_b}"
    assert acted.user_email == e_b
    assert metadata.user_metadata == {"target_user_id": p_b}
    erasure_row = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "account_erasure", AuditLog.user_id == p_b)
        )
    ).scalar_one()
    assert erasure_row.user_email == e_b
    assert erasure_row.resource == f"user_pseudonym:{p_b[:16]}"
    assert erasure_row.user_metadata["initiated_by"] == p_b

    # the re-run of today's erasure rules finds nothing to do under B
    with _salt(SALT_B):
        rerun = await reprocess_erasures.reprocess_erasures(
            db_session, request_id=request_id, dry_run=True
        )
    assert rerun.changes == 0, rerun

    # a second apply: nothing to stamp, rewrite or patch
    before = {table: await _snapshot(db_session, table) for table in _TABLES}
    again = await _rekey(db_session, vectors, dry_run=False)
    assert (again.stamped, again.columns, again.changes) == (0, {}, 0)
    assert again.payload == cli.PayloadCounts()
    vectors.author.assert_awaited_once()
    assert {table: await _snapshot(db_session, table) for table in _TABLES} == before


@pytest.mark.asyncio
async def test_stamp_current_marks_the_unmarked_rows_and_rewrites_nothing(
    db_session: AsyncSession, subject: dict[str, Any], stores: None
) -> None:
    """A deployment that did not rotate: rows written before the fingerprint
    get it and nothing else changes; a re-key under the same salt then has
    nothing to do."""
    salt_a = _audit_salt()
    request_id = await _erase(db_session, subject, subject["sub"])
    # what a row written before v0.141.0 looks like: no fingerprint
    await db_session.execute(
        update(ErasureRequest)
        .where(ErasureRequest.id == request_id)
        .values(
            deleted_data_summary=ErasureRequest.deleted_data_summary.delete_path(
                [NOTE_PSEUDONYM_SALT_KEY]
            )
        )
    )
    await db_session.commit()
    before = {table: await _snapshot(db_session, table) for table in _TABLES}
    vectors = _Vectors({subject["authored"]: sha256_hex(subject["sub"], salt=salt_a)})
    scope, points, author = vectors.patches()

    with _salt(salt_a), scope, points, author:
        plan = await cli.rekey_erasure_pseudonyms(db_session, dry_run=True, stamp_current=True)
        applied = await cli.rekey_erasure_pseudonyms(db_session, dry_run=False, stamp_current=True)
        assert plan.unmarked >= 1 and plan.stamped == applied.stamped == plan.unmarked
        assert applied.columns == {} and applied.payload is None
        after = {table: await _snapshot(db_session, table) for table in _TABLES}
        # the salt is the shipped default here: let the re-key run under it
        with patch.object(cli, "is_placeholder_secret", return_value=False):
            rekey = await cli.rekey_erasure_pseudonyms(db_session, dry_run=False)

    assert (rekey.to_rekey, rekey.stamped, rekey.columns, rekey.changes) == (0, 0, {}, 0)
    vectors.author.assert_not_awaited()
    requests = after.pop("erasure_requests")
    assert after == {table: rows for table, rows in before.items() if table != "erasure_requests"}
    fingerprint = pseudonym_salt_fingerprint(salt_a)
    for row_id, row in requests.items():
        old = before["erasure_requests"].get(row_id)
        assert old is not None
        summary = row["deleted_data_summary"]
        if old["deleted_data_summary"] != summary:
            # the one change: the fingerprint, on a row that had none
            assert summary == {**old["deleted_data_summary"], NOTE_PSEUDONYM_SALT_KEY: fingerprint}
        assert {k: v for k, v in row.items() if k != "deleted_data_summary"} == {
            k: v for k, v in old.items() if k != "deleted_data_summary"
        }
    assert requests[request_id]["deleted_data_summary"][NOTE_PSEUDONYM_SALT_KEY] == fingerprint
    assert await _snapshot(db_session, "erasure_requests") == requests


@pytest.mark.asyncio
async def test_unfinished_erasures_and_scopes_are_counted_and_left_alone(
    db_session: AsyncSession, subject: dict[str, Any], stores: None
) -> None:
    """A request still in progress after its Postgres step (its row stores the
    account id) and a vector-erasure scope not drained have to finish under
    the salt they started with, before a rotation: the plan counts both, the
    stamp's plan too, and the re-key leaves the request row as it is."""
    sub = subject["sub"]
    request_id = await _erase(db_session, subject, sub)
    # a run that died after its Postgres commit, before the drain and the close
    await db_session.execute(
        update(ErasureRequest)
        .where(ErasureRequest.id == request_id)
        .values(status="in_progress", user_id=sub, initiated_by=sub, completed_at=None)
    )
    await db_session.execute(
        update(VectorErasureJob)
        .where(VectorErasureJob.request_id == request_id)
        .values(completed_at=None)
    )
    await db_session.commit()
    vectors = _Vectors({})

    stamp = await _rekey(db_session, vectors, dry_run=True, stamp_current=True)
    plan = await _rekey(db_session, vectors, dry_run=True)
    applied = await _rekey(db_session, vectors, dry_run=False)

    assert plan.in_progress >= 1 and plan.scopes_pending >= 1
    for result in (stamp, applied):
        assert (result.in_progress, result.scopes_pending) == (
            plan.in_progress,
            plan.scopes_pending,
        )
    row = await _request(db_session, request_id)
    assert (row.status, row.user_id) == ("in_progress", sub)
    # the fingerprint the erasure wrote under A: neither re-keyed nor re-stamped
    assert row.deleted_data_summary[NOTE_PSEUDONYM_SALT_KEY] == pseudonym_salt_fingerprint(
        _audit_salt()
    )


@pytest.mark.asyncio
async def test_a_failed_payload_patch_is_patched_again_by_the_next_apply(
    db_session: AsyncSession, subject: dict[str, Any], stores: None
) -> None:
    """The Postgres re-key commits; a vector-store failure after it leaves the
    request marked, and the next apply patches the point — the old author is
    recognized without the old salt — and removes the mark."""
    salt_a = _audit_salt()
    p_a = sha256_hex(subject["sub"], salt=salt_a)
    p_b = sha256_hex(p_a, salt=SALT_B)
    request_id = await _erase(db_session, subject, subject["sub"])
    vectors = _Vectors({subject["authored"]: p_a})
    vectors.fail = True

    first = await _rekey(db_session, vectors, dry_run=False)

    assert first.payload is not None
    assert (first.payload.failed, first.payload.requests_pending) == (1, 1)
    row = await _request(db_session, request_id)
    assert row.user_id == p_b
    assert row.deleted_data_summary[NOTE_PSEUDONYM_REKEY_PAYLOADS_KEY] is True
    assert vectors.points == {subject["authored"]: p_a}

    vectors.fail = False
    plan = await _rekey(db_session, vectors, dry_run=True)
    assert (plan.payload_pending, plan.rows, plan.changes) == (1, 0, 1)
    second = await _rekey(db_session, vectors, dry_run=False)

    assert second.payload is not None
    assert (second.payload.points, second.payload.failed, second.payload.requests_done) == (
        1,
        0,
        1,
    )
    assert vectors.points == {subject["authored"]: p_b}
    row = await _request(db_session, request_id)
    assert NOTE_PSEUDONYM_REKEY_PAYLOADS_KEY not in row.deleted_data_summary
    assert row.user_id == p_b  # never hashed again
    third = await _rekey(db_session, vectors, dry_run=True)
    assert third.changes == 0


@pytest.mark.asyncio
async def test_two_subjects_naming_each_other_are_re_keyed_once_per_value(
    db_session: AsyncSession, subject: dict[str, Any], stores: None
) -> None:
    """Two members who acted on each other: each one's email pseudonym sits on
    the other's rows too. One map for both requests re-keys each value once
    (a per-request pass would hash the shared value twice)."""
    tag = uuid4().hex[:8]
    x, y = _user(tag, "x"), _user(tag, "y")
    db_session.add_all([x, y])
    await db_session.flush()
    for owner in (x, y):
        db_session.add(
            Workspace(
                id=uuid4(),
                name=f"ws-2598-{owner.user_id}",
                plan_name="free",
                owner_user_id=owner.user_id,
                daily_api_limit=500,
                weekly_api_limit=2500,
            )
        )
    x_on_y = AuditLog(
        user_email=x.email, user_id=y.user_id, action="role_assign", resource=f"user:{y.email}"
    )
    y_on_x = AuditLog(
        user_email=y.email, user_id=x.user_id, action="role_assign", resource=f"user:{x.email}"
    )
    db_session.add_all([x_on_y, y_on_x])
    await db_session.commit()
    # read now: the erasures' rollbacks expire the instances
    x_on_y_id, y_on_x_id = x_on_y.id, y_on_x.id
    subject["audit"] += [x_on_y_id, y_on_x_id]
    subject["subs"] = [x.user_id, y.user_id]
    salt_a = _audit_salt()
    e_x, e_y = (sha256_hex(u.email, salt=salt_a) for u in (x, y))
    p_x, p_y = (sha256_hex(u.user_id, salt=salt_a) for u in (x, y))
    x_sub, y_sub = x.user_id, y.user_id

    await _erase(db_session, subject, x_sub)
    await _erase(db_session, subject, y_sub)
    rows = {
        r.id: (r.user_id, r.user_email, r.resource)
        for r in (
            await db_session.execute(
                select(AuditLog).where(AuditLog.id.in_([x_on_y_id, y_on_x_id]))
            )
        ).scalars()
    }
    assert rows == {x_on_y_id: (p_y, e_x, f"user:{e_y}"), y_on_x_id: (p_x, e_y, f"user:{e_x}")}

    await _rekey(db_session, _Vectors({}), dry_run=False)

    def b(value: str) -> str:
        return sha256_hex(value, salt=SALT_B)

    rows = {
        r.id: (r.user_id, r.user_email, r.resource)
        for r in (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.id.in_([x_on_y_id, y_on_x_id]))
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    assert rows == {
        x_on_y_id: (b(p_y), b(e_x), f"user:{b(e_y)}"),
        y_on_x_id: (b(p_x), b(e_y), f"user:{b(e_x)}"),
    }
    # the erasure rows carry the same values: one correlation per member
    erasure_rows = dict(
        (
            await db_session.execute(
                select(AuditLog.user_id, AuditLog.user_email).where(
                    AuditLog.action == "account_erasure",
                    AuditLog.user_id.in_([b(p_x), b(p_y)]),
                )
            )
        ).all()
    )
    assert erasure_rows == {b(p_x): b(e_x), b(p_y): b(e_y)}
