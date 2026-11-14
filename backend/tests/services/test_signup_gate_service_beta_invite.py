"""Signup gate × closed-beta invite links (Issue #1581) — unit level, no DB.

Same idiom as ``test_signup_gate_service.py``: the gate's internal lookups are
patched so each test pins one decision. What is pinned here:

- the invite is tried ONLY where the gate would otherwise block a new user, so
  existing users, the first user, allowlisted identities, a promoted Google
  pending row and the disabled gate (open registration / legacy env gate) never
  consume a token;
- ``ENABLE_BETA_INVITES=false`` makes a presented token completely inert;
- a redemption that loses the single-use race rolls back and blocks;
- ``_allowlist_admission`` honours ``source='beta_invite'`` under ``mode='manual'``.

The real transaction behaviour (allowlist row + claim + audit in one commit, the
double-redeem race) is in ``tests/integration/test_beta_invite_signup_gate_db.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError

from auth.admission import AdmissionRecord
from models.auth import AuditLog
from models.signup_gate import SignupAllowlistEntry
from services.signup_gate_service import SignupGateService, check_signup_access

TOKEN_HASH = "c" * 64


# #2803: a redemption answers how the account is admitted.
REDEEMED = AdmissionRecord(
    "beta_invite",
    beta_invite_id=UUID("00000000-0000-4000-8000-000000002803"),
    inviter_user_id="inviter-sub",
)


def _svc() -> SignupGateService:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return SignupGateService(db)


def _config(*, enabled: bool, mode: str = "manual") -> SimpleNamespace:
    return SimpleNamespace(enabled=enabled, mode=mode)


def _closed_gate_new_user(svc: SignupGateService) -> None:
    """Patch the lookups so the only thing between the caller and a block is the invite."""
    svc._load_config = AsyncMock(return_value=_config(enabled=True))
    svc._is_existing_user = AsyncMock(return_value=False)
    svc._is_first_user = AsyncMock(return_value=False)
    svc._allowlist_admission = AsyncMock(return_value=None)
    svc._promote_pending_google_entry = AsyncMock(return_value=False)
    svc._admit_by_workspace_invitation = AsyncMock(return_value=False)
    svc._record_blocked_signup = AsyncMock()


@pytest.fixture
def invites_enabled(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "enable_beta_invites", True)


class TestInvitePathPlacement:
    @pytest.mark.asyncio
    async def test_valid_invite_passes_a_new_user_through_the_closed_gate(self):
        svc = _svc()
        _closed_gate_new_user(svc)
        svc._redeem_beta_invite = AsyncMock(return_value=REDEEMED)

        result = await svc.decide(
            provider="github",
            oauth_sub="1234",
            email="new@example.test",
            username="octocat",
            ip_address="203.0.113.7",
            user_agent="ua",
            beta_invite_token_hash=TOKEN_HASH,
        )

        # #2803: the redemption's admission (invite id and inviter) is the answer.
        assert result is REDEEMED
        svc._redeem_beta_invite.assert_awaited_once_with(
            token_hash=TOKEN_HASH,
            provider="github",
            oauth_sub="1234",
            email="new@example.test",
            username="octocat",
            ip_address="203.0.113.7",
            user_agent="ua",
        )
        svc._record_blocked_signup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dead_invite_blocks_exactly_like_no_invite(self):
        """Expired / revoked / redeemed / unknown all surface as redeem=False."""
        svc = _svc()
        _closed_gate_new_user(svc)
        svc._redeem_beta_invite = AsyncMock(return_value=None)

        result = await svc.check_access(
            provider="google",
            oauth_sub="108276939729829363",
            email="new@example.test",
            username="new@example.test",
            beta_invite_token_hash=TOKEN_HASH,
        )

        assert isinstance(result, RedirectResponse)
        assert "/signup-blocked" in result.headers["location"]
        svc._record_blocked_signup.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "admitting_lookup",
        [
            "_is_existing_user",
            "_is_first_user",
            "_allowlist_admission",
            "_promote_pending_google_entry",
            "_admit_by_workspace_invitation",
        ],
    )
    async def test_anyone_admitted_without_the_invite_keeps_the_token(self, admitting_lookup):
        """An existing user logging in with ``invite=`` must not burn the link."""
        svc = _svc()
        _closed_gate_new_user(svc)
        admits = (
            AdmissionRecord("allowlist") if admitting_lookup == "_allowlist_admission" else True
        )
        setattr(svc, admitting_lookup, AsyncMock(return_value=admits))
        svc._redeem_beta_invite = AsyncMock(return_value=REDEEMED)

        result = await svc.check_access(
            provider="google",
            oauth_sub="108276939729829363",
            email="someone@example.test",
            username="someone@example.test",
            beta_invite_token_hash=TOKEN_HASH,
            # The address-matched admissions (#2785) need a verified address.
            email_verified=True,
        )

        assert result is None
        svc._redeem_beta_invite.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("legacy_blocks", [False, True])
    async def test_disabled_gate_never_consumes_the_token(self, legacy_blocks):
        """Gate off = the legacy env gate owns the decision (open registration
        when ``ALLOW_REGISTRATION=true``). The token is left alone either way,
        and the admission is open registration, never the invite (#2803)."""
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=False))
        svc._allowlist_admission = AsyncMock(return_value=None)
        legacy = RedirectResponse("/login?error=registration_disabled", status_code=303)
        svc._legacy_check = AsyncMock(return_value=legacy if legacy_blocks else "open_registration")
        svc._redeem_beta_invite = AsyncMock(return_value=REDEEMED)

        result = await svc.decide(
            provider="github",
            oauth_sub="1234",
            email="new@example.test",
            username="octocat",
            beta_invite_token_hash=TOKEN_HASH,
        )

        if legacy_blocks:
            assert result is legacy
        else:
            assert result == AdmissionRecord("open_registration")
        svc._redeem_beta_invite.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_login_admitted_by_invite_is_not_reblocked_by_the_legacy_gate(self):
        """``_check_registration_allowed`` only runs via ``_legacy_check`` when the
        DB gate is disabled. Under an enabled gate an invite admission must return
        straight away — never fall through to a legacy gate that would block
        (``ALLOW_REGISTRATION=false`` is the closed-beta default)."""
        svc = _svc()
        _closed_gate_new_user(svc)
        svc._redeem_beta_invite = AsyncMock(return_value=REDEEMED)
        svc._legacy_check = AsyncMock(
            return_value=RedirectResponse("/login?error=registration_disabled", status_code=303)
        )

        result = await svc.check_access(
            provider="github",
            oauth_sub="1234",
            email="new@example.test",
            username="octocat",
            beta_invite_token_hash=TOKEN_HASH,
        )

        assert result is None
        svc._legacy_check.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wrapper_forwards_the_hash(self):
        captured = {}

        async def fake_decide(self, **kwargs):
            captured.update(kwargs)
            return REDEEMED

        async def fake_get_db():
            yield MagicMock()

        with (
            patch("services.signup_gate_service.get_db", fake_get_db),
            patch.object(SignupGateService, "decide", fake_decide),
        ):
            answer = await check_signup_access(
                provider="github",
                oauth_sub="1234",
                email="new@example.test",
                beta_invite_token_hash=TOKEN_HASH,
            )

        assert captured["beta_invite_token_hash"] == TOKEN_HASH
        # #2803: the wrapper hands back the gate's admission as is.
        assert answer is REDEEMED


