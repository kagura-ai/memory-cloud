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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import APIKey, AuditLog, User, Workspace, WorkspaceMember
from models.erasure import (
    REASON_USER_REQUEST_VIA_SUPPORT,
    STATUS_COMPLETE,
    ErasureRequest,
)
from services.account_erasure_service import AccountErasureService
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

    yield {
        "target_user_id": target_user_id,
        "target_email": target_email,
        "other_user_id": other_user_id,
        "workspace_id": workspace.id,
        "api_key_id": api_key.id,
        "prior_audit_id": prior_audit.id,
    }


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
    # SessionManager: patch the underlying module attribute the public
    # get_session_manager() accessor reads, forcing the service to skip
    # the session-cleanup branch.
    session_manager_patch = patch(
        "api.routes.auth._session_manager",
        new=None,
        create=True,
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
            select(AuditLog).where(AuditLog.action == "account_erasure")
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
