"""Unit tests for AccountErasureService (Issue #360, GDPR right-to-erasure).

These tests target the state-machine + guard logic in the service layer.
The cross-store deletion pipeline (`_execute`) requires real Qdrant +
Redis + Postgres containers and is exercised separately via
``make test-integration`` with the migrations applied; the unit tests
here use mocks for the data-layer collaborators so they stay fast and
deterministic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.erasure import (
    REASON_SELF_SERVICE,
    REASON_USER_REQUEST_VIA_SUPPORT,
    STATUS_CANCELLED,
    STATUS_COOLING_OFF,
    STATUS_PENDING,
    ErasureRequest,
)
from services.account_erasure_service import (
    COOLING_OFF_PERIOD,
    AccountErasureService,
    _sha256_hex,
)
from utils.exceptions import (
    ErasureAlreadyInProgressError,
    ErasureForbiddenError,
    ErasureRequestNotFoundError,
    ErasureTokenInvalidError,
    InitialAdminCannotBeErasedError,
    NotFoundException,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(
    *,
    user_id: str = "u-1",
    email: str = "alice@example.com",
    is_initial_admin: bool = False,
    auth_method: str = "oauth",
    password_hash: str | None = None,
    role: str = "user",
) -> SimpleNamespace:
    """Light User stand-in. AccountErasureService only reads attributes."""
    return SimpleNamespace(
        user_id=user_id,
        email=email,
        is_initial_admin=is_initial_admin,
        auth_method=auth_method,
        password_hash=password_hash,
        role=role,
    )


def _service() -> AccountErasureService:
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock()
    email = AsyncMock()
    email.send_erasure_receipt = AsyncMock(return_value=True)
    email.send_erasure_cooling_off_started = AsyncMock(return_value=True)
    email.send_erasure_complete = AsyncMock(return_value=True)
    return AccountErasureService(db, email_service=email)


# ---------------------------------------------------------------------------
# request_self_service_erasure
# ---------------------------------------------------------------------------


class TestRequestSelfServiceErasure:
    @pytest.mark.asyncio
    async def test_creates_pending_row_and_issues_token(self):
        svc = _service()
        target = _user()
        svc._load_user_or_404 = AsyncMock(return_value=target)
        svc._find_active_request = AsyncMock(return_value=None)

        # Capture the row added so we can inspect its initial fields.
        added_rows: list[ErasureRequest] = []
        svc.db.add = lambda row: added_rows.append(row)

        async def _refresh(row):
            row.id = uuid4()

        svc.db.refresh = AsyncMock(side_effect=_refresh)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(setex=AsyncMock())
            request, token = await svc.request_self_service_erasure(user_id="u-1")

        assert len(added_rows) == 1
        row = added_rows[0]
        assert row.status == STATUS_PENDING
        assert row.is_self_service is True
        assert row.reason_code == REASON_SELF_SERVICE
        assert row.confirm_token_hash == _sha256_hex(token)
        assert row.user_email_hash == _sha256_hex(target.email)
        assert request is row
        svc.email_service.send_erasure_receipt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blocks_initial_admin(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user(is_initial_admin=True))
        svc._find_active_request = AsyncMock(return_value=None)

        with pytest.raises(InitialAdminCannotBeErasedError):
            await svc.request_self_service_erasure(user_id="u-1")

    @pytest.mark.asyncio
    async def test_blocks_when_active_request_exists(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user())
        existing = SimpleNamespace(status=STATUS_COOLING_OFF)
        svc._find_active_request = AsyncMock(return_value=existing)

        with pytest.raises(ErasureAlreadyInProgressError):
            await svc.request_self_service_erasure(user_id="u-1")

    @pytest.mark.asyncio
    async def test_unknown_user_raises_not_found(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(side_effect=NotFoundException("User", "u-1"))

        with pytest.raises(NotFoundException):
            await svc.request_self_service_erasure(user_id="u-1")


# ---------------------------------------------------------------------------
# confirm_self_service
# ---------------------------------------------------------------------------


class TestConfirmSelfService:
    @pytest.mark.asyncio
    async def test_oauth_user_confirms_with_token_only(self):
        svc = _service()
        target = _user(auth_method="oauth")
        svc._load_user_or_404 = AsyncMock(return_value=target)
        # User owns no workspaces — workspace pre-check is a no-op.
        svc._check_no_blocking_workspace_transfers = AsyncMock()

        token = "raw-token-abc"
        request_id = uuid4()
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash=_sha256_hex(target.email),
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = request_id
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            redis_client = MagicMock()
            redis_client.get = AsyncMock(return_value=str(request_id))
            redis_client.delete = AsyncMock()
            mock_redis.return_value = redis_client

            await svc.confirm_self_service(user_id="u-1", token=token)

        assert request.status == STATUS_COOLING_OFF
        assert request.scheduled_for is not None
        # Cooling-off window equals the configured policy.
        assert (request.scheduled_for - request.confirmed_at) == COOLING_OFF_PERIOD
        redis_client.delete.assert_awaited_once()
        svc.email_service.send_erasure_cooling_off_started.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_token_missing_in_redis_raises_invalid(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user())

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(get=AsyncMock(return_value=None))
            with pytest.raises(ErasureTokenInvalidError):
                await svc.confirm_self_service(user_id="u-1", token="x")

    @pytest.mark.asyncio
    async def test_password_user_requires_password(self):
        svc = _service()
        target = _user(auth_method="password", password_hash="hashed")
        svc._load_user_or_404 = AsyncMock(return_value=target)

        token = "tok"
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = uuid4()
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(
                get=AsyncMock(return_value=str(request.id)),
                delete=AsyncMock(),
            )
            with pytest.raises(ErasureForbiddenError):
                await svc.confirm_self_service(user_id="u-1", token=token, password=None)

    @pytest.mark.asyncio
    async def test_password_user_wrong_password_blocked(self):
        svc = _service()
        target = _user(auth_method="password", password_hash="hashed")
        svc._load_user_or_404 = AsyncMock(return_value=target)

        token = "tok"
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = uuid4()
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(
                get=AsyncMock(return_value=str(request.id)),
                delete=AsyncMock(),
            )
            with patch("auth.password.verify_password", return_value=False):
                with pytest.raises(ErasureForbiddenError):
                    await svc.confirm_self_service(user_id="u-1", token=token, password="bad")

    @pytest.mark.asyncio
    async def test_token_for_other_user_rejected(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user(user_id="u-1"))

        token = "tok"
        request = ErasureRequest(
            user_id="u-OTHER",
            user_email_hash="x",
            initiated_by="u-OTHER",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = uuid4()
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(
                get=AsyncMock(return_value=str(request.id)),
                delete=AsyncMock(),
            )
            with pytest.raises(ErasureTokenInvalidError):
                await svc.confirm_self_service(user_id="u-1", token=token)

    @pytest.mark.asyncio
    async def test_already_confirmed_request_rejected(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user())

        token = "tok"
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_COOLING_OFF,
            confirm_token_hash=_sha256_hex(token),
        )
        request.id = uuid4()
        svc._load_request_or_404 = AsyncMock(return_value=request)

        with patch("services.account_erasure_service.get_redis_client") as mock_redis:
            mock_redis.return_value = MagicMock(
                get=AsyncMock(return_value=str(request.id)),
                delete=AsyncMock(),
            )
            with pytest.raises(ErasureTokenInvalidError):
                await svc.confirm_self_service(user_id="u-1", token=token)


# ---------------------------------------------------------------------------
# cancel_self_service
# ---------------------------------------------------------------------------


class TestCancelSelfService:
    @pytest.mark.asyncio
    async def test_cooling_off_request_can_be_cancelled(self):
        svc = _service()
        request = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_COOLING_OFF,
        )
        request.id = uuid4()
        svc._find_active_request = AsyncMock(return_value=request)

        result = await svc.cancel_self_service(user_id="u-1")

        assert result.status == STATUS_CANCELLED
        assert result.cancelled_at is not None

    @pytest.mark.asyncio
    async def test_no_active_request_raises_not_found(self):
        svc = _service()
        svc._find_active_request = AsyncMock(return_value=None)

        with pytest.raises(ErasureRequestNotFoundError):
            await svc.cancel_self_service(user_id="u-1")

    @pytest.mark.asyncio
    async def test_pending_request_can_be_cancelled(self):
        """Pending IS cancellable now (Copilot /review iter 2 finding).

        Without this, an unconfirmed pending row whose Redis token TTL
        has elapsed would block all future erasure requests via the
        partial unique index — user permanently wedged. Allow cancel
        to give the user immediate recourse.
        """
        svc = _service()
        pending = ErasureRequest(
            user_id="u-1",
            user_email_hash="x",
            initiated_by="u-1",
            is_self_service=True,
            reason_code=REASON_SELF_SERVICE,
            status=STATUS_PENDING,
        )
        pending.id = uuid4()
        svc._find_active_request = AsyncMock(return_value=pending)

        result = await svc.cancel_self_service(user_id="u-1")
        assert result.status == STATUS_CANCELLED
        assert result.cancelled_at is not None


# ---------------------------------------------------------------------------
# admin_force_erase guard rails
# ---------------------------------------------------------------------------


class TestAdminForceErase:
    @pytest.mark.asyncio
    async def test_self_service_reason_rejected(self):
        svc = _service()
        with pytest.raises(ValidationError):
            await svc.admin_force_erase(
                target_user_id="u-1",
                initiator_user_id="admin-1",
                reason_code=REASON_SELF_SERVICE,
            )

    @pytest.mark.asyncio
    async def test_unknown_reason_code_rejected(self):
        svc = _service()
        with pytest.raises(ValidationError):
            await svc.admin_force_erase(
                target_user_id="u-1",
                initiator_user_id="admin-1",
                reason_code="not_a_real_code",
            )

    @pytest.mark.asyncio
    async def test_reason_detail_length_capped(self):
        svc = _service()
        with pytest.raises(ValidationError):
            await svc.admin_force_erase(
                target_user_id="u-1",
                initiator_user_id="admin-1",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                reason_detail="x" * 1001,
            )

    @pytest.mark.asyncio
    async def test_initial_admin_blocked(self):
        svc = _service()
        svc._load_user_or_404 = AsyncMock(return_value=_user(is_initial_admin=True, role="admin"))

        with pytest.raises(InitialAdminCannotBeErasedError):
            await svc.admin_force_erase(
                target_user_id="u-1",
                initiator_user_id="admin-1",
                reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            )

    @pytest.mark.asyncio
    async def test_last_admin_blocked(self):
        svc = _service()
        target = _user(role="admin")
        svc._load_user_or_404 = AsyncMock(return_value=target)
        with patch("services.account_erasure_service.SystemAdminService") as MockSvcCls:
            instance = MockSvcCls.return_value
            instance.can_delete_admin = AsyncMock(
                return_value=(False, "Cannot delete the last remaining system administrator")
            )
            with pytest.raises(ErasureForbiddenError):
                await svc.admin_force_erase(
                    target_user_id="u-1",
                    initiator_user_id="admin-2",
                    reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
                )


# ---------------------------------------------------------------------------
# Workspace ownership transfer logic
# ---------------------------------------------------------------------------


class TestHandleOwnedWorkspaces:
    @staticmethod
    def _bulk_members_result(rows: list[SimpleNamespace]) -> MagicMock:
        """Build a MagicMock that mimics the bulk SELECT WorkspaceMember
        result the service now expects (one round-trip across all workspaces)."""
        result_obj = MagicMock()
        result_obj.scalars.return_value.all.return_value = rows
        return result_obj

    @pytest.mark.asyncio
    async def test_sole_owner_workspace_passes_through(self):
        svc = _service()
        ws_id = uuid4()
        ws = SimpleNamespace(id=ws_id, owner_user_id="u-1")
        rows = [SimpleNamespace(workspace_id=ws_id, user_id="u-1", role="owner")]
        svc.db.execute = AsyncMock(return_value=self._bulk_members_result(rows))
        svc.db.commit = AsyncMock()

        out = await svc._handle_owned_workspaces("u-1", [ws])
        assert out["sole_owner_workspaces"] == 1
        assert out["transferred"] == []

    @pytest.mark.asyncio
    async def test_other_admin_triggers_auto_transfer(self):
        svc = _service()
        ws_id = uuid4()
        ws = SimpleNamespace(id=ws_id, owner_user_id="u-1")
        new_admin = SimpleNamespace(workspace_id=ws_id, user_id="u-2", role="admin")
        rows = [
            SimpleNamespace(workspace_id=ws_id, user_id="u-1", role="owner"),
            new_admin,
        ]
        svc.db.execute = AsyncMock(return_value=self._bulk_members_result(rows))
        svc.db.commit = AsyncMock()

        out = await svc._handle_owned_workspaces("u-1", [ws])
        assert ws.owner_user_id == "u-2"
        assert new_admin.role == "owner"
        assert len(out["transferred"]) == 1

    @pytest.mark.asyncio
    async def test_members_without_admin_blocks_with_typed_error(self):
        from utils.exceptions import WorkspaceTransferRequiredError

        svc = _service()
        ws_id = uuid4()
        ws = SimpleNamespace(id=ws_id, owner_user_id="u-1")
        rows = [
            SimpleNamespace(workspace_id=ws_id, user_id="u-1", role="owner"),
            SimpleNamespace(workspace_id=ws_id, user_id="u-2", role="member"),
            SimpleNamespace(workspace_id=ws_id, user_id="u-3", role="viewer"),
        ]
        svc.db.execute = AsyncMock(return_value=self._bulk_members_result(rows))
        svc.db.commit = AsyncMock()

        with pytest.raises(WorkspaceTransferRequiredError) as exc_info:
            await svc._handle_owned_workspaces("u-1", [ws])
        # The error carries member_count so the API can surface it.
        assert exc_info.value.details["member_count"] == 2

    @pytest.mark.asyncio
    async def test_no_workspaces_short_circuits(self):
        svc = _service()
        # No bulk-load issued when there are no workspaces — verify execute
        # is never awaited.
        svc.db.execute = AsyncMock()
        out = await svc._handle_owned_workspaces("u-1", [])
        assert out == {"transferred": [], "sole_owner_workspaces": 0}
        svc.db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Helpers (sanity)
# ---------------------------------------------------------------------------


def test_sha256_hex_is_stable_and_salt_aware():
    assert _sha256_hex("a") == _sha256_hex("a")
    assert _sha256_hex("a") != _sha256_hex("a", salt="x")
    assert len(_sha256_hex("anything")) == 64


# ---------------------------------------------------------------------------
# AUDIT_PSEUDO_SALT settings sourcing + SessionManager accessor
# ---------------------------------------------------------------------------


def test_audit_salt_reads_from_settings(monkeypatch):
    """`_audit_salt()` must reflect the active Settings.audit_pseudo_salt."""
    from services import account_erasure_service

    fake_settings = SimpleNamespace(audit_pseudo_salt="prod-rotated-salt-2026Q2")
    monkeypatch.setattr(account_erasure_service, "get_settings", lambda: fake_settings)

    assert account_erasure_service._audit_salt() == "prod-rotated-salt-2026Q2"

    pseudo_a = _sha256_hex("u-1", salt=account_erasure_service._audit_salt())
    monkeypatch.setattr(
        account_erasure_service,
        "get_settings",
        lambda: SimpleNamespace(audit_pseudo_salt="different-salt"),
    )
    pseudo_b = _sha256_hex("u-1", salt=account_erasure_service._audit_salt())
    assert pseudo_a != pseudo_b, "rotating the salt must change downstream pseudonyms"


def test_get_session_manager_returns_module_state(monkeypatch):
    """`get_session_manager()` must expose the live `_session_manager` module attribute."""
    from api.routes import auth as auth_module

    sentinel = object()
    monkeypatch.setattr(auth_module, "_session_manager", sentinel)
    assert auth_module.get_session_manager() is sentinel

    monkeypatch.setattr(auth_module, "_session_manager", None)
    assert auth_module.get_session_manager() is None
