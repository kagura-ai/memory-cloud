"""Unit tests for ``BetaInviteService`` (Issues #1581, #1595) — no database.

What can be pinned without Postgres: the URL shape, the derived-status
precedence, that minting stores the hash and never the plaintext, the label
normalisation matrix (#1595), the exact predicates of the atomic redeem /
revoke ``UPDATE`` statements and of the slot-count aggregate, and the order of
operations inside a reissue. The quota COUNT semantics, the inviter row lock,
the double-redeem race, the reissue rollback and ``redeemed_email`` need real
transactions and live in ``tests/integration/test_beta_invite_signup_gate_db.py``
and ``tests/integration/test_beta_invite_label_reissue_db.py``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from models.auth import AuditLog
from models.beta_invite import BetaInvite
from services.beta_invite_service import (
    BETA_INVITE_LABEL_MAX_LENGTH,
    BETA_INVITE_TTL,
    BetaInviteService,
    build_beta_invite_url,
    build_redeem_update,
    build_revoke_update,
    build_slot_counts_select,
    normalize_beta_invite_label,
)
from utils.exceptions import (
    BetaInviteAlreadyRedeemedError,
    BetaInviteAlreadyRevokedError,
    BetaInviteQuotaExceededError,
    NotFoundException,
)
from utils.hashing import sha256_hex

NOW = datetime(2026, 9, 1, 12, 0, 0)


def _invite(**overrides) -> BetaInvite:
    fields = {
        "id": uuid4(),
        "token_hash": "a" * 64,
        "inviter_user_id": "inviter-1",
        "created_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=6),
        "redeemed_at": None,
        "redeemed_allowlist_entry_id": None,
        "revoked_at": None,
        "label": None,
    }
    fields.update(overrides)
    return BetaInvite(**fields)


class TestLabelNormalisation:
    """#1595: the label is optional inviter-private free text, <= 100 chars."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("\t\n ", None),
            ("Alice", "Alice"),
            ("  Alice  ", "Alice"),
            # Leading / trailing whitespace is trimmed, so a pasted trailing
            # newline is forgiven — only what would be STORED is validated.
            ("Alice\n", "Alice"),
            ("alice@example.test", "alice@example.test"),
            ("山田 太郎 (大学の友人)", "山田 太郎 (大学の友人)"),
            ("x" * 100, "x" * 100),
            # 100 after the trim is still fine.
            (" " + "x" * 100 + " ", "x" * 100),
            # Counted in characters, not bytes — matches VARCHAR(100).
            ("あ" * 100, "あ" * 100),
        ],
    )
    def test_accepted_values(self, raw: str | None, expected: str | None) -> None:
        assert normalize_beta_invite_label(raw) == expected

    def test_max_length_constant_matches_the_column(self) -> None:
        assert BETA_INVITE_LABEL_MAX_LENGTH == 100
        assert BetaInvite.__table__.c.label.type.length == BETA_INVITE_LABEL_MAX_LENGTH
        assert BetaInvite.__table__.c.label.nullable is True

    @pytest.mark.parametrize(
        "raw",
        [
            "x" * 101,
            "あ" * 101,
            "Ali\x00ce",
            "Ali\nce",
            "Ali\tce",
            "Ali\rce",
            "Ali\x1bce",
            "Ali\x1fce",
            "Ali\x7fce",
            # A lone UTF-16 surrogate is a legal JSON escape and a legal Python
            # ``str``, but it cannot be encoded for the database driver.
            "Ali\ud800ce",
            "\udc00",
        ],
    )
    def test_rejected_values_never_echo_the_label(self, raw: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            normalize_beta_invite_label(raw)
        # The message reaches the 422 body (``msg``) — it must not carry the text.
        assert raw not in str(excinfo.value)
        assert raw.strip()[:10] not in str(excinfo.value)
        # Nor may a chained exception carry it into a rendered traceback (a
        # ``UnicodeEncodeError`` holds the whole string as ``.object``).
        assert excinfo.value.__context__ is None or excinfo.value.__suppress_context__


class TestUrl:
    def test_url_is_frontend_join_path(self, monkeypatch) -> None:
        monkeypatch.setenv("FRONTEND_URL", "https://app.example.test")
        assert build_beta_invite_url("tok") == "https://app.example.test/join/tok"

    def test_trailing_slash_on_frontend_url_does_not_double_up(self, monkeypatch) -> None:
        monkeypatch.setenv("FRONTEND_URL", "https://app.example.test/")
        assert build_beta_invite_url("tok") == "https://app.example.test/join/tok"

    def test_ttl_is_seven_days(self) -> None:
        assert BETA_INVITE_TTL == timedelta(days=7)


class TestDerivedStatus:
    def test_active(self) -> None:
        assert _invite().status_at(NOW) == "active"

    def test_expired_at_the_boundary(self) -> None:
        """``expires_at > now`` is what the redeem UPDATE requires, so an invite
        whose expiry equals ``now`` is already expired."""
        assert _invite(expires_at=NOW).status_at(NOW) == "expired"

    def test_redeemed_outranks_expired(self) -> None:
        invite = _invite(redeemed_at=NOW - timedelta(days=3), expires_at=NOW - timedelta(days=1))
        assert invite.status_at(NOW) == "redeemed"

    def test_revoked_outranks_everything(self) -> None:
        invite = _invite(
            revoked_at=NOW, redeemed_at=NOW - timedelta(days=1), expires_at=NOW - timedelta(days=1)
        )
        assert invite.status_at(NOW) == "revoked"


def _service_with_db(
    *, role: str, used: int, quota: int = 4
) -> tuple[BetaInviteService, MagicMock]:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    svc = BetaInviteService(db)
    svc.settings = SimpleNamespace(beta_invite_quota_per_user=quota)
    svc._lock_inviter_role = AsyncMock(return_value=role)  # type: ignore[method-assign]
    svc._count_used = AsyncMock(return_value=used)  # type: ignore[method-assign]
    return svc, db


class TestMint:
    @pytest.mark.asyncio
    async def test_only_the_hash_is_stored(self, monkeypatch) -> None:
        monkeypatch.setenv("FRONTEND_URL", "https://app.example.test")
        svc, db = _service_with_db(role="user", used=0)

        minted = await svc.create(user_id="inviter-1", user_email="i@example.test")

        token = minted.url.rsplit("/", 1)[1]
        # secrets.token_urlsafe(32) -> 43 URL-safe chars (256 bits).
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)

        added = [c.args[0] for c in db.add.call_args_list]
        invite = next(o for o in added if isinstance(o, BetaInvite))
        assert invite.token_hash == sha256_hex(token)
        assert invite.inviter_user_id == "inviter-1"
        assert invite.expires_at - invite.created_at == BETA_INVITE_TTL
        # Nothing persisted carries the plaintext or the URL.
        for obj in added:
            for value in vars(obj).values():
                assert token not in str(value)

    @pytest.mark.asyncio
    async def test_audit_row_names_the_invite_id_only(self) -> None:
        svc, db = _service_with_db(role="user", used=0)

        minted = await svc.create(user_id="inviter-1", user_email="i@example.test")

        token = minted.url.rsplit("/", 1)[1]
        audit = next(c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditLog))
        assert audit.action == "beta_invite.created"
        assert audit.resource == f"beta_invite:{minted.invite.id}"
        assert audit.user_id == "inviter-1"
        serialized = repr(vars(audit))
        assert token not in serialized
        assert sha256_hex(token) not in serialized

    @pytest.mark.asyncio
    async def test_label_is_stored_on_the_invite_and_nowhere_else(self) -> None:
        """#1595: the label may hold a name or an address — invite row only."""
        svc, db = _service_with_db(role="user", used=0)

        minted = await svc.create(
            user_id="inviter-1", user_email="i@example.test", label="Alice <alice@friend.test>"
        )

        assert minted.invite.label == "Alice <alice@friend.test>"
        audit = next(c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditLog))
        assert "Alice" not in repr(vars(audit))
        assert "friend.test" not in repr(vars(audit))

    @pytest.mark.asyncio
    async def test_label_defaults_to_none(self) -> None:
        svc, _db = _service_with_db(role="user", used=0)

        minted = await svc.create(user_id="inviter-1", user_email="i@example.test")

        assert minted.invite.label is None

    @pytest.mark.asyncio
    async def test_the_fifth_invite_is_refused(self) -> None:
        svc, db = _service_with_db(role="user", used=4)

        with pytest.raises(BetaInviteQuotaExceededError) as excinfo:
            await svc.create(user_id="inviter-1", user_email="i@example.test")

        assert excinfo.value.status_code == 409
        assert excinfo.value.error_code == "BETA-INVITE-001"
        assert excinfo.value.details == {"reason": "quota_exceeded", "quota": 4}
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_admin_is_not_capped(self) -> None:
        svc, _db = _service_with_db(role="admin", used=40)

        minted = await svc.create(user_id="admin-1", user_email="a@example.test")

        assert minted.url
        svc._count_used.assert_not_awaited()  # type: ignore[attr-defined]


