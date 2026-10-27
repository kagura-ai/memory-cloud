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

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import APIKey, AuditLog, User, Workspace, WorkspaceMember
from models.erasure import (
    REASON_USER_REQUEST_VIA_SUPPORT,
    STATUS_CANCELLED,
    STATUS_COMPLETE,
    STATUS_FAILED,
    ErasureRequest,
)
from services.account_erasure_service import AccountErasureService, _audit_salt
from services.erasure_details import erasure_pseudonym
from utils.datetime import to_naive_utc, utcnow
from utils.exceptions import ErasureAlreadyInProgressError
from utils.hashing import sha256_hex

from ._connector_world import (
    _key_rows,
    _live_connector_key,
    _without_owner,
)

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
        # #2345: the subject's request metadata, cleared by the erasure
        ip_address="203.0.113.5",
        user_agent="ua",
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


@pytest_asyncio.fixture
async def other_actors_audit(db_session: AsyncSession, erasure_scenario):
    """Other actors' audit rows around the target (#2287, #2298), in order:

    0. the other member acting on the target (``resource`` names the
       target's email): only ``resource`` changes, to the email pseudonym —
       the acting member's IP stays;
    1. the target acting as an admin on the other member (``user_email`` is
       the target's, ``user_id`` and ``resource`` the other's): ``user_email``
       changes and the target's IP / user agent are cleared;
    2. a third account acting on itself, with an IP: byte-identical;
    3. a look-alike ``resource`` (the target's email plus a suffix):
       byte-identical — exact values only, never a substring.
    """
    target_email = erasure_scenario["target_email"]
    other = erasure_scenario["other_user_id"]
    rows = [
        AuditLog(
            user_email=f"{other}@example.com",
            user_id=other,
            action="role_assign",
            resource=f"user:{target_email}",
            ip_address="198.51.100.20",
            user_agent="admin-agent",
        ),
        AuditLog(
            user_email=target_email,
            user_id=other,
            action="role_assign",
            resource=f"user:{other}@example.com",
            ip_address="198.51.100.21",
            user_agent="target-agent",
        ),
        AuditLog(
            user_email="third@example.com",
            user_id=f"third_{uuid4().hex[:8]}",
            action="password_set",
            resource="user:third@example.com",
            ip_address="198.51.100.22",
        ),
        AuditLog(
            user_email=f"{other}@example.com",
            user_id=other,
            action="role_assign",
            resource=f"user:{target_email}-x",
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    ids = [row.id for row in rows]
    yield ids
    await db_session.rollback()
    await db_session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
    await db_session.commit()


async def _audit_rows_snapshot(db_session: AsyncSession, ids) -> dict:
    """Byte-level ``to_jsonb`` of the ``audit_logs`` rows ``ids``."""
    rows = (
        await db_session.execute(
            text("SELECT t.id, to_jsonb(t) FROM audit_logs AS t WHERE t.id = ANY(:ids)"),
            {"ids": list(ids)},
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _table_snapshot(db_session: AsyncSession, table: str) -> dict:
    """Byte-level ``to_jsonb`` of every row of ``table``."""
    rows = (
        await db_session.execute(
            text(f"SELECT t.id, to_jsonb(t) FROM {table} AS t")  # noqa: S608 - fixed names
        )
    ).all()
    return {row[0]: row[1] for row in rows}


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
        other_actors_audit,
        patched_external_stores,
    ):
        target_user_id = erasure_scenario["target_user_id"]
        target_email = erasure_scenario["target_email"]
        api_key_id = erasure_scenario["api_key_id"]
        prior_audit_id = erasure_scenario["prior_audit_id"]
        workspace_id = erasure_scenario["workspace_id"]

        service = AccountErasureService(db_session)
        others_before = await _audit_rows_snapshot(db_session, other_actors_audit)

        request = await service.admin_force_erase(
            target_user_id=target_user_id,
            initiator_user_id="admin-runner",
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            reason_detail="integration test",
            ip_address="203.0.113.9",
            user_agent="agent",
        )

        # The erasure_requests row reached terminal complete with a summary.
        assert request.status == STATUS_COMPLETE
        assert request.deleted_data_summary is not None
        assert "postgres" in request.deleted_data_summary
        assert request.deleted_data_summary["postgres"]["users"] == 1
        # The audit-log pseudonymize ran inside the Postgres step (#2287;
        # count >= 1 because the prior audit row above was authored by the
        # target).
        assert request.deleted_data_summary["postgres"]["audit_logs_pseudonymized"] >= 1
        assert "audit_logs_pseudonymized" not in request.deleted_data_summary
        # #2358: a completed erasure re-queues nothing and records no retry
        assert "retry" not in request.deleted_data_summary
        # #2298: the rows naming the target change in that one value only;
        # every other actor's row is byte-identical
        assert request.deleted_data_summary["postgres"]["audit_mentions_pseudonymized"] == 2
        others_after = await _audit_rows_snapshot(db_session, other_actors_audit)
        about, by_target, third, lookalike = other_actors_audit
        email_pseudonym = erasure_pseudonym(target_email)
        assert others_after[about] == {
            **others_before[about],
            "resource": f"user:{email_pseudonym}",
        }
        # the target was the actor there: their IP / UA go too (#2345)
        assert others_after[by_target] == {
            **others_before[by_target],
            "user_email": email_pseudonym,
            "ip_address": None,
            "user_agent": None,
        }
        assert others_after[about]["ip_address"] == "198.51.100.20"  # the admin's
        for row_id in (third, lookalike):
            assert others_after[row_id] == others_before[row_id]

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
        # #2345: the subject's IP / user agent leave their own rows
        assert (prior.ip_address, prior.user_agent) == (None, None)

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
        assert new_audit.user_metadata.get("initiated_by") == "admin-runner"
        # And no plaintext email/user_id anywhere else in user_metadata.
        metadata_repr = repr(new_audit.user_metadata)
        assert target_email not in metadata_repr
        assert target_user_id not in metadata_repr
        # #2345: no request metadata on the row, the admin's included (the
        # request row clears both too)
        assert (new_audit.ip_address, new_audit.user_agent) == (None, None)

    @pytest.mark.asyncio
    async def test_a_self_initiated_admin_request_pseudonymizes_initiated_by(
        self, db_session: AsyncSession, erasure_scenario, patched_external_stores
    ):
        """#2345: ``initiated_by`` equal to the subject is pseudonymized on the
        audit row like on the request row, though ``is_self_service`` is
        False (the admin route refuses this; the service does not)."""
        target_user_id = erasure_scenario["target_user_id"]

        request = await AccountErasureService(db_session).admin_force_erase(
            target_user_id=target_user_id,
            initiator_user_id=target_user_id,
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            reason_detail="integration test (#2345)",
            ip_address="203.0.113.9",
            user_agent="agent",
        )

        pseudonym = erasure_pseudonym(target_user_id)
        assert (request.user_id, request.initiated_by) == (pseudonym, pseudonym)
        audit = (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "account_erasure",
                    AuditLog.user_metadata["request_id"].as_string() == str(request.id),
                )
            )
        ).scalar_one()
        assert audit.user_metadata["is_self_service"] is False
        assert audit.user_metadata["initiated_by"] == pseudonym
        assert (audit.ip_address, audit.user_agent) == (None, None)
        assert target_user_id not in repr(audit.user_metadata)


class TestAuditLogFailureRollsThePostgresStepBack:
    """#2287: the audit-log pseudonymize is part of the Postgres step's
    transaction. When it fails, nothing of the Postgres step is committed:
    the user row stays, the audit rows keep their values, and the request is
    ``failed`` without the after-commit marker, keeping its ids so a new
    request can retry."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failing",
        [
            "services.account_erasure_service.AccountErasureService._pseudonymize_audit_logs",
            # #2298: after the own rows' UPDATE ran in the transaction
            "services.erasure_details.pseudonymize_audit_mentions",
            # #2275: the per-key strip of the editor arm, after the restore
            "services.erasure_details._strip_editor_keys",
        ],
    )
    async def test_an_audit_log_failure_rolls_the_postgres_step_back(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        other_actors_audit,
        patched_external_stores,
        failing: str,
    ):
        from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT, STATUS_FAILED

        target = erasure_scenario["target_user_id"]
        tables = ("users", "audit_logs", "api_keys", "memories", "memory_revisions")
        before = {table: await _table_snapshot(db_session, table) for table in tables}

        with patch(failing, AsyncMock(side_effect=RuntimeError("audit_logs unavailable"))):
            with pytest.raises(RuntimeError):
                await AccountErasureService(db_session).admin_force_erase(
                    target_user_id=target,
                    initiator_user_id="admin-runner",
                    reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                    reason_detail="integration test (#2287)",
                )

        request = (
            await db_session.execute(
                select(ErasureRequest)
                .where(ErasureRequest.user_id == target)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert request.status == STATUS_FAILED
        assert request.failure_reason == "audit_logs unavailable"
        assert not request.failure_reason.startswith(FAILURE_AFTER_POSTGRES_COMMIT)
        assert "postgres" not in (request.deleted_data_summary or {})
        for table in tables:
            assert await _table_snapshot(db_session, table) == before[table], table


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
        # #2275: the target's ``by`` and ``location``; nothing ambiguous
        assert (postgres["details_keys_reverted"], postgres["details_keys_ambiguous"]) == (2, 0)
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
        assert sync.await_args.kwargs["delete_keys"] == ["location", "observed_at"]  # #2330
        # #2268: the scrub time, the re-run's bound, before the completion
        scrubbed = datetime.fromisoformat(postgres["memory_scrub_at"])
        assert scrubbed.tzinfo is not None
        assert (
            to_naive_utc(request.started_at)
            <= to_naive_utc(scrubbed)
            <= to_naive_utc(request.completed_at)
        )

    @pytest.mark.asyncio
    async def test_the_logs_are_read_once_for_restore_and_strip(
        self, db_session: AsyncSession, erasure_scenario, edited_by_target, patched_external_stores
    ):
        """#2268: one ``_updated_rows`` read per erasure serves the restore and
        the strip (``erase_subject_details``); the outcome is the two-call one."""
        from services import erasure_details

        with (
            patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()),
            patch.object(
                erasure_details, "_updated_rows", wraps=erasure_details._updated_rows
            ) as read,
        ):
            request = await AccountErasureService(db_session).admin_force_erase(
                target_user_id=erasure_scenario["target_user_id"],
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="integration test (#2268)",
            )

        assert request.status == STATUS_COMPLETE
        assert read.await_count == 1
        assert await self._state(db_session, edited_by_target) == (
            {"by": "author"},
            3,
            ["api", "erasure"],
        )

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


class TestFailureAfterThePostgresCommit:
    """#2268: a step after the Postgres commit fails (here ``_finalize``). The
    subject is erased in Postgres and the user row is gone: the request is
    ``failed`` with the marker and stores the pseudonym, and the re-run
    treats it as an erasure that ran (nothing left to change)."""

    @pytest.mark.asyncio
    async def test_the_request_is_pseudonymized_and_the_rerun_finds_it_done(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        edited_by_target,
        patched_external_stores,
        capsys: pytest.CaptureFixture[str],
    ):
        from cli import reprocess_erasures as cli
        from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT, STATUS_FAILED

        target = erasure_scenario["target_user_id"]
        with (
            patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()),
            patch.object(
                AccountErasureService,
                "_finalize",
                AsyncMock(side_effect=RuntimeError("finalize failed")),
            ),
        ):
            with pytest.raises(RuntimeError):
                await AccountErasureService(db_session).admin_force_erase(
                    target_user_id=target,
                    initiator_user_id="admin-runner",
                    reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                    reason_detail="integration test (#2268)",
                )

        pseudonym = sha256_hex(target, salt=_audit_salt())
        request = (
            await db_session.execute(
                select(ErasureRequest)
                .where(ErasureRequest.user_id == pseudonym)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert request.status == STATUS_FAILED
        assert request.failure_reason == f"{FAILURE_AFTER_POSTGRES_COMMIT}finalize: RuntimeError"
        assert (request.initiated_by, request.ip_address, request.user_agent) == (
            "admin-runner",
            None,
            None,
        )
        assert "memory_scrub_at" in request.deleted_data_summary["postgres"]
        assert (
            await db_session.execute(select(User).where(User.user_id == target))
        ).scalar_one_or_none() is None
        # the restore happened in the committed Postgres step
        assert await TestEditorDetailsRestore._state(db_session, edited_by_target) == (
            {"by": "author"},
            3,
            ["api", "erasure"],
        )
        await db_session.commit()
        capsys.readouterr()  # the erasure's own log lines carry the raw id

        async def get_db():
            yield db_session

        with (
            patch("cli._oneshot.get_db", get_db),
            patch.object(cli, "configure_logging", lambda level: None),
        ):
            code = await cli._main(cli._parse(["--request", str(request.id)]))
        assert code == 0
        out = capsys.readouterr().out
        # in scope (not "not a completed erasure request"), nothing to change
        assert "1 completed erasure(s): would change 0, skipped 0" in out
        assert target not in out


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
    the target's edit. The target's erasure must not make it live again: the
    walk goes past the earlier member's edit to the author's value (#2268)."""

    @pytest.mark.asyncio
    async def test_a_value_an_erased_member_wrote_is_walked_past(
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
                1,
                0,
            )
            live = (
                await db_session.execute(
                    select(Memory.details)
                    .where(Memory.id == memory.id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            # not the earlier erased member's value: the author's, below it
            assert live == {"by": "author"}
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


# ---------------------------------------------------------------------------
# #2286: a run interrupted after the Postgres commit; a user row gone unrun
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def bystanders(db_session: AsyncSession):
    """A live account with a scheduled (not yet due) request and another
    account's request in progress (not stale): the sweep must leave both
    rows, and the live user row, byte-identical."""
    from datetime import timedelta

    from models.erasure import STATUS_COOLING_OFF, STATUS_IN_PROGRESS

    tag = uuid4().hex[:8]
    user = User(
        email=f"bystander-{tag}@example.com",
        user_id=f"bystander_{tag}",
        name="Bystander",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
    )
    db_session.add(user)
    await db_session.flush()
    requests = [
        ErasureRequest(
            user_id=user.user_id,
            user_email_hash=sha256_hex(user.email),
            initiated_by=user.user_id,
            is_self_service=True,
            reason_code="self_service",
            status=STATUS_COOLING_OFF,
            scheduled_for=utcnow() + timedelta(days=3),
            ip_address="198.51.100.7",
        ),
        ErasureRequest(
            user_id=f"running_{tag}",
            user_email_hash="1" * 64,
            initiated_by="admin-x",
            is_self_service=False,
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            status=STATUS_IN_PROGRESS,
            started_at=utcnow() - timedelta(minutes=5),
        ),
    ]
    db_session.add_all(requests)
    await db_session.commit()
    ids = [r.id for r in requests]
    user_id = user.user_id  # read now: the teardown's rollback expires it
    yield {"user_id": user_id, "request_ids": ids}
    await db_session.rollback()
    await db_session.execute(delete(ErasureRequest).where(ErasureRequest.id.in_(ids)))
    await db_session.execute(delete(User).where(User.user_id == user_id))
    await db_session.commit()


async def _everything_but(db_session: AsyncSession, request_id) -> dict:
    """Whole-table snapshots the sweep must not change, minus the request
    it closes."""
    requests = await _table_snapshot(db_session, "erasure_requests")
    requests.pop(request_id, None)
    return {
        "erasure_requests": requests,
        **{
            table: await _table_snapshot(db_session, table)
            for table in ("users", "memories", "memory_revisions", "audit_logs")
        },
    }


async def _age(db_session: AsyncSession, request_id, *, hours: int = 2) -> None:
    """Make the request stale for the sweep (``started_at`` past its cutoff)."""
    from datetime import timedelta

    from sqlalchemy import update

    await db_session.execute(
        update(ErasureRequest)
        .where(ErasureRequest.id == request_id)
        .values(started_at=utcnow() - timedelta(hours=hours))
    )
    await db_session.commit()


async def _request_snapshot(db_session: AsyncSession, request_id) -> dict:
    return (await _table_snapshot(db_session, "erasure_requests"))[request_id]


_QDRANT_PATCH = "services.erasure_details.update_memory_payload_in_qdrant"


async def _crash_after_the_postgres_commit(db_session: AsyncSession, target: str) -> UUID:
    """An admin force-erase of ``target`` whose process dies after the
    Postgres step committed (#2286 recipe: a ``CancelledError`` at the Redis
    step, which no handler catches). Returns the request id; the row is
    ``in_progress`` with the raw sub, as another process sees it."""
    import asyncio

    with (
        patch(_QDRANT_PATCH, AsyncMock()),
        patch.object(
            AccountErasureService, "_clear_redis", AsyncMock(side_effect=asyncio.CancelledError())
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await AccountErasureService(db_session).admin_force_erase(
                target_user_id=target,
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="integration test (#2302)",
                ip_address="203.0.113.9",
                user_agent="agent",
            )
    await db_session.rollback()
    db_session.expire_all()
    request_id = (
        await db_session.execute(select(ErasureRequest.id).where(ErasureRequest.user_id == target))
    ).scalar_one()
    await db_session.rollback()
    return request_id


class TestARunInterruptedAfterThePostgresCommit:
    """#2286: the process dies after ``_delete_postgres`` committed (here a
    ``CancelledError``, which no handler catches): the request stays
    ``in_progress`` with the raw sub. The Postgres step wrote its summary on
    the row in its own transaction, so the sweep (user row gone) marks it
    ``failed`` with the ``interrupted`` marker and pseudonymizes it; the
    erased-members set and the re-run treat it as an erasure that ran."""

    @pytest.mark.asyncio
    async def test_the_sweep_pseudonymizes_it_and_the_rerun_finds_it_done(
        self,
        db_session: AsyncSession,
        async_engine,
        erasure_scenario,
        edited_by_target,
        other_actors_audit,
        bystanders,
        patched_external_stores,
        capsys: pytest.CaptureFixture[str],
    ):
        import asyncio

        from sqlalchemy.ext.asyncio import async_sessionmaker

        from cli import reprocess_erasures as cli
        from models.erasure import (
            INTERRUPTED_AFTER_POSTGRES_COMMIT,
            STATUS_FAILED,
            STATUS_IN_PROGRESS,
        )
        from services.erasure_details import erased_pseudonyms

        target = erasure_scenario["target_user_id"]
        pseudonym = sha256_hex(target, salt=_audit_salt())
        with (
            patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()),
            patch.object(
                AccountErasureService,
                "_clear_redis",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await AccountErasureService(db_session).admin_force_erase(
                    target_user_id=target,
                    initiator_user_id="admin-runner",
                    reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                    reason_detail="integration test (#2286)",
                    ip_address="203.0.113.9",
                    user_agent="agent",
                )

        # what another process sees: the Postgres step committed, and with it
        # its summary; no handler ran, so the row keeps the raw sub
        maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as other:
            row = (
                await other.execute(select(ErasureRequest).where(ErasureRequest.user_id == target))
            ).scalar_one()
            assert row.status == STATUS_IN_PROGRESS
            assert "memory_scrub_at" in row.deleted_data_summary["postgres"]
            assert row.deleted_data_summary["postgres"]["users"] == 1
            assert (
                await other.execute(select(User).where(User.user_id == target))
            ).scalar_one_or_none() is None
        request_id = row.id
        await db_session.rollback()
        # the sweep runs in another process: nothing of the dead run's session
        # is cached (its request instance predates the in-transaction write)
        db_session.expire_all()
        # not stale yet: the sweep leaves a running request alone
        assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        assert (await _request_snapshot(db_session, request_id))["status"] == STATUS_IN_PROGRESS

        await _age(db_session, request_id)
        before = await _everything_but(db_session, request_id)
        capsys.readouterr()
        # #2302: the sweep re-drives the payload patch of the restored memory;
        # #2363: and repeats the late point delete by the raw sub
        late_points = AsyncMock(return_value={"kagura_memories": 1})
        with (
            patch(_QDRANT_PATCH, AsyncMock()) as qdrant,
            patch("services.account_erasure_service.delete_user_points", late_points),
        ):
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        qdrant.assert_awaited_once()
        assert qdrant.await_args.kwargs["memory_id"] == edited_by_target
        late_points.assert_awaited_once_with(target)

        closed = await _request_snapshot(db_session, request_id)
        assert closed["status"] == STATUS_FAILED
        assert closed["failure_reason"] == INTERRUPTED_AFTER_POSTGRES_COMMIT
        assert closed["user_id"] == pseudonym != target
        assert closed["initiated_by"] == "admin-runner"  # the admin's id stays
        assert (closed["ip_address"], closed["user_agent"], closed["completed_at"]) == (
            None,
            None,
            None,
        )
        assert "memory_scrub_at" in closed["deleted_data_summary"]["postgres"]
        assert closed["deleted_data_summary"]["postgres"]["details_payload_synced"] == 1
        assert closed["deleted_data_summary"]["postgres"]["details_payload_requeue_failed"] == 0
        assert "redis" in closed["deleted_data_summary"]
        assert closed["deleted_data_summary"]["qdrant_after_commit"] == {"kagura_memories": 1}
        assert target not in repr(closed)
        # every other row of every table it could reach is byte-identical
        assert await _everything_but(db_session, request_id) == before
        assert pseudonym in await erased_pseudonyms(db_session)
        assert target not in capsys.readouterr().out

        # a second sweep never re-claims it (``failed`` is terminal)
        with patch(_QDRANT_PATCH, AsyncMock()) as qdrant:
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        qdrant.assert_not_awaited()
        assert await _request_snapshot(db_session, request_id) == closed
        assert await _everything_but(db_session, request_id) == before

        # the re-run: in scope as an erasure that ran; nothing left to change
        await db_session.commit()

        async def get_db():
            yield db_session

        with (
            patch("cli._oneshot.get_db", get_db),
            patch.object(cli, "configure_logging", lambda level: None),
            patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()),
        ):
            code = await cli._main(cli._parse(["--request", str(request_id), "--apply", "--yes"]))
        assert code == 0
        out = capsys.readouterr().out
        assert "1 completed erasure(s): would change 0, skipped 0" in out
        assert target not in out
        assert await _request_snapshot(db_session, request_id) == closed


class TestTheSweepsPayloadReDriveAndAReRegisteredSub:
    """#2302: the sweep's re-drive of an interrupted run's payload patch when
    the vector store is down, and a sub registered again before the sweep
    reached the interrupted row."""

    @pytest.mark.asyncio
    async def test_a_vector_store_outage_re_queues_the_restored_memory(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        edited_by_target,
        bystanders,
        patched_external_stores,
    ):
        from models.erasure import INTERRUPTED_AFTER_POSTGRES_COMMIT, STATUS_FAILED
        from models.memory import Memory
        from utils.exceptions import QdrantError

        request_id = await _crash_after_the_postgres_commit(
            db_session, erasure_scenario["target_user_id"]
        )
        await _age(db_session, request_id)
        before = await _everything_but(db_session, request_id)

        with patch(_QDRANT_PATCH, AsyncMock(side_effect=QdrantError("down"))) as qdrant:
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        qdrant.assert_awaited_once()

        closed = await _request_snapshot(db_session, request_id)
        assert (closed["status"], closed["failure_reason"]) == (
            STATUS_FAILED,
            INTERRUPTED_AFTER_POSTGRES_COMMIT,
        )
        postgres = closed["deleted_data_summary"]["postgres"]
        assert postgres["details_payload_requeued_after_error"] == 1
        assert postgres["details_payload_synced"] == 0
        status = (
            await db_session.execute(
                select(Memory.embedding_status).where(Memory.id == edited_by_target)
            )
        ).scalar_one()
        assert status == "pending"
        after = await _everything_but(db_session, request_id)
        after["memories"].pop(edited_by_target), before["memories"].pop(edited_by_target)
        assert after == before

    @pytest.mark.asyncio
    async def test_a_re_registered_sub_is_never_erased_by_the_old_request(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        bystanders,
        patched_external_stores,
    ):
        from models.auth import Context
        from models.erasure import INTERRUPTED_AFTER_POSTGRES_COMMIT, STATUS_FAILED
        from models.memory import Memory
        from services.erasure_details import erased_pseudonyms

        sub = erasure_scenario["target_user_id"]
        pseudonym = sha256_hex(sub, salt=_audit_salt())
        request_id = await _crash_after_the_postgres_commit(db_session, sub)

        # the same IdP identity signs up again before the sweep comes by
        user = User(
            email=f"again-{uuid4().hex[:6]}@example.com",
            user_id=sub,
            name="Again",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
        db_session.add(user)
        await db_session.flush()
        workspace = Workspace(
            id=uuid4(),
            name=f"ws-again-{uuid4().hex[:8]}",
            plan_name="free",
            owner_user_id=sub,
            daily_api_limit=500,
            weekly_api_limit=2500,
        )
        db_session.add(workspace)
        await db_session.flush()
        db_session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=sub, role=WorkspaceRole.OWNER)
        )
        context = Context(id=uuid4(), workspace_id=workspace.id, name="again", created_by=sub)
        db_session.add(context)
        await db_session.flush()
        db_session.add(
            Memory(
                id=uuid4(),
                user_id=sub,
                workspace_id=workspace.id,
                context_id=context.id,
                summary="the new account's memory",
                content="body",
                type="note",
                client="test",
                version=1,
                embedding_status="success",
            )
        )
        await db_session.commit()
        workspace_id = workspace.id
        try:
            await _age(db_session, request_id)
            before = await _everything_but(db_session, request_id)
            members = await _table_snapshot(db_session, "workspace_members")
            workspaces = await _table_snapshot(db_session, "workspaces")
            delete_points = AsyncMock(return_value={"kagura_memories": 0})
            clear_redis = AsyncMock(return_value={})

            for _ in range(2):  # a second sweep changes nothing either
                with (
                    patch(_QDRANT_PATCH, AsyncMock()),
                    patch("services.account_erasure_service.delete_user_points", delete_points),
                    patch.object(AccountErasureService, "_clear_redis", clear_redis),
                ):
                    assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

                closed = await _request_snapshot(db_session, request_id)
                assert (closed["status"], closed["failure_reason"]) == (
                    STATUS_FAILED,
                    INTERRUPTED_AFTER_POSTGRES_COMMIT,
                )
                assert closed["user_id"] == pseudonym
                assert "redis" not in closed["deleted_data_summary"]
                # the new account: user row, memory, workspace, membership
                assert await _everything_but(db_session, request_id) == before
                assert await _table_snapshot(db_session, "workspace_members") == members
                assert await _table_snapshot(db_session, "workspaces") == workspaces
                delete_points.assert_not_awaited()
                clear_redis.assert_not_awaited()

            assert pseudonym in await erased_pseudonyms(db_session)
            # the row no longer blocks the new account's own request (no 409)
            service = AccountErasureService(db_session)
            assert await service.get_active_request_for_user(sub) is None
        finally:
            await db_session.rollback()
            await db_session.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await db_session.execute(delete(User).where(User.user_id == sub))
            await db_session.commit()


class TestTheNeverRanCloseKeepsALatePostgresMarker:
    """Review of #2302: the sweep read a claimed row without ``postgres``; a
    run alive past the cutoff then committed its Postgres step (writing
    ``postgres`` and deleting the user). The never-ran close must not replace
    that summary with the note: it matches nothing (moved), and the next
    sweep closes the row as interrupted."""

    @pytest.mark.asyncio
    async def test_the_note_does_not_replace_a_postgres_summary(
        self, db_session: AsyncSession, patched_external_stores
    ):
        from datetime import timedelta

        from models.erasure import (
            INTERRUPTED_AFTER_POSTGRES_COMMIT,
            STATUS_FAILED,
            STATUS_IN_PROGRESS,
        )

        sub = f"late_{uuid4().hex[:8]}"  # no user row: the late commit deleted it
        request = ErasureRequest(
            user_id=sub,
            user_email_hash="3" * 64,
            initiated_by="admin-runner",
            is_self_service=False,
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            status=STATUS_IN_PROGRESS,
            started_at=utcnow() - timedelta(hours=2),
        )
        db_session.add(request)
        await db_session.commit()
        request_id = request.id
        try:
            # what the sweep read: no ``postgres`` yet
            await db_session.refresh(request)
            # the late commit lands on the row meanwhile (another session)
            postgres = {"users": 1, "memory_scrub_at": "2026-10-01T12:00:00Z"}
            await db_session.execute(
                text("UPDATE erasure_requests SET deleted_data_summary = :s WHERE id = :id"),
                {"s": json.dumps({"postgres": postgres}), "id": request_id},
            )
            await db_session.commit()
            # the stale read the sweep holds (detached: nothing flushes it)
            db_session.expunge(request)
            request.deleted_data_summary = None

            with patch("services.account_erasure_service.logger") as log:
                outcome = await AccountErasureService(db_session)._close_request_without_user(
                    request
                )
            assert outcome == "moved"
            assert "erasure_sweep_request_moved" in [c.args[0] for c in log.info.call_args_list]
            row = await _request_snapshot(db_session, request_id)
            assert row["status"] == STATUS_IN_PROGRESS
            assert row["deleted_data_summary"] == {"postgres": postgres}

            # the next sweep reads ``postgres`` and closes it as interrupted
            with patch(_QDRANT_PATCH, AsyncMock()):
                assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
            row = await _request_snapshot(db_session, request_id)
            assert (row["status"], row["failure_reason"]) == (
                STATUS_FAILED,
                INTERRUPTED_AFTER_POSTGRES_COMMIT,
            )
            assert row["deleted_data_summary"]["postgres"]["users"] == 1
        finally:
            await db_session.rollback()
            await db_session.execute(delete(ErasureRequest).where(ErasureRequest.id == request_id))
            await db_session.commit()


class TestARequestWhoseUserVanishedUnrun:
    """#2286: the user row disappears before the sweep runs the request
    (removed by hand): nothing was erased, so the request is ``complete``
    with the note — and its own ids are pseudonymized all the same."""

    @pytest.mark.asyncio
    async def test_it_is_marked_complete_and_pseudonymized(
        self, db_session: AsyncSession, bystanders, patched_external_stores
    ):
        from datetime import timedelta

        from models.erasure import STATUS_COOLING_OFF
        from services.erasure_details import erased_pseudonyms

        tag = uuid4().hex[:8]
        sub, email = f"vanished_{tag}", f"vanished-{tag}@example.com"
        pseudonym = sha256_hex(sub, salt=_audit_salt())
        db_session.add(
            User(
                email=email,
                user_id=sub,
                name="Vanished",
                role="user",
                is_initial_admin=False,
                auth_method="oauth",
                auth_provider="google",
            )
        )
        await db_session.flush()
        request = ErasureRequest(
            user_id=sub,
            user_email_hash=sha256_hex(email),
            initiated_by=sub,
            is_self_service=True,
            reason_code="self_service",
            status=STATUS_COOLING_OFF,
            scheduled_for=utcnow() - timedelta(minutes=1),
            ip_address="203.0.113.9",
            user_agent="agent",
        )
        db_session.add(request)
        await db_session.commit()
        request_id = request.id
        try:
            await db_session.execute(delete(User).where(User.user_id == sub))
            await db_session.commit()
            before = await _everything_but(db_session, request_id)

            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

            closed = await _request_snapshot(db_session, request_id)
            assert closed["status"] == STATUS_COMPLETE
            assert closed["deleted_data_summary"] == {
                "note": "user_row_already_absent",
                "pseudonymized": True,
            }
            assert closed["completed_at"] is not None
            assert closed["failure_reason"] is None
            assert closed["user_id"] == closed["initiated_by"] == pseudonym
            assert (closed["ip_address"], closed["user_agent"]) == (None, None)
            assert sub not in repr(closed)
            assert await _everything_but(db_session, request_id) == before
            # an erasure interrupted on v0.124.x (no summary yet) closes the
            # same way, and its rows carry this pseudonym: counted as erased
            assert pseudonym in await erased_pseudonyms(db_session)
            assert sha256_hex(pseudonym, salt=_audit_salt()) not in await erased_pseudonyms(
                db_session
            )

            # idempotent: the next sweep does not see it again
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
            assert await _request_snapshot(db_session, request_id) == closed
            assert await _everything_but(db_session, request_id) == before
        finally:
            await db_session.rollback()
            await db_session.execute(delete(ErasureRequest).where(ErasureRequest.id == request_id))
            await db_session.commit()


class TestALegacyNoteRowsMemberIsWalkedPast:
    """#2286: a request the sweep marked ``complete`` with the note before
    v0.125.0 keeps the raw sub, and its erasure may have run (interrupted
    after its Postgres commit, when the summary was written only after it).
    ``erased_pseudonyms`` derives its pseudonym, so a later erasure walks
    past that member's edit to the author's value."""

    @pytest.mark.asyncio
    async def test_the_derived_pseudonyms_edit_is_walked_past(
        self, db_session: AsyncSession, erasure_scenario, patched_external_stores
    ):
        from models.auth import Context
        from models.memory import Memory, MemoryRevision

        other = erasure_scenario["other_user_id"]
        target = erasure_scenario["target_user_id"]
        absent = f"google-oauth2|absent-{uuid4().hex[:8]}"  # the raw sub on the row
        absent_p = sha256_hex(absent, salt=_audit_salt())  # what its rows carry
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
        legacy = ErasureRequest(
            user_id=absent,
            user_email_hash="0" * 64,
            initiated_by="admin-x",
            is_self_service=False,
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            status=STATUS_COMPLETE,
            completed_at=utcnow(),
            deleted_data_summary={"note": "user_row_already_absent"},
        )
        db_session.add_all(
            [
                MemoryRevision(
                    memory_id=memory.id,
                    context_id=context.id,
                    version=2,
                    kind="updated",
                    writer="api",
                    actor_user_id=absent_p,
                    prior={"details": {"by": "author"}},
                ),
                MemoryRevision(
                    memory_id=memory.id,
                    context_id=context.id,
                    version=3,
                    kind="updated",
                    writer="api",
                    actor_user_id=target,
                    prior={"details": {"by": "absent"}},
                ),
                legacy,
            ]
        )
        await db_session.commit()
        try:
            with patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()):
                request = await AccountErasureService(db_session).admin_force_erase(
                    target_user_id=target,
                    initiator_user_id="admin-runner",
                    reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                    reason_detail="integration test (#2286)",
                )

            assert request.status == STATUS_COMPLETE
            postgres = request.deleted_data_summary["postgres"]
            assert (postgres["details_restored"], postgres["details_restored_unavailable"]) == (
                1,
                0,
            )
            live = (
                await db_session.execute(
                    select(Memory.details)
                    .where(Memory.id == memory.id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            # not the legacy note row's member's value: the author's, below it
            assert live == {"by": "author"}
        finally:
            await db_session.execute(delete(ErasureRequest).where(ErasureRequest.id == legacy.id))
            await db_session.commit()


class TestAFinalizeAfterTheSweepsClose:
    """#2286 review: a run still alive past the stale cutoff commits its
    Postgres step; the sweep closes the row as ``interrupted``; then the run
    reaches ``_finalize``. The row ends ``complete`` with no marker."""

    @pytest.mark.asyncio
    async def test_the_row_ends_complete_without_the_marker(
        self, db_session: AsyncSession, patched_external_stores
    ):
        from datetime import timedelta
        from types import SimpleNamespace

        from models.erasure import (
            INTERRUPTED_AFTER_POSTGRES_COMMIT,
            STATUS_FAILED,
            STATUS_IN_PROGRESS,
        )

        sub = f"slow_{uuid4().hex[:8]}"  # its user row is gone: Postgres committed
        pseudonym = sha256_hex(sub, salt=_audit_salt())
        request = ErasureRequest(
            user_id=sub,
            user_email_hash="2" * 64,
            initiated_by="admin-runner",
            is_self_service=False,
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            status=STATUS_IN_PROGRESS,
            started_at=utcnow() - timedelta(hours=2),
            deleted_data_summary={"postgres": {"users": 1, "memory_scrub_at": "x"}},
        )
        db_session.add(request)
        await db_session.commit()
        request_id = request.id
        try:
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
            closed = await _request_snapshot(db_session, request_id)
            assert (closed["status"], closed["failure_reason"]) == (
                STATUS_FAILED,
                INTERRUPTED_AFTER_POSTGRES_COMMIT,
            )

            # the live run, which read the request before the sweep, finishes
            live = SimpleNamespace(id=request_id, user_id=sub, initiated_by="admin-runner")
            summary = {"postgres": {"users": 1, "memory_scrub_at": "x"}, "redis": {}}
            await AccountErasureService(db_session)._finalize(live, summary)

            done = await _request_snapshot(db_session, request_id)
            assert done["status"] == STATUS_COMPLETE
            assert done["failure_reason"] is None
            assert done["completed_at"] is not None
            assert done["user_id"] == pseudonym
            assert done["deleted_data_summary"] == summary
        finally:
            await db_session.rollback()
            await db_session.execute(delete(ErasureRequest).where(ErasureRequest.id == request_id))
            await db_session.commit()


class TestThePostgresStepMergesTheSummary:
    """#2286 review: the in-transaction summary write merges into what the
    row holds, so a key written before the Postgres step (#2282's
    ``retry``) survives a crash after it."""

    @pytest.mark.asyncio
    async def test_an_earlier_key_survives(
        self, db_session: AsyncSession, erasure_scenario, patched_external_stores
    ):
        from models.erasure import STATUS_IN_PROGRESS

        target_id = erasure_scenario["target_user_id"]
        request = ErasureRequest(
            user_id=target_id,
            user_email_hash=sha256_hex(erasure_scenario["target_email"]),
            initiated_by="admin-runner",
            is_self_service=False,
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            status=STATUS_IN_PROGRESS,
            started_at=utcnow(),
            deleted_data_summary={"retry": {"attempts": 1}},
        )
        db_session.add(request)
        await db_session.commit()
        request_id = request.id
        target = (
            await db_session.execute(select(User).where(User.user_id == target_id))
        ).scalar_one()

        with patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()):
            erased = await AccountErasureService(db_session)._delete_postgres(
                target, request_id=request_id
            )

        summary = (await _request_snapshot(db_session, request_id))["deleted_data_summary"]
        assert summary["retry"] == {"attempts": 1}
        assert summary["postgres"]["users"] == 1
        assert summary["postgres"]["memory_scrub_at"]
        assert {k: v for k, v in summary["postgres"].items() if k != "memory_scrub_at"} == (
            erased.counts
        )


# ---------------------------------------------------------------------------
# #2282: a vector-store outage at the first step reschedules the request
# ---------------------------------------------------------------------------


def _vector_store_down():
    from utils.exceptions import QdrantError

    return patch(
        "services.account_erasure_service.delete_user_points",
        new=AsyncMock(side_effect=QdrantError("connection refused")),
    )


async def _due_self_service_request(
    db_session: AsyncSession, scenario: dict, *, summary: dict | None = None
):
    """A confirmed self-service request of the scenario's target, due now."""
    from datetime import timedelta

    from models.erasure import STATUS_COOLING_OFF

    target = scenario["target_user_id"]
    request = ErasureRequest(
        user_id=target,
        user_email_hash=sha256_hex(scenario["target_email"]),
        initiated_by=target,
        is_self_service=True,
        reason_code="self_service",
        status=STATUS_COOLING_OFF,
        requested_at=utcnow() - timedelta(days=8),
        confirmed_at=utcnow() - timedelta(days=7, minutes=1),
        scheduled_for=utcnow() - timedelta(minutes=1),
        ip_address="203.0.113.9",
        user_agent="agent",
        deleted_data_summary=summary,
    )
    db_session.add(request)
    await db_session.commit()
    return request.id


async def _make_due(db_session: AsyncSession, request_id) -> None:
    from datetime import timedelta

    from sqlalchemy import update

    await db_session.execute(
        update(ErasureRequest)
        .where(ErasureRequest.id == request_id)
        .values(scheduled_for=utcnow() - timedelta(minutes=1))
    )
    await db_session.commit()


async def _user_exists(db_session: AsyncSession, user_id: str) -> bool:
    row = (await db_session.execute(select(User.id).where(User.user_id == user_id))).first()
    return row is not None


class TestAVectorStoreOutageAtStepOne:
    """#2282: ``delete_user_points`` raising ``QdrantError`` leaves nothing
    erased: the request goes back to ``cooling_off`` with a later
    ``scheduled_for`` and ``summary.retry``; the sweep runs it again when
    due, and the fourth failed attempt fails it."""

    @pytest.mark.asyncio
    async def test_it_reschedules_and_the_next_sweep_erases(
        self, db_session: AsyncSession, erasure_scenario, bystanders, patched_external_stores
    ):
        from datetime import timedelta

        from models.erasure import STATUS_COOLING_OFF

        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(db_session, erasure_scenario)
        before_row = await _request_snapshot(db_session, request_id)
        before = await _everything_but(db_session, request_id)

        with _vector_store_down():
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_COOLING_OFF
        scheduled_for = datetime.fromisoformat(row["scheduled_for"])
        assert scheduled_for > datetime.fromisoformat(before_row["scheduled_for"]) + timedelta(
            minutes=14
        )
        assert row["started_at"] is not None  # the claim's; set again on the next one
        retry = row["deleted_data_summary"]["retry"]
        assert row["deleted_data_summary"] == {"retry": retry}
        assert (retry["attempts"], retry["step"], retry["error_class"]) == (
            1,
            "vector_store",
            "QdrantError",
        )
        assert row["failure_reason"] is None
        # nothing was erased or pseudonymized: the row keeps what it held
        # before the first step, and every other row of every table is as it was
        unchanged = {
            k: v
            for k, v in row.items()
            if k not in ("status", "scheduled_for", "started_at", "deleted_data_summary")
        }
        assert unchanged == {
            k: v
            for k, v in before_row.items()
            if k not in ("status", "scheduled_for", "started_at", "deleted_data_summary")
        }
        assert row["user_id"] == target  # the raw sub stays, as on any live request
        assert await _everything_but(db_session, request_id) == before
        assert await _user_exists(db_session, target)

        # not due yet: a sweep right after leaves it alone
        assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        assert await _request_snapshot(db_session, request_id) == row

        # due, the vector store is back: the erasure runs and keeps the history
        await _make_due(db_session, request_id)
        assert await AccountErasureService(db_session).sweep_pending_erasures() == 1
        done = await _request_snapshot(db_session, request_id)
        assert done["status"] == STATUS_COMPLETE
        assert done["deleted_data_summary"]["retry"] == retry
        assert done["deleted_data_summary"]["qdrant"] == {"kagura_memories": 0}
        assert done["user_id"] == sha256_hex(target, salt=_audit_salt())
        assert not await _user_exists(db_session, target)
        # the bystanders' requests and user row are untouched throughout
        after = await _table_snapshot(db_session, "erasure_requests")
        for bystander_id in bystanders["request_ids"]:
            assert after[bystander_id] == before["erasure_requests"][bystander_id]
        assert await _user_exists(db_session, bystanders["user_id"])

    @pytest.mark.asyncio
    async def test_the_fourth_attempt_fails_it(
        self, db_session: AsyncSession, erasure_scenario, bystanders, patched_external_stores
    ):
        from models.erasure import FAILURE_AFTER_POSTGRES_COMMIT, STATUS_FAILED

        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(
            db_session,
            erasure_scenario,
            summary={"retry": {"attempts": 3, "step": "vector_store"}},
        )
        before = await _everything_but(db_session, request_id)

        with _vector_store_down():
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_FAILED
        assert row["failure_reason"].startswith(
            "vector_store_unavailable after 4 attempts: Qdrant service error"
        )
        assert not row["failure_reason"].startswith(FAILURE_AFTER_POSTGRES_COMMIT)
        assert row["deleted_data_summary"]["retry"]["attempts"] == 4
        # nothing erased: raw ids kept (a new request is the retry)
        assert (row["user_id"], row["initiated_by"], row["ip_address"]) == (
            target,
            target,
            "203.0.113.9",
        )
        assert await _everything_but(db_session, request_id) == before
        assert await _user_exists(db_session, target)
        # terminal: no later sweep runs it
        await _make_due(db_session, request_id)
        assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        assert (await _request_snapshot(db_session, request_id))["status"] == STATUS_FAILED

    @pytest.mark.asyncio
    async def test_the_user_can_cancel_it_while_it_waits(
        self, db_session: AsyncSession, erasure_scenario, patched_external_stores
    ):
        from models.erasure import STATUS_CANCELLED

        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(db_session, erasure_scenario)
        with _vector_store_down():
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        cancelled = await AccountErasureService(db_session).cancel_self_service(user_id=target)
        assert cancelled.id == request_id
        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_CANCELLED
        assert row["deleted_data_summary"]["retry"]["attempts"] == 1

        # never run again, even once its time has passed
        await _make_due(db_session, request_id)
        assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        assert (await _request_snapshot(db_session, request_id))["status"] == STATUS_CANCELLED
        assert await _user_exists(db_session, target)

        # a new request starts its own count
        new_id = await _due_self_service_request(db_session, erasure_scenario)
        with _vector_store_down():
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        assert (await _request_snapshot(db_session, new_id))["deleted_data_summary"]["retry"][
            "attempts"
        ] == 1

    @pytest.mark.asyncio
    async def test_a_rescheduled_request_whose_user_vanished_is_closed_unrun(
        self, db_session: AsyncSession, erasure_scenario, patched_external_stores
    ):
        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(db_session, erasure_scenario)
        with _vector_store_down():
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        await db_session.execute(delete(User).where(User.user_id == target))
        await db_session.commit()
        await _make_due(db_session, request_id)
        assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_COMPLETE
        assert row["deleted_data_summary"] == {
            "note": "user_row_already_absent",
            "pseudonymized": True,
        }
        assert row["user_id"] == sha256_hex(target, salt=_audit_salt())
        assert target not in repr(row)

    @pytest.mark.asyncio
    async def test_the_retry_history_survives_a_run_interrupted_after_the_commit(
        self, db_session: AsyncSession, erasure_scenario, patched_external_stores
    ):
        """The Postgres step merges its summary into the row's, so the
        ``retry`` block written on the reschedule is still there when the
        sweep closes an interrupted run (#2286)."""
        import asyncio

        from models.erasure import INTERRUPTED_AFTER_POSTGRES_COMMIT, STATUS_FAILED

        request_id = await _due_self_service_request(db_session, erasure_scenario)
        with _vector_store_down():
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        retry = (await _request_snapshot(db_session, request_id))["deleted_data_summary"]["retry"]

        await _make_due(db_session, request_id)
        with (
            patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()),
            patch.object(
                AccountErasureService,
                "_clear_redis",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await AccountErasureService(db_session).sweep_pending_erasures()
        await db_session.rollback()
        db_session.expire_all()

        interrupted = (await _request_snapshot(db_session, request_id))["deleted_data_summary"]
        assert interrupted["retry"] == retry
        assert interrupted["postgres"]["users"] == 1

        await _age(db_session, request_id)
        assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        closed = await _request_snapshot(db_session, request_id)
        assert closed["status"] == STATUS_FAILED
        assert closed["failure_reason"] == INTERRUPTED_AFTER_POSTGRES_COMMIT
        assert closed["deleted_data_summary"]["retry"] == retry
        assert "memory_scrub_at" in closed["deleted_data_summary"]["postgres"]

    @pytest.mark.asyncio
    async def test_admin_force_erase_answers_503_and_keeps_the_request(
        self, db_session: AsyncSession, erasure_scenario, bystanders, patched_external_stores
    ):
        from models.erasure import STATUS_COOLING_OFF
        from utils.exceptions import ErasureRetryScheduledError

        target = erasure_scenario["target_user_id"]
        before_users = await _table_snapshot(db_session, "users")
        with _vector_store_down():
            with pytest.raises(ErasureRetryScheduledError) as caught:
                await AccountErasureService(db_session).admin_force_erase(
                    target_user_id=target,
                    initiator_user_id="admin-runner",
                    reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                    ip_address="198.51.100.1",
                )

        assert caught.value.status_code == 503
        request_id = UUID(caught.value.details["request_id"])
        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_COOLING_OFF
        assert row["is_self_service"] is False
        # the time the 503 names is the one stored (asyncpg reads a naive
        # value as local time, as on every write of this column: compare the same way)
        assert caught.value.scheduled_for.astimezone(UTC) == datetime.fromisoformat(
            row["scheduled_for"]
        )
        assert row["deleted_data_summary"]["retry"]["attempts"] == 1
        assert (row["user_id"], row["initiated_by"], row["ip_address"]) == (
            target,
            "admin-runner",
            "198.51.100.1",
        )
        assert await _table_snapshot(db_session, "users") == before_users

        # the subject cannot call an admin erasure off while it waits
        from utils.exceptions import ErasureRequestNotFoundError

        with pytest.raises(ErasureRequestNotFoundError):
            await AccountErasureService(db_session).cancel_self_service(user_id=target)
        assert (await _request_snapshot(db_session, request_id))["status"] == STATUS_COOLING_OFF

        # one active request per user: a second force-erase meanwhile is refused
        with pytest.raises(ErasureAlreadyInProgressError):
            await AccountErasureService(db_session).admin_force_erase(
                target_user_id=target,
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            )

        # the sweep runs it when due
        await _make_due(db_session, request_id)
        assert await AccountErasureService(db_session).sweep_pending_erasures() == 1
        done = await _request_snapshot(db_session, request_id)
        assert done["status"] == STATUS_COMPLETE
        assert done["initiated_by"] == "admin-runner"
        assert not await _user_exists(db_session, target)

    @pytest.mark.asyncio
    async def test_a_run_claimed_again_meanwhile_is_not_rescheduled(
        self, db_session: AsyncSession, async_engine, erasure_scenario, patched_external_stores
    ):
        """The run outlived the stale cutoff and a later sweep claimed the row
        again (a new ``started_at``) before its vector-store call failed: the
        reschedule is guarded by the run's own claim, so the newer run's row
        is left alone and the sweep logs a plain failure."""
        from datetime import timedelta

        from sqlalchemy import update
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from models.erasure import STATUS_IN_PROGRESS
        from utils.exceptions import QdrantError

        request_id = await _due_self_service_request(db_session, erasure_scenario)
        reclaimed_at = utcnow() + timedelta(hours=1)
        maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

        async def reclaimed_then_down(user_id: str) -> dict[str, int]:
            async with maker() as other:
                await other.execute(
                    update(ErasureRequest)
                    .where(ErasureRequest.id == request_id)
                    .values(started_at=reclaimed_at)
                )
                await other.commit()
            raise QdrantError("timeout")

        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(side_effect=reclaimed_then_down),
        ):
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_IN_PROGRESS
        assert row["deleted_data_summary"] is None
        assert datetime.fromisoformat(row["started_at"]) == reclaimed_at.astimezone(UTC)
        assert await _user_exists(db_session, erasure_scenario["target_user_id"])

    @pytest.mark.parametrize("error", ["generic", "transfer"])
    @pytest.mark.asyncio
    async def test_a_stale_runs_failure_does_not_mark_the_newer_claim(
        self,
        error,
        db_session: AsyncSession,
        async_engine,
        erasure_scenario,
        bystanders,
        patched_external_stores,
    ):
        """#2306: the run outlived the stale cutoff and a later sweep claimed
        the row again before the run failed (not a vector-store error, so the
        generic handler, or the workspace-transfer one): its ``failed`` write
        is guarded by the run's own claim, so the newer run's row, and every
        other request, stay as they are."""
        from datetime import timedelta

        from sqlalchemy import update
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from models.erasure import STATUS_IN_PROGRESS
        from utils.exceptions import WorkspaceTransferRequiredError

        request_id = await _due_self_service_request(db_session, erasure_scenario)
        reclaimed_at = utcnow() + timedelta(hours=1)
        maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

        async def reclaimed_then_broken(user_id: str) -> dict[str, int]:
            async with maker() as other:
                await other.execute(
                    update(ErasureRequest)
                    .where(ErasureRequest.id == request_id)
                    .values(started_at=reclaimed_at)
                )
                await other.commit()
            if error == "transfer":
                raise WorkspaceTransferRequiredError("ws-1", 2)
            raise RuntimeError("unexpected")

        before = await _everything_but(db_session, request_id)
        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(side_effect=reclaimed_then_broken),
        ):
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_IN_PROGRESS
        assert row["failure_reason"] is None
        assert row["deleted_data_summary"] is None
        assert datetime.fromisoformat(row["started_at"]) == reclaimed_at.astimezone(UTC)
        assert await _user_exists(db_session, erasure_scenario["target_user_id"])
        assert await _everything_but(db_session, request_id) == before

    @pytest.mark.asyncio
    async def test_the_same_runs_failure_is_still_recorded(
        self, db_session: AsyncSession, erasure_scenario, patched_external_stores
    ):
        """#2306: the guard matches the run's own claim, so an unclaimed-again
        failure is still written as ``failed``."""
        request_id = await _due_self_service_request(db_session, erasure_scenario)

        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(side_effect=RuntimeError("unexpected")),
        ):
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_FAILED
        assert row["failure_reason"] == "unexpected"

    @pytest.mark.asyncio
    async def test_a_cancel_losing_to_a_concurrent_cancel_answers_the_cancelled_row(
        self, db_session: AsyncSession, async_engine, erasure_scenario, bystanders
    ):
        """#2306: a double-click — another cancel of the same request commits
        between this cancel's read and its compare-and-set. This one returns
        the cancelled row (the route's 200) instead of a 409."""
        from sqlalchemy import update
        from sqlalchemy.ext.asyncio import async_sessionmaker

        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(db_session, erasure_scenario)
        maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
        service = AccountErasureService(db_session)
        read = service._find_active_request
        cancelled_at = utcnow()

        async def read_then_cancelled(user_id: str):
            request = await read(user_id)
            async with maker() as other:
                await other.execute(
                    update(ErasureRequest)
                    .where(ErasureRequest.id == request_id)
                    .values(status=STATUS_CANCELLED, cancelled_at=cancelled_at)
                )
                await other.commit()
            return request

        before = await _everything_but(db_session, request_id)
        with patch.object(service, "_find_active_request", side_effect=read_then_cancelled):
            result = await service.cancel_self_service(user_id=target)

        assert result.id == request_id
        assert result.status == STATUS_CANCELLED
        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_CANCELLED
        assert datetime.fromisoformat(row["cancelled_at"]) == cancelled_at.astimezone(UTC)
        assert await _everything_but(db_session, request_id) == before

    @pytest.mark.asyncio
    async def test_a_cancel_racing_the_sweeps_claim_does_not_overwrite_it(
        self, db_session: AsyncSession, async_engine, erasure_scenario, bystanders
    ):
        """#2282 review: the cancel is a compare-and-set on the status. The
        sweep claims the row (``in_progress``) after the cancel read it: the
        cancel answers 409 and the claimed row, and every other request, stay
        as they are."""
        from sqlalchemy import update
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from models.erasure import STATUS_IN_PROGRESS

        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(db_session, erasure_scenario)
        maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
        service = AccountErasureService(db_session)
        read = service._find_active_request
        claimed_at = utcnow()

        async def read_then_claimed(user_id: str):
            request = await read(user_id)
            async with maker() as sweep:
                await sweep.execute(
                    update(ErasureRequest)
                    .where(ErasureRequest.id == request_id)
                    .values(status=STATUS_IN_PROGRESS, started_at=claimed_at)
                )
                await sweep.commit()
            return request

        before = await _everything_but(db_session, request_id)
        with patch.object(service, "_find_active_request", side_effect=read_then_claimed):
            with pytest.raises(ErasureAlreadyInProgressError) as caught:
                await service.cancel_self_service(user_id=target)

        assert caught.value.status_code == 409
        assert caught.value.details == {"existing_status": STATUS_IN_PROGRESS}
        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_IN_PROGRESS
        assert row["cancelled_at"] is None
        assert datetime.fromisoformat(row["started_at"]) == claimed_at.astimezone(UTC)
        assert await _everything_but(db_session, request_id) == before


# ---------------------------------------------------------------------------
# #2305: connector write keys survive their provisioner's erasure
# ---------------------------------------------------------------------------


class TestConnectorKeysSurviveErasure:
    """#2305: an erased admin's live connector keys move to the workspace's
    owner, unchanged otherwise, so the connector keeps writing."""

    @pytest.mark.asyncio
    async def test_an_admins_connector_key_moves_to_the_owner(
        self, db_session: AsyncSession, connector_world, patched_external_stores
    ):
        from auth.api_keys import APIKeyManager
        from services.connector_provisioning import ConnectorProvisioningService

        world = connector_world
        target = await world.user("target")
        owner = f"owner_{uuid4().hex[:8]}"
        ws = await world.workspace(owner, **{target: WorkspaceRole.ADMIN})

        # a connector the target provisioned and rotated once: the first key
        # is now revoked and still the target's
        provisioned = await world.connector(ws, by=target)
        connector_id = provisioned.connector.id
        revoked_key = await _live_connector_key(db_session, connector_id)
        revoked_key_id = revoked_key.id
        rotated = await ConnectorProvisioningService(db_session).rotate_kmc_key(
            workspace_id=ws.id, connector_id=connector_id, user_id=target
        )
        await db_session.flush()
        live = await _live_connector_key(db_session, connector_id)
        assert live.user_id == target
        live_id = live.id
        personal_id = await world.personal_key(target, ws)
        before = (await _key_rows(db_session, [live_id]))[live_id]
        ws_id = ws.id

        counts = await world.erase(target)

        after = (await _key_rows(db_session, [live_id]))[live_id]
        assert after["user_id"] == owner
        assert after["revoked_at"] is None
        assert _without_owner(after) == _without_owner(before)
        # the target's personal key and revoked connector key are gone
        assert await _key_rows(db_session, [personal_id, revoked_key_id]) == {}
        assert counts["connector_api_keys_reowned"] == 1
        assert counts["api_keys"] == 2

        # the worker's stored plaintext still verifies, now as the owner
        verified = await APIKeyManager(db_session).verify_key(rotated.plaintext_kmc_api_key)
        assert verified is not None
        assert verified.user_id == owner
        assert verified.is_connector_key is True

        # and the owner can rotate it; the replacement stays the owner's
        again = await ConnectorProvisioningService(db_session).rotate_kmc_key(
            workspace_id=ws_id, connector_id=connector_id, user_id=owner
        )
        await db_session.flush()
        assert again.key_owner_user_id == owner
        assert (await _live_connector_key(db_session, connector_id)).user_id == owner

    @pytest.mark.asyncio
    async def test_other_members_and_workspaces_keys_are_untouched(
        self, db_session: AsyncSession, connector_world, patched_external_stores
    ):
        world = connector_world
        target = await world.user("target")
        owner = f"owner_{uuid4().hex[:8]}"
        member = f"member_{uuid4().hex[:8]}"
        ws = await world.workspace(
            owner, **{target: WorkspaceRole.ADMIN, member: WorkspaceRole.ADMIN}
        )
        mine = await world.connector(ws, by=target)
        theirs = await world.connector(ws, by=member)
        owners_key = await world.personal_key(owner, ws)
        elsewhere_owner = f"else_{uuid4().hex[:8]}"
        elsewhere = await world.workspace(elsewhere_owner)
        unrelated = await world.connector(elsewhere, by=elsewhere_owner)
        bystanders = [
            (await _live_connector_key(db_session, theirs.connector.id)).id,
            (await _live_connector_key(db_session, unrelated.connector.id)).id,
            owners_key,
        ]
        mine_id = (await _live_connector_key(db_session, mine.connector.id)).id
        before = await _key_rows(db_session, bystanders)
        assert len(before) == 3

        counts = await world.erase(target)

        assert await _key_rows(db_session, bystanders) == before
        assert (await _key_rows(db_session, [mine_id]))[mine_id]["user_id"] == owner
        assert counts["connector_api_keys_reowned"] == 1

    @pytest.mark.asyncio
    async def test_an_owners_key_follows_the_workspace_to_its_new_owner(
        self, db_session: AsyncSession, connector_world, patched_external_stores
    ):
        world = connector_world
        target = await world.user("target")
        # step 2 hands the workspace to the lowest-user_id admin
        heir = f"a_heir_{uuid4().hex[:8]}"
        later = f"z_later_{uuid4().hex[:8]}"
        ws = await world.workspace(
            target, **{heir: WorkspaceRole.ADMIN, later: WorkspaceRole.ADMIN}
        )
        provisioned = await world.connector(ws, by=target)
        key_id = (await _live_connector_key(db_session, provisioned.connector.id)).id
        ws_id = ws.id

        counts = await world.erase(target)

        workspace = (
            await db_session.execute(select(Workspace).where(Workspace.id == ws_id))
        ).scalar_one()
        assert workspace.owner_user_id == heir
        assert (await _key_rows(db_session, [key_id]))[key_id]["user_id"] == heir
        assert counts["connector_api_keys_reowned"] == 1

    @pytest.mark.asyncio
    async def test_a_sole_owned_workspace_takes_its_connector_key_with_it(
        self, db_session: AsyncSession, connector_world, patched_external_stores
    ):
        world = connector_world
        target = await world.user("target")
        ws = await world.workspace(target)
        provisioned = await world.connector(ws, by=target)
        key_id = (await _live_connector_key(db_session, provisioned.connector.id)).id
        ws_id = ws.id

        counts = await world.erase(target)

        assert (
            await db_session.execute(select(Workspace).where(Workspace.id == ws_id))
        ).scalar_one_or_none() is None
        assert await _key_rows(db_session, [key_id]) == {}
        assert counts["connector_api_keys_reowned"] == 0

    @pytest.mark.asyncio
    async def test_keys_in_two_workspaces_go_to_each_workspaces_owner(
        self, db_session: AsyncSession, connector_world, patched_external_stores
    ):
        world = connector_world
        target = await world.user("target")
        owner_1 = f"owner1_{uuid4().hex[:8]}"
        owner_2 = f"owner2_{uuid4().hex[:8]}"
        ws_1 = await world.workspace(owner_1, **{target: WorkspaceRole.ADMIN})
        ws_2 = await world.workspace(owner_2, **{target: WorkspaceRole.ADMIN})
        key_1 = (
            await _live_connector_key(
                db_session, (await world.connector(ws_1, by=target)).connector.id
            )
        ).id
        key_2 = (
            await _live_connector_key(
                db_session, (await world.connector(ws_2, by=target)).connector.id
            )
        ).id

        counts = await world.erase(target)

        rows = await _key_rows(db_session, [key_1, key_2])
        assert rows[key_1]["user_id"] == owner_1
        assert rows[key_2]["user_id"] == owner_2
        assert counts["connector_api_keys_reowned"] == 2

    @pytest.mark.asyncio
    async def test_an_expired_unrevoked_connector_key_moves_too(
        self, db_session: AsyncSession, connector_world, patched_external_stores
    ):
        from sqlalchemy import update

        world = connector_world
        target = await world.user("target")
        owner = f"owner_{uuid4().hex[:8]}"
        ws = await world.workspace(owner, **{target: WorkspaceRole.ADMIN})
        provisioned = await world.connector(ws, by=target)
        key_id = (await _live_connector_key(db_session, provisioned.connector.id)).id
        await db_session.execute(
            update(APIKey)
            .where(APIKey.id == key_id)
            .values(expires_at=to_naive_utc(datetime(2020, 1, 1, tzinfo=UTC)))
            .execution_options(synchronize_session=False)
        )

        counts = await world.erase(target)

        row = (await _key_rows(db_session, [key_id]))[key_id]
        assert row["user_id"] == owner
        assert row["revoked_at"] is None
        assert counts["connector_api_keys_reowned"] == 1


class TestContextAuthorshipSweepLocksInIdOrder:
    """#2278: the ``contexts.created_by`` sweep reaches its rows through an
    id-ordered ``FOR UPDATE`` subquery and rewrites only the subject's."""

    @pytest.mark.asyncio
    async def test_only_the_subjects_contexts_change_and_rows_lock_in_id_order(
        self, async_engine, db_session: AsyncSession, erasure_scenario
    ):
        from sqlalchemy import event

        from models.auth import Context

        target = erasure_scenario["target_user_id"]
        other = erasure_scenario["other_user_id"]
        ws_id = erasure_scenario["workspace_id"]

        def context(created_by: str, *, deleted: bool = False) -> Context:
            return Context(
                id=uuid4(),
                workspace_id=ws_id,
                name=f"ctx-{uuid4().hex[:8]}",
                created_by=created_by,
                deleted_at=utcnow() if deleted else None,
            )

        mine = [context(target), context(target, deleted=True)]
        theirs = context(other)
        db_session.add_all([*mine, theirs])
        await db_session.flush()
        before = (
            await db_session.execute(
                select(func.count(Context.id)).where(Context.created_by == target)
            )
        ).scalar_one()

        statements: list[str] = []

        def record(_conn, _cursor, statement, *_rest) -> None:
            statements.append(" ".join(statement.split()))

        service = AccountErasureService(db_session)
        event.listen(async_engine.sync_engine, "before_cursor_execute", record)
        try:
            swept = await service._pseudonymize_field(Context, Context.created_by, target)
        finally:
            event.remove(async_engine.sync_engine, "before_cursor_execute", record)

        # The fixture's own context counts too: every row the subject created.
        assert swept == before >= 2
        rows = dict(
            (
                await db_session.execute(
                    select(Context.id, Context.created_by).where(
                        Context.id.in_([c.id for c in (*mine, theirs)])
                    )
                )
            ).all()
        )
        pseudonym = service._pseudonym(target)
        assert {rows[c.id] for c in mine} == {pseudonym}
        assert rows[theirs.id] == other
        # Loaded objects follow the UPDATE: the session keeps no raw sub.
        assert {c.created_by for c in mine} == {pseudonym}
        left = await db_session.execute(select(Context.id).where(Context.created_by == target))
        assert left.first() is None

        (update_sql,) = [s for s in statements if s.startswith("UPDATE contexts")]
        assert "ORDER BY contexts_1.id FOR UPDATE" in update_sql


# ---------------------------------------------------------------------------
# #2304: re-queue embeddings after a cancelled or failed vector-store retry
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def subject_memories(db_session: AsyncSession, erasure_scenario):
    """The target's memories in every embedding state, and a member's
    ``success`` memory in the same context (a bystander)."""
    from models.auth import Context
    from models.memory import Memory
    from services.resource_indexer import owns_resource_point, resource_point_id

    target = erasure_scenario["target_user_id"]
    context = Context(
        id=uuid4(),
        workspace_id=erasure_scenario["workspace_id"],
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=target,
    )
    db_session.add(context)
    await db_session.flush()

    def _memory(user_id: str, status: str, *, retries: int = 1, **extra) -> Memory:
        return Memory(
            id=uuid4(),
            user_id=user_id,
            workspace_id=erasure_scenario["workspace_id"],
            context_id=context.id,
            summary=f"{status} memory",
            content="body",
            type="note",
            client="test",
            embedding_status=status,
            embedding_retry_count=retries,
            embedding_error="earlier" if status == "failed" else None,
            version=3,
            **extra,
        )

    live = [_memory(target, "success") for _ in range(3)]
    # an API row with an external id (its point is its own id)
    external = _memory(target, "success", details={"resource_id": "crm"})
    # a resource-indexer row: it owns the indexer's point, which the
    # pipeline rebuilds from the row (#2065)
    indexer = _memory(
        target,
        "success",
        details={"resource_id": "crm", "doc_id": "doc-1", "version": 2},
        summary_embedding_id=resource_point_id("crm", "doc-1", 2),
    )
    # a run in flight: fenced off and embedded again
    in_flight = _memory(target, "processing", embedding_attempted_at=utcnow())
    untouched = [
        _memory(target, "pending"),
        # stale with no retry budget left: the sweep's to fail
        _memory(target, "processing", retries=3),
        _memory(target, "failed"),
        _memory(target, "success", deleted_at=utcnow()),  # a tombstone
        _memory(erasure_scenario["other_user_id"], "success"),  # the bystander
    ]
    db_session.add_all([*live, external, indexer, in_flight, *untouched])
    await db_session.commit()
    await db_session.refresh(indexer)  # the generated resource columns
    assert owns_resource_point(indexer)
    return {
        "requeued": {m.id for m in [*live, external, indexer, in_flight]},
        "live": live[0].id,
        "untouched": {m.id for m in untouched},
    }


def _without_status(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "embedding_status"}


async def _assert_requeued(db_session: AsyncSession, before: dict, memories: dict) -> None:
    """The subject's live ``success`` / ``processing`` rows are ``pending`` with nothing else
    changed; every other memory row is byte-identical."""
    after = await _table_snapshot(db_session, "memories")
    assert after.keys() == before.keys()
    for memory_id in memories["requeued"]:
        assert before[memory_id]["embedding_status"] in ("success", "processing")
        assert after[memory_id]["embedding_status"] == "pending"
        assert _without_status(after[memory_id]) == _without_status(before[memory_id])
    for memory_id in set(after) - memories["requeued"]:
        assert after[memory_id] == before[memory_id]


class TestEmbeddingsRequeuedAfterAVectorStoreRetry:
    """#2304: step 1 may delete the subject's points before it fails; a
    request that then ends without an erasure puts the subject's live
    embedded memories back to ``pending``."""

    @pytest.mark.asyncio
    async def test_a_cancel_after_a_failed_attempt_requeues(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        subject_memories,
        bystanders,
        patched_external_stores,
    ):
        from models.erasure import STATUS_CANCELLED
        from services.account_erasure_service import requeue_subject_embeddings

        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(db_session, erasure_scenario)
        with _vector_store_down():
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        retry = (await _request_snapshot(db_session, request_id))["deleted_data_summary"]["retry"]
        before = await _everything_but(db_session, request_id)

        cancelled = await AccountErasureService(db_session).cancel_self_service(user_id=target)

        assert cancelled.id == request_id
        assert cancelled.status == STATUS_CANCELLED
        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_CANCELLED
        got = row["deleted_data_summary"]["retry"]
        assert got["requeued_at"].endswith("Z")
        assert {k: v for k, v in got.items() if k != "requeued_at"} == {
            **retry,
            "embeddings_requeued": 6,
        }
        assert row["deleted_data_summary"] == {"retry": got}
        await _assert_requeued(db_session, before["memories"], subject_memories)
        after = await _everything_but(db_session, request_id)
        assert {k: v for k, v in after.items() if k != "memories"} == {
            k: v for k, v in before.items() if k != "memories"
        }
        assert await _user_exists(db_session, target)

        # idempotent: nothing is success any more
        assert await requeue_subject_embeddings(db_session, target) == 0

    @pytest.mark.asyncio
    async def test_the_fourth_attempt_requeues(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        subject_memories,
        bystanders,
        patched_external_stores,
    ):
        from models.erasure import STATUS_FAILED

        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(
            db_session,
            erasure_scenario,
            summary={"retry": {"attempts": 3, "step": "vector_store"}},
        )
        before = await _everything_but(db_session, request_id)

        with _vector_store_down():
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_FAILED
        assert row["failure_reason"].startswith("vector_store_unavailable after 4 attempts")
        retry = row["deleted_data_summary"]["retry"]
        assert retry["attempts"] == 4
        assert retry["embeddings_requeued"] == 6
        assert retry["requeued_at"].endswith("Z")
        # nothing erased: raw ids kept
        assert (row["user_id"], row["initiated_by"]) == (target, target)
        await _assert_requeued(db_session, before["memories"], subject_memories)
        after = await _everything_but(db_session, request_id)
        assert {k: v for k, v in after.items() if k != "memories"} == {
            k: v for k, v in before.items() if k != "memories"
        }
        assert await _user_exists(db_session, target)

    @pytest.mark.asyncio
    async def test_a_cancel_without_an_attempt_requeues_nothing(
        self, db_session: AsyncSession, erasure_scenario, subject_memories
    ):
        from datetime import timedelta

        from models.erasure import STATUS_CANCELLED, STATUS_COOLING_OFF

        target = erasure_scenario["target_user_id"]
        request = ErasureRequest(
            user_id=target,
            user_email_hash=sha256_hex(erasure_scenario["target_email"]),
            initiated_by=target,
            is_self_service=True,
            reason_code="self_service",
            status=STATUS_COOLING_OFF,
            scheduled_for=utcnow() + timedelta(days=7),
        )
        db_session.add(request)
        await db_session.commit()
        request_id = request.id
        before = await _table_snapshot(db_session, "memories")

        await AccountErasureService(db_session).cancel_self_service(user_id=target)

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_CANCELLED
        assert row["deleted_data_summary"] is None
        assert await _table_snapshot(db_session, "memories") == before

    @pytest.mark.asyncio
    async def test_a_held_row_ends_the_requeue_not_the_cancel(
        self,
        db_session: AsyncSession,
        async_engine,
        erasure_scenario,
        subject_memories,
        patched_external_stores,
    ):
        """A memory row another transaction holds: the batch gives up after
        its lock timeout, the cancel stands, and nothing is recorded."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from models.erasure import STATUS_CANCELLED
        from models.memory import Memory

        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(db_session, erasure_scenario)
        with _vector_store_down():
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0
        before = await _table_snapshot(db_session, "memories")

        maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as holder:
            await holder.execute(
                select(Memory.id).where(Memory.id == subject_memories["live"]).with_for_update()
            )
            with (
                patch("services.account_erasure_service.logger") as log,
                # the production wait is 5 s; the test needs only its error
                patch(
                    "services.account_erasure_service._REQUEUE_LOCK_TIMEOUT_SQL",
                    text("SET LOCAL lock_timeout = '200ms'"),
                ),
            ):
                cancelled = await AccountErasureService(db_session).cancel_self_service(
                    user_id=target
                )
            await holder.rollback()

        assert cancelled.status == STATUS_CANCELLED
        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_CANCELLED
        assert "embeddings_requeued" not in row["deleted_data_summary"]["retry"]
        assert await _table_snapshot(db_session, "memories") == before
        (failed,) = [
            c
            for c in log.error.call_args_list
            if c.args and c.args[0] == "erasure_embeddings_requeue_failed"
        ]
        assert failed.kwargs["error_class"] == "DBAPIError"  # lock_timeout

    @pytest.mark.asyncio
    async def test_a_run_rescheduling_between_the_read_and_the_cancel_requeues(
        self,
        db_session: AsyncSession,
        async_engine,
        erasure_scenario,
        subject_memories,
        patched_external_stores,
    ):
        """The cancel read the request before any attempt; the hourly sweep
        ran it with the store down before the cancel's compare-and-set. The
        row the cancel moved has spent an attempt, so it re-queues."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from models.erasure import STATUS_CANCELLED

        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(db_session, erasure_scenario)
        maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
        svc = AccountErasureService(db_session)
        original = svc._find_active_request

        async def read_then_sweep(user_id: str):
            row = await original(user_id)
            assert row is not None and row.deleted_data_summary is None
            async with maker() as other:
                with _vector_store_down():
                    assert await AccountErasureService(other).sweep_pending_erasures() == 0
            return row

        with patch.object(svc, "_find_active_request", new=read_then_sweep):
            cancelled = await svc.cancel_self_service(user_id=target)

        assert cancelled.status == STATUS_CANCELLED
        retry = (await _request_snapshot(db_session, request_id))["deleted_data_summary"]["retry"]
        assert retry["attempts"] == 1
        assert retry["embeddings_requeued"] == 6
        rows = await _table_snapshot(db_session, "memories")
        assert {rows[i]["embedding_status"] for i in subject_memories["requeued"]} == {"pending"}


# ---------------------------------------------------------------------------
# #2358: re-queue embeddings after a failure before the Postgres commit
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def other_workspace_memories(db_session: AsyncSession, bystanders):
    """Another account's workspace with its ``success`` and in-flight
    ``processing`` memories: the target's re-queue must leave them alone."""
    from models.auth import Context
    from models.memory import Memory

    owner = bystanders["user_id"]
    workspace = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )
    db_session.add(workspace)
    await db_session.flush()
    context = Context(
        id=uuid4(), workspace_id=workspace.id, name=f"ctx-{uuid4().hex[:8]}", created_by=owner
    )
    db_session.add(context)
    await db_session.flush()
    memories = [
        Memory(
            id=uuid4(),
            user_id=owner,
            workspace_id=workspace.id,
            context_id=context.id,
            summary=f"{status} memory of another workspace",
            content="body",
            type="note",
            client="test",
            embedding_status=status,
            embedding_retry_count=1,
            embedding_attempted_at=utcnow() if status == "processing" else None,
        )
        for status in ("success", "processing")
    ]
    db_session.add_all(memories)
    await db_session.commit()
    ids = [m.id for m in memories]
    workspace_id, context_id = workspace.id, context.id
    yield ids
    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.id.in_(ids)))
    await db_session.execute(delete(Context).where(Context.id == context_id))
    await db_session.execute(delete(Workspace).where(Workspace.id == workspace_id))
    await db_session.commit()


async def _demote_the_other_member(db_session: AsyncSession, scenario: dict) -> None:
    """No other admin in the target's workspace: step 2 raises
    ``WorkspaceTransferRequiredError``."""
    from sqlalchemy import update

    await db_session.execute(
        update(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == scenario["workspace_id"],
            WorkspaceMember.user_id == scenario["other_user_id"],
        )
        .values(role=WorkspaceRole.MEMBER)
    )
    await db_session.commit()


async def _workspace_tables(db_session: AsyncSession) -> dict:
    return {
        table: await _table_snapshot(db_session, table)
        for table in ("workspaces", "workspace_members", "contexts", "api_keys")
    }


def _assert_requeue_record(summary: dict, *, attempts: int = 0) -> None:
    """``summary.retry`` holds the re-queue of the six live rows (#2358)."""
    retry = summary["retry"]
    assert retry["requeued_at"].endswith("Z")
    assert {k: v for k, v in retry.items() if k != "requeued_at"} == {
        "attempts": attempts,
        "embeddings_requeued": 6,
    }


class TestEmbeddingsRequeuedAfterAFailureBeforeTheCommit:
    """#2358: step 1 deleted the subject's points, then the request failed
    before the Postgres commit; the account stays, so its live embedded
    memories go back to ``pending`` — nobody else's."""

    @pytest.mark.asyncio
    async def test_a_transfer_error_requeues(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        subject_memories,
        bystanders,
        other_workspace_memories,
        patched_external_stores,
    ):
        from services.account_erasure_service import requeue_subject_embeddings

        target = erasure_scenario["target_user_id"]
        await _demote_the_other_member(db_session, erasure_scenario)
        request_id = await _due_self_service_request(db_session, erasure_scenario)
        before = await _everything_but(db_session, request_id)
        workspaces_before = await _workspace_tables(db_session)

        assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_FAILED
        assert row["failure_reason"].startswith("workspace_transfer_required")
        summary = row["deleted_data_summary"]
        assert set(summary) == {"qdrant", "retry"}
        assert summary["qdrant"] == {"kagura_memories": 0}
        _assert_requeue_record(summary)
        # nothing erased: raw ids kept
        assert (row["user_id"], row["initiated_by"]) == (target, target)
        await _assert_requeued(db_session, before["memories"], subject_memories)
        after = await _everything_but(db_session, request_id)
        assert {k: v for k, v in after.items() if k != "memories"} == {
            k: v for k, v in before.items() if k != "memories"
        }
        # the other workspace's rows are among the byte-identical ones
        assert set(other_workspace_memories) <= set(after["memories"])
        for memory_id in other_workspace_memories:
            assert after["memories"][memory_id] == before["memories"][memory_id]
        # no ownership moved, no role changed, no key touched
        assert await _workspace_tables(db_session) == workspaces_before
        assert await _user_exists(db_session, target)

        # idempotent: nothing is success any more
        assert await requeue_subject_embeddings(db_session, target) == 0

    @pytest.mark.asyncio
    async def test_a_failure_inside_the_postgres_step_requeues(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        subject_memories,
        bystanders,
        other_workspace_memories,
        patched_external_stores,
    ):
        """The step's transaction is rolled back (user, keys, audit rows
        identical) and the subject's rows are re-queued."""
        target = erasure_scenario["target_user_id"]
        request_id = await _due_self_service_request(db_session, erasure_scenario)
        before = await _everything_but(db_session, request_id)
        keys_before = await _table_snapshot(db_session, "api_keys")

        with patch(
            "services.account_erasure_service.AccountErasureService._pseudonymize_audit_logs",
            AsyncMock(side_effect=RuntimeError("audit_logs unavailable")),
        ):
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_FAILED
        assert row["failure_reason"] == "audit_logs unavailable"
        summary = row["deleted_data_summary"]
        assert "postgres" not in summary
        _assert_requeue_record(summary)
        assert (row["user_id"], row["initiated_by"]) == (target, target)
        await _assert_requeued(db_session, before["memories"], subject_memories)
        after = await _everything_but(db_session, request_id)
        assert {k: v for k, v in after.items() if k != "memories"} == {
            k: v for k, v in before.items() if k != "memories"
        }
        assert await _table_snapshot(db_session, "api_keys") == keys_before
        assert await _user_exists(db_session, target)

    @pytest.mark.asyncio
    async def test_an_admin_force_erase_requeues_before_the_409(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        subject_memories,
        bystanders,
        other_workspace_memories,
        patched_external_stores,
    ):
        from utils.exceptions import WorkspaceTransferRequiredError

        target = erasure_scenario["target_user_id"]
        await _demote_the_other_member(db_session, erasure_scenario)
        before = await _everything_but(db_session, None)
        workspaces_before = await _workspace_tables(db_session)

        with pytest.raises(WorkspaceTransferRequiredError):
            await AccountErasureService(db_session).admin_force_erase(
                target_user_id=target,
                initiator_user_id="admin-runner",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="integration test (#2358)",
            )

        request_id = (
            await db_session.execute(
                select(ErasureRequest.id).where(ErasureRequest.user_id == target)
            )
        ).scalar_one()
        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_FAILED
        assert row["failure_reason"].startswith("workspace_transfer_required")
        _assert_requeue_record(row["deleted_data_summary"])
        assert (row["user_id"], row["initiated_by"]) == (target, "admin-runner")
        await _assert_requeued(db_session, before["memories"], subject_memories)
        after = await _everything_but(db_session, request_id)
        assert after["erasure_requests"] == before["erasure_requests"]
        assert {k: v for k, v in after.items() if k != "memories"} == {
            k: v for k, v in before.items() if k != "memories"
        }
        assert await _workspace_tables(db_session) == workspaces_before
        assert await _user_exists(db_session, target)

    @pytest.mark.asyncio
    async def test_another_error_on_a_retry_run_requeues(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        subject_memories,
        bystanders,
        other_workspace_memories,
        patched_external_stores,
    ):
        """A configuration error at step 1 of a retry run: this run deleted
        nothing, the earlier attempts may have; the request is ``failed``
        with its attempts, and the subject's rows are re-queued."""
        target = erasure_scenario["target_user_id"]
        previous = {"attempts": 2, "step": "vector_store", "error_class": "QdrantError"}
        request_id = await _due_self_service_request(
            db_session, erasure_scenario, summary={"retry": previous}
        )
        before = await _everything_but(db_session, request_id)
        workspaces_before = await _workspace_tables(db_session)

        with patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(side_effect=RuntimeError("bad config")),
        ):
            assert await AccountErasureService(db_session).sweep_pending_erasures() == 0

        row = await _request_snapshot(db_session, request_id)
        assert row["status"] == STATUS_FAILED
        assert row["failure_reason"] == "bad config"
        retry = row["deleted_data_summary"]["retry"]
        assert row["deleted_data_summary"] == {"retry": retry}
        assert retry["requeued_at"].endswith("Z")
        assert {k: v for k, v in retry.items() if k != "requeued_at"} == {
            **previous,
            "embeddings_requeued": 6,
        }
        assert (row["user_id"], row["initiated_by"]) == (target, target)
        await _assert_requeued(db_session, before["memories"], subject_memories)
        after = await _everything_but(db_session, request_id)
        assert {k: v for k, v in after.items() if k != "memories"} == {
            k: v for k, v in before.items() if k != "memories"
        }
        assert await _workspace_tables(db_session) == workspaces_before
        assert await _user_exists(db_session, target)