def _redeemable_invite() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        inviter_user_id="inviter-1",
        expires_at=datetime(2026, 9, 8) + timedelta(days=1),
    )


def _redeem_kwargs(**overrides) -> dict:
    kwargs = {
        "token_hash": TOKEN_HASH,
        "provider": "google",
        "oauth_sub": "108276939729829363",
        "email": "New.Person@example.test",
        "username": "New.Person@example.test",
        "ip_address": "203.0.113.7",
        "user_agent": "ua",
    }
    kwargs.update(overrides)
    return kwargs


class TestRedeem:
    @pytest.mark.asyncio
    async def test_flag_off_is_inert(self, monkeypatch):
        """The kill switch stops redemption too — the invite table is never read."""
        from config.settings import get_settings

        monkeypatch.setattr(get_settings(), "enable_beta_invites", False)
        svc = _svc()

        with patch("services.signup_gate_service.BetaInviteService") as invites_cls:
            assert await svc._redeem_beta_invite(**_redeem_kwargs()) is None

        invites_cls.assert_not_called()
        svc.db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_token_is_inert(self, invites_enabled):
        svc = _svc()

        with patch("services.signup_gate_service.BetaInviteService") as invites_cls:
            assert await svc._redeem_beta_invite(**_redeem_kwargs(token_hash=None)) is None

        invites_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_dead_token_writes_nothing(self, invites_enabled):
        svc = _svc()

        with patch("services.signup_gate_service.BetaInviteService") as invites_cls:
            invites_cls.return_value.find_redeemable = AsyncMock(return_value=None)
            assert await svc._redeem_beta_invite(**_redeem_kwargs()) is None

        svc.db.add.assert_not_called()
        svc.db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_writes_allowlist_row_claim_and_audit_in_one_commit(
        self, invites_enabled
    ):
        svc = _svc()
        invite = _redeemable_invite()

        with patch("services.signup_gate_service.BetaInviteService") as invites_cls:
            invites_cls.return_value.find_redeemable = AsyncMock(return_value=invite)
            invites_cls.return_value.mark_redeemed = AsyncMock(return_value=True)
            # #2803: the admission names the invite and its inviter.
            assert await svc._redeem_beta_invite(**_redeem_kwargs()) == AdmissionRecord(
                "beta_invite", invite.id, "inviter-1"
            )
            claim = invites_cls.return_value.mark_redeemed.await_args.kwargs

        added = [c.args[0] for c in svc.db.add.call_args_list]
        entry = next(o for o in added if isinstance(o, SignupAllowlistEntry))
        # Keyed on the immutable IdP identity; the e-mail is the label only (#655).
        assert (entry.provider, entry.subject_id) == ("google", "108276939729829363")
        assert entry.subject_label == "New.Person@example.test"
        assert entry.source == "beta_invite"
        assert entry.state == "active"
        assert entry.added_by_user_id == "inviter-1"
        # Deprecated NOT-NULL columns filled the way the admin route fills a Google row.
        assert entry.github_user_id == "google:108276939729829363"
        assert entry.github_username == "New.Person@example.test"

        assert claim["token_hash"] == TOKEN_HASH
        assert claim["allowlist_entry_id"] == entry.id

        audit = next(o for o in added if isinstance(o, AuditLog))
        assert audit.action == "beta_invite.redeemed"
        assert audit.resource == f"beta_invite:{invite.id}"
        assert audit.user_id == "108276939729829363"
        # E-mail HMAC'd, never plaintext; the token hash never reaches the row.
        serialized = repr(vars(audit))
        assert "example.test" not in serialized
        assert TOKEN_HASH not in serialized
        assert len(audit.new_value_hash) == 64

        svc.db.commit.assert_awaited_once()
        svc.db.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_github_row_fills_legacy_columns_with_id_and_login(self, invites_enabled):
        svc = _svc()

        with patch("services.signup_gate_service.BetaInviteService") as invites_cls:
            invites_cls.return_value.find_redeemable = AsyncMock(return_value=_redeemable_invite())
            invites_cls.return_value.mark_redeemed = AsyncMock(return_value=True)
            await svc._redeem_beta_invite(
                **_redeem_kwargs(provider="github", oauth_sub="583231", username="octocat")
            )

        entry = next(
            c.args[0]
            for c in svc.db.add.call_args_list
            if isinstance(c.args[0], SignupAllowlistEntry)
        )
        assert entry.subject_id == "583231"
        assert entry.github_user_id == "583231"
        assert entry.github_username == "octocat"

    @pytest.mark.asyncio
    async def test_losing_the_single_use_race_rolls_back_and_blocks(self, invites_enabled):
        svc = _svc()

        with patch("services.signup_gate_service.BetaInviteService") as invites_cls:
            invites_cls.return_value.find_redeemable = AsyncMock(return_value=_redeemable_invite())
            invites_cls.return_value.mark_redeemed = AsyncMock(return_value=False)
            assert await svc._redeem_beta_invite(**_redeem_kwargs()) is None

        svc.db.rollback.assert_awaited_once()
        svc.db.commit.assert_not_awaited()
        assert not any(isinstance(c.args[0], AuditLog) for c in svc.db.add.call_args_list)

    @pytest.mark.asyncio
    async def test_same_identity_in_two_tabs_defers_to_the_winning_row(self, invites_enabled):
        """The second tab's allowlist INSERT collides on (provider, subject, source).
        Roll back, then answer from the allowlist — the winner's row IS the grant."""
        svc = _svc()
        svc.db.flush = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("dup")))
        svc._allowlist_admission = AsyncMock(return_value=REDEEMED)

        with patch("services.signup_gate_service.BetaInviteService") as invites_cls:
            invites_cls.return_value.find_redeemable = AsyncMock(return_value=_redeemable_invite())
            invites_cls.return_value.mark_redeemed = AsyncMock(return_value=True)
            # #2803: the answer is the winning row's admission (its invite).
            assert await svc._redeem_beta_invite(**_redeem_kwargs()) is REDEEMED
            invites_cls.return_value.mark_redeemed.assert_not_awaited()

        svc.db.rollback.assert_awaited_once()
        svc._allowlist_admission.assert_awaited_once_with("google", "108276939729829363", "manual")


