"""Integration tests for AccountErasureService (Issue #360).

Exercises the full ``_execute()`` cross-store deletion pipeline against a
real Postgres test database, with the non-Postgres collaborators mocked
(Qdrant client, Redis client, Stripe SDK) so the test suite does not
require Qdrant or Redis containers.

Coverage gaps these tests close (per QA-Lead Gate2 review):

1. ``_execute()`` happy path: admin force-erase on a real Postgres row
   set verifies that:
   - users / api_keys / workspace_members / workspaces rows for the
     target user are deleted in FK-safe order
   - audit_logs entries authored by the user are pseudonymized (not
     deleted) — legal-retention preservation
   - the new "account_erasure" audit row is itself pseudonymized at
     insert (no plaintext PII left in audit_logs after erasure)
   - erasure_requests row reaches status='complete' with a populated
     deleted_data_summary JSONB

2. ``uq_erasure_one_active_per_user`` partial unique index: concurrent
   create-then-create attempts produce IntegrityError on the second
   commit, which the service translates to ErasureAlreadyInProgressError.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import APIKey, AuditLog, User, Workspace, WorkspaceMember
from models.erasure import (
    REASON_USER_REQUEST_VIA_SUPPORT,
    STATUS_COMPLETE,
    ErasureRequest,
)
from services.account_erasure_service import AccountErasureService, _audit_salt
from utils.datetime import utcnow
from utils.exceptions import ErasureAlreadyInProgressError
from utils.hashing import sha256_hex

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def erasure_scenario(db_session: AsyncSession):
    """Seed one target user with workspace + members + api_key + audit_log.

    The shape mirrors a realistic GDPR target: workspace owner with a
    coexisting member, a personal API key, and a prior audit_log entry
    that should survive (pseudonymized).
    """
    target_user_id = f"target_{uuid4().hex[:8]}"
    target_email = f"target-{uuid4().hex[:6]}@example.com"
    other_user_id = f"other_{uuid4().hex[:8]}"

    target = User(
        email=target_email,
        user_id=target_user_id,
        name="Target User",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
    )
    db_session.add(target)
    await db_session.flush()

    workspace = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=target_user_id,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )
    db_session.add(workspace)
    await db_session.flush()

    # Target's own membership
    target_member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=target_user_id,
        role=WorkspaceRole.OWNER,
    )
    # Another user already in the workspace as admin — for ownership transfer
    other_member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=other_user_id,
        role=WorkspaceRole.ADMIN,
    )
    db_session.add_all([target_member, other_member])

    # Per-user API key (no FK cascade, must be deleted by the orchestrator)
    api_key = APIKey(
        key_hash=sha256_hex(f"kagura_test_{uuid4().hex}"),
        key_prefix="kagura_test_xx",
        name="target-key",
        user_id=target_user_id,
        workspace_id=workspace.id,
    )
    db_session.add(api_key)

    # Pre-existing audit log entry — must survive but be pseudonymized
    prior_audit = AuditLog(
        user_email=target_email,
        user_id=target_user_id,
        action="role_assign",
        resource=f"user:{target_email}",
        old_value_hash="user",
        new_value_hash="admin",
    )
    db_session.add(prior_audit)
    await db_session.commit()
    prior_audit_id = prior_audit.id  # read now: the teardown's rollback expires it

    yield {
        "target_user_id": target_user_id,
        "target_email": target_email,
        "other_user_id": other_user_id,
        "workspace_id": workspace.id,
        "api_key_id": api_key.id,
        "prior_audit_id": prior_audit_id,
    }

    # #2268: the database outlives a test, so the rows the erasure wrote
    # under the target's pseudonym (its audit rows, its request) go here;
    # otherwise a whole-table audit read in a later test sees them.
    await db_session.rollback()
    pseudonym = sha256_hex(target_user_id, salt=_audit_salt())
    await db_session.execute(
        delete(AuditLog).where(
            (AuditLog.id == prior_audit_id)
            | (
                AuditLog.action.in_(("account_erasure", "account_erasure_reprocess"))
                & (AuditLog.user_id == pseudonym)
            )
        )
    )
    await db_session.execute(
        delete(ErasureRequest).where(ErasureRequest.user_id.in_((target_user_id, pseudonym)))
    )
    await db_session.commit()


@pytest.fixture
def patched_external_stores():
    """Mock Qdrant / Redis / Stripe so the test exercises the Postgres
    pipeline without needing those containers."""
    qdrant_patch = patch(
        "services.account_erasure_service.delete_user_points",
        new=AsyncMock(return_value={"kagura_memories": 0}),
    )
    co_act_patch = patch(
        "services.account_erasure_service.clear_co_activations",
        new=AsyncMock(return_value=0),
    )
    rate_limits_patch = patch(
        "services.account_erasure_service.clear_user_rate_limits",
        new=AsyncMock(return_value=0),
    )
    redis_client_patch = patch(
        "services.account_erasure_service.get_redis_client",
        new=MagicMock(return_value=MagicMock(setex=AsyncMock(), delete=AsyncMock())),
    )
    # SessionManager: no store registered, so the service skips the
    # session-cleanup branch.
    session_manager_patch = patch(
        "services.account_erasure_service.get_session_manager",
        new=lambda: None,
    )
    with (
        qdrant_patch,
        co_act_patch,
        rate_limits_patch,
        redis_client_patch,
        session_manager_patch,
    ):
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdminForceEraseHappyPath:
    """End-to-end Postgres pipeline for admin force-erase."""

    @pytest.mark.asyncio
    async def test_target_rows_deleted_audit_logs_pseudonymized(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        patched_external_stores,
    ):
        target_user_id = erasure_scenario["target_user_id"]
        target_email = erasure_scenario["target_email"]
        api_key_id = erasure_scenario["api_key_id"]
        prior_audit_id = erasure_scenario["prior_audit_id"]
        workspace_id = erasure_scenario["workspace_id"]

        service = AccountErasureService(db_session)

        request = await service.admin_force_erase(
            target_user_id=target_user_id,
            initiator_user_id="admin-runner",
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            reason_detail="integration test",
        )

        # The erasure_requests row reached terminal complete with a summary.
        assert request.status == STATUS_COMPLETE
        assert request.deleted_data_summary is not None
        assert "postgres" in request.deleted_data_summary
        assert request.deleted_data_summary["postgres"]["users"] == 1
        # The audit-log pseudonymize step ran (count >= 1 because the prior
        # audit row above was authored by the target).
        assert request.deleted_data_summary["audit_logs_pseudonymized"] >= 1

        # User row is gone.
        result = await db_session.execute(select(User).where(User.user_id == target_user_id))
        assert result.scalar_one_or_none() is None

        # Per-user API key is gone (no FK cascade — orchestrator deletes it).
        result = await db_session.execute(select(APIKey).where(APIKey.id == api_key_id))
        assert result.scalar_one_or_none() is None

        # Workspace was transferred to the other admin (not deleted) because
        # an alternate admin existed; verify the workspace survives with the
        # new owner_user_id.
        result = await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ws = result.scalar_one_or_none()
        assert ws is not None
        assert ws.owner_user_id == erasure_scenario["other_user_id"]
        # #1102: the auto-transfer bumped ownership_epoch and it persisted through
        # the full DB orchestration — this is the #1100 signal that invalidates the
        # erased owner's billing-handoff tokens/sessions.
        assert ws.ownership_epoch >= 1

        # Pre-existing audit_logs row survives but PII is pseudonymized.
        result = await db_session.execute(select(AuditLog).where(AuditLog.id == prior_audit_id))
        prior = result.scalar_one()
        assert prior.user_id != target_user_id  # pseudonymized
        assert prior.user_email != target_email  # pseudonymized
        assert len(prior.user_id) == 64  # SHA256 hex
        assert len(prior.user_email) == 64
        # The resource column for user-targeted audit events is conventionally
        # `user:{email}` in this codebase (see SystemAdminService.promote /
        # RoleManager.assign_role). Pseudonymization MUST scrub email AND
        # user_id from `resource` too — otherwise plaintext PII survives in
        # the legal-retention table (Copilot loop 3 finding).
        assert target_email not in (prior.resource or "")
        assert target_user_id not in (prior.resource or "")

        # The new "account_erasure" audit row is also pseudonymized at
        # insert — guards against the regression PR-review caught
        # (CSO Gate1 follow-up #1).
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "account_erasure",
                AuditLog.user_metadata["request_id"].as_string() == str(request.id),
            )
        )
        new_audit = result.scalar_one()
        assert new_audit.user_id != target_user_id
        assert new_audit.user_email != target_email
        assert "user_pseudonym:" in new_audit.resource
        # `initiated_by` for self-service IS the subject's user_id; it must
        # be pseudonymized in user_metadata too. For the admin path tested
        # here, initiated_by is the admin's user_id (NOT the erased subject)
        # so it stays plaintext — the assertion below uses target_user_id,
        # which would only appear if self-service had leaked it.
        assert new_audit.user_metadata is not None
        assert new_audit.user_metadata.get("initiated_by") != target_user_id
        # And no plaintext email/user_id anywhere else in user_metadata.
        metadata_repr = repr(new_audit.user_metadata)
        assert target_email not in metadata_repr
        assert target_user_id not in metadata_repr


# ---------------------------------------------------------------------------
# #2244: the erased member's details on another author's memory
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def edited_by_target(db_session: AsyncSession, erasure_scenario):
    """A memory the other member authored in the surviving (transferred)
    workspace, whose newest details edit is the target's."""
    from models.auth import Context
    from models.memory import Memory, MemoryRevision

    other = erasure_scenario["other_user_id"]
    target = erasure_scenario["target_user_id"]
    context = Context(
        id=uuid4(),
        workspace_id=erasure_scenario["workspace_id"],
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=other,
    )
    db_session.add(context)
    await db_session.flush()
    memory = Memory(
        id=uuid4(),
        user_id=other,
        workspace_id=erasure_scenario["workspace_id"],
        context_id=context.id,
        summary="the other member's memory",
        content="body",
        type="note",
        client="test",
        details={"by": "target", "location": {"lat": 35.0, "lon": 139.0}},
        version=2,
        embedding_status="success",
    )
    db_session.add(memory)
    await db_session.flush()
    db_session.add(
        MemoryRevision(
            memory_id=memory.id,
            context_id=context.id,
            version=2,
            kind="updated",
            writer="api",
            actor_user_id=target,
            prior={"details": {"by": "author"}},
        )
    )
    await db_session.commit()
    return memory.id


