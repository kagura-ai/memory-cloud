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
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.api_keys import APIKeyManager, VerifiedKey


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
    async def test_verify_api_key_user_rejects_bound_keys(self) -> None:
        """Privilege-escalation guard: ``verify_api_key_user`` MUST 403 a
        public-bound key (#626) so it cannot authenticate any endpoint
        other than ``/api/v1/public/{ctx}/*``.

        Without this gate, the bound key would inherit the owner's
        ``current_workspace_id`` via ``_build_api_key_user_dict`` and
        grant full account access — exactly the escalation the bound
        scoping is supposed to prevent.
        """
        from fastapi import HTTPException

        from auth.api_keys import VerifiedKey
        from auth.dependencies import verify_api_key_user

        bound_result = VerifiedKey(
            id=42,
            user_id="user-1",
            workspace_id=None,
            bound_context_id=uuid.uuid4(),
        )

        with patch(
            "auth.api_keys.APIKeyManager.verify_key",
            new=AsyncMock(return_value=bound_result),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await verify_api_key_user(api_key="kagura_test", db=AsyncMock())

        assert exc_info.value.status_code == 403
        assert "public-bound" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_verify_api_key_standalone_rejects_bound_keys(self) -> None:
        """``auth.dependencies.verify_api_key`` (used by MCP) MUST return
        None for public-bound keys — same privilege-escalation guard,
        different code path. MCP auth treats None as "invalid token",
        which is the correct rejection shape on that surface.
        """
        from auth.api_keys import VerifiedKey
        from auth.dependencies import verify_api_key

        bound_result = VerifiedKey(
            id=42,
            user_id="user-1",
            workspace_id=None,
            bound_context_id=uuid.uuid4(),
        )

        # Patch the inner db generator + manager.verify_key. The standalone
        # opens its own session via async for db in get_db(), so we patch
        # the whole APIKeyManager.verify_key path.
        async def _fake_get_db():
            yield AsyncMock()

        with (
            patch("db.base.get_db", new=_fake_get_db),
            patch(
                "auth.api_keys.APIKeyManager.verify_key",
                new=AsyncMock(return_value=bound_result),
            ),
        ):
            result = await verify_api_key("kagura_test")

        assert result is None  # bound key rejected, masquerades as "invalid"

    @pytest.mark.asyncio
    async def test_bound_context_id_set_workspace_id_none_succeeds(self) -> None:
        """Happy path: bound key with valid public context creates a row.

        Patches ``utils.encryption.get_encryptor`` because ``create_key``
        encrypts the plaintext-for-reveal at-rest via Fernet, and the
        global encryptor reads ``API_KEY_SECRET`` from the environment.
        Setting that env var in unit tests would leak production-shaped
        config into CI; mocking the encryptor keeps this test a true
        validation-layer unit test.
        """
        ctx = _public_context_row()
        # Execute sequence:
        #   1. Context lookup → returns the public ctx row
        #   2. Name-uniqueness check → returns None (no collision)
        db = _make_db_mock(execute_results=[ctx, None])
        manager = APIKeyManager(db)

        mock_encryptor = MagicMock()
        mock_encryptor.encrypt = MagicMock(return_value="encrypted-blob")
        with patch("utils.encryption.get_encryptor", return_value=mock_encryptor):
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


class TestVerifyApiKeyStandaloneCommitsLastUsed:
    """#945: the standalone ``verify_api_key`` (MCP auth path) consumes
    ``get_db()`` via ``async for db in get_db(): ... return verified`` — the
    early ``return`` raises ``GeneratorExit`` at the generator's ``yield``, so
    ``get_db``'s post-yield ``await session.commit()`` never runs and the
    ``last_used_at`` flushed by ``verify_key`` is rolled back. The wrapper must
    commit explicitly. A commit failure must NOT fail authentication
    (``last_used_at`` is non-critical metadata, and the outer
    ``except Exception: return None`` would otherwise reject a valid key)."""

    @staticmethod
    def _ok_key() -> VerifiedKey:
        return VerifiedKey(id=1, user_id="user-1", workspace_id=None, bound_context_id=None)

    @staticmethod
    @contextmanager
    def _patched(db: AsyncMock, verified: VerifiedKey):
        """Patch the standalone ``verify_api_key``'s inner ``get_db()`` to yield
        ``db`` and its ``verify_key`` to return ``verified``. The standalone opens
        its own session via ``async for db in get_db()``, so both the generator
        and the manager method must be patched."""

        async def _fake_get_db():
            yield db

        with (
            patch("db.base.get_db", new=_fake_get_db),
            patch(
                "auth.api_keys.APIKeyManager.verify_key",
                new=AsyncMock(return_value=verified),
            ),
        ):
            yield

    @pytest.mark.asyncio
    async def test_standalone_commits_on_success(self) -> None:
        from auth.dependencies import verify_api_key

        ok = self._ok_key()
        db = AsyncMock()

        with self._patched(db, ok):
            result = await verify_api_key("kagura_test")

        assert result is ok
        # The explicit commit is what persists last_used_at past the
        # GeneratorExit that skips get_db's own commit.
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_standalone_commit_failure_does_not_fail_auth(self) -> None:
        from auth.dependencies import verify_api_key

        ok = self._ok_key()
        db = AsyncMock()
        db.commit = AsyncMock(side_effect=RuntimeError("commit boom"))

        with self._patched(db, ok):
            result = await verify_api_key("kagura_test")

        # A non-critical last_used_at commit failure must not turn a valid key
        # into an auth rejection (would otherwise hit `except Exception: None`).
        assert result is ok