class TestIsAllowlistedHonoursBetaInvite:
    """The most likely bug (#1581): without this a redeemed invitee only passes
    via ``_is_existing_user`` — and is locked out if user creation failed after
    the invite was consumed (e.g. ``email_in_use``)."""

    async def _sql_for(self, mode: str) -> str:
        svc = _svc()
        captured = {}

        async def fake_execute(stmt):
            captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            result = MagicMock()
            result.first = MagicMock(return_value=None)
            return result

        svc.db.execute = fake_execute
        await svc._allowlist_admission("github", "1234", mode)
        return captured["sql"]

    @pytest.mark.asyncio
    async def test_manual_mode_accepts_manual_and_beta_invite_rows(self):
        sql = await self._sql_for("manual")
        assert "signup_allowlist.source IN ('manual', 'beta_invite')" in sql
        assert "signup_allowlist.state = 'active'" in sql

    @pytest.mark.asyncio
    async def test_sponsors_mode_still_excludes_beta_invite_rows(self):
        sql = await self._sql_for("github_sponsors")
        where = sql.split("ORDER BY")[0]
        assert "signup_allowlist.source = 'github_sponsors'" in where
        assert "beta_invite" not in where

    @pytest.mark.asyncio
    async def test_a_beta_invite_row_is_preferred(self):
        """#2803: the identity's beta_invite row decides the admission first."""
        sql = await self._sql_for("manual")
        order = sql.split("ORDER BY")[1]
        assert order.strip().startswith("signup_allowlist.source = 'beta_invite' DESC")


