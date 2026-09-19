"""Closed-beta invite links against real Postgres (Issue #1581).

The unit suites mock the session, so they cannot show the properties that only
exist inside real transactions:

- **quota semantics** — the COUNT that decides "is there a slot": active +
  redeemed occupy one, expired + revoked free it; system admins are uncapped;
- **the inviter row lock** — N parallel creates at quota 4 yield exactly 4;
- **atomic single-use redemption** — N identities racing ONE link through the
  real ``SignupGateService.check_access``: exactly one is admitted, exactly one
  ``signup_allowlist`` row exists, every loser is blocked with nothing written;
- **the two other races** — one identity finishing the sign-in in two tabs (both
  admitted, one row, one spend) and a revoke racing a redeem (never both stamps);
- **hash-only storage** — no column of the persisted row contains the token;
- the gate's no-consume paths, on real rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config.settings import get_settings
from models.auth import AuditLog, User
from models.beta_invite import BetaInvite
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from services.beta_invite_service import BetaInviteService
from services.signup_gate_service import SignupGateService
from utils.datetime import utcnow
from utils.exceptions import (
    BetaInviteAlreadyRedeemedError,
    BetaInviteGoneError,
    BetaInviteQuotaExceededError,
    NotFoundException,
)
from utils.hashing import sha256_hex

_PARALLEL = 12
# A worker that raises before it reaches a sync point would otherwise leave the
# rest waiting until the CI job times out. Every wait carries this timeout.
_SYNC_TIMEOUT = 10.0


def _new_user(role: str = "user") -> User:
    user_id = f"u_{uuid4().hex[:10]}"
    return User(
        email=f"{user_id}@beta-invite.invalid",
        user_id=user_id,
        name="Beta Invite Test",
        role=role,
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
    )


@pytest_asyncio.fixture(loop_scope="session")
async def invites_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "enable_beta_invites", True)
    monkeypatch.setattr(get_settings(), "beta_invite_quota_per_user", 4)


@pytest_asyncio.fixture(loop_scope="session")
async def people(db_session: AsyncSession, invites_on: None) -> AsyncIterator[dict[str, str]]:
    """An inviter, a second user and a system admin; everything they caused is
    removed afterwards so reruns do not accumulate rows."""
    inviter, other, admin = _new_user(), _new_user(), _new_user(role="admin")
    ids = {"inviter": inviter.user_id, "other": other.user_id, "admin": admin.user_id}
    db_session.add_all([inviter, other, admin])
    await db_session.commit()

    yield ids

    await db_session.rollback()
    user_ids = list(ids.values())
    entry_subs = list(
        await db_session.scalars(
            select(SignupAllowlistEntry.subject_id).where(
                SignupAllowlistEntry.added_by_user_id.in_(user_ids)
            )
        )
    )
    await db_session.execute(delete(AuditLog).where(AuditLog.resource.like("beta_invite:%")))
    await db_session.execute(delete(AuditLog).where(AuditLog.user_id.in_(entry_subs + user_ids)))
    await db_session.execute(delete(AuditLog).where(AuditLog.user_id.like("sub-%")))
    await db_session.execute(
        delete(SignupAllowlistEntry).where(SignupAllowlistEntry.added_by_user_id.in_(user_ids))
    )
    # CASCADE removes the users' beta_invites.
    await db_session.execute(delete(User).where(User.user_id.in_(user_ids)))
    await db_session.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def closed_gate(db_session: AsyncSession) -> AsyncIterator[None]:
    """Gate ON, mode=manual — the closed-beta configuration. Restored afterwards."""
    svc = SignupGateService(db_session)
    config = await svc.get_config()
    previous = (config.enabled, config.mode)
    await svc.update_config(enabled=True, mode="manual")
    yield
    await db_session.rollback()
    await db_session.execute(
        update(SignupGateConfig)
        .where(SignupGateConfig.id == 1)
        .values(enabled=previous[0], mode=previous[1])
    )
    await db_session.commit()


def _token(url: str) -> str:
    return url.rsplit("/", 1)[1]


async def _mint(session: AsyncSession, user_id: str) -> tuple[UUID, str]:
    minted = await BetaInviteService(session).create(
        user_id=user_id, user_email=f"{user_id}@beta-invite.invalid"
    )
    return minted.invite.id, _token(minted.url)


async def _status(session: AsyncSession, invite_id: UUID) -> str:
    session.expire_all()
    invite = await session.get(BetaInvite, invite_id)
    assert invite is not None
    return invite.status


async def _gate(session: AsyncSession, *, sub: str, token: str | None, provider: str = "google"):
    return await SignupGateService(session).check_access(
        provider=provider,  # type: ignore[arg-type]
        oauth_sub=sub,
        email=f"{sub}@invitee.invalid",
        username="octocat" if provider == "github" else f"{sub}@invitee.invalid",
        ip_address="203.0.113.7",
        user_agent="pytest",
        beta_invite_token_hash=sha256_hex(token) if token else None,
    )


async def _until_a_backend_waits_on_a_lock(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Return once some backend of this database is blocked on a lock.

    ``pg_stat_activity`` is snapshotted per transaction, hence the rollback
    between polls.
    """
    async with session_maker() as probe:
        async with asyncio.timeout(_SYNC_TIMEOUT):
            while not await probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ):
                await probe.rollback()
                await asyncio.sleep(0.02)


