"""Unit tests for ``SecretStoreService`` hardening (audit follow-ups).

Postgres-backed behaviour (advisory locks actually blocking, audit chain,
constraints) lives in ``tests/integration/test_secret_store_integration.py``.
These tests pin two service-layer contracts with a mocked session:

1. ``get_secret`` denial is timing-uniform: the grant probe runs even when
   the secret does not exist, so "missing" vs "exists but ungranted" cost
   the same DB round trips (``secret_get`` is rate-limit-exempt, leaving
   response time as the only enumeration channel).
2. ``delete_secret`` takes the same per-name advisory lock as ``put_secret``
   before touching the row, so a concurrent put cannot race the delete into
   a raw FK IntegrityError.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.secret_store_service import (
    SecretAccessDenied,
    SecretNotFound,
    SecretStoreService,
)


@pytest.fixture
def db():
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.delete = AsyncMock()
    return db


@pytest.fixture
def service(db):
    return SecretStoreService(db)


class TestGetSecretTimingUniformDenial:
    @pytest.mark.asyncio
    async def test_grant_probe_runs_even_when_secret_missing(self, service, monkeypatch):
        """Missing secret must still cost the grant-check round trip."""
        monkeypatch.setattr(service, "_load_active_secret", AsyncMock(return_value=None))
        grant_probe = AsyncMock(return_value=False)
        monkeypatch.setattr(service, "_caller_has_active_grant", grant_probe)
        monkeypatch.setattr(service, "_append_audit", AsyncMock())

        with pytest.raises(SecretAccessDenied):
            await service.get_secret(
                workspace_id=uuid4(),
                actor_user_id="user-1",
                name="cf/token",
            )

        grant_probe.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ungranted_existing_secret_denied_with_same_probe(self, service, monkeypatch):
        secret = MagicMock()
        secret.id = uuid4()
        monkeypatch.setattr(service, "_load_active_secret", AsyncMock(return_value=secret))
        grant_probe = AsyncMock(return_value=False)
        monkeypatch.setattr(service, "_caller_has_active_grant", grant_probe)
        monkeypatch.setattr(service, "_append_audit", AsyncMock())

        with pytest.raises(SecretAccessDenied):
            await service.get_secret(
                workspace_id=uuid4(),
                actor_user_id="user-1",
                name="cf/token",
            )

        grant_probe.assert_awaited_once()
        assert grant_probe.await_args is not None
        assert grant_probe.await_args.args[0] == secret.id
        assert grant_probe.await_args.args[1] == "user-1"


class TestDeleteSecretAdvisoryLock:
    @pytest.mark.asyncio
    async def test_delete_takes_the_put_lock_before_selecting(self, service, db):
        """First statement must be the per-name advisory lock, keyed exactly
        like ``put_secret``'s, so put/delete on one name serialize."""
        workspace_id = uuid4()
        missing = MagicMock()
        missing.scalar_one_or_none = MagicMock(return_value=None)
        db.execute.side_effect = [MagicMock(), missing]  # lock, then SELECT

        with pytest.raises(SecretNotFound):
            await service.delete_secret(
                workspace_id=workspace_id,
                actor_user_id="owner-1",
                name="cf/token",
            )

        first_call = db.execute.await_args_list[0]
        stmt = first_call.args[0]
        params = first_call.args[1]
        assert "pg_advisory_xact_lock" in str(stmt)
        assert params == {"k": f"secret_put:{workspace_id}:cf/token"}