class TestRedeemUpdateSQL:
    """The double-use guard is this one statement — pin its predicate.

    A missing clause only fails at runtime, under a race or after expiry, which
    is exactly what a compile-time assertion catches cheaply.
    """

    def _sql(self) -> str:
        stmt = build_redeem_update(token_hash="b" * 64, allowlist_entry_id=uuid4(), now=NOW)
        return str(stmt.compile(dialect=postgresql.dialect()))

    def test_sets_both_redemption_columns(self) -> None:
        sql = self._sql()
        assert "redeemed_at=" in sql
        assert "redeemed_allowlist_entry_id=" in sql

    def test_predicate_requires_unused_unrevoked_unexpired(self) -> None:
        where = self._sql().split("WHERE", 1)[1]
        assert "beta_invites.token_hash = " in where
        assert "beta_invites.redeemed_at IS NULL" in where
        assert "beta_invites.revoked_at IS NULL" in where
        assert "beta_invites.expires_at > " in where


class TestRevokeUpdateSQL:
    """The revoke guard shared by ``DELETE`` and reissue (#1595) — pin its predicate."""

    def _sql(self) -> str:
        stmt = build_revoke_update(invite_id=uuid4(), user_id="inviter-1", now=NOW)
        return str(stmt.compile(dialect=postgresql.dialect()))

    def test_predicate_requires_own_unused_unrevoked(self) -> None:
        sql = self._sql()
        where = sql.split("WHERE", 1)[1].split("RETURNING", 1)[0]
        assert "beta_invites.id = " in where
        assert "beta_invites.inviter_user_id = " in where
        assert "beta_invites.redeemed_at IS NULL" in where
        assert "beta_invites.revoked_at IS NULL" in where
        # Expired rows stay revocable / reissuable: no expiry bound here.
        assert "expires_at" not in where

    def test_sets_revoked_at_and_returns_the_label(self) -> None:
        sql = self._sql()
        assert "SET revoked_at=" in sql
        assert sql.rstrip().endswith("RETURNING beta_invites.label")