class TestQuota:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_fifth_is_refused_and_revoke_or_expiry_frees_a_slot(
        self, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        svc = BetaInviteService(db_session)
        inviter = people["inviter"]
        minted = [await _mint(db_session, inviter) for _ in range(4)]

        with pytest.raises(BetaInviteQuotaExceededError):
            await _mint(db_session, inviter)
        await db_session.rollback()  # release the inviter row lock

        summary = await svc.get_summary(inviter)
        assert (summary.quota, summary.used, summary.remaining) == (4, 4, 0)
        assert [i.status for i in summary.invites] == ["active"] * 4

        # Revoking frees the slot.
        await svc.revoke(user_id=inviter, invite_id=minted[0][0], user_email="x@y.invalid")
        assert (await svc.get_summary(inviter)).remaining == 1
        await _mint(db_session, inviter)

        # So does expiry.
        await db_session.execute(
            update(BetaInvite)
            .where(BetaInvite.id == minted[1][0])
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await db_session.commit()
        assert (await svc.get_summary(inviter)).remaining == 1
        await _mint(db_session, inviter)

        # A REDEEMED invite keeps its slot even long after its expiry date.
        await db_session.execute(
            update(BetaInvite)
            .where(BetaInvite.id == minted[2][0])
            .values(redeemed_at=utcnow(), expires_at=utcnow() - timedelta(days=30))
        )
        await db_session.commit()
        summary = await svc.get_summary(inviter)
        assert (summary.used, summary.remaining) == (4, 0)
        assert sorted(i.status for i in summary.invites) == [
            "active",
            "active",
            "active",
            "expired",
            "redeemed",
            "revoked",
        ]
        with pytest.raises(BetaInviteQuotaExceededError):
            await _mint(db_session, inviter)
        await db_session.rollback()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_system_admin_is_uncapped(
        self, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        for _ in range(6):
            await _mint(db_session, people["admin"])
        summary = await BetaInviteService(db_session).get_summary(people["admin"])
        assert (summary.quota, summary.used, summary.remaining) == (None, 6, None)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_parallel_creates_cannot_exceed_the_quota(
        self, async_engine, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        async def attempt() -> bool:
            async with session_maker() as session:
                try:
                    await _mint(session, people["inviter"])
                except BetaInviteQuotaExceededError:
                    return False
                return True

        results = await asyncio.gather(*[attempt() for _ in range(_PARALLEL)])

        assert sum(results) == 4
        count = await db_session.scalar(
            select(func.count(BetaInvite.id)).where(BetaInvite.inviter_user_id == people["inviter"])
        )
        assert count == 4


class TestStorageAndRevoke:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_database_holds_only_the_hash(
        self, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        invite_id, token = await _mint(db_session, people["inviter"])

        row = (
            await db_session.execute(
                text("SELECT * FROM beta_invites WHERE id = :id"), {"id": invite_id}
            )
        ).one()
        assert row.token_hash == sha256_hex(token)
        assert all(token not in str(value) for value in row)

        audit = (
            await db_session.execute(
                text("SELECT * FROM audit_logs WHERE resource = :r"),
                {"r": f"beta_invite:{invite_id}"},
            )
        ).one()
        assert audit.action == "beta_invite.created"
        assert all(token not in str(v) and sha256_hex(token) not in str(v) for v in audit)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_revoke_rules(self, db_session: AsyncSession, people: dict[str, str]) -> None:
        svc = BetaInviteService(db_session)
        inviter, other = people["inviter"], people["other"]
        invite_id, _ = await _mint(db_session, inviter)

        # Someone else's invite and an unknown id are the same 404.
        with pytest.raises(NotFoundException):
            await svc.revoke(user_id=other, invite_id=invite_id, user_email="o@y.invalid")
        with pytest.raises(NotFoundException):
            await svc.revoke(user_id=inviter, invite_id=uuid4(), user_email="x@y.invalid")
        assert await _status(db_session, invite_id) == "active"

        # Own unused invite: revoked; a retry is a no-op with no second audit row.
        await svc.revoke(user_id=inviter, invite_id=invite_id, user_email="x@y.invalid")
        await svc.revoke(user_id=inviter, invite_id=invite_id, user_email="x@y.invalid")
        assert await _status(db_session, invite_id) == "revoked"
        revoked_audits = await db_session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.resource == f"beta_invite:{invite_id}",
                AuditLog.action == "beta_invite.revoked",
            )
        )
        assert revoked_audits == 1

        # A redeemed invite cannot be revoked.
        used_id, _ = await _mint(db_session, inviter)
        await db_session.execute(
            update(BetaInvite).where(BetaInvite.id == used_id).values(redeemed_at=utcnow())
        )
        await db_session.commit()
        with pytest.raises(BetaInviteAlreadyRedeemedError):
            await svc.revoke(user_id=inviter, invite_id=used_id, user_email="x@y.invalid")
        assert await _status(db_session, used_id) == "redeemed"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_preview(self, db_session: AsyncSession, people: dict[str, str]) -> None:
        svc = BetaInviteService(db_session)
        inviter = people["inviter"]
        active_id, active = await _mint(db_session, inviter)
        revoked_id, revoked = await _mint(db_session, inviter)
        expired_id, expired = await _mint(db_session, inviter)
        await svc.revoke(user_id=inviter, invite_id=revoked_id, user_email="x@y.invalid")
        await db_session.execute(
            update(BetaInvite)
            .where(BetaInvite.id == expired_id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await db_session.commit()

        assert (await svc.preview(active)).id == active_id
        # Previewing is read-only.
        assert await _status(db_session, active_id) == "active"
        with pytest.raises(NotFoundException):
            await svc.preview("not-a-real-token-" + "x" * 30)
        with pytest.raises(NotFoundException):
            await svc.preview(revoked)
        with pytest.raises(BetaInviteGoneError):
            await svc.preview(expired)


class TestGateRedemption:
    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("provider", ["google", "github"])
    async def test_valid_invite_admits_once_and_records_the_allowlist_row(
        self, db_session: AsyncSession, people: dict[str, str], closed_gate: None, provider: str
    ) -> None:
        invite_id, token = await _mint(db_session, people["inviter"])
        sub = f"sub-{uuid4().hex[:12]}"

        # Without the invite this identity is blocked.
        assert isinstance(
            await _gate(db_session, sub=sub, token=None, provider=provider), RedirectResponse
        )
        assert await _gate(db_session, sub=sub, token=token, provider=provider) is None

        entry = await db_session.scalar(
            select(SignupAllowlistEntry).where(SignupAllowlistEntry.subject_id == sub)
        )
        assert entry is not None
        assert (entry.provider, entry.source, entry.state) == (provider, "beta_invite", "active")
        assert entry.subject_label == f"{sub}@invitee.invalid"
        assert entry.added_by_user_id == people["inviter"]
        entry_id = entry.id  # snapshot: expire_all() below would make this a lazy load

        db_session.expire_all()
        invite = await db_session.get(BetaInvite, invite_id)
        assert invite is not None
        assert invite.status == "redeemed"
        assert invite.redeemed_allowlist_entry_id == entry_id

        audit = await db_session.scalar(
            select(AuditLog).where(
                AuditLog.resource == f"beta_invite:{invite_id}",
                AuditLog.action == "beta_invite.redeemed",
            )
        )
        assert audit is not None
        assert audit.user_id == sub
        assert "invitee.invalid" not in repr(vars(audit))

        # Single use: the same token admits nobody else, and previews as gone.
        assert isinstance(
            await _gate(db_session, sub=f"sub-{uuid4().hex[:12]}", token=token, provider=provider),
            RedirectResponse,
        )
        with pytest.raises(BetaInviteGoneError):
            await BetaInviteService(db_session).preview(token)

        # Regression (#1581): the invitee's own row keeps admitting them with no
        # token at all — _is_allowlisted honours source='beta_invite'. They have
        # no users row here, so this is NOT the existing-user path.
        assert await _gate(db_session, sub=sub, token=None, provider=provider) is None

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("provider", ["google", "github"])
    async def test_dead_tokens_do_not_pass_and_write_nothing(
        self, db_session: AsyncSession, people: dict[str, str], closed_gate: None, provider: str
    ) -> None:
        svc = BetaInviteService(db_session)
        inviter = people["inviter"]
        revoked_id, revoked = await _mint(db_session, inviter)
        expired_id, expired = await _mint(db_session, inviter)
        await svc.revoke(user_id=inviter, invite_id=revoked_id, user_email="x@y.invalid")
        await db_session.execute(
            update(BetaInvite)
            .where(BetaInvite.id == expired_id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await db_session.commit()

        for token in (revoked, expired, "unknown-token-" + "z" * 30):
            sub = f"sub-{uuid4().hex[:12]}"
            assert isinstance(
                await _gate(db_session, sub=sub, token=token, provider=provider), RedirectResponse
            )
            rows = await db_session.scalar(
                select(func.count(SignupAllowlistEntry.id)).where(
                    SignupAllowlistEntry.subject_id == sub
                )
            )
            assert rows == 0

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("provider", ["google", "github"])
    async def test_existing_user_and_open_gate_leave_the_token_unused(
        self, db_session: AsyncSession, people: dict[str, str], closed_gate: None, provider: str
    ) -> None:
        invite_id, token = await _mint(db_session, people["inviter"])

        # An existing user logging in with ?invite= is a login, not a redemption.
        assert await _gate(db_session, sub=people["other"], token=token, provider=provider) is None
        assert await _status(db_session, invite_id) == "active"

        # Gate disabled -> the legacy env gate decides; never the invite path.
        await SignupGateService(db_session).update_config(enabled=False, mode="manual")
        await _gate(db_session, sub=f"sub-{uuid4().hex[:12]}", token=token, provider=provider)
        assert await _status(db_session, invite_id) == "active"

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("provider", ["google", "github"])
    async def test_kill_switch_stops_redemption(
        self,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
        monkeypatch: pytest.MonkeyPatch,
        provider: str,
    ) -> None:
        invite_id, token = await _mint(db_session, people["inviter"])
        monkeypatch.setattr(get_settings(), "enable_beta_invites", False)

        result = await _gate(
            db_session, sub=f"sub-{uuid4().hex[:12]}", token=token, provider=provider
        )

        assert isinstance(result, RedirectResponse)
        assert await _status(db_session, invite_id) == "active"

    async def _race(
        self, async_engine, monkeypatch: pytest.MonkeyPatch, token: str, subs: list[str]
    ) -> list[bool]:
        """Race ``subs`` for one link, all released at the claim simultaneously.

        The barrier sits right after the ``find_redeemable`` pre-check, so every
        worker has already seen the invite as redeemable before any of them
        claims it. That is the worst case, made deterministic: the pre-check
        cannot save anyone, only the guarded claim ``UPDATE`` can.
        """
        barrier = asyncio.Barrier(len(subs))
        real_find = BetaInviteService.find_redeemable

        async def find_then_wait(self_, token_hash, now):
            invite = await real_find(self_, token_hash, now)
            await asyncio.wait_for(barrier.wait(), _SYNC_TIMEOUT)
            return invite

        monkeypatch.setattr(BetaInviteService, "find_redeemable", find_then_wait)
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        async def attempt(sub: str) -> bool:
            async with session_maker() as session:
                return await _gate(session, sub=sub, token=token) is None

        return list(await asyncio.gather(*[attempt(sub) for sub in subs]))

    @pytest.mark.asyncio(loop_scope="session")
    async def test_negative_control_without_the_guard_the_link_admits_many(
        self,
        async_engine,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Strip ``redeemed_at IS NULL`` from the claim and the same race admits
        more than one identity — proof the positive test below is genuinely racy
        and is held by the guard, not by lucky scheduling."""
        import services.beta_invite_service as svc_module

        def unguarded(*, token_hash, allowlist_entry_id, now):
            return (
                update(BetaInvite)
                .where(BetaInvite.token_hash == token_hash)
                .values(redeemed_at=now, redeemed_allowlist_entry_id=allowlist_entry_id)
            )

        monkeypatch.setattr(svc_module, "build_redeem_update", unguarded)
        _, token = await _mint(db_session, people["inviter"])
        subs = [f"sub-{uuid4().hex[:12]}" for _ in range(_PARALLEL)]

        results = await self._race(async_engine, monkeypatch, token, subs)

        assert sum(results) > 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_double_redeem_race_admits_exactly_one(
        self,
        async_engine,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        invite_id, token = await _mint(db_session, people["inviter"])
        subs = [f"sub-{uuid4().hex[:12]}" for _ in range(_PARALLEL)]

        results = await self._race(async_engine, monkeypatch, token, subs)

        assert sum(results) == 1, f"single-use link admitted {sum(results)} identities"
        winner = subs[results.index(True)]
        entries = list(
            await db_session.scalars(
                select(SignupAllowlistEntry).where(SignupAllowlistEntry.subject_id.in_(subs))
            )
        )
        # Every loser's allowlist INSERT was rolled back with its failed claim.
        assert [e.subject_id for e in entries] == [winner]
        winning_entry_id = entries[0].id  # snapshot before expire_all()
        db_session.expire_all()
        invite = await db_session.get(BetaInvite, invite_id)
        assert invite is not None
        assert invite.redeemed_allowlist_entry_id == winning_entry_id

    @pytest.mark.asyncio(loop_scope="session")
    async def test_same_identity_in_two_tabs_is_admitted_twice_and_written_once(
        self,
        async_engine,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One person opens the link in two tabs and finishes both sign-ins.

        Both tabs pass the pre-check and INSERT the same ``(provider, subject_id,
        source)`` allowlist row. The unique constraint serializes them: the second
        gets ``IntegrityError``, rolls back and is answered from the winner's row.
        Neither tab is bounced and the link is spent exactly once.
        """
        invite_id, token = await _mint(db_session, people["inviter"])
        sub = f"sub-{uuid4().hex[:12]}"

        results = await self._race(async_engine, monkeypatch, token, [sub, sub])

        assert results == [True, True]
        rows = await db_session.scalar(
            select(func.count(SignupAllowlistEntry.id)).where(
                SignupAllowlistEntry.subject_id == sub
            )
        )
        assert rows == 1
        assert await _status(db_session, invite_id) == "redeemed"

    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("first", ["redeem", "revoke"])
    async def test_revoke_racing_a_redeem_never_leaves_both_stamps(
        self,
        async_engine,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
        first: str,
    ) -> None:
        """The inviter revokes while the invitee is mid-callback.

        Revoke (``redeemed_at IS NULL``) and the claim (``revoked_at IS NULL``)
        are mirrored guarded UPDATEs on one row: whichever commits first wins and
        the other matches nothing.

        Both orders, made deterministic: the ``first`` side runs up to its COMMIT
        and stops there holding the row lock; only then does the other side issue
        its own UPDATE, which blocks on that lock; and only when Postgres reports
        a backend waiting on a lock does the first side commit. The blocked
        UPDATE then re-checks its WHERE against the committed row — the step the
        whole design rests on — and must match nothing.
        """
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        inviter = people["inviter"]
        invite_id, token = await _mint(db_session, inviter)
        sub = f"sub-{uuid4().hex[:12]}"
        holding = asyncio.Event()

        def hold_commit_until_the_rival_is_blocked(session: AsyncSession) -> None:
            real_commit = session.commit

            async def commit() -> None:
                holding.set()
                await _until_a_backend_waits_on_a_lock(session_maker)
                await real_commit()

            session.commit = commit  # type: ignore[method-assign]

        async def redeem() -> bool:
            async with session_maker() as session:
                if first == "redeem":
                    hold_commit_until_the_rival_is_blocked(session)
                else:
                    await asyncio.wait_for(holding.wait(), _SYNC_TIMEOUT)
                return await _gate(session, sub=sub, token=token) is None

        async def revoke() -> bool:
            async with session_maker() as session:
                if first == "revoke":
                    hold_commit_until_the_rival_is_blocked(session)
                else:
                    await asyncio.wait_for(holding.wait(), _SYNC_TIMEOUT)
                try:
                    await BetaInviteService(session).revoke(
                        user_id=inviter, invite_id=invite_id, user_email="x@y.invalid"
                    )
                except BetaInviteAlreadyRedeemedError:
                    return False
                return True

        admitted, revoked = await asyncio.gather(redeem(), revoke())

        # The side that held the lock won, and each side was told the truth.
        assert (admitted, revoked) == ((True, False) if first == "redeem" else (False, True))
        db_session.expire_all()
        invite = await db_session.get(BetaInvite, invite_id)
        assert invite is not None
        assert not (invite.redeemed_at and invite.revoked_at), "invite is redeemed AND revoked"
        assert invite.status == ("redeemed" if first == "redeem" else "revoked")
        # The allowlist row exists exactly when the redeem won: the losing
        # redeemer's INSERT went away with its failed claim.
        rows = await db_session.scalar(
            select(func.count(SignupAllowlistEntry.id)).where(
                SignupAllowlistEntry.subject_id == sub
            )
        )
        assert rows == (1 if first == "redeem" else 0)
