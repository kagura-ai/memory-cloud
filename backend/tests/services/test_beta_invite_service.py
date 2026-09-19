"""Unit tests for ``BetaInviteService`` (Issue #1581) — no database.

What can be pinned without Postgres: the URL shape, the derived-status
precedence, that minting stores the hash and never the plaintext, and the exact
predicate of the atomic redeem ``UPDATE``. The quota COUNT semantics, the inviter
row lock and the double-redeem race need real transactions and live in
``tests/integration/test_beta_invite_service_db.py``.
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
    BETA_INVITE_TTL,
    BetaInviteService,
    build_beta_invite_url,
    build_redeem_update,
)
from utils.exceptions import BetaInviteQuotaExceededError
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
    }
    fields.update(overrides)
    return BetaInvite(**fields)


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
