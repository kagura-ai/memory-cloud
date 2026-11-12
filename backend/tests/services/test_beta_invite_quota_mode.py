"""The beta-invite quota exemption follows SYSTEM_ADMIN_MODE (#2767).

``role = 'admin'`` is uncapped only in ``session`` mode; in ``off`` and
``service`` an admin-role inviter is capped like everyone (the issue: the role
grants nothing outside session mode). Batch minting stays CLI-only and keeps
its users-row admin check in every mode (owner decision).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import get_settings
from services.beta_invite_service import BetaInviteService
from utils.exceptions import BetaInviteQuotaExceededError


@pytest.fixture
def mode(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(get_settings(), "system_admin_mode", value)

    return _set


def _service(*, role: str, used: int, quota: int = 4) -> BetaInviteService:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    svc = BetaInviteService(db)
    svc.settings = SimpleNamespace(beta_invite_quota_per_user=quota)
    svc._lock_inviter_role = AsyncMock(return_value=role)  # type: ignore[method-assign]
    svc._count_used = AsyncMock(return_value=used)  # type: ignore[method-assign]
    return svc


@pytest.mark.parametrize(("value", "admin_quota"), [("session", None), ("off", 4), ("service", 4)])
def test_quota_for_an_admin_role_follows_the_mode(mode, value, admin_quota):
    mode(value)
    svc = _service(role="admin", used=0)
    assert svc._quota_for("admin") == admin_quota
    assert svc._quota_for("user") == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["off", "service"])
async def test_admin_role_inviter_is_capped_outside_session(mode, value):
    mode(value)
    svc = _service(role="admin", used=4)
    with pytest.raises(BetaInviteQuotaExceededError):
        await svc.create(user_id="admin-1", user_email="a@example.test")


@pytest.mark.asyncio
async def test_admin_role_inviter_is_uncapped_in_session(mode):
    mode("session")
    svc = _service(role="admin", used=40)
    minted = await svc.create(user_id="admin-1", user_email="a@example.test")
    assert minted.url


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["session", "off", "service"])
async def test_batch_admin_check_is_unchanged_in_every_mode(mode, value):
    # CLI-only batches keep a users-row admin as inviter in every mode.
    mode(value)
    svc = _service(role="admin", used=0)
    await svc._require_batch_admin("admin-1")
    svc._lock_inviter_role = AsyncMock(return_value="user")  # type: ignore[method-assign]
    with pytest.raises(PermissionError):
        await svc._require_batch_admin("user-1")
