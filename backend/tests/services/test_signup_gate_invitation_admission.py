"""Signup gate × pending workspace invitations (#2785) — unit level, no DB.

A pending workspace invitation addressed to the applicant's verified address
admits a new person, Google and GitHub alike, in both gate states:

- with the gate on, after the allowlist and the pending Google row and before
  the beta invite (an invitee keeps the beta invite token);
- the address counts only when the provider reports it verified; without that
  the invitation lookup is not even made (and a pending Google row is not
  promoted either);
- each admission writes one ``signup_admitted`` audit row that commits before
  the gate says yes; a failed write admits nobody;
- the row names each invitation's public id, workspace and inviter, never the
  address (HMAC only) or the invitation token.

Real rows and transactions: ``tests/integration/test_signup_gate_invitation_db.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import OperationalError

from auth.admission import AdmissionRecord
from auth.roles import _OAUTH_CALLBACK_ACTOR
from models.auth import AuditLog
from services.signup_gate_service import SignupGateService, check_signup_access

TOKEN_HASH = "e" * 64
ADDRESS = "invitee@example.test"


# #2803: a redemption answers how the account is admitted.
REDEEMED = AdmissionRecord(
    "beta_invite",
    beta_invite_id=UUID("00000000-0000-4000-8000-000000002803"),
    inviter_user_id="inviter-sub",
)


def _inv(
    *,
    email: str | None = ADDRESS,
    role: str = "member",
    public_id: str | None = None,
    invited_by: str = "inviter-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        email=email,
        role=role,
        public_id=public_id or f"winv_{uuid4().hex[:12]}",
        workspace_id=uuid4(),
        invited_by=invited_by,
        token="t" * 64,
    )


def _svc() -> SignupGateService:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return SignupGateService(db)


def _closed_gate_new_user(svc: SignupGateService, *, enabled: bool = True) -> None:
    svc._load_config = AsyncMock(return_value=SimpleNamespace(enabled=enabled, mode="manual"))
    svc._is_existing_user = AsyncMock(return_value=False)
    svc._is_first_user = AsyncMock(return_value=False)
    svc._allowlist_admission = AsyncMock(return_value=None)
    svc._promote_pending_google_entry = AsyncMock(return_value=False)
    svc._redeem_beta_invite = AsyncMock(return_value=None)
    svc._record_blocked_signup = AsyncMock()


def _invitation_service(rows: list) -> tuple[MagicMock, object]:
    service = MagicMock()
    service.get_pending_invitations_for_email = AsyncMock(return_value=rows)
    return service, patch("services.invitation_service.InvitationService", return_value=service)


async def _check(svc: SignupGateService, *, provider: str = "google", **kwargs):
    # #2803: the gate's full answer (redirect, admission record or None).
    return await svc.decide(
        provider=provider,  # type: ignore[arg-type]
        oauth_sub="sub-12345678",
        email=ADDRESS,
        username="label",
        ip_address="203.0.113.9",
        user_agent="ua",
        **kwargs,
    )


def _audits(svc: SignupGateService) -> list[AuditLog]:
    return [c.args[0] for c in svc.db.add.call_args_list if isinstance(c.args[0], AuditLog)]


class TestGateOn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["google", "github"])
    async def test_verified_addressed_invitation_admits_and_records(self, provider) -> None:
        svc = _svc()
        _closed_gate_new_user(svc)
        row = _inv()
        service, patched = _invitation_service([row])
        with patched:
            result = await _check(
                svc, provider=provider, email_verified=True, beta_invite_token_hash=TOKEN_HASH
            )

        assert result == AdmissionRecord("workspace_invitation")
        service.get_pending_invitations_for_email.assert_awaited_once_with(email=ADDRESS)
        svc._redeem_beta_invite.assert_not_awaited()
        svc._record_blocked_signup.assert_not_awaited()
        (audit,) = _audits(svc)
        assert audit.action == "signup_admitted"
        assert audit.user_email == _OAUTH_CALLBACK_ACTOR
        assert audit.user_id == "sub-12345678"
        assert audit.resource == f"signup_gate:{provider}"
        assert audit.user_metadata == {
            "provider": provider,
            "admission": "workspace_invitation",
            "invitations": [
                {
                    "id": row.public_id,
                    "workspace_id": str(row.workspace_id),
                    "invited_by": "inviter-1",
                }
            ],
            "invitation_count": 1,
        }
        assert (audit.ip_address, audit.user_agent) == ("203.0.113.9", "ua")
        assert ADDRESS not in repr(vars(audit))
        assert row.token not in repr(vars(audit))
        assert audit.new_value_hash and len(audit.new_value_hash) == 64
        svc.db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["google", "github"])
    @pytest.mark.parametrize("verified", [False, None])
    async def test_unverified_address_never_looks_up_invitations(self, provider, verified) -> None:
        svc = _svc()
        _closed_gate_new_user(svc)
        service, patched = _invitation_service([_inv()])
        kwargs = {} if verified is None else {"email_verified": verified}
        with patched:
            result = await _check(svc, provider=provider, **kwargs)

        assert isinstance(result, RedirectResponse)
        assert "/signup-blocked" in result.headers["location"]
        service.get_pending_invitations_for_email.assert_not_awaited()
        svc._record_blocked_signup.assert_awaited_once()
        assert _audits(svc) == []

    @pytest.mark.asyncio
    async def test_unverified_address_with_a_beta_invite_is_admitted_by_the_invite(self) -> None:
        svc = _svc()
        _closed_gate_new_user(svc)
        svc._redeem_beta_invite = AsyncMock(return_value=REDEEMED)
        service, patched = _invitation_service([_inv()])
        with patched:
            result = await _check(svc, email_verified=False, beta_invite_token_hash=TOKEN_HASH)

        assert result is REDEEMED
        svc._redeem_beta_invite.assert_awaited_once()
        service.get_pending_invitations_for_email.assert_not_awaited()
        assert _audits(svc) == []

    @pytest.mark.asyncio
    async def test_no_invitation_falls_through_to_the_beta_invite_then_blocks(self) -> None:
        svc = _svc()
        _closed_gate_new_user(svc)
        _service, patched = _invitation_service([])
        with patched:
            result = await _check(svc, email_verified=True, beta_invite_token_hash=TOKEN_HASH)

        assert isinstance(result, RedirectResponse)
        svc._redeem_beta_invite.assert_awaited_once()
        svc._record_blocked_signup.assert_awaited_once()
        svc.db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "admitting_lookup", ["_is_existing_user", "_is_first_user", "_allowlist_admission"]
    )
    async def test_earlier_admissions_never_look_up_invitations(self, admitting_lookup) -> None:
        svc = _svc()
        _closed_gate_new_user(svc)
        answers = {
            "_is_existing_user": True,
            "_is_first_user": True,
            "_allowlist_admission": AdmissionRecord("allowlist"),
        }
        # #2803: a login answers None; the others say how the account is admitted.
        expected = {
            "_is_existing_user": None,
            "_is_first_user": AdmissionRecord("first_user"),
            "_allowlist_admission": AdmissionRecord("allowlist"),
        }
        setattr(svc, admitting_lookup, AsyncMock(return_value=answers[admitting_lookup]))
        service, patched = _invitation_service([_inv()])
        with patched:
            assert await _check(svc, email_verified=True) == expected[admitting_lookup]
        service.get_pending_invitations_for_email.assert_not_awaited()
        assert _audits(svc) == []

    @pytest.mark.asyncio
    async def test_promoted_pending_google_row_comes_before_the_invitation(self) -> None:
        svc = _svc()
        _closed_gate_new_user(svc)
        svc._promote_pending_google_entry = AsyncMock(return_value=True)
        service, patched = _invitation_service([_inv()])
        with patched:
            assert await _check(svc, email_verified=True) == AdmissionRecord("allowlist")
        service.get_pending_invitations_for_email.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_audit_write_admits_nobody(self) -> None:
        svc = _svc()
        _closed_gate_new_user(svc)
        svc.db.commit = AsyncMock(side_effect=OperationalError("COMMIT", {}, Exception("down")))
        _service, patched = _invitation_service([_inv()])
        with patched, pytest.raises(OperationalError):
            await _check(svc, email_verified=True, beta_invite_token_hash=TOKEN_HASH)
        svc._redeem_beta_invite.assert_not_awaited()
        svc._record_blocked_signup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_record_lists_five_invitations_and_the_total(self) -> None:
        svc = _svc()
        _closed_gate_new_user(svc)
        rows = [_inv() for _ in range(7)]
        _service, patched = _invitation_service(rows)
        with patched:
            assert await _check(svc, email_verified=True) == AdmissionRecord("workspace_invitation")
        (audit,) = _audits(svc)
        assert [i["id"] for i in audit.user_metadata["invitations"]] == [
            r.public_id for r in rows[:5]
        ]
        assert audit.user_metadata["invitation_count"] == 7


class TestPendingGoogleRowNeedsAVerifiedAddress:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_unverified_google_address_is_not_promoted(self, enabled) -> None:
        svc = _svc()
        _closed_gate_new_user(svc, enabled=enabled)
        svc._promote_pending_google_entry = AsyncMock(return_value=True)
        svc._legacy_check = AsyncMock(
            return_value=RedirectResponse("/login?error=registration_disabled", status_code=303)
        )
        _service, patched = _invitation_service([])
        with patched:
            result = await _check(svc, email_verified=False)

        assert isinstance(result, RedirectResponse)
        svc._promote_pending_google_entry.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_verified_google_address_is_promoted(self, enabled) -> None:
        svc = _svc()
        _closed_gate_new_user(svc, enabled=enabled)
        svc._promote_pending_google_entry = AsyncMock(return_value=True)
        svc._legacy_check = AsyncMock()

        assert await _check(svc, email_verified=True) == AdmissionRecord("allowlist")
        svc._promote_pending_google_entry.assert_awaited_once_with(
            email=ADDRESS, oauth_sub="sub-12345678"
        )
        svc._legacy_check.assert_not_awaited()


class TestGateOff:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["google", "github"])
    @pytest.mark.parametrize("verified", [True, False])
    async def test_legacy_gate_gets_the_invitation_admission_only_when_verified(
        self, provider, verified
    ) -> None:
        svc = _svc()
        _closed_gate_new_user(svc, enabled=False)
        captured: dict = {}

        async def fake_registration_check(email, db, *, user_id, admit_by_invitation):
            captured.update(email=email, db=db, user_id=user_id, admit=admit_by_invitation)
            return "open_registration"

        with patch("api.routes.auth._check_registration_allowed", fake_registration_check):
            await _check(svc, provider=provider, email_verified=verified)

        assert (captured["email"], captured["db"], captured["user_id"]) == (
            ADDRESS,
            svc.db,
            "sub-12345678",
        )
        assert (captured["admit"] is not None) is verified

    @pytest.mark.asyncio
    async def test_legacy_invitation_admission_records_like_the_gate_on_path(self) -> None:
        svc = _svc()
        _closed_gate_new_user(svc, enabled=False)
        row = _inv()
        captured: dict = {}

        async def fake_registration_check(email, db, *, user_id, admit_by_invitation):
            captured["admitted"] = await admit_by_invitation()
            return "workspace_invitation"

        _service, patched = _invitation_service([row])
        with patched, patch("api.routes.auth._check_registration_allowed", fake_registration_check):
            await _check(svc, provider="github", email_verified=True)

        assert captured["admitted"] is True
        (audit,) = _audits(svc)
        assert audit.action == "signup_admitted"
        assert audit.resource == "signup_gate:github"
        assert audit.user_metadata["invitations"][0]["id"] == row.public_id
        svc.db.commit.assert_awaited_once()


class TestAddressedInvitations:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("address", ["", "invítée@example.test"])
    async def test_empty_or_non_ascii_address_makes_no_query(self, address) -> None:
        svc = _svc()
        service, patched = _invitation_service([_inv(email=address)])
        with patched:
            assert await svc._addressed_invitations(address) == []
        service.get_pending_invitations_for_email.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_eligible_rows_for_exactly_this_address_remain(self) -> None:
        svc = _svc()
        keep = _inv(email="Invitee@Example.TEST")
        rows = [
            keep,
            _inv(role="owner"),
            _inv(email=None),
            _inv(email="other@example.test"),
        ]
        _service, patched = _invitation_service(rows)
        with patched:
            assert await svc._addressed_invitations(ADDRESS) == [keep]

    @pytest.mark.asyncio
    async def test_stored_non_ascii_address_is_dropped(self) -> None:
        # U+212A KELVIN SIGN lowers to "k" in Python, not in every database
        # collation: a stored non-ASCII address never admits.
        svc = _svc()
        _service, patched = _invitation_service([_inv(email="\u212aate@example.test")])
        with patched:
            assert await svc._addressed_invitations("kate@example.test") == []


class TestCheckSignupAccessForwards:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("passed,expected", [(None, False), (True, True), (False, False)])
    async def test_email_verified_reaches_the_gate(self, passed, expected) -> None:
        async def fake_get_db():
            yield MagicMock()

        kwargs = {} if passed is None else {"email_verified": passed}
        with (
            patch("services.signup_gate_service.get_db", fake_get_db),
            patch.object(SignupGateService, "decide", AsyncMock(return_value=None)) as gate,
        ):
            await check_signup_access(provider="google", oauth_sub="sub", email=ADDRESS, **kwargs)
        assert gate.await_args.kwargs["email_verified"] is expected