# ---------------------------------------------------------------------------
# #2363: an erased member's queued survivors are not re-embedded
# ---------------------------------------------------------------------------

#: the embedding columns the dequeue writes
_DEQUEUE_COLUMNS = (
    "embedding_status",
    "embedding_error",
    "embedding_retry_count",
    "embedding_attempted_at",
)


@pytest_asyncio.fixture
async def queued_survivors(db_session: AsyncSession, erasure_scenario, bystanders):
    """The target's memories in the auto-transferred workspace (they survive
    the erasure) in every embedding state, with bystanders around them:

    - ``queued``: the target's live rows that are not ``success`` — a pending
      one with coordinates, a run in flight, a failed one with budget left
      (the sweep would retry it) and an exhausted one;
    - ``embedded``: the target's ``success`` row;
    - ``tombstone``: the target's tombstoned pending row (hard-deleted);
    - ``bystander_pending`` / ``bystander_success``: the other member's rows
      in the same context;
    - ``elsewhere``: another account's ``pending`` and ``failed`` rows in its
      own workspace.
    """
    from datetime import timedelta

    from models.auth import Context
    from models.memory import Memory

    target = erasure_scenario["target_user_id"]
    other = erasure_scenario["other_user_id"]
    workspace_id = erasure_scenario["workspace_id"]
    context = Context(
        id=uuid4(), workspace_id=workspace_id, name=f"ctx-{uuid4().hex[:8]}", created_by=other
    )
    owner = bystanders["user_id"]
    elsewhere_ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )
    db_session.add_all([context, elsewhere_ws])
    await db_session.flush()
    elsewhere_ctx = Context(
        id=uuid4(), workspace_id=elsewhere_ws.id, name=f"ctx-{uuid4().hex[:8]}", created_by=owner
    )
    db_session.add(elsewhere_ctx)
    await db_session.flush()
    long_ago = utcnow() - timedelta(hours=1)

    def _memory(
        user_id: str, status: str, *, retries: int = 0, ctx=context, ws=None, **extra
    ) -> Memory:
        return Memory(
            id=uuid4(),
            user_id=user_id,
            workspace_id=ws or workspace_id,
            context_id=ctx.id,
            summary=f"{status} memory of {user_id}",
            content="body",
            type="note",
            client="test",
            embedding_status=status,
            embedding_retry_count=retries,
            embedding_error="earlier: provider timeout" if status == "failed" else None,
            created_at=long_ago,
            **{
                "embedding_attempted_at": long_ago if status in ("failed", "processing") else None,
                **extra,
            },
        )

    queued = [
        _memory(target, "pending", details={"location": {"lat": 35.0, "lon": 139.0}, "k": "v"}),
        _memory(target, "processing", retries=1, embedding_attempted_at=utcnow()),
        _memory(target, "failed", retries=1),
        _memory(target, "failed", retries=3),
    ]
    embedded = _memory(target, "success", details={"k": "v"})
    tombstone = _memory(target, "pending", deleted_at=utcnow())
    bystander_pending = _memory(other, "pending")
    bystander_success = _memory(other, "success")
    elsewhere = [
        _memory(owner, "pending", ctx=elsewhere_ctx, ws=elsewhere_ws.id),
        _memory(owner, "failed", retries=1, ctx=elsewhere_ctx, ws=elsewhere_ws.id),
    ]
    db_session.add_all(
        [*queued, embedded, tombstone, bystander_pending, bystander_success, *elsewhere]
    )
    await db_session.commit()
    ids = {
        "context_id": context.id,
        "queued": [m.id for m in queued],
        "embedded": embedded.id,
        "tombstone": tombstone.id,
        "bystander_pending": bystander_pending.id,
        "bystander_success": bystander_success.id,
        "elsewhere": [m.id for m in elsewhere],
    }
    elsewhere_ws_id, elsewhere_ctx_id = elsewhere_ws.id, elsewhere_ctx.id
    yield ids
    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.context_id == elsewhere_ctx_id))
    await db_session.execute(delete(Context).where(Context.id == elsewhere_ctx_id))
    await db_session.execute(delete(Workspace).where(Workspace.id == elsewhere_ws_id))
    await db_session.commit()