class TestSlotCountsSQL:
    """``active`` / ``redeemed`` come from ONE conditional-aggregation statement."""

    def test_one_statement_two_filtered_counts(self) -> None:
        sql = str(
            build_slot_counts_select(user_id="inviter-1", now=NOW).compile(
                dialect=postgresql.dialect()
            )
        )
        assert sql.count("SELECT") == 1
        assert sql.count("FILTER (WHERE") == 2
        where = sql.split("\nWHERE", 1)[1]
        assert "beta_invites.inviter_user_id = " in where
        assert "beta_invites.revoked_at IS NULL" in where

    @pytest.mark.asyncio
    async def test_used_is_the_sum_of_the_breakdown(self) -> None:
        """``_count_used`` (the quota gate) is derived from the same statement, so
        the header can never disagree with what ``create`` enforces."""
        svc = BetaInviteService(MagicMock())
        svc._count_slots = AsyncMock(return_value=(2, 1))  # type: ignore[method-assign]

        assert await svc._count_used("inviter-1", NOW) == 3


def _reissue_service(
    *, role: str = "user", used: int = 0, revoked: tuple[bool, str | None] = (True, "Alice")
) -> tuple[BetaInviteService, MagicMock, list[str]]:
    """A service whose DB-touching internals are stubbed and record their order."""
    svc, db = _service_with_db(role=role, used=used)
    db.rollback = AsyncMock()
    order: list[str] = []

    async def lock(user_id: str) -> str:
        order.append("lock")
        return role

    async def revoke_unused(**_kwargs) -> tuple[bool, str | None]:
        order.append("revoke")
        return revoked

    async def count_used(user_id: str, now: datetime) -> int:
        order.append("quota")
        return used

    async def commit() -> None:
        order.append("commit")

    svc._lock_inviter_role = AsyncMock(side_effect=lock)  # type: ignore[method-assign]
    svc._revoke_unused = AsyncMock(side_effect=revoke_unused)  # type: ignore[method-assign]
    svc._count_used = AsyncMock(side_effect=count_used)  # type: ignore[method-assign]
    db.commit = AsyncMock(side_effect=commit)
    return svc, db, order