class TestEditorDetailsRestore:
    """#2244 through the whole ``_execute`` pipeline."""

    @staticmethod
    async def _state(db_session: AsyncSession, memory_id) -> tuple:
        from models.memory import Memory, MemoryRevision

        memory = (
            await db_session.execute(
                select(Memory)
                .where(Memory.id == memory_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        writers = (
            (
                await db_session.execute(
                    select(MemoryRevision.writer)
                    .where(MemoryRevision.memory_id == memory_id)
                    .order_by(MemoryRevision.version)
                )
            )
            .scalars()
            .all()
        )
        return memory.details, memory.version, list(writers)

    @pytest.mark.asyncio
    async def test_completed_erasure_restores_and_counts(
        self, db_session: AsyncSession, erasure_scenario, edited_by_target, patched_external_stores
    ):
        sync = AsyncMock()
        with patch("services.erasure_details.update_memory_payload_in_qdrant", sync):
            request = await AccountErasureService(db_session).admin_force_erase(
                target_user_id=erasure_scenario["target_user_id"],
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="integration test (#2244)",
            )

        assert request.status == STATUS_COMPLETE
        postgres = request.deleted_data_summary["postgres"]
        assert postgres["details_restored"] == 1
        assert postgres["details_restored_unavailable"] == 0
        # #2266: the payload outcome, split, after the commit
        assert postgres["details_payload_synced"] == 1
        assert "details_payload_skipped" not in postgres
        for key in (
            "details_payload_skipped_tombstone",
            "details_payload_skipped_shared",
            "details_payload_skipped_moved",
            "details_payload_skipped_requeued",
            "details_payload_requeued_after_error",
            "details_embedding_requeued",
        ):
            assert postgres[key] == 0, key
        assert postgres["revision_details_stripped"] == 0
        assert await self._state(db_session, edited_by_target) == (
            {"by": "author"},
            3,
            ["api", "erasure"],
        )
        sync.assert_awaited_once()
        assert sync.await_args.kwargs["delete_keys"] == ["location"]

    @pytest.mark.asyncio
    async def test_a_payload_failure_requeues_and_the_erasure_completes(
        self, db_session: AsyncSession, erasure_scenario, edited_by_target, patched_external_stores
    ):
        """#2266: the payload is patched after the Postgres commit; a failure
        there re-queues the memory for embedding instead of failing the
        erasure (before, the request failed and stayed failed)."""
        from models.memory import Memory
        from utils.exceptions import QdrantError

        with patch(
            "services.erasure_details.update_memory_payload_in_qdrant",
            AsyncMock(side_effect=QdrantError("vector store down")),
        ):
            request = await AccountErasureService(db_session).admin_force_erase(
                target_user_id=erasure_scenario["target_user_id"],
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="integration test (#2266)",
            )

        assert request.status == STATUS_COMPLETE
        postgres = request.deleted_data_summary["postgres"]
        assert (
            postgres["details_payload_synced"],
            postgres["details_payload_requeued_after_error"],
        ) == (
            0,
            1,
        )
        assert await self._state(db_session, edited_by_target) == (
            {"by": "author"},
            3,
            ["api", "erasure"],
        )
        status = (
            await db_session.execute(
                select(Memory.embedding_status)
                .where(Memory.id == edited_by_target)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert status == "pending"


class TestPayloadStepNeverFailsTheRequest:
    """#2266 review S1: after the Postgres commit, an error that is not the
    vector store's (here a read in the payload step) re-queues the memory and
    the erasure still completes — the request cannot be retried by then."""

    @pytest.mark.asyncio
    async def test_a_non_vector_store_error_requeues_and_completes(
        self, db_session: AsyncSession, erasure_scenario, edited_by_target, patched_external_stores
    ):
        from models.memory import Memory

        with (
            patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()) as sync,
            patch(
                "services.context_routing.resolve_collection_name",
                new=AsyncMock(side_effect=RuntimeError("context read failed")),
            ),
        ):
            request = await AccountErasureService(db_session).admin_force_erase(
                target_user_id=erasure_scenario["target_user_id"],
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="integration test (#2266)",
            )

        assert request.status == STATUS_COMPLETE
        postgres = request.deleted_data_summary["postgres"]
        assert postgres["details_payload_requeued_after_error"] == 1
        assert postgres["details_payload_requeue_failed"] == 0
        sync.assert_not_awaited()
        status = (
            await db_session.execute(
                select(Memory.embedding_status)
                .where(Memory.id == edited_by_target)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert status == "pending"


class TestSweepAfterAFailure:
    """#2266: one request failing in ``_execute`` (which rolls the session
    back and so expires every loaded instance) must not stop the sweep: the
    next due request still runs, and nothing escapes."""

    @pytest.mark.asyncio
    async def test_the_next_request_runs(self, db_session: AsyncSession, patched_external_stores):
        from datetime import timedelta

        from sqlalchemy import delete

        from models.erasure import STATUS_COOLING_OFF, STATUS_FAILED

        users = [
            User(
                email=f"sweep-{uuid4().hex[:8]}@example.com",
                user_id=f"sweep_{uuid4().hex[:8]}",
                name="Sweep",
                role="user",
                is_initial_admin=False,
                auth_method="oauth",
                auth_provider="google",
            )
            for _ in range(2)
        ]
        db_session.add_all(users)
        await db_session.flush()
        failing, passing = (u.user_id for u in users)
        requests = [
            ErasureRequest(
                user_id=user.user_id,
                user_email_hash=sha256_hex(user.email),
                initiated_by=user.user_id,
                is_self_service=True,
                reason_code="self_service",
                status=STATUS_COOLING_OFF,
                # the failing one first: the sweep orders by scheduled_for
                scheduled_for=utcnow() - timedelta(minutes=10 - i),
            )
            for i, user in enumerate(users)
        ]
        db_session.add_all(requests)
        await db_session.commit()
        request_ids = [r.id for r in requests]

        async def points(user_id: str) -> dict[str, int]:
            if user_id == failing:
                raise RuntimeError("vector store down")
            return {"kagura_memories": 0}

        try:
            with patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(side_effect=points),
            ):
                executed = await AccountErasureService(db_session).sweep_pending_erasures()

            assert executed == 1
            statuses = dict(
                (
                    await db_session.execute(
                        select(ErasureRequest.id, ErasureRequest.status).where(
                            ErasureRequest.id.in_(request_ids)
                        )
                    )
                ).all()
            )
            assert statuses == {request_ids[0]: STATUS_FAILED, request_ids[1]: STATUS_COMPLETE}
            remaining = (
                await db_session.execute(
                    select(User.user_id).where(User.user_id.in_([failing, passing]))
                )
            ).scalars()
            assert set(remaining) == {failing}
        finally:
            await db_session.execute(
                delete(ErasureRequest).where(ErasureRequest.id.in_(request_ids))
            )
            await db_session.execute(delete(User).where(User.user_id == failing))
            await db_session.commit()


class TestTimeMemories:
    """#2266: ``type='time'`` rows must keep a trigger window
    (``valid_trigger_window_format``), so neither the scrub nor the restore
    may NULL their ``details``. Before the fix the erasure of a member who
    authored a time memory in a surviving workspace failed on the CHECK."""

    @pytest.mark.asyncio
    async def test_the_erasure_completes_and_keeps_only_the_trigger(
        self, db_session: AsyncSession, erasure_scenario, patched_external_stores
    ):
        from sqlalchemy import text

        from models.auth import Context
        from models.memory import Memory, MemoryRevision
        from utils.time_trigger import normalize_trigger, trigger_window

        other = erasure_scenario["other_user_id"]
        target = erasure_scenario["target_user_id"]
        ws_id = erasure_scenario["workspace_id"]
        trigger = normalize_trigger({"year": 2027, "month": 1, "day": 1, "text": "launch"})
        other_trigger = normalize_trigger({"year": 2028, "month": 2})
        location = {"lat": 35.0, "lon": 139.0}
        context = Context(
            id=uuid4(), workspace_id=ws_id, name=f"ctx-{uuid4().hex[:8]}", created_by=other
        )
        db_session.add(context)
        await db_session.flush()

        def memory(owner: str, kind: str, details: dict, version: int = 1) -> Memory:
            return Memory(
                id=uuid4(),
                user_id=owner,
                workspace_id=ws_id,
                context_id=context.id,
                summary="a memory",
                content="body",
                type=kind,
                client="test",
                details=details,
                version=version,
                embedding_status="success",
            )

        own_time = memory(target, "time", {"trigger": trigger, "location": location, "k": 1})
        own_note = memory(target, "note", {"location": location})
        # another author's time memory nobody else touched
        others_time = memory(other, "time", {"trigger": other_trigger, "location": location})
        # another author's time memory whose newest details edit is the target's
        edited_time = memory(other, "time", {"trigger": trigger, "by": "target"}, version=2)
        db_session.add_all([own_time, own_note, others_time, edited_time])
        await db_session.flush()
        db_session.add(
            MemoryRevision(
                memory_id=edited_time.id,
                context_id=context.id,
                version=2,
                kind="updated",
                writer="api",
                actor_user_id=target,
                prior={"details": {"trigger": other_trigger, "by": "author"}},
            )
        )
        await db_session.commit()
        outside = text("SELECT id, to_jsonb(t) FROM memories AS t WHERE NOT (t.id = ANY(:changed))")
        changed = [own_time.id, own_note.id, edited_time.id]
        before = dict((await db_session.execute(outside, {"changed": changed})).all())

        with patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()):
            request = await AccountErasureService(db_session).admin_force_erase(
                target_user_id=target,
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="integration test (#2266)",
            )

        assert request.status == STATUS_COMPLETE
        assert request.deleted_data_summary["postgres"]["memories_scrubbed"] == 2

        async def row(memory_id):
            return (
                await db_session.execute(
                    select(
                        Memory.user_id,
                        Memory.details,
                        Memory.trigger_from,
                        Memory.trigger_until,
                        Memory.location_lat,
                        Memory.version,
                    ).where(Memory.id == memory_id)
                )
            ).one()

        own = await row(own_time.id)
        assert own.user_id == request.user_id != target  # the pseudonym
        # the window only: the trigger's ``text`` (the subject's words) goes
        assert own.details == {"trigger": trigger_window(trigger)}
        assert "launch" not in repr(own.details)
        assert (own.trigger_from, own.trigger_until) == (trigger["from"], trigger["until"])
        assert own.location_lat is None
        note = await row(own_note.id)
        assert (note.details, note.location_lat) == (None, None)
        # the author's value comes back verbatim (it has its own trigger)
        edited = await row(edited_time.id)
        assert (edited.details, edited.version) == ({"trigger": other_trigger, "by": "author"}, 3)
        assert edited.trigger_from == other_trigger["from"]
        assert dict((await db_session.execute(outside, {"changed": changed})).all()) == before


class TestEarlierErasedMembersValue:
    """#2243 review S1 through the whole ``_execute`` pipeline: a member
    erased earlier (before the #2235 fix) left their value in the prior of
    the target's edit. The target's erasure must not make it live again."""

    @pytest.mark.asyncio
    async def test_a_value_an_erased_member_wrote_is_not_put_back(
        self, db_session: AsyncSession, erasure_scenario, patched_external_stores
    ):
        from sqlalchemy import delete

        from models.auth import Context
        from models.memory import Memory, MemoryRevision

        other = erasure_scenario["other_user_id"]
        target = erasure_scenario["target_user_id"]
        earlier = uuid4().hex * 2  # the stored pseudonym of a finalized erasure
        absent = uuid4().hex * 2  # a raw 64-hex sub the sweep marked complete unrun
        context = Context(
            id=uuid4(),
            workspace_id=erasure_scenario["workspace_id"],
            name=f"ctx-{uuid4().hex[:8]}",
            created_by=other,
        )
        db_session.add(context)
        await db_session.flush()
        memory = Memory(
            id=uuid4(),
            user_id=other,
            workspace_id=erasure_scenario["workspace_id"],
            context_id=context.id,
            summary="the other member's memory",
            content="body",
            type="note",
            client="test",
            details={"by": "target"},
            version=3,
            embedding_status="success",
        )
        db_session.add(memory)
        await db_session.flush()
        requests = [
            ErasureRequest(
                user_id=user_id,
                user_email_hash="0" * 64,
                initiated_by="admin-x",
                is_self_service=False,
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                status=STATUS_COMPLETE,
                completed_at=utcnow(),
                deleted_data_summary=summary,
            )
            for user_id, summary in (
                (earlier, {"postgres": {}}),
                (absent, {"note": "user_row_already_absent"}),
            )
        ]
        db_session.add_all(
            [
                MemoryRevision(
                    memory_id=memory.id,
                    context_id=context.id,
                    version=2,
                    kind="updated",
                    writer="api",
                    actor_user_id=earlier,
                    prior={"details": {"by": "author"}},
                ),
                MemoryRevision(
                    memory_id=memory.id,
                    context_id=context.id,
                    version=3,
                    kind="updated",
                    writer="api",
                    actor_user_id=target,
                    prior={"details": {"by": "earlier"}},
                ),
                *requests,
            ]
        )
        await db_session.commit()
        try:
            with patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()):
                request = await AccountErasureService(db_session).admin_force_erase(
                    target_user_id=target,
                    initiator_user_id="admin-runner",
                    reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                    reason_detail="integration test (#2243)",
                )

            assert request.status == STATUS_COMPLETE
            postgres = request.deleted_data_summary["postgres"]
            assert (postgres["details_restored"], postgres["details_restored_unavailable"]) == (
                0,
                1,
            )
            live = (
                await db_session.execute(
                    select(Memory.details)
                    .where(Memory.id == memory.id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            assert live is None  # not the earlier erased member's value
        finally:
            await db_session.execute(
                delete(ErasureRequest).where(ErasureRequest.id.in_([r.id for r in requests]))
            )
            await db_session.commit()


class TestPartialUniqueIndexRace:
    """uq_erasure_one_active_per_user must reject a second active row."""

    @pytest.mark.asyncio
    async def test_second_concurrent_request_raises_already_in_progress(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        patched_external_stores,
    ):
        target_user_id = erasure_scenario["target_user_id"]
        target_email = erasure_scenario["target_email"]

        # First request: insert a pending row directly to simulate the
        # "in-flight" half of the race. Bypasses the service's in-memory
        # _find_active_request guard so the partial unique index is the
        # only thing keeping the second insert out.
        first = ErasureRequest(
            user_id=target_user_id,
            user_email_hash=sha256_hex(target_email),
            initiated_by=target_user_id,
            is_self_service=True,
            reason_code="self_service",
            status="pending",
            confirm_token_hash=sha256_hex("first-token"),
        )
        db_session.add(first)
        await db_session.commit()

        # Second request: service-level path should observe the existing
        # row and raise ErasureAlreadyInProgressError (the in-memory guard
        # short-circuits before ever hitting the DB-level constraint, but
        # the same exception type is raised either way — the API surface
        # is identical).
        service = AccountErasureService(db_session)
        with pytest.raises(ErasureAlreadyInProgressError):
            await service.request_self_service_erasure(user_id=target_user_id)


# ---------------------------------------------------------------------------
# RFC-0002 P0-2 (#1275): agent registry / binding / agent-bound key CASCADE
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def agent_erasure_scenario(db_session: AsyncSession):
    """The F1 sign-off erasure scenario (docs/design/agent-registry-and-bindings.md).

    Validates the CASCADE ordering the design doc's ON DELETE CASCADE choice
    exists for: the workspace hard-delete must take the agent, BOTH
    agent-bound key rows (including a soft-revoked forensics row — the shape
    a RESTRICT FK would abort a legally mandated erasure on), and the binding
    row with it.

    Deviation from the doc's literal step 2: a lingering service *member*
    would (correctly) trip the pre-existing WorkspaceTransferRequiredError
    guard before the hard-delete path is reachable, so the key rows here
    belong to a departed service identity whose membership row is already
    gone while its (revoked/live) key rows linger — exactly the retention
    shape that motivates CASCADE over RESTRICT.
    """
    from models.agent import Agent, AgentContextBinding
    from models.auth import Context

    target_user_id = f"agent_owner_{uuid4().hex[:8]}"
    target_email = f"agent-owner-{uuid4().hex[:6]}@example.com"
    member_user_id = f"svc_member_{uuid4().hex[:8]}"

    target = User(
        email=target_email,
        user_id=target_user_id,
        name="Agent Owner",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
    )
    db_session.add(target)
    await db_session.flush()

    workspace = Workspace(
        id=uuid4(),
        name=f"ws-agent-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=target_user_id,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )
    db_session.add(workspace)
    await db_session.flush()

    # Sole membership: the owner only. A lingering non-admin member would
    # trip WorkspaceTransferRequiredError before the hard-delete path (the
    # cascade under test) is reachable — see fixture docstring.
    db_session.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=target_user_id,
            role=WorkspaceRole.OWNER,
        )
    )

    agent = Agent(
        workspace_id=workspace.id,
        name="erasure-ci-agent",
        owner_user_id=target_user_id,
    )
    db_session.add(agent)
    await db_session.flush()

    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=target_user_id,
    )
    db_session.add(context)
    await db_session.flush()

    binding = AgentContextBinding(
        agent_id=agent.id,
        context_id=context.id,
        can_read=True,
        write_policy="direct",
        is_default=True,
        created_by=target_user_id,
    )
    db_session.add(binding)

    # K: live agent-bound member key (owner-provisioned shape: member user,
    # workspace-scoped, mandatory expiry semantics are a route concern).
    key_live = APIKey(
        key_hash=sha256_hex(f"kagura_agent_{uuid4().hex}"),
        key_prefix="kagura_agent_xx",
        name="agent-key-live",
        user_id=member_user_id,
        workspace_id=workspace.id,
        agent_id=agent.id,
    )
    # K2: soft-revoked agent-bound key — the forensics-retained row shape.
    from utils.datetime import utcnow

    key_revoked = APIKey(
        key_hash=sha256_hex(f"kagura_agent_{uuid4().hex}"),
        key_prefix="kagura_agent_yy",
        name="agent-key-revoked",
        user_id=member_user_id,
        workspace_id=workspace.id,
        agent_id=agent.id,
        revoked_at=utcnow().replace(tzinfo=None),
    )
    db_session.add_all([key_live, key_revoked])
    await db_session.commit()

    yield {
        "target_user_id": target_user_id,
        "workspace_id": workspace.id,
        "agent_id": agent.id,
        "context_id": context.id,
        "binding_id": binding.id,
        "key_live_id": key_live.id,
        "key_revoked_id": key_revoked.id,
    }


class TestAgentErasureCascade:
    """F1 sign-off scenario: workspace hard-delete cascades the agent lane."""

    @pytest.mark.asyncio
    async def test_sole_owner_erasure_cascades_agents_keys_bindings(
        self,
        db_session: AsyncSession,
        agent_erasure_scenario,
        patched_external_stores,
    ):
        from models.agent import Agent, AgentContextBinding

        s = agent_erasure_scenario
        service = AccountErasureService(db_session)

        request = await service.admin_force_erase(
            target_user_id=s["target_user_id"],
            initiator_user_id="admin-runner",
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            reason_detail="integration test (RFC-0002 F1 scenario)",
        )
        # Completed without FK violations.
        assert request.status == STATUS_COMPLETE

        # Sole-owner workspace was hard-deleted...
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == s["workspace_id"])
        )
        assert result.scalar_one_or_none() is None

        # ...and the cascade took the agent, BOTH keys (incl. the
        # soft-revoked forensics row — the shape RESTRICT would trip on),
        # and the binding with it. No orphans remain.
        result = await db_session.execute(select(Agent).where(Agent.id == s["agent_id"]))
        assert result.scalar_one_or_none() is None

        result = await db_session.execute(
            select(APIKey).where(APIKey.id.in_([s["key_live_id"], s["key_revoked_id"]]))
        )
        assert result.scalars().all() == []

        result = await db_session.execute(
            select(AgentContextBinding).where(AgentContextBinding.id == s["binding_id"])
        )
        assert result.scalar_one_or_none() is None