async def _erase(db_session: AsyncSession, scenario: dict):
    """An admin force-erase of the scenario's target, its payload patch stubbed."""
    with patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()):
        return await AccountErasureService(db_session).admin_force_erase(
            target_user_id=scenario["target_user_id"],
            initiator_user_id="admin-runner",
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            reason_detail="integration test (#2363)",
        )


def _test_engine_sessions(db_session: AsyncSession):
    """``db.base.get_db`` over the test engine: one new session per call, as
    the sweep's worker gets in production. Returns the patch and the list of
    sessions to close."""
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    sessions: list[AsyncSession] = []

    def _get_db() -> AsyncIterator[AsyncSession]:
        async def _gen() -> AsyncIterator[AsyncSession]:
            session = maker()
            sessions.append(session)
            yield session

        return _gen()

    return patch("db.base.get_db", side_effect=_get_db), sessions


async def _embed_once(
    db_session: AsyncSession,
    memory_id,
    *,
    now=None,
    embed=None,
    build_point: bool = False,
) -> dict:
    """Run the sweep's worker (``process_pending_embedding``) on ``memory_id``
    against the test database, the provider and the vector store stubbed.
    ``now`` moves the pipeline's clock (past every backoff); ``build_point``
    keeps the real payload builder. Returns the stubs."""
    from contextlib import ExitStack

    from services import memory_service

    svc = MagicMock(model="m")
    svc.embed = embed or AsyncMock(return_value=[0.1] * 8)
    stubs = {
        "embed": svc.embed,
        "add_memory_to_qdrant": AsyncMock(),
        "delete_points": AsyncMock(),
        "delete_chunks": AsyncMock(),
        "logger": MagicMock(),
    }
    get_db, sessions = _test_engine_sessions(db_session)
    try:
        with ExitStack() as stack:
            stack.enter_context(get_db)
            if now is not None:
                stack.enter_context(patch("utils.datetime.utcnow", return_value=now))
            stack.enter_context(patch("services.embedding_service.EmbeddingService", MagicMock()))
            stack.enter_context(
                patch(
                    "services.context_routing.resolve_context_routing",
                    AsyncMock(return_value=("kagura_memories", svc)),
                )
            )
            stack.enter_context(
                patch.object(memory_service, "add_memory_to_qdrant", stubs["add_memory_to_qdrant"])
            )
            if not build_point:
                stack.enter_context(
                    patch.object(memory_service, "build_memory_point", return_value=({}, [], []))
                )
            stack.enter_context(patch.object(memory_service, "write_content_chunks", AsyncMock()))
            stack.enter_context(patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()))
            stack.enter_context(
                patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock())
            )
            stack.enter_context(
                patch("db.qdrant.delete_points_from_qdrant", stubs["delete_points"])
            )
            stack.enter_context(patch("db.qdrant.delete_chunk_points", stubs["delete_chunks"]))
            stack.enter_context(patch.object(memory_service, "logger", stubs["logger"]))
            await memory_service.process_pending_embedding(memory_id)
    finally:
        for session in sessions:
            await session.close()
    return stubs