class TestReissue:
    """#1595: revoke + mint in ONE transaction. Real-transaction properties
    (rollback, quota arithmetic) are in the integration suite."""

    @pytest.mark.asyncio
    async def test_lock_then_revoke_then_quota_then_one_commit(self, monkeypatch) -> None:
        monkeypatch.setenv("FRONTEND_URL", "https://app.example.test")
        svc, db, order = _reissue_service(used=3)
        old_id = uuid4()

        minted = await svc.reissue(
            user_id="inviter-1", invite_id=old_id, user_email="i@example.test"
        )

        assert order == ["lock", "revoke", "quota", "commit"]
        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()
        assert svc._revoke_unused.await_args.kwargs["invite_id"] == old_id  # type: ignore[attr-defined]
        assert svc._revoke_unused.await_args.kwargs["user_id"] == "inviter-1"  # type: ignore[attr-defined]

        # The new invite carries the old row's label and a fresh token / expiry.
        token = minted.url.rsplit("/", 1)[1]
        assert minted.invite.id != old_id
        assert minted.invite.label == "Alice"
        assert minted.invite.token_hash == sha256_hex(token)
        assert minted.invite.expires_at - minted.invite.created_at == BETA_INVITE_TTL

    @pytest.mark.asyncio
    async def test_two_audit_rows_cross_reference_ids_only(self) -> None:
        svc, db, _order = _reissue_service(revoked=(True, "Alice <alice@friend.test>"))
        old_id = uuid4()

        minted = await svc.reissue(
            user_id="inviter-1", invite_id=old_id, user_email="i@example.test"
        )

        token = minted.url.rsplit("/", 1)[1]
        audits = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditLog)]
        by_action = {a.action: a for a in audits}
        assert sorted(by_action) == ["beta_invite.created", "beta_invite.revoked"]
        assert len(audits) == 2

        revoked, created = by_action["beta_invite.revoked"], by_action["beta_invite.created"]
        assert revoked.resource == f"beta_invite:{old_id}"
        assert created.resource == f"beta_invite:{minted.invite.id}"
        refs = {"reissued_from": str(old_id), "reissued_to": str(minted.invite.id)}
        assert revoked.user_metadata == refs
        assert {k: created.user_metadata[k] for k in refs} == refs
        assert set(created.user_metadata) == {"expires_at", "reissued_from", "reissued_to"}

        for audit in audits:
            serialized = repr({k: v for k, v in vars(audit).items() if k != "user_email"})
            assert "Alice" not in serialized
            assert "friend.test" not in serialized
            assert "@" not in serialized
            assert token not in serialized
            assert sha256_hex(token) not in serialized

    @pytest.mark.asyncio
    async def test_quota_refusal_rolls_the_revoke_back_and_mints_nothing(self) -> None:
        """An EXPIRED row's slot was already free, so its reissue can hit the cap."""
        svc, db, order = _reissue_service(used=4)

        with pytest.raises(BetaInviteQuotaExceededError):
            await svc.reissue(user_id="inviter-1", invite_id=uuid4(), user_email="i@example.test")

        assert order == ["lock", "revoke", "quota"]
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_admin_skips_the_quota_check(self) -> None:
        svc, _db, order = _reissue_service(role="admin", used=40)

        await svc.reissue(user_id="admin-1", invite_id=uuid4(), user_email="a@example.test")

        assert order == ["lock", "revoke", "commit"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("existing", "expected"),
        [
            (None, NotFoundException),
            ({"redeemed_at": NOW}, BetaInviteAlreadyRedeemedError),
            ({"revoked_at": NOW}, BetaInviteAlreadyRevokedError),
        ],
    )
    async def test_nothing_revoked_means_nothing_minted(self, existing, expected) -> None:
        svc, db, order = _reissue_service(revoked=(False, None))
        db.scalar = AsyncMock(return_value=None if existing is None else _invite(**existing))

        with pytest.raises(expected):
            await svc.reissue(user_id="inviter-1", invite_id=uuid4(), user_email="i@example.test")

        assert order == ["lock", "revoke"]
        db.add.assert_not_called()
        db.commit.assert_not_awaited()
        db.rollback.assert_awaited_once()

    def test_already_revoked_is_a_409_with_its_own_code(self) -> None:
        exc = BetaInviteAlreadyRevokedError()
        assert exc.status_code == 409
        assert exc.error_code == "BETA-INVITE-003"
        assert exc.details == {"reason": "already_revoked"}

    def test_already_redeemed_keeps_its_code_for_reissue(self) -> None:
        exc = BetaInviteAlreadyRedeemedError()
        assert (exc.status_code, exc.error_code) == (409, "BETA-INVITE-002")
        assert exc.details == {"reason": "already_redeemed"}
