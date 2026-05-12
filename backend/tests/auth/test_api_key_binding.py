"""Unit tests for `APIKeyManager.create_key` public-bound validation (#626).

Covers the validation gates added in #626:

1. Mutual exclusion: ``workspace_id`` and ``bound_context_id`` together → ValueError.
2. Non-existent context: ``bound_context_id`` referencing a missing context → ValueError.
3. Non-public context: ``bound_context_id`` of an ``is_public=False`` context → ValueError.
4. Happy path: valid public context produces ``VerifiedKey`` with ``bound_context_id`` set.

These are unit tests with mocked ``AsyncSession`` — they exercise only the
``APIKeyManager`` logic without touching a real database. The DB-level CHECK
constraint is covered by ``tests/integration/test_e10_626_apikey_bound_context_id_migration.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from auth.api_keys import APIKeyManager


def _execute_result(value: object | None = None):
    """Build a MagicMock that mimics ``await db.execute(stmt)`` result.

    ``.scalar_one_or_none()`` returns ``value``. This avoids depending on
    MagicMock's auto-attribute behavior, which would silently return a
    fresh MagicMock and could mask future refactors.
    """
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _make_db_mock(execute_results: list[object | None]) -> AsyncMock:
    """Build an AsyncMock DB that returns the given results in order."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_execute_result(r) for r in execute_results])
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _public_context_row():
    """Mock row for an ``is_public=True`` Context."""
    ctx = MagicMock()
    ctx.id = uuid.uuid4()
    ctx.is_public = True
    return ctx


def _private_context_row():
    """Mock row for an ``is_public=False`` Context."""
    ctx = MagicMock()
    ctx.id = uuid.uuid4()
    ctx.is_public = False
    return ctx


class TestCreateKeyBoundContextValidation:
    """Validation rules around ``create_key(..., bound_context_id=...)``."""

    @pytest.mark.asyncio
    async def test_workspace_id_and_bound_context_id_both_set_raises(self) -> None:
        """Both scoping params at once → ValueError before any DB work."""
        db = AsyncMock()  # No execute calls expected — validator runs first.
        manager = APIKeyManager(db)

        with pytest.raises(ValueError, match="mutually exclusive"):
            await manager.create_key(
                name="bad-key",
                user_id="user-1",
                workspace_id=uuid.uuid4(),
                bound_context_id=uuid.uuid4(),
            )

        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_bound_context_not_found_raises(self) -> None:
        """``bound_context_id`` referencing missing context → ValueError."""
        # The context lookup returns None (first execute call).
        db = _make_db_mock(execute_results=[None])
        manager = APIKeyManager(db)

        with pytest.raises(ValueError, match="not found"):
            await manager.create_key(
                name="missing-ctx",
                user_id="user-1",
                bound_context_id=uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_bound_context_not_public_raises(self) -> None:
        """``bound_context_id`` of a private context → ValueError, no key created."""
        db = _make_db_mock(execute_results=[_private_context_row()])
        manager = APIKeyManager(db)

        with pytest.raises(ValueError, match="non-public"):
            await manager.create_key(
                name="priv-ctx",
                user_id="user-1",
                bound_context_id=uuid.uuid4(),
            )

        # Validator runs BEFORE the name-uniqueness query, so only one
        # execute should have fired (the context lookup).
        assert db.execute.call_count == 1
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_bound_context_id_set_workspace_id_none_succeeds(self) -> None:
        """Happy path: bound key with valid public context creates a row."""
        ctx = _public_context_row()
        # Execute sequence:
        #   1. Context lookup → returns the public ctx row
        #   2. Name-uniqueness check → returns None (no collision)
        db = _make_db_mock(execute_results=[ctx, None])
        manager = APIKeyManager(db)

        plaintext, returned_key = await manager.create_key(
            name="bound-ok",
            user_id="user-1",
            bound_context_id=ctx.id,
        )

        # Returns a (plaintext, APIKey) tuple — the plaintext is kagura_…
        # and the row carries the binding.
        assert isinstance(plaintext, str)
        assert plaintext.startswith("kagura_")
        assert returned_key.bound_context_id == ctx.id
        assert returned_key.workspace_id is None
        # ``returned_key`` is the same row that was added to the session.
        db.add.assert_called_once()
        added_key = db.add.call_args.args[0]
        assert added_key is returned_key
