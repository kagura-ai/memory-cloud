"""Where the capacity-over lock is enforced outside MemoryService (#1941).

Uploads and context creation grow a workspace, so both refuse while it is
locked; the lock itself is patched to "locked" here (the predicate is pinned
in ``test_capacity_lock``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.context_service import ContextService
from services.file_storage_service import FileStorageService
from tests.storage._fakes import FakeBlobStorage
from utils.exceptions import CapacityLockedError


def _locked() -> CapacityLockedError:
    return CapacityLockedError(
        memory_count=0,
        memory_limit=0,
        over_memories=0,
        used_bytes=2,
        storage_limit_bytes=1,
        over_bytes=1,
        cleanup_url="https://app.example.test/workspace/settings/plan",
    )


def _db_returning(value) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


class TestFileUploads:
    @pytest.fixture(autouse=True)
    def _allowlist(self, monkeypatch):
        from config.settings import get_settings

        monkeypatch.setattr(get_settings(), "allowed_file_content_types", "text/plain")

    @pytest.mark.asyncio
    async def test_reserve_refuses_before_reserving_quota(self) -> None:
        ws = MagicMock(id=uuid4(), effective_storage_limit_bytes=10**9)
        service = FileStorageService(_db_returning(ws), storage=FakeBlobStorage())
        reserve = AsyncMock()
        with (
            patch(
                "services.file_storage_service.ensure_not_capacity_locked",
                AsyncMock(side_effect=_locked()),
            ) as ensure,
            patch(
                "services.file_storage_service.storage_quota_service.reserve_storage_bytes",
                reserve,
            ),
            pytest.raises(CapacityLockedError),
        ):
            await service.reserve_upload(
                workspace_id=ws.id,
                created_by="u",
                filename="notes.txt",
                content_type="text/plain",
                size_bytes=10,
                sha256="a" * 64,
            )
        assert ensure.await_args.args[1] is ws
        reserve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirm_refuses_a_pending_reservation(self) -> None:
        ws_id = uuid4()
        file = MagicMock(
            id=uuid4(),
            workspace_id=ws_id,
            context_id=None,
            status="reserved",
            sha256="a" * 64,
            size_bytes=10,
            storage_key="k",
        )
        storage = FakeBlobStorage()
        service = FileStorageService(_db_returning(MagicMock(id=ws_id)), storage=storage)
        service._load_file = AsyncMock(return_value=file)  # type: ignore[method-assign]
        storage.head_object = AsyncMock()  # type: ignore[method-assign]
        with (
            patch(
                "services.file_storage_service.ensure_not_capacity_locked",
                AsyncMock(side_effect=_locked()),
            ),
            pytest.raises(CapacityLockedError),
        ):
            await service.confirm_upload(workspace_id=ws_id, file_id=file.id, sha256="a" * 64)
        storage.head_object.assert_not_awaited()
        assert file.status == "reserved"


class TestContextCreate:
    @pytest.mark.asyncio
    async def test_create_context_refuses_on_a_locked_workspace(self) -> None:
        ws = MagicMock(id=uuid4(), plan_name="free")
        service = ContextService(_db_returning(ws))
        with (
            patch(
                "services.capacity_lock.ensure_not_capacity_locked",
                AsyncMock(side_effect=_locked()),
            ) as ensure,
            pytest.raises(CapacityLockedError),
        ):
            await service.create_context(workspace_id=ws.id, name="new-context")
        assert ensure.await_args.args[1] is ws