class TestAllowlistAdmission:
    """#2803: the matched row, not the gate step, says how the account is admitted."""

    @staticmethod
    def _svc_matching(row) -> SignupGateService:
        svc = _svc()
        result = MagicMock()
        result.first = MagicMock(return_value=row)
        svc.db.execute = AsyncMock(return_value=result)
        return svc

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["manual", "github_sponsors"])
    async def test_any_other_row_is_allowlist(self, source):
        svc = self._svc_matching(SimpleNamespace(id=uuid4(), source=source))

        with patch("services.signup_gate_service.BetaInviteService") as invites_cls:
            assert await svc._allowlist_admission("github", "1", "both") == AdmissionRecord(
                "allowlist"
            )
        invites_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_beta_invite_row_names_its_invite(self):
        entry_id = uuid4()
        svc = self._svc_matching(SimpleNamespace(id=entry_id, source="beta_invite"))
        invite_id = uuid4()

        with patch("services.signup_gate_service.BetaInviteService") as invites_cls:
            invites_cls.return_value.redeemed_with_entry = AsyncMock(
                return_value=(invite_id, "inviter-1")
            )
            answer = await svc._allowlist_admission("google", "1", "manual")

        assert answer == AdmissionRecord("beta_invite", invite_id, "inviter-1")
        invites_cls.return_value.redeemed_with_entry.assert_awaited_once_with(entry_id)

    @pytest.mark.asyncio
    async def test_a_beta_invite_row_whose_invite_is_gone(self):
        svc = self._svc_matching(SimpleNamespace(id=uuid4(), source="beta_invite"))

        with patch("services.signup_gate_service.BetaInviteService") as invites_cls:
            invites_cls.return_value.redeemed_with_entry = AsyncMock(return_value=None)
            answer = await svc._allowlist_admission("google", "1", "manual")

        assert answer == AdmissionRecord("beta_invite")
