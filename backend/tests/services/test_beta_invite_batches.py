"""Batch credentials: authorization, atomicity and secret-free auditing (#2704)."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.auth import AuditLog
from services.beta_invite_service import BetaInviteService
from utils.datetime import utcnow


def service(role="admin", existing=None):
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[role, existing, "admin@example.invalid"])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return BetaInviteService(db)


async def test_batch_expiry_hashes_and_private_audit():
    svc = service()
    expiry = utcnow() + timedelta(days=20)
    minted = await svc.create_batch("admin", "private-label", expiry, 3)
    assert len(minted) == 3
    assert len({m.url for m in minted}) == 3
    assert all(m.invite.expires_at == expiry for m in minted)
    audits = [c.args[0] for c in svc.db.add.call_args_list if isinstance(c.args[0], AuditLog)]
    assert len(audits) == 3
    for audit in audits:
        assert audit.action == "beta_invite.created"
        assert audit.user_email == "admin@example.invalid"
        assert "private-label" not in str(audit.__dict__)
        assert all(m.url not in str(audit.__dict__) for m in minted)
    svc.db.commit.assert_awaited_once()


@pytest.mark.parametrize(
    "role,existing,error", [("user", None, PermissionError), ("admin", 1, ValueError)]
)
async def test_refused_batch_writes_nothing(role, existing, error):
    svc = service(role, existing)
    with pytest.raises(error):
        await svc.create_batch("actor", "batch", utcnow() + timedelta(days=1), 2)
    svc.db.add.assert_not_called()
    svc.db.commit.assert_not_awaited()
    svc.db.rollback.assert_awaited_once()


@pytest.mark.parametrize("days", [-1, 0, 61])
async def test_invalid_expiry(days):
    svc = service()
    with pytest.raises(ValueError):
        await svc.create_batch("admin", "batch", utcnow() + timedelta(days=days), 2)
    svc.db.commit.assert_not_awaited()


async def test_commit_failure_rolls_back():
    svc = service()
    svc.db.commit.side_effect = RuntimeError("commit failed")
    with pytest.raises(RuntimeError):
        await svc.create_batch("admin", "batch", utcnow() + timedelta(days=1), 2)
    svc.db.rollback.assert_awaited_once()


async def test_revoke_scopes_the_guard_and_audits_only_changed_rows():
    from uuid import uuid4

    from sqlalchemy.dialects import postgresql

    svc = service()
    svc.db.scalar = AsyncMock(side_effect=["admin", "admin@example.invalid"])
    result = MagicMock()
    ids = [uuid4(), uuid4()]
    result.scalars.return_value.all.return_value = ids
    svc.db.execute = AsyncMock(return_value=result)
    assert await svc.revoke_unused_by_label("admin", "private-label") == 2
    statement = svc.db.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    for guard in ["inviter_user_id =", "label =", "redeemed_at IS NULL", "revoked_at IS NULL"]:
        assert guard in sql
    audits = [call.args[0] for call in svc.db.add.call_args_list]
    assert {a.resource for a in audits} == {f"beta_invite:{i}" for i in ids}
    assert all(a.action == "beta_invite.revoked" and a.user_metadata is None for a in audits)
    svc.db.commit.assert_awaited_once()


async def test_expiry_at_60_day_boundary_is_allowed(monkeypatch):
    now = utcnow()
    monkeypatch.setattr("services.beta_invite_service.utcnow", lambda: now)
    svc = service()
    minted = await svc.create_batch("admin", "batch", now + timedelta(days=60), 1)
    assert minted[0].invite.expires_at == now + timedelta(days=60)


@pytest.mark.parametrize("count", [0, -1, 1001, True, 1.5])
async def test_invalid_count(count):
    svc = service()
    with pytest.raises(ValueError):
        await svc.create_batch("admin", "batch", utcnow() + timedelta(days=1), count)
    svc.db.add.assert_not_called()


@pytest.mark.parametrize("label", [" padded ", "a" * 101, "", "   "])
@pytest.mark.parametrize("operation", ["mint", "revoke"])
async def test_batch_label_must_already_be_normalized(label, operation):
    svc = service()
    with pytest.raises(ValueError):
        if operation == "mint":
            await svc.create_batches("admin", [(label, utcnow() + timedelta(days=1), 1)])
        else:
            await svc.revoke_unused_by_label("admin", label)
    svc.db.add.assert_not_called()
    svc.db.commit.assert_not_awaited()


async def test_inviter_lock_allows_foreign_key_checks():
    from sqlalchemy.dialects import postgresql

    svc = service()
    await svc._lock_inviter_role("admin")
    statement = svc.db.scalar.await_args.args[0]
    assert "FOR NO KEY UPDATE" in str(statement.compile(dialect=postgresql.dialect()))


async def test_invite_issuer_lock_is_key_share():
    """#2803: the sign-up takes the inviter's row FOR KEY SHARE (it only must
    stay undeleted); FOR NO KEY UPDATE would also queue every concurrent
    invitee and the inviter's own row updates behind it."""
    from sqlalchemy.dialects import postgresql

    from services.beta_invite_service import lock_invite_issuer

    db = AsyncMock()
    db.scalar.return_value = "user"
    assert await lock_invite_issuer(db, "inviter") == ("user", "inviter")
    statement = db.scalar.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert sql.rstrip().endswith("FOR KEY SHARE")
