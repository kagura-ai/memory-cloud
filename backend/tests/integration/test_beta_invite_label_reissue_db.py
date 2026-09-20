"""Beta invite labels, reissue, usage breakdown and ``redeemed_email`` against
real Postgres (Issue #1595).

What only real transactions can show:

- **the breakdown** — ``active + redeemed == used`` for every mix of rows,
  including hand-edited ones, because both come from one aggregate;
- **reissue is one transaction** — one revoked row + one new active row with the
  same label, quota standing unchanged; a double-click mints nothing; an
  EXPIRED row at the cap is refused AND its revoke is rolled back;
- **``redeemed_email`` follows the live ``users`` row** — resolved through the
  identity the real ``RoleManager.ensure_user`` writes, never through the
  allowlist ``subject_label`` snapshot, so account erasure makes it disappear;
- **nothing private is logged or audited** — the label and the admitted e-mail
  exist in ``beta_invites`` / ``users`` and in the inviter's responses only.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import structlog
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.roles import RoleManager
from config.settings import get_settings
from models.auth import AuditLog, User
from models.beta_invite import BetaInvite
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from services.beta_invite_service import BetaInviteService
from services.signup_gate_service import SignupGateService
from utils.datetime import utcnow
from utils.exceptions import (
    BetaInviteAlreadyRedeemedError,
    BetaInviteAlreadyRevokedError,
    BetaInviteQuotaExceededError,
    NotFoundException,
)
from utils.hashing import sha256_hex

_PARALLEL = 8
LABEL = "Dana <dana@friend.invalid>"


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
    """An inviter, a second user and a system admin; everything they (and the
    invitees they admitted) caused is removed afterwards."""
    inviter, other, admin = _new_user(), _new_user(), _new_user(role="admin")
    ids = {"inviter": inviter.user_id, "other": other.user_id, "admin": admin.user_id}
    db_session.add_all([inviter, other, admin])
    await db_session.commit()

    yield ids

    await db_session.rollback()
    user_ids = list(ids.values())
    await db_session.execute(delete(AuditLog).where(AuditLog.resource.like("beta_invite:%")))
    await db_session.execute(delete(AuditLog).where(AuditLog.user_id.in_(user_ids)))
    await db_session.execute(delete(AuditLog).where(AuditLog.user_id.like("sub-%")))
    await db_session.execute(
        delete(SignupAllowlistEntry).where(SignupAllowlistEntry.added_by_user_id.in_(user_ids))
    )
    # CASCADE removes the users' beta_invites and the invitees' provider links.
    await db_session.execute(delete(User).where(User.user_id.like("sub-%")))
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


async def _mint(session: AsyncSession, user_id: str, label: str | None = None) -> tuple[UUID, str]:
    minted = await BetaInviteService(session).create(
        user_id=user_id, user_email=f"{user_id}@beta-invite.invalid", label=label
    )
    return minted.invite.id, _token(minted.url)


async def _reissue(session: AsyncSession, user_id: str, invite_id: UUID):
    return await BetaInviteService(session).reissue(
        user_id=user_id, invite_id=invite_id, user_email=f"{user_id}@beta-invite.invalid"
    )


async def _expire(session: AsyncSession, invite_id: UUID) -> None:
    await session.execute(
        update(BetaInvite)
        .where(BetaInvite.id == invite_id)
        .values(expires_at=utcnow() - timedelta(seconds=1))
    )
    await session.commit()


async def _statuses(session: AsyncSession, user_id: str) -> dict[UUID, str]:
    session.expire_all()
    summary = await BetaInviteService(session).get_summary(user_id)
    return {invite.id: invite.status for invite in summary.invites}


async def _invite_rows(session: AsyncSession, user_id: str) -> int:
    return (
        await session.scalar(
            select(func.count(BetaInvite.id)).where(BetaInvite.inviter_user_id == user_id)
        )
    ) or 0


async def _admit(session: AsyncSession, *, sub: str, token: str, provider: str = "google") -> None:
    """Spend ``token`` on ``sub`` through the real signup gate."""
    blocked = await SignupGateService(session).check_access(
        provider=provider,  # type: ignore[arg-type]
        oauth_sub=sub,
        # The snapshot the gate stores as ``subject_label``. Deliberately NOT the
        # address the account ends up with, so a test can tell the two apart.
        email=f"{sub}@snapshot.invalid",
        username="octocat" if provider == "github" else f"{sub}@snapshot.invalid",
        ip_address="203.0.113.7",
        user_agent="pytest",
        beta_invite_token_hash=sha256_hex(token),
    )
    assert blocked is None


async def _sign_up(
    session_maker: async_sessionmaker[AsyncSession], *, sub: str, email: str, provider: str
) -> None:
    """Create the account exactly the way the OAuth callback does: the REAL
    ``RoleManager.ensure_user``, pointed at the test database. Whatever identity
    rows it writes are what ``redeemed_email`` must resolve through."""

    async def test_db():
        async with session_maker() as session:
            yield session

    with patch("db.base.get_db", new=test_db):
        await RoleManager(use_postgres=True).ensure_user(
            email=email,
            user_id=sub,
            name="Invitee",
            auth_provider=provider,
            email_verified=True,
        )


async def _redeemed_email(session: AsyncSession, user_id: str, invite_id: UUID) -> str | None:
    session.expire_all()
    summary = await BetaInviteService(session).get_summary(user_id)
    return summary.redeemed_emails.get(invite_id)


class TestLabelAndCounts:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_label_round_trip(self, db_session: AsyncSession, people: dict[str, str]) -> None:
        labelled, _ = await _mint(db_session, people["inviter"], LABEL)
        plain, _ = await _mint(db_session, people["inviter"])

        db_session.expire_all()
        summary = await BetaInviteService(db_session).get_summary(people["inviter"])
        labels = {invite.id: invite.label for invite in summary.invites}
        assert labels == {labelled: LABEL, plain: None}

        stored = await db_session.scalar(
            text("SELECT label FROM beta_invites WHERE id = :id"), {"id": labelled}
        )
        assert stored == LABEL

    @pytest.mark.asyncio(loop_scope="session")
    async def test_breakdown_always_sums_to_used(
        self, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        """Every status, plus two rows no code path can produce (a hand-edited
        database): the header must still agree with the quota gate."""
        svc = BetaInviteService(db_session)
        admin = people["admin"]  # uncapped, so the mix can be larger than 4
        ids = [(await _mint(db_session, admin))[0] for _ in range(8)]
        now = utcnow()
        edits: list[dict] = [
            {},  # active
            {},  # active
            {"redeemed_at": now},  # redeemed
            {"redeemed_at": now, "expires_at": now - timedelta(days=30)},  # redeemed, long ago
            {"expires_at": now - timedelta(seconds=1)},  # expired
            {"revoked_at": now},  # revoked
            {"revoked_at": now, "redeemed_at": now},  # hand-edited: status says revoked
            {"revoked_at": now, "expires_at": now - timedelta(days=1)},  # revoked + expired
        ]
        for invite_id, values in zip(ids, edits, strict=True):
            if values:
                await db_session.execute(
                    update(BetaInvite).where(BetaInvite.id == invite_id).values(**values)
                )
        await db_session.commit()
        db_session.expire_all()

        summary = await svc.get_summary(admin)

        assert (summary.active, summary.redeemed) == (2, 2)
        assert summary.active + summary.redeemed == summary.used == 4
        assert summary.used == await svc._count_used(admin, utcnow())
        # ...and the counts agree with the statuses the list renders.
        statuses = [invite.status for invite in summary.invites]
        assert statuses.count("active") == summary.active
        assert statuses.count("redeemed") == summary.redeemed
        assert (summary.quota, summary.remaining) == (None, None)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_capped_user_breakdown(
        self, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        inviter = people["inviter"]
        minted = [(await _mint(db_session, inviter))[0] for _ in range(3)]
        await db_session.execute(
            update(BetaInvite).where(BetaInvite.id == minted[0]).values(redeemed_at=utcnow())
        )
        await db_session.commit()

        summary = await BetaInviteService(db_session).get_summary(inviter)

        assert (summary.quota, summary.used, summary.remaining) == (4, 3, 1)
        assert (summary.active, summary.redeemed) == (2, 1)


class TestReissue:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_active_row_at_the_cap_one_revoked_one_new_same_label(
        self, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        svc = BetaInviteService(db_session)
        inviter = people["inviter"]
        old_id, old_token = await _mint(db_session, inviter, LABEL)
        for _ in range(3):
            await _mint(db_session, inviter)
        before = await svc.get_summary(inviter)
        assert (before.used, before.remaining) == (4, 0)

        # At the cap a plain create is refused — but the reissue frees the slot
        # it then takes, so it goes through.
        minted = await _reissue(db_session, inviter, old_id)
        new_id, new_token = minted.invite.id, _token(minted.url)

        assert new_id != old_id
        assert new_token != old_token
        statuses = await _statuses(db_session, inviter)
        assert statuses[old_id] == "revoked"
        assert statuses[new_id] == "active"
        assert sorted(statuses.values()) == ["active"] * 4 + ["revoked"]
        after = await svc.get_summary(inviter)
        assert (after.used, after.active, after.redeemed, after.remaining) == (4, 4, 0, 0)

        new_row = await db_session.get(BetaInvite, new_id)
        assert new_row is not None
        assert new_row.label == LABEL
        assert new_row.token_hash == sha256_hex(new_token)
        assert new_row.expires_at > utcnow() + timedelta(days=6)

        # The old link is dead (and looks like it never existed); the new one works.
        with pytest.raises(NotFoundException):
            await svc.preview(old_token)
        assert (await svc.preview(new_token)).id == new_id

        audits = (
            await db_session.execute(
                text(
                    "SELECT * FROM audit_logs WHERE resource IN (:old, :new)"
                    " AND (user_metadata->>'reissued_from') IS NOT NULL"
                ),
                {"old": f"beta_invite:{old_id}", "new": f"beta_invite:{new_id}"},
            )
        ).all()
        by_action = {row.action: row for row in audits}
        assert sorted(by_action) == ["beta_invite.created", "beta_invite.revoked"]
        assert by_action["beta_invite.revoked"].resource == f"beta_invite:{old_id}"
        assert by_action["beta_invite.created"].resource == f"beta_invite:{new_id}"
        for row in audits:
            assert row.user_metadata["reissued_from"] == str(old_id)
            assert row.user_metadata["reissued_to"] == str(new_id)
            for value in row:
                for private in (LABEL, "Dana", "friend.invalid", old_token, new_token):
                    assert private not in str(value)
                assert sha256_hex(new_token) not in str(value)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_double_click_does_not_mint_a_second_link(
        self, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        inviter = people["inviter"]
        old_id, _ = await _mint(db_session, inviter, LABEL)
        await _reissue(db_session, inviter, old_id)
        rows = await _invite_rows(db_session, inviter)

        with pytest.raises(BetaInviteAlreadyRevokedError) as excinfo:
            await _reissue(db_session, inviter, old_id)

        assert excinfo.value.error_code == "BETA-INVITE-003"
        assert await _invite_rows(db_session, inviter) == rows == 2

        # A row revoked through DELETE is just as un-reissuable.
        revoked_id, _ = await _mint(db_session, inviter)
        await BetaInviteService(db_session).revoke(
            user_id=inviter, invite_id=revoked_id, user_email="x@y.invalid"
        )
        with pytest.raises(BetaInviteAlreadyRevokedError):
            await _reissue(db_session, inviter, revoked_id)
        assert await _invite_rows(db_session, inviter) == 3

    @pytest.mark.asyncio(loop_scope="session")
    async def test_parallel_reissues_of_one_row_mint_exactly_one(
        self, async_engine, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        inviter = people["inviter"]
        old_id, _ = await _mint(db_session, inviter, LABEL)
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        async def attempt() -> bool:
            async with session_maker() as session:
                try:
                    await _reissue(session, inviter, old_id)
                except BetaInviteAlreadyRevokedError:
                    return False
                return True

        results = await asyncio.gather(*[attempt() for _ in range(_PARALLEL)])

        assert sum(results) == 1
        assert sorted((await _statuses(db_session, inviter)).values()) == ["active", "revoked"]

    @pytest.mark.asyncio(loop_scope="session")
    async def test_redeemed_is_refused_and_mints_nothing(
        self, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        inviter = people["inviter"]
        used_id, _ = await _mint(db_session, inviter)
        await db_session.execute(
            update(BetaInvite).where(BetaInvite.id == used_id).values(redeemed_at=utcnow())
        )
        await db_session.commit()

        with pytest.raises(BetaInviteAlreadyRedeemedError) as excinfo:
            await _reissue(db_session, inviter, used_id)

        assert excinfo.value.error_code == "BETA-INVITE-002"
        assert await _statuses(db_session, inviter) == {used_id: "redeemed"}

    @pytest.mark.asyncio(loop_scope="session")
    async def test_unknown_or_someone_elses_is_the_same_404(
        self, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        inviter, other = people["inviter"], people["other"]
        invite_id, _ = await _mint(db_session, inviter)

        with pytest.raises(NotFoundException) as not_mine:
            await _reissue(db_session, other, invite_id)
        with pytest.raises(NotFoundException) as unknown:
            await _reissue(db_session, inviter, uuid4())

        assert not_mine.value.message == unknown.value.message
        assert await _statuses(db_session, inviter) == {invite_id: "active"}
        assert await _invite_rows(db_session, other) == 0

    @pytest.mark.asyncio(loop_scope="session")
    async def test_expired_row_at_the_cap_is_refused_and_not_revoked(
        self, async_engine, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        """An expired row's slot was already free, so this quota check is real —
        and the revoke that preceded it in the transaction must not survive."""
        inviter = people["inviter"]
        expired_id, _ = await _mint(db_session, inviter, LABEL)
        await _expire(db_session, expired_id)
        for _ in range(4):
            await _mint(db_session, inviter)

        with pytest.raises(BetaInviteQuotaExceededError) as excinfo:
            await _reissue(db_session, inviter, expired_id)
        assert excinfo.value.error_code == "BETA-INVITE-001"

        # If the service had left the revoke pending, this commit would persist it.
        await db_session.commit()
        async with async_sessionmaker(async_engine, expire_on_commit=False)() as fresh:
            row = await fresh.get(BetaInvite, expired_id)
            assert row is not None
            assert row.revoked_at is None
            assert row.status == "expired"
            assert await _invite_rows(fresh, inviter) == 5
            revoked_audits = await fresh.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.resource == f"beta_invite:{expired_id}",
                    AuditLog.action == "beta_invite.revoked",
                )
            )
            assert revoked_audits == 0

    @pytest.mark.asyncio(loop_scope="session")
    async def test_expired_row_below_the_cap_is_reissued(
        self, db_session: AsyncSession, people: dict[str, str]
    ) -> None:
        inviter = people["inviter"]
        expired_id, _ = await _mint(db_session, inviter, LABEL)
        await _expire(db_session, expired_id)

        minted = await _reissue(db_session, inviter, expired_id)

        assert minted.invite.label == LABEL
        statuses = await _statuses(db_session, inviter)
        assert statuses == {expired_id: "revoked", minted.invite.id: "active"}
        # The expired row held no slot, so this reissue does take one.
        assert (await BetaInviteService(db_session).get_summary(inviter)).used == 1


class TestRedeemedEmail:
    @pytest.mark.asyncio(loop_scope="session")
    @pytest.mark.parametrize("provider", ["google", "github"])
    async def test_follows_the_live_users_row_and_vanishes_with_it(
        self,
        async_engine,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
        provider: str,
    ) -> None:
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        inviter = people["inviter"]
        invite_id, token = await _mint(db_session, inviter, LABEL)
        sub = f"sub-{uuid4().hex[:12]}"
        await _admit(db_session, sub=sub, token=token, provider=provider)

        # Admitted, but the account does not exist yet: nothing to show — and in
        # particular NOT the allowlist row's ``subject_label`` snapshot.
        entry = await db_session.scalar(
            select(SignupAllowlistEntry).where(SignupAllowlistEntry.subject_id == sub)
        )
        assert entry is not None
        assert entry.subject_label == f"{sub}@snapshot.invalid"
        assert await _redeemed_email(db_session, inviter, invite_id) is None

        await _sign_up(session_maker, sub=sub, email=f"{sub}@account.invalid", provider=provider)
        assert await _redeemed_email(db_session, inviter, invite_id) == f"{sub}@account.invalid"

        # The LIVE row: an address change at the IdP shows up here.
        await db_session.execute(
            update(User).where(User.user_id == sub).values(email=f"{sub}@moved.invalid")
        )
        await db_session.commit()
        assert await _redeemed_email(db_session, inviter, invite_id) == f"{sub}@moved.invalid"

        # Account erasure deletes the ``users`` row. The allowlist snapshot is
        # still there — and must not be used as a fallback.
        await db_session.execute(delete(User).where(User.user_id == sub))
        await db_session.commit()
        assert await _redeemed_email(db_session, inviter, invite_id) is None
        assert (
            await db_session.scalar(
                select(SignupAllowlistEntry.subject_label).where(
                    SignupAllowlistEntry.subject_id == sub
                )
            )
        ) == f"{sub}@snapshot.invalid"
        assert (await _statuses(db_session, inviter))[invite_id] == "redeemed"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_null_once_the_allowlist_row_is_pruned(
        self,
        async_engine,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
    ) -> None:
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        inviter = people["inviter"]
        invite_id, token = await _mint(db_session, inviter)
        sub = f"sub-{uuid4().hex[:12]}"
        await _admit(db_session, sub=sub, token=token)
        await _sign_up(session_maker, sub=sub, email=f"{sub}@account.invalid", provider="google")
        assert await _redeemed_email(db_session, inviter, invite_id) == f"{sub}@account.invalid"

        await db_session.execute(
            delete(SignupAllowlistEntry).where(SignupAllowlistEntry.subject_id == sub)
        )
        await db_session.commit()

        assert await _redeemed_email(db_session, inviter, invite_id) is None
        assert (await _statuses(db_session, inviter))[invite_id] == "redeemed"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_the_provider_is_part_of_the_identity(
        self,
        async_engine,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
    ) -> None:
        """The same ``sub`` string under another provider is another person."""
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        inviter = people["inviter"]
        invite_id, token = await _mint(db_session, inviter)
        sub = f"sub-{uuid4().hex[:12]}"
        await _admit(db_session, sub=sub, token=token, provider="google")
        await _sign_up(session_maker, sub=sub, email=f"{sub}@account.invalid", provider="github")

        assert await _redeemed_email(db_session, inviter, invite_id) is None

    @pytest.mark.asyncio(loop_scope="session")
    async def test_only_redeemed_rows_and_only_the_inviters_own(
        self,
        async_engine,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
    ) -> None:
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        inviter, other = people["inviter"], people["other"]
        redeemed_id, token = await _mint(db_session, inviter)
        active_id, _ = await _mint(db_session, inviter)
        expired_id, _ = await _mint(db_session, inviter)
        revoked_id, _ = await _mint(db_session, inviter)
        await _expire(db_session, expired_id)
        await BetaInviteService(db_session).revoke(
            user_id=inviter, invite_id=revoked_id, user_email="x@y.invalid"
        )
        sub = f"sub-{uuid4().hex[:12]}"
        await _admit(db_session, sub=sub, token=token)
        await _sign_up(session_maker, sub=sub, email=f"{sub}@account.invalid", provider="google")

        db_session.expire_all()
        mine = await BetaInviteService(db_session).get_summary(inviter)
        theirs = await BetaInviteService(db_session).get_summary(other)

        assert mine.redeemed_emails == {redeemed_id: f"{sub}@account.invalid"}
        assert {active_id, expired_id, revoked_id}.isdisjoint(mine.redeemed_emails)
        assert theirs.invites == []
        assert theirs.redeemed_emails == {}

    @pytest.mark.asyncio(loop_scope="session")
    async def test_the_list_costs_the_same_however_many_rows_are_redeemed(
        self,
        async_engine,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
    ) -> None:
        """No N+1: the e-mails ride the list query's joins."""
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        admin = people["admin"]
        statements: list[str] = []

        def record(_conn, _cursor, statement, *_rest) -> None:
            statements.append(statement)

        async def summary_cost() -> tuple[int, int]:
            db_session.expire_all()
            await db_session.rollback()
            statements.clear()
            event.listen(async_engine.sync_engine, "before_cursor_execute", record)
            try:
                summary = await BetaInviteService(db_session).get_summary(admin)
            finally:
                event.remove(async_engine.sync_engine, "before_cursor_execute", record)
            selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
            return len(selects), len(summary.redeemed_emails)

        async def redeem_one() -> None:
            _, token = await _mint(db_session, admin)
            sub = f"sub-{uuid4().hex[:12]}"
            await _admit(db_session, sub=sub, token=token)
            await _sign_up(
                session_maker, sub=sub, email=f"{sub}@account.invalid", provider="google"
            )

        await redeem_one()
        cost_one, emails_one = await summary_cost()
        for _ in range(3):
            await redeem_one()
        cost_four, emails_four = await summary_cost()

        assert (emails_one, emails_four) == (1, 4)
        assert cost_one == cost_four
        # role + slot counts + the list itself.
        assert cost_four == 3


