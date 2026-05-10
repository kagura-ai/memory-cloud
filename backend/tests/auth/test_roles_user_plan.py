"""UserPlan creation in `RoleManager._ensure_user_postgres` (Issue #586).

Pre-#586: the default ``UserPlan`` row was lazy-created on the GET path
of ``/usage/current`` (route layer). #586 moves that creation to the
write path — specifically into the ``User`` INSERT transaction in
``RoleManager._ensure_user_postgres`` — so the GET handler can be
pure-read.

These tests pin two contracts on the new-user path:

1. ``db.add`` is called with both a ``User`` and a ``UserPlan`` instance
   in the same transaction (atomic), and they share the same ``user_id``.
2. The ``UserPlan`` is constructed from ``settings.default_plan_*``
   values, matching the legacy lazy-create branch this replaces.

The existing-user path (``_sync_existing_user``) intentionally does NOT
add a ``UserPlan`` row. Existing users predate this change and are
covered by the lazy fallback in ``api/routes/usage.py``; backfilling
their plan rows is explicitly out-of-scope (Issue #586 ``## Out of
scope``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from auth.roles import RoleManager
from models.auth import User, UserPlan


def _build_patched_get_db(monkeypatch, *, orphan_plan_user_id: str | None = None):
    """Patch ``db.base.get_db`` to yield a controlled MagicMock session.

    The new-user path issues three SELECTs in order:
    1. User lookup by user_id.
    2. User row count (to decide ADMIN vs USER).
    3. UserPlan pre-check for orphan rows (Issue #586, Copilot review).

    ``orphan_plan_user_id`` toggles the third SELECT's return value:
    - ``None`` (default): no orphan row → handler will add a fresh UserPlan.
    - ``"<user_id>"``: orphan row exists → handler must skip the UserPlan add.
    """
    from db import base as db_base

    captured_db = MagicMock()

    user_lookup = MagicMock()
    user_lookup.scalar_one_or_none.return_value = None
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    plan_precheck = MagicMock()
    plan_precheck.scalar_one_or_none.return_value = orphan_plan_user_id

    captured_db.execute = AsyncMock(side_effect=[user_lookup, count_result, plan_precheck])
    captured_db.add = MagicMock()
    captured_db.commit = AsyncMock()
    captured_db.rollback = AsyncMock()
    captured_db.flush = AsyncMock()

    async def _fake_get_db():
        yield captured_db

    monkeypatch.setattr(db_base, "get_db", _fake_get_db)
    return captured_db


@pytest.fixture
def patched_get_db(monkeypatch):
    """Default scenario: no orphan UserPlan row exists for the new user."""
    return _build_patched_get_db(monkeypatch, orphan_plan_user_id=None)


class TestEnsureUserCreatesDefaultPlan:
    """`_ensure_user_postgres` must create the default UserPlan in the
    same transaction as the User row (Issue #586)."""

    @pytest.mark.asyncio
    async def test_user_and_user_plan_added_atomically(self, patched_get_db):
        """Pillar 1: User + UserPlan are added once each, both before commit."""
        rm = RoleManager(use_postgres=True)

        await rm._ensure_user_postgres(
            email="new@example.com",
            user_id="oauth-sub-new",
            name="New User",
            auth_provider="google",
            email_verified=True,
            ip_address=None,
            user_agent=None,
        )

        added = [call.args[0] for call in patched_get_db.add.call_args_list]
        users = [obj for obj in added if isinstance(obj, User)]
        user_plans = [obj for obj in added if isinstance(obj, UserPlan)]

        assert len(users) == 1, f"expected one User add, got {len(users)}"
        assert len(user_plans) == 1, f"expected one UserPlan add, got {len(user_plans)}"
        assert users[0].user_id == "oauth-sub-new"
        assert user_plans[0].user_id == "oauth-sub-new"
        # Both adds must precede the commit (atomic transaction).
        patched_get_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orphan_user_plan_row_skipped_to_avoid_pk_violation(self, monkeypatch):
        """Orphan UserPlan path: a pre-existing UserPlan row without its
        User row (e.g. from a partial account_erasure failure) must NOT
        cause first-login to bubble a 500.

        Without the pre-check in `_ensure_user_postgres`, the UserPlan
        INSERT would PK-violate at commit time and the IntegrityError
        handler would re-raise (it only recognizes users.user_id races
        and users.email collisions). Copilot review on PR #589 flagged
        this; the fix is to SELECT for an existing UserPlan row first
        and skip the add when it exists.
        """
        captured_db = _build_patched_get_db(monkeypatch, orphan_plan_user_id="oauth-sub-orphan")

        rm = RoleManager(use_postgres=True)
        await rm._ensure_user_postgres(
            email="orphan@example.com",
            user_id="oauth-sub-orphan",
            name=None,
            auth_provider="google",
            email_verified=True,
            ip_address=None,
            user_agent=None,
        )

        added = [call.args[0] for call in captured_db.add.call_args_list]
        users = [obj for obj in added if isinstance(obj, User)]
        user_plans = [obj for obj in added if isinstance(obj, UserPlan)]

        # User row IS added (this is still a fresh user creation).
        assert len(users) == 1
        # UserPlan add is SKIPPED — the orphan row already occupies the PK.
        assert user_plans == [], (
            f"expected zero UserPlan adds when an orphan row exists, got {len(user_plans)}"
        )
        # Commit still runs so the User row is persisted, reusing the orphan plan.
        captured_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_user_plan_uses_settings_defaults(self, patched_get_db, monkeypatch):
        """Pillar 2: the UserPlan row uses settings.default_plan_* values."""
        sentinel = MagicMock()
        sentinel.default_plan_memory_limit = 7777
        sentinel.default_plan_daily_api_limit = 333
        sentinel.default_plan_weekly_api_limit = 1111
        sentinel.audit_hmac_key = "test-hmac-key"
        from auth import roles as roles_module

        monkeypatch.setattr(roles_module, "get_settings", lambda: sentinel)

        rm = RoleManager(use_postgres=True)

        await rm._ensure_user_postgres(
            email="defaults@example.com",
            user_id="oauth-sub-defaults",
            name=None,
            auth_provider="github",
            email_verified=True,
            ip_address=None,
            user_agent=None,
        )

        added = [call.args[0] for call in patched_get_db.add.call_args_list]
        user_plans = [obj for obj in added if isinstance(obj, UserPlan)]
        assert len(user_plans) == 1
        plan = user_plans[0]
        assert plan.plan_name == "free"
        assert plan.memory_limit == 7777
        assert plan.daily_api_limit == 333
        assert plan.weekly_api_limit == 1111