def _info_events(logger: MagicMock) -> list[str]:
    return [c.args[0] for c in logger.info.call_args_list if c.args]


class TestQueuedSurvivorsAreNotReEmbedded:
    """#2363: step 1 deletes the subject's points; a surviving memory still
    queued for an embedding (or retrying one) is taken out of the queue in
    the Postgres step, so the sweep does not write its point again under the
    pseudonym. The admin retry is the only way back."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("explicit_retry", [False, True])
    async def test_an_old_run_after_a_newer_success_and_erasure_cannot_revive_the_author(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        queued_survivors,
        patched_external_stores,
        explicit_retry: bool,
    ):
        """#2393: W1 loses its claim, W2 succeeds, erasure finishes, then W1 writes.

        All state transitions use Postgres; only provider/vector IO is stubbed.
        An explicit admin retry after erasure may legitimately rebuild a point:
        if W1 overwrites that new success, delete the stale point and requeue
        the current author rather than leaving its success without a point.
        """
        import asyncio

        from sqlalchemy import update

        from api.routes.admin import retry_failed_embeddings
        from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR
        from models.memory import Memory

        target = erasure_scenario["target_user_id"]
        memory_id = queued_survivors["queued"][0]
        embedding, release = asyncio.Event(), asyncio.Event()

        async def _blocked_embed(*_a, **_k) -> list[float]:
            embedding.set()
            await release.wait()
            return [0.1] * 8

        run = asyncio.create_task(
            _embed_once(db_session, memory_id, embed=AsyncMock(side_effect=_blocked_embed))
        )
        try:
            await asyncio.wait_for(embedding.wait(), 15)
            # A pre-commit erasure failure's compensation (#2358), or an edit,
            # can revoke W1's claim while its provider call is still running.
            await db_session.execute(
                update(Memory).where(Memory.id == memory_id).values(embedding_status="pending")
            )
            await db_session.commit()
            newer = await _embed_once(db_session, memory_id)
            newer["add_memory_to_qdrant"].assert_awaited_once()
            assert (await _table_snapshot(db_session, "memories"))[memory_id][
                "embedding_status"
            ] == "success"
            request = await _erase(db_session, erasure_scenario)
            assert request.status == STATUS_COMPLETE

            if explicit_retry:
                await retry_failed_embeddings(
                    user={"user_id": "admin-runner"},
                    db=db_session,
                    context_id=str(queued_survivors["context_id"]),
                    workspace_id=None,
                )
                row = (await _table_snapshot(db_session, "memories"))[memory_id]
                assert row["embedding_status"] == "pending"
                assert row["embedding_error"] is None
                await _embed_once(db_session, memory_id)
                assert (await _table_snapshot(db_session, "memories"))[memory_id][
                    "embedding_status"
                ] == "success"
            release.set()
            stale = await asyncio.wait_for(run, 15)
        finally:
            release.set()
            if not run.done():
                await asyncio.wait_for(run, 15)

        assert stale["add_memory_to_qdrant"].await_args.kwargs["user_id"] == target
        stale["delete_points"].assert_awaited_once_with([str(memory_id)], "kagura_memories")
        stale["delete_chunks"].assert_awaited_once_with([memory_id], "kagura_memories")
        row = (await _table_snapshot(db_session, "memories"))[memory_id]
        assert row["user_id"] == erasure_pseudonym(target)
        if explicit_retry:
            assert row["embedding_status"] == "pending"
            rebuilt = await _embed_once(db_session, memory_id, build_point=True)
            assert rebuilt["add_memory_to_qdrant"].await_args.kwargs["user_id"] == row["user_id"]
            assert (await _table_snapshot(db_session, "memories"))[memory_id][
                "embedding_status"
            ] == "success"
        else:
            assert row["embedding_status"] == "failed"
            assert row["embedding_error"] == ERASED_AUTHOR_EMBEDDING_ERROR
            sweep = await _embed_once(db_session, memory_id)
            sweep["embed"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_erasure_terminalizes_only_the_subjects_survivors(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        queued_survivors,
        edited_by_target,
        patched_external_stores,
    ):
        from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR, MAX_EMBEDDING_RETRIES

        target = erasure_scenario["target_user_id"]
        pseudonym = erasure_pseudonym(target)
        before = await _table_snapshot(db_session, "memories")
        started = utcnow()

        request = await _erase(db_session, erasure_scenario)

        assert request.status == STATUS_COMPLETE
        assert request.deleted_data_summary["postgres"]["memories_embedding_dequeued"] == 5
        after = await _table_snapshot(db_session, "memories")
        for memory_id in [*queued_survivors["queued"], queued_survivors["embedded"]]:
            row = after[memory_id]
            assert row["user_id"] == pseudonym
            assert row["details"] is None
            assert row["location_lat"] is None
            assert row["embedding_status"] == "failed"
            assert row["embedding_error"] == ERASED_AUTHOR_EMBEDDING_ERROR
            assert row["embedding_retry_count"] == MAX_EMBEDDING_RETRIES
            assert to_naive_utc(datetime.fromisoformat(row["embedding_attempted_at"])) >= started
        # A success must also stay out of lost-claim automatic recovery.
        embedded = queued_survivors["embedded"]
        assert after[embedded]["user_id"] == pseudonym
        assert queued_survivors["tombstone"] not in after
        # another author's row the target edited: its details are put back
        # (#2244), its embedding state is not the dequeue's
        assert {c: after[edited_by_target][c] for c in _DEQUEUE_COLUMNS} == {
            c: before[edited_by_target][c] for c in _DEQUEUE_COLUMNS
        }
        # every row of another account, in this workspace or another one,
        # is byte-identical
        others = {
            queued_survivors["bystander_pending"],
            queued_survivors["bystander_success"],
            *queued_survivors["elsewhere"],
        }
        for memory_id in others:
            assert after[memory_id] == before[memory_id]
        changed = {m for m in after if after[m] != before.get(m)}
        assert changed == {*queued_survivors["queued"], embedded, edited_by_target}

    @pytest.mark.asyncio
    async def test_the_sweep_writes_no_point_for_them(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        queued_survivors,
        patched_external_stores,
    ):
        """Neither the sweep's prefilter nor the claim takes a dequeued row,
        however long after the erasure; the bystander's pending row next to
        them is embedded (the control)."""
        from datetime import timedelta

        from sqlalchemy import or_

        from models.memory import Memory
        from services.embedding_claim import STALE_CLAIM_SECONDS
        from services.memory_service import (
            embedding_reclaim_eligible_clause,
            embedding_reclaim_exhausted_clause,
            embedding_retry_eligible_clause,
        )

        await _erase(db_session, erasure_scenario)
        queued = [*queued_survivors["queued"], queued_survivors["embedded"]]
        before = await _table_snapshot(db_session, "memories")
        later = utcnow() + timedelta(days=1)
        stale_cutoff = later - timedelta(seconds=STALE_CLAIM_SECONDS)
        candidates = (
            (
                await db_session.execute(
                    select(Memory.id).where(
                        Memory.id.in_([*queued, queued_survivors["bystander_pending"]]),
                        Memory.deleted_at.is_(None),
                        or_(
                            Memory.embedding_status == "pending",
                            embedding_reclaim_eligible_clause(stale_cutoff),
                            embedding_retry_eligible_clause(later),
                            # nor ``_fail_exhausted_reclaims``
                            embedding_reclaim_exhausted_clause(stale_cutoff),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert candidates == [queued_survivors["bystander_pending"]]

        for memory_id in queued:
            stubs = await _embed_once(db_session, memory_id, now=later)
            stubs["embed"].assert_not_awaited()
            stubs["add_memory_to_qdrant"].assert_not_awaited()
        after = await _table_snapshot(db_session, "memories")
        for memory_id in queued:
            assert after[memory_id] == before[memory_id]

        control = queued_survivors["bystander_pending"]
        stubs = await _embed_once(db_session, control, now=later)
        assert stubs["add_memory_to_qdrant"].await_args.kwargs["memory_id"] == control
        assert (await _table_snapshot(db_session, "memories"))[control][
            "embedding_status"
        ] == "success"

    @pytest.mark.asyncio
    async def test_the_admin_retry_brings_them_back_under_the_pseudonym(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        queued_survivors,
        patched_external_stores,
    ):
        from api.routes.admin import retry_failed_embeddings

        pseudonym = erasure_pseudonym(erasure_scenario["target_user_id"])
        await _erase(db_session, erasure_scenario)

        response = await retry_failed_embeddings(
            user={"user_id": "admin-runner"},
            db=db_session,
            context_id=str(queued_survivors["context_id"]),
            workspace_id=None,
        )

        assert response["reset_count"] == 5
        rows = await _table_snapshot(db_session, "memories")
        for memory_id in [*queued_survivors["queued"], queued_survivors["embedded"]]:
            assert rows[memory_id]["embedding_status"] == "pending"
            assert rows[memory_id]["embedding_error"] is None
            assert rows[memory_id]["embedding_retry_count"] == 0
        # the one that had coordinates: embedded under the pseudonym, no location
        with_location = queued_survivors["queued"][0]
        stubs = await _embed_once(db_session, with_location, build_point=True)
        written = stubs["add_memory_to_qdrant"].await_args.kwargs
        assert written["memory_id"] == with_location
        assert written["user_id"] == pseudonym
        assert written["payload"]["user_id"] == pseudonym
        assert "location" not in written["payload"]
        assert (await _table_snapshot(db_session, "memories"))[with_location][
            "embedding_status"
        ] == "success"

    @pytest.mark.asyncio
    async def test_a_run_in_flight_deletes_the_point_it_wrote_after_the_erasure(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        queued_survivors,
        patched_external_stores,
    ):
        """The run loaded the row (raw sub, coordinates) before the erasure
        and writes its point after step 1 deleted the subject's: its outcome
        write misses the fence and it deletes that point."""
        import asyncio

        from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR

        target = erasure_scenario["target_user_id"]
        memory_id = queued_survivors["queued"][0]  # pending, with coordinates
        embedding, release = asyncio.Event(), asyncio.Event()

        async def _blocked_embed(*_a, **_k) -> list[float]:
            embedding.set()
            await release.wait()
            return [0.1] * 8

        run = asyncio.create_task(
            _embed_once(db_session, memory_id, embed=AsyncMock(side_effect=_blocked_embed))
        )
        await asyncio.wait_for(embedding.wait(), 15)
        request = await _erase(db_session, erasure_scenario)
        assert request.status == STATUS_COMPLETE
        release.set()
        stubs = await asyncio.wait_for(run, 15)

        # the residue the guard removes: a point under the raw sub
        assert stubs["add_memory_to_qdrant"].await_args.kwargs["user_id"] == target
        stubs["delete_points"].assert_awaited_once_with([str(memory_id)], "kagura_memories")
        stubs["delete_chunks"].assert_awaited_once_with([memory_id], "kagura_memories")
        events = _info_events(stubs["logger"])
        assert "embedding_claim_lost" in events
        removed = next(
            c
            for c in stubs["logger"].info.call_args_list
            if c.args and c.args[0] == "embedding_points_removed_after_lost_claim"
        )
        assert removed.kwargs["reason"] == "author_changed"
        claim_lost = next(
            c
            for c in stubs["logger"].info.call_args_list
            if c.args and c.args[0] == "embedding_claim_lost"
        )
        assert claim_lost.kwargs["requeued"] is False
        row = (await _table_snapshot(db_session, "memories"))[memory_id]
        assert row["embedding_status"] == "failed"
        assert row["embedding_error"] == ERASED_AUTHOR_EMBEDDING_ERROR
        assert row["user_id"] == erasure_pseudonym(target)

    @pytest.mark.asyncio
    async def test_a_point_written_before_the_row_locks_goes_after_the_commit(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        queued_survivors,
        patched_external_stores,
    ):
        """A run embeds one of the queued survivors to ``success`` between step
        1 and the Postgres step (here: during the workspace hand-over). The
        dequeue terminalizes the row but cannot delete its existing point,
        so the subject's points are deleted again after the commit."""
        target = erasure_scenario["target_user_id"]
        memory_id = queued_survivors["queued"][0]  # pending, with coordinates
        order: list[tuple] = []
        hand_over = AccountErasureService._handle_owned_workspaces

        async def _delete(user_id: str) -> dict[str, int]:
            order.append(("delete_user_points", user_id))
            return {"kagura_memories": 1}

        async def _embed_then_hand_over(self, *args, **kwargs):
            stubs = await _embed_once(db_session, memory_id, build_point=True)
            written = stubs["add_memory_to_qdrant"].await_args.kwargs
            order.append(("point_written", written["user_id"], "location" in written["payload"]))
            return await hand_over(self, *args, **kwargs)

        with (
            patch(
                "services.account_erasure_service.delete_user_points",
                new=AsyncMock(side_effect=_delete),
            ),
            patch.object(AccountErasureService, "_handle_owned_workspaces", _embed_then_hand_over),
        ):
            request = await _erase(db_session, erasure_scenario)

        assert request.status == STATUS_COMPLETE
        assert order == [
            ("delete_user_points", target),
            ("point_written", target, True),  # the leak: raw sub and coordinates
            ("delete_user_points", target),  # after the commit
        ]
        summary = request.deleted_data_summary
        assert summary["qdrant_after_commit"] == {"kagura_memories": 1}
        # ``success`` at the lock: terminalized like every other survivor.
        assert summary["postgres"]["memories_embedding_dequeued"] == 5
        row = (await _table_snapshot(db_session, "memories"))[memory_id]
        assert row["embedding_status"] == "failed"
        assert row["user_id"] == erasure_pseudonym(target)
        assert row["details"] is None

    @pytest.mark.asyncio
    async def test_a_run_whose_row_the_cascade_deleted_removes_its_point(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        patched_external_stores,
    ):
        """A row of the target's sole-owned workspace goes with the workspace
        while a run embeds it; the run's point would be an orphan with the
        raw sub."""
        import asyncio

        from models.auth import Context
        from models.memory import Memory

        target = erasure_scenario["target_user_id"]
        workspace = Workspace(
            id=uuid4(),
            name=f"ws-{uuid4().hex[:8]}",
            plan_name="free",
            owner_user_id=target,
            daily_api_limit=500,
            weekly_api_limit=2500,
        )
        db_session.add(workspace)
        await db_session.flush()
        db_session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=target, role=WorkspaceRole.OWNER)
        )
        context = Context(
            id=uuid4(), workspace_id=workspace.id, name=f"ctx-{uuid4().hex[:8]}", created_by=target
        )
        db_session.add(context)
        await db_session.flush()
        memory = Memory(
            id=uuid4(),
            user_id=target,
            workspace_id=workspace.id,
            context_id=context.id,
            summary="a memory of a sole-owned workspace",
            content="body",
            type="note",
            client="test",
            embedding_status="pending",
        )
        db_session.add(memory)
        await db_session.commit()
        memory_id = memory.id
        embedding, release = asyncio.Event(), asyncio.Event()

        async def _blocked_embed(*_a, **_k) -> list[float]:
            embedding.set()
            await release.wait()
            return [0.1] * 8

        run = asyncio.create_task(
            _embed_once(db_session, memory_id, embed=AsyncMock(side_effect=_blocked_embed))
        )
        await asyncio.wait_for(embedding.wait(), 15)
        request = await _erase(db_session, erasure_scenario)
        assert request.status == STATUS_COMPLETE
        release.set()
        stubs = await asyncio.wait_for(run, 15)

        assert memory_id not in await _table_snapshot(db_session, "memories")
        stubs["delete_points"].assert_awaited_once_with([str(memory_id)], "kagura_memories")
        stubs["delete_chunks"].assert_awaited_once_with([memory_id], "kagura_memories")
        removed = next(
            c
            for c in stubs["logger"].info.call_args_list
            if c.args and c.args[0] == "embedding_points_removed_after_lost_claim"
        )
        assert removed.kwargs["reason"] == "row_gone"

    @pytest.mark.asyncio
    async def test_the_rerun_finds_nothing_to_change(
        self,
        db_session: AsyncSession,
        erasure_scenario,
        queued_survivors,
        patched_external_stores,
        capsys: pytest.CaptureFixture[str],
    ):
        """``reprocess_erasures`` never reads the embedding columns: an
        ``--apply`` re-run of an erasure that dequeued rows changes nothing."""
        from cli import reprocess_erasures as cli

        request = await _erase(db_session, erasure_scenario)
        assert request.deleted_data_summary["postgres"]["memories_embedding_dequeued"] == 5
        before = await _table_snapshot(db_session, "memories")
        capsys.readouterr()  # the erasure's own log lines carry the raw id

        async def get_db():
            yield db_session

        with (
            patch("cli._oneshot.get_db", get_db),
            patch.object(cli, "configure_logging", lambda level: None),
            patch("services.erasure_details.update_memory_payload_in_qdrant", AsyncMock()) as sync,
        ):
            code = await cli._main(cli._parse(["--request", str(request.id), "--apply", "--yes"]))

        assert code == 0
        assert "1 completed erasure(s): would change 0, skipped 0" in capsys.readouterr().out
        sync.assert_not_awaited()
        assert await _table_snapshot(db_session, "memories") == before