class TestNothingPrivateIsLogged:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_create_reissue_and_list_log_neither_label_nor_email(
        self,
        async_engine,
        db_session: AsyncSession,
        people: dict[str, str],
        closed_gate: None,
    ) -> None:
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        svc = BetaInviteService(db_session)
        inviter = people["inviter"]
        sub = f"sub-{uuid4().hex[:12]}"
        email = f"{sub}@account.invalid"

        with structlog.testing.capture_logs() as logs:
            redeemed_id, token = await _mint(db_session, inviter, LABEL)
            await _admit(db_session, sub=sub, token=token)
            await _sign_up(session_maker, sub=sub, email=email, provider="google")
            old_id, _ = await _mint(db_session, inviter, LABEL)
            reissued = await _reissue(db_session, inviter, old_id)
            with pytest.raises(BetaInviteAlreadyRevokedError):
                await _reissue(db_session, inviter, old_id)
            db_session.expire_all()
            summary = await svc.get_summary(inviter)

        assert summary.redeemed_emails == {redeemed_id: email}
        events = {entry["event"] for entry in logs}
        assert {"beta_invite_created", "beta_invite_reissued"} <= events
        rendered = repr(logs)
        for private in (LABEL, "Dana", "friend.invalid", email, _token(reissued.url)):
            assert private not in rendered

        # ...and no audit row of the whole scenario carries them either.
        audit_rows = (
            await db_session.execute(
                text("SELECT * FROM audit_logs WHERE resource LIKE 'beta_invite:%'")
            )
        ).all()
        assert audit_rows
        for row in audit_rows:
            for value in row:
                for private in (LABEL, "Dana", "friend.invalid", email):
                    assert private not in str(value)
