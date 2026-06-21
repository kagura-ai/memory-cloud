"""Unit tests for the context-scoped read-only TTL share key (#1027).

Covers the security invariants the gate-1 (CSO) review pinned:

1. TTL clamp + mandatory expiry (#889 prior art) — requested lifetime is
   clamped to a 30-day ceiling; omitting it defaults to 30 days; a share key
   is never minted without an ``expires_at``.
2. Rejection on use — expired and revoked keys verify to ``None``; the
   constant-time hash gate (#964) rejects a surfaced-but-mismatched row.
3. Confinement is derived from the BOUND CONTEXT (CSO invariant #2) — the
   share-recall principal's workspace comes from the bound context's
   workspace, never the owner's current workspace.
4. Fail-closed allow-list (CSO invariant #1) — the share-recall dependency
   only honors a ``kagura_sk_`` bearer; anything else is 401.

These are unit tests with a mocked ``AsyncSession`` (mirroring
``tests/auth/test_api_key_binding.py``); the DB schema / migration is covered
separately by the integration suite.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.share_keys import (
    DEFAULT_TTL_DAYS,
    MAX_TTL_DAYS,
    SHARE_KEY_PREFIX,
    SHARE_KEY_SCOPE,
    ShareKeyManager,
    VerifiedShareKey,
)
from utils.hashing import sha256_hex

NOW = datetime(2026, 6, 21, 12, 0, 0)


def _execute_result(value: object | None = None):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _make_db_mock(execute_results: list[object | None]) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_execute_result(r) for r in execute_results])
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# TTL clamp (#889 prior art) — pure function, no DB
# ---------------------------------------------------------------------------


class TestClampTtlDays:
    def test_none_defaults_to_ceiling(self) -> None:
        assert ShareKeyManager._clamp_ttl_days(None) == DEFAULT_TTL_DAYS == 30

    def test_over_ceiling_clamped_down(self) -> None:
        assert ShareKeyManager._clamp_ttl_days(365) == MAX_TTL_DAYS == 30

    def test_within_range_passthrough(self) -> None:
        assert ShareKeyManager._clamp_ttl_days(7) == 7

    def test_zero_floors_to_one(self) -> None:
        assert ShareKeyManager._clamp_ttl_days(0) == 1

    def test_negative_floors_to_one(self) -> None:
        assert ShareKeyManager._clamp_ttl_days(-5) == 1


# ---------------------------------------------------------------------------
# create_key — mandatory clamped expiry, fixed read scope, single-context bind
# ---------------------------------------------------------------------------


class TestCreateKey:
    @pytest.mark.asyncio
    async def test_happy_path_sets_prefix_scope_context_and_expiry(self) -> None:
        ctx_id = uuid.uuid4()
        db = _make_db_mock(execute_results=[None])  # name-dedup → no collision
        manager = ShareKeyManager(db)

        with patch("auth.share_keys.utcnow", return_value=NOW):
            plaintext, row = await manager.create_key(
                name="dash", user_id="user-1", context_id=ctx_id, ttl_days=7
            )

        assert plaintext.startswith(SHARE_KEY_PREFIX)
        assert row.scope == SHARE_KEY_SCOPE == "memory:read"
        assert row.context_id == ctx_id
        assert row.user_id == "user-1"
        # Mandatory, clamped expiry.
        assert row.expires_at == NOW + timedelta(days=7)
        db.add.assert_called_once()
        assert db.add.call_args.args[0] is row

    @pytest.mark.asyncio
    async def test_omitted_ttl_defaults_to_30d_expiry(self) -> None:
        db = _make_db_mock(execute_results=[None])
        manager = ShareKeyManager(db)

        with patch("auth.share_keys.utcnow", return_value=NOW):
            _, row = await manager.create_key(
                name="dash", user_id="user-1", context_id=uuid.uuid4()
            )

        assert row.expires_at == NOW + timedelta(days=30)

    @pytest.mark.asyncio
    async def test_oversized_ttl_clamped_to_30d_expiry(self) -> None:
        db = _make_db_mock(execute_results=[None])
        manager = ShareKeyManager(db)

        with patch("auth.share_keys.utcnow", return_value=NOW):
            _, row = await manager.create_key(
                name="dash", user_id="user-1", context_id=uuid.uuid4(), ttl_days=365
            )

        assert row.expires_at == NOW + timedelta(days=30)

    @pytest.mark.asyncio
    async def test_duplicate_active_name_raises(self) -> None:
        existing = SimpleNamespace(id=1, name="dash")
        db = _make_db_mock(execute_results=[existing])  # dedup → collision
        manager = ShareKeyManager(db)

        with pytest.raises(ValueError, match="already exists"):
            await manager.create_key(name="dash", user_id="user-1", context_id=uuid.uuid4())
        db.add.assert_not_called()


# ---------------------------------------------------------------------------
# verify_key — rejection on use (expired / revoked / hash mismatch)
# ---------------------------------------------------------------------------


class TestVerifyKey:
    KEY = f"{SHARE_KEY_PREFIX}testtoken"

    @staticmethod
    def _record(
        *,
        key_hash: str,
        revoked_at: datetime | None = None,
        expires_at: datetime,
        last_used_at: datetime | None = None,
        context_id: uuid.UUID | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=1,
            user_id="user-1",
            context_id=context_id or uuid.uuid4(),
            key_hash=key_hash,
            revoked_at=revoked_at,
            expires_at=expires_at,
            last_used_at=last_used_at,
        )

    async def _verify(self, record: SimpleNamespace, now: datetime = NOW):
        db = _make_db_mock(execute_results=[record])
        manager = ShareKeyManager(db)
        fake_settings = SimpleNamespace(api_key_last_used_throttle_seconds=60)
        with (
            patch("auth.share_keys.utcnow", return_value=now),
            patch("auth.share_keys.get_settings", return_value=fake_settings),
        ):
            return db, await manager.verify_key(self.KEY)

    @pytest.mark.asyncio
    async def test_not_found_returns_none(self) -> None:
        db = _make_db_mock(execute_results=[None])
        manager = ShareKeyManager(db)
        assert await manager.verify_key(self.KEY) is None

    @pytest.mark.asyncio
    async def test_valid_key_returns_verified(self) -> None:
        ctx = uuid.uuid4()
        rec = self._record(
            key_hash=sha256_hex(self.KEY),
            expires_at=NOW + timedelta(days=1),
            context_id=ctx,
        )
        _, result = await self._verify(rec)
        assert isinstance(result, VerifiedShareKey)
        assert result.user_id == "user-1"
        assert result.context_id == ctx

    @pytest.mark.asyncio
    async def test_expired_key_rejected(self) -> None:
        rec = self._record(
            key_hash=sha256_hex(self.KEY),
            expires_at=NOW - timedelta(seconds=1),  # already past
        )
        _, result = await self._verify(rec)
        assert result is None

    @pytest.mark.asyncio
    async def test_revoked_key_rejected(self) -> None:
        rec = self._record(
            key_hash=sha256_hex(self.KEY),
            revoked_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
        )
        _, result = await self._verify(rec)
        assert result is None

    @pytest.mark.asyncio
    async def test_hash_mismatch_rejected(self) -> None:
        # Row surfaced by a (hypothetical) loose lookup whose stored hash does
        # not match the constant-time re-derivation → rejected (#964).
        rec = self._record(
            key_hash="deadbeef_not_the_real_hash",
            expires_at=NOW + timedelta(days=1),
        )
        _, result = await self._verify(rec)
        assert result is None

    @pytest.mark.asyncio
    async def test_last_used_throttled_write_on_first_use(self) -> None:
        rec = self._record(
            key_hash=sha256_hex(self.KEY),
            expires_at=NOW + timedelta(days=1),
            last_used_at=None,
        )
        db, result = await self._verify(rec)
        assert result is not None
        assert rec.last_used_at == NOW
        db.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# revoke_key — ownership-scoped soft delete
# ---------------------------------------------------------------------------


class TestRevokeKey:
    @pytest.mark.asyncio
    async def test_revokes_owned_active_key(self) -> None:
        rec = SimpleNamespace(id=5, user_id="user-1", revoked_at=None)
        db = _make_db_mock(execute_results=[rec])
        manager = ShareKeyManager(db)
        with patch("auth.share_keys.utcnow", return_value=NOW):
            ok = await manager.revoke_key(key_id=5, user_id="user-1")
        assert ok is True
        assert rec.revoked_at == NOW

    @pytest.mark.asyncio
    async def test_missing_or_unowned_returns_false(self) -> None:
        db = _make_db_mock(execute_results=[None])
        manager = ShareKeyManager(db)
        assert await manager.revoke_key(key_id=999, user_id="user-1") is False


# ---------------------------------------------------------------------------
# get_share_key_principal — fail-closed allow-list + bound-context workspace
# ---------------------------------------------------------------------------


class TestShareKeyPrincipalDependency:
    @pytest.mark.asyncio
    async def test_missing_bearer_rejected(self) -> None:
        from fastapi import HTTPException

        from auth.dependencies import get_share_key_principal

        with pytest.raises(HTTPException) as exc:
            await get_share_key_principal(authorization=None, db=AsyncMock())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_non_share_key_bearer_rejected(self) -> None:
        """A normal api key / oauth bearer is NOT honored on the share surface
        (fail-closed allow-list — the share surface is share-key-only)."""
        from fastapi import HTTPException

        from auth.dependencies import get_share_key_principal

        with pytest.raises(HTTPException) as exc:
            await get_share_key_principal(authorization="Bearer kagura_normalkey", db=AsyncMock())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_share_key_rejected(self) -> None:
        from fastapi import HTTPException

        from auth.dependencies import get_share_key_principal

        db = AsyncMock()
        with patch(
            "auth.share_keys.ShareKeyManager.verify_key",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_share_key_principal(authorization=f"Bearer {SHARE_KEY_PREFIX}bad", db=db)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_key_principal_uses_bound_context_workspace(self) -> None:
        """CSO invariant #2: effective workspace comes from the BOUND CONTEXT,
        never the owner's current workspace."""
        from auth.dependencies import get_share_key_principal

        ctx_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        verified = VerifiedShareKey(id=7, user_id="owner-1", context_id=ctx_id)
        context_row = SimpleNamespace(id=ctx_id, workspace_id=ws_id, deleted_at=None)

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_execute_result(context_row))
        db.commit = AsyncMock()

        with patch(
            "auth.share_keys.ShareKeyManager.verify_key",
            new=AsyncMock(return_value=verified),
        ):
            principal = await get_share_key_principal(
                authorization=f"Bearer {SHARE_KEY_PREFIX}good", db=db
            )

        assert principal["user_id"] == "owner-1"
        assert principal["current_context_id"] == ctx_id
        assert principal["share_key_context_id"] == ctx_id
        assert principal["current_workspace_id"] == ws_id  # from bound context
        assert principal["scope"] == SHARE_KEY_SCOPE
        assert principal["role"] == "share-key"

    @pytest.mark.asyncio
    async def test_bound_context_missing_rejected(self) -> None:
        from fastapi import HTTPException

        from auth.dependencies import get_share_key_principal

        verified = VerifiedShareKey(id=7, user_id="owner-1", context_id=uuid.uuid4())
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_execute_result(None))  # context gone
        db.commit = AsyncMock()

        with patch(
            "auth.share_keys.ShareKeyManager.verify_key",
            new=AsyncMock(return_value=verified),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_share_key_principal(authorization=f"Bearer {SHARE_KEY_PREFIX}good", db=db)
        assert exc.value.status_code == 401
