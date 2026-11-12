"""Every sign-up control decision for an email sign-up (#1734), no DB.

Parity with Google / GitHub: gate off -> first account, ALLOW_REGISTRATION, a
pending invitation addressed to the address (a beta invite is ignored); gate
on -> first account, a pending invitation addressed to the address (#2785,
before the beta invite) or a redeemable beta invite (ALLOW_REGISTRATION
ignored, sponsor modes treated as manual). Never the manual allowlist or
pending Google rows. The staged redemption is in a savepoint and never
commits; an invitation admission stages its ``signup_admitted`` row. Real
transactions: ``tests/integration/test_email_signup_db.py`` and
``tests/integration/test_signup_gate_invitation_db.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from config.settings import get_settings
from models.auth import AuditLog
from models.signup_gate import SignupAllowlistEntry
from services.signup_gate_service import SignupGateService

TOKEN_HASH = "d" * 64


def _svc(*, first_user: bool = False) -> SignupGateService:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    savepoint = MagicMock()
    savepoint.commit = AsyncMock()
    savepoint.rollback = AsyncMock()
    db.begin_nested = AsyncMock(return_value=savepoint)
    svc = SignupGateService(db)
    svc._is_first_user = AsyncMock(return_value=first_user)
    svc._is_allowlisted = AsyncMock(return_value=True)
    svc._promote_pending_google_entry = AsyncMock(return_value=True)
    svc._record_blocked_signup = AsyncMock()
    return svc


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "allow_registration", False)
    monkeypatch.setattr(s, "enable_beta_invites", True)
    monkeypatch.setattr(s, "enable_email_signup", True)
    return s


def _invitation_row(**overrides) -> SimpleNamespace:
    row = {
        "email": "a@example.test",
        "role": "member",
        "public_id": "winv_abc",
        "workspace_id": uuid4(),
        "invited_by": "workspace-admin",
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def _invitations(found: bool):
    service = MagicMock()
    service.get_pending_invitations_for_email = AsyncMock(
        return_value=[_invitation_row()] if found else []
    )
    return patch("services.invitation_service.InvitationService", return_value=service)


async def _admissible(
    svc, *, enabled, mode="manual", token=TOKEN_HASH, invitation=False, redeemable=True
):
    svc._load_config = AsyncMock(return_value=SimpleNamespace(enabled=enabled, mode=mode))
    with (
        _invitations(invitation),
        patch("services.signup_gate_service.BetaInviteService") as invites_cls,
    ):
        invites_cls.return_value.find_redeemable = AsyncMock(
            return_value=SimpleNamespace(id=uuid4(), inviter_user_id="inviter")
            if redeemable
            else None
        )
        return await svc.email_signup_admissible(
            email="a@example.test", beta_invite_token_hash=token
        )


class TestGateOff:
    @pytest.mark.asyncio
    async def test_first_account(self, settings) -> None:
        assert await _admissible(_svc(first_user=True), enabled=False) == "first_user"

    @pytest.mark.asyncio
    async def test_open_registration(self, settings, monkeypatch) -> None:
        monkeypatch.setattr(settings, "allow_registration", True)
        assert await _admissible(_svc(), enabled=False) == "open_registration"

    @pytest.mark.asyncio
    async def test_addressed_invitation(self, settings) -> None:
        assert await _admissible(_svc(), enabled=False, invitation=True) == "workspace_invitation"

    @pytest.mark.asyncio
    async def test_closed_registration_and_no_invitation_refuses_even_with_a_beta_invite(
        self, settings
    ) -> None:
        svc = _svc()
        assert await _admissible(svc, enabled=False, invitation=False) is None
        # The provider-identity grants never admit an email identity.
        svc._is_allowlisted.assert_not_awaited()
        svc._promote_pending_google_entry.assert_not_awaited()
        svc._record_blocked_signup.assert_not_awaited()


class TestGateOn:
    @pytest.mark.asyncio
    async def test_first_account(self, settings) -> None:
        assert await _admissible(_svc(first_user=True), enabled=True) == "first_user"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["manual", "github_sponsors", "both"])
    async def test_beta_invite_in_every_mode(self, settings, mode) -> None:
        assert await _admissible(_svc(), enabled=True, mode=mode) == "beta_invite"

    @pytest.mark.asyncio
    async def test_dead_invite_refuses(self, settings) -> None:
        assert await _admissible(_svc(), enabled=True, redeemable=False) is None

    @pytest.mark.asyncio
    async def test_no_invite_refuses(self, settings) -> None:
        assert await _admissible(_svc(), enabled=True, token=None) is None

    @pytest.mark.asyncio
    async def test_kill_switch_makes_the_invite_inert(self, settings, monkeypatch) -> None:
        monkeypatch.setattr(settings, "enable_beta_invites", False)
        assert await _admissible(_svc(), enabled=True) is None

    @pytest.mark.asyncio
    async def test_open_registration_does_not_admit(self, settings, monkeypatch) -> None:
        monkeypatch.setattr(settings, "allow_registration", True)
        assert await _admissible(_svc(), enabled=True, token=None, invitation=False) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["manual", "github_sponsors", "both"])
    async def test_addressed_invitation_admits(self, settings, mode) -> None:
        assert (
            await _admissible(_svc(), enabled=True, mode=mode, token=None, invitation=True)
            == "workspace_invitation"
        )

    @pytest.mark.asyncio
    async def test_addressed_invitation_comes_before_the_beta_invite(self, settings) -> None:
        svc = _svc()
        svc._load_config = AsyncMock(return_value=SimpleNamespace(enabled=True, mode="manual"))
        with (
            _invitations(True),
            patch("services.signup_gate_service.BetaInviteService") as invites_cls,
        ):
            invites_cls.return_value.find_redeemable = AsyncMock()
            assert (
                await svc.email_signup_admissible(
                    email="a@example.test", beta_invite_token_hash=TOKEN_HASH
                )
                == "workspace_invitation"
            )
        invites_cls.return_value.find_redeemable.assert_not_awaited()
        svc.db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_corrupted_mode_refuses(self, settings) -> None:
        assert await _admissible(_svc(), enabled=True, mode="nonsense") is None

    @pytest.mark.asyncio
    async def test_corrupted_mode_refuses_before_the_invitation_lookup(self, settings) -> None:
        svc = _svc()
        svc._load_config = AsyncMock(return_value=SimpleNamespace(enabled=True, mode="nonsense"))
        with _invitations(True) as service_cls:
            assert (
                await svc.email_signup_admissible(
                    email="a@example.test", beta_invite_token_hash=None
                )
                is None
            )
        service_cls.return_value.get_pending_invitations_for_email.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admissible_never_writes(self, settings) -> None:
        svc = _svc()
        await _admissible(svc, enabled=True)
        svc.db.add.assert_not_called()
        svc.db.commit.assert_not_awaited()


async def _admit(
    svc,
    *,
    claimed=True,
    redeemable=True,
    flush_error=False,
    invitation=False,
    gate_enabled=True,
):
    if flush_error:
        svc.db.flush = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("dup")))
    with (
        _invitations(invitation),
        patch("services.signup_gate_service.BetaInviteService") as invites_cls,
    ):
        invites_cls.return_value.find_redeemable = AsyncMock(
            return_value=SimpleNamespace(id=uuid4(), inviter_user_id="inviter")
            if redeemable
            else None
        )
        invites_cls.return_value.mark_redeemed = AsyncMock(return_value=claimed)
        return await svc.admit_email_signup(
            gate_enabled=gate_enabled,
            gate_mode="manual",
            email="a@example.test",
            new_user_id="email:new",
            beta_invite_token_hash=TOKEN_HASH,
            ip_address="203.0.113.1",
            user_agent="ua",
        )


class TestConsumingRedemption:
    @pytest.mark.asyncio
    async def test_success_stages_row_claim_and_audit_and_never_commits(self, settings) -> None:
        svc = _svc()
        assert await _admit(svc) == "beta_invite"
        added = [c.args[0] for c in svc.db.add.call_args_list]
        entry = next(o for o in added if isinstance(o, SignupAllowlistEntry))
        assert (entry.provider, entry.subject_id, entry.source) == (
            "email",
            "email:new",
            "beta_invite",
        )
        assert entry.subject_label == "a@example.test"
        assert entry.added_by_user_id == "inviter"
        audit = next(o for o in added if isinstance(o, AuditLog))
        assert audit.action == "beta_invite.redeemed"
        assert audit.user_email == "email-signup"
        assert "a@example.test" not in repr(vars(audit))
        svc.db.commit.assert_not_awaited()
        svc.db.rollback.assert_not_awaited()
        svc.db.begin_nested.return_value.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lost_race_rolls_back_the_savepoint_only(self, settings) -> None:
        svc = _svc()
        assert await _admit(svc, claimed=False) is None
        svc.db.begin_nested.return_value.rollback.assert_awaited_once()
        svc.db.rollback.assert_not_awaited()
        svc.db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_constraint_error_rolls_back_the_savepoint_only(self, settings) -> None:
        svc = _svc()
        assert await _admit(svc, flush_error=True) is None
        svc.db.begin_nested.return_value.rollback.assert_awaited_once()
        svc.db.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dead_invite_opens_no_savepoint(self, settings) -> None:
        svc = _svc()
        assert await _admit(svc, redeemable=False) is None
        svc.db.begin_nested.assert_not_awaited()
        svc.db.add.assert_not_called()


class TestInvitationAdmissionRecord:
    """#2785: an admission by invitation stages one ``signup_admitted`` row in
    the account-creating transaction; the beta invite stays unredeemed."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("gate_enabled", [True, False])
    async def test_stages_the_record_and_never_commits(self, settings, gate_enabled) -> None:
        svc = _svc()
        assert (
            await _admit(svc, invitation=True, gate_enabled=gate_enabled) == "workspace_invitation"
        )
        added = [c.args[0] for c in svc.db.add.call_args_list]
        (audit,) = added
        assert isinstance(audit, AuditLog)
        assert audit.action == "signup_admitted"
        assert (audit.user_email, audit.user_id) == ("email-signup", "email:new")
        assert audit.resource == "signup_gate:email"
        assert audit.user_metadata["admission"] == "workspace_invitation"
        assert audit.user_metadata["invitations"][0]["id"] == "winv_abc"
        assert audit.user_metadata["invitations"][0]["invited_by"] == "workspace-admin"
        assert audit.user_metadata["invitation_count"] == 1
        assert "a@example.test" not in repr(vars(audit))
        svc.db.commit.assert_not_awaited()
        svc.db.begin_nested.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_request_time_check_records_nothing(self, settings) -> None:
        svc = _svc()
        assert await _admissible(svc, enabled=True, invitation=True) == "workspace_invitation"
        svc.db.add.assert_not_called()
        svc.db.commit.assert_not_awaited()


class TestInvitationPreviewFlag:
    async def _flag(
        self,
        svc,
        *,
        enabled,
        restricted=True,
        blocked=False,
        owner=False,
        deleted=False,
        mode="manual",
    ):
        svc._load_config = AsyncMock(return_value=SimpleNamespace(enabled=enabled, mode=mode))
        return await svc.email_signup_available_for_invitation(
            email_restricted=restricted,
            plan_blocked=blocked,
            owner_role=owner,
            workspace_deleted=deleted,
        )

    @pytest.mark.asyncio
    async def test_off_when_email_signup_is_off(self, settings, monkeypatch) -> None:
        monkeypatch.setattr(settings, "enable_email_signup", False)
        assert await self._flag(_svc(first_user=True), enabled=False) is False

    @pytest.mark.asyncio
    async def test_gate_off_addressed_invitation(self, settings) -> None:
        assert await self._flag(_svc(), enabled=False) is True
        assert await self._flag(_svc(), enabled=False, restricted=False) is False
        assert await self._flag(_svc(), enabled=False, blocked=True) is False

    @pytest.mark.asyncio
    async def test_gate_off_open_registration(self, settings, monkeypatch) -> None:
        monkeypatch.setattr(settings, "allow_registration", True)
        assert await self._flag(_svc(), enabled=False, restricted=False) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_owner_role_or_deleted_workspace_admits_nobody(self, settings, enabled) -> None:
        assert await self._flag(_svc(), enabled=enabled, owner=True) is False
        assert await self._flag(_svc(), enabled=enabled, deleted=True) is False

    @pytest.mark.asyncio
    async def test_gate_on_addressed_invitation(self, settings, monkeypatch) -> None:
        # #2785: the same eligibility as with the gate off.
        monkeypatch.setattr(settings, "allow_registration", True)
        assert await self._flag(_svc(), enabled=True) is True
        assert await self._flag(_svc(), enabled=True, restricted=False) is False
        assert await self._flag(_svc(), enabled=True, blocked=True) is False
        assert await self._flag(_svc(), enabled=True, mode="nonsense") is False
        assert await self._flag(_svc(first_user=True), enabled=True, restricted=False) is True

    @pytest.mark.asyncio
    async def test_gate_on_off_when_email_signup_is_off(self, settings, monkeypatch) -> None:
        monkeypatch.setattr(settings, "enable_email_signup", False)
        assert await self._flag(_svc(), enabled=True) is False


class TestExistingUserPredicate:
    @pytest.mark.asyncio
    async def test_case_variant_of_an_email_signup_account_counts_as_existing(self) -> None:
        svc = _svc()
        captured = {}

        async def fake_execute(stmt):
            captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            result = MagicMock()
            result.first = MagicMock(return_value=None)
            return result

        svc.db.execute = fake_execute
        await svc._is_existing_user("Person@Example.TEST", "sub-1")
        sql = captured["sql"]
        assert "users.email = 'person@example.test'" in sql
        assert "users.login_id IS NULL" in sql
        assert "users.auth_method = 'password'" in sql
        assert "LIMIT" in sql
