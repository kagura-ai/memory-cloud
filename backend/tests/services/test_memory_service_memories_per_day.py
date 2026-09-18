"""Which writes are charged against the daily memory-creation quota (#1549).

``MemoryService.remember`` is the ONE place user-visible memory creation is
charged (MCP ``remember``, REST ``POST /memory/remember`` and the create half
of ``update_memory(external_id=...)`` all funnel through it). Everything that
merely mutates an existing row must not touch the counter — pinned here by
making ``QuotaService.check_memories_per_day`` raise and asserting the
excluded path still succeeds.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import PatchMemoryRequest, RememberRequest, UpdateMemoryRequest
from services.memory_service import MemoryService
from utils.exceptions import QuotaExceededError

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def service(mock_db):
    svc = MemoryService(mock_db)
    svc.memory_repo = MagicMock()
    svc.memory_repo.create = AsyncMock()
    return svc


@pytest.fixture
def quota_service():
    """Patch the lazily imported ``QuotaService`` class inside ``remember``."""
    with patch("services.quota_service.QuotaService") as cls:
        instance = cls.return_value
        instance.check_memory_quota = AsyncMock(return_value=(True, None))
        instance.check_memories_per_day = AsyncMock(return_value=(True, None))
        yield instance


def _req() -> RememberRequest:
    return RememberRequest(summary="daily quota test memory", content="body", type="note")


def _make_memory(**overrides) -> MagicMock:
    memory = MagicMock()
    memory.id = overrides.get("id", uuid4())
    memory.user_id = "test_user"
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    memory.summary = "Original summary for testing edits"
    memory.context_summary = None
    memory.content = "Original content body"
    memory.details = None
    memory.type = "note"
    memory.importance = 0.5
    memory.tags = ["original"]
    memory.context = None
    memory.scope = "working"
    memory.client = "mcp"
    # Real datetimes: patch_memory's ReferenceResponse validates them.
    memory.created_at = datetime(2026, 4, 25, 10, 0, 0)
    memory.updated_at = datetime(2026, 4, 25, 10, 0, 0)
    memory.deleted_at = None
    memory.deleted_by = None
    memory.embedding_status = "success"
    memory.source_uri = None
    memory.source_type = None
    return memory


class TestRememberCharges:
    """``remember`` reserves exactly one unit, after the total-count check."""

    async def test_remember_charges_once_after_total_count_check(self, service, quota_service):
        ws_id = uuid4()
        # Stop right after the quota gates: no context → the established
        # ValueError, before any row is written.
        service._get_context_isolation_params = AsyncMock(return_value=(None, None, None))

        with pytest.raises(ValueError, match="requires current_context_id"):
            await service.remember(_req(), user_id="u", current_workspace_id=ws_id)

        quota_service.check_memory_quota.assert_awaited_once_with(ws_id, raise_on_exceeded=True)
        quota_service.check_memories_per_day.assert_awaited_once_with(
            ws_id, count=1, raise_on_exceeded=True
        )
        assert [c[0] for c in quota_service.mock_calls] == [
            "check_memory_quota",
            "check_memories_per_day",
        ]

    async def test_refusal_propagates_and_nothing_is_written(self, service, quota_service):
        quota_service.check_memories_per_day.side_effect = QuotaExceededError(
            "Daily memory-creation quota exceeded", quota_type="memories_per_day"
        )
        service._get_context_isolation_params = AsyncMock()

        with pytest.raises(QuotaExceededError) as excinfo:
            await service.remember(_req(), user_id="u", current_workspace_id=uuid4())

        assert excinfo.value.details["quota_type"] == "memories_per_day"
        service._get_context_isolation_params.assert_not_awaited()
        service.memory_repo.create.assert_not_awaited()

    async def test_no_workspace_skips_both_quota_gates(self, service, quota_service):
        service._get_context_isolation_params = AsyncMock(return_value=(None, None, None))

        with pytest.raises(ValueError, match="requires current_context_id"):
            await service.remember(_req(), user_id="u", current_workspace_id=None)

        quota_service.check_memory_quota.assert_not_awaited()
        quota_service.check_memories_per_day.assert_not_awaited()

    async def test_skip_flag_keeps_total_count_check_but_not_daily(self, service, quota_service):
        """The private opt-out used by an external_id *replacement*."""
        ws_id = uuid4()
        service._get_context_isolation_params = AsyncMock(return_value=(None, None, None))

        with pytest.raises(ValueError, match="requires current_context_id"):
            await service.remember(
                _req(), user_id="u", current_workspace_id=ws_id, _skip_daily_quota=True
            )

        quota_service.check_memory_quota.assert_awaited_once()
        quota_service.check_memories_per_day.assert_not_awaited()


class TestUpsertByExternalId:
    """Create → charged (via ``remember``); replace → not charged."""

    def _stub_remember(self, service):
        response = MagicMock()
        response.memory_id = uuid4()
        response.scope = "working"
        response.persistence = None
        response.lint = []
        service.remember = AsyncMock(return_value=response)

    def _request(self) -> UpdateMemoryRequest:
        return UpdateMemoryRequest(
            external_id="doc-1",
            summary="Upsert daily quota test memory",
            content="body",
            type="note",
        )

    async def test_brand_new_external_id_is_charged(self, service):
        service.memory_repo.get_by_resource_id = AsyncMock(return_value=None)
        self._stub_remember(service)

        result = await service.update_memory(
            self._request(), user_id="u", current_context_id=uuid4(), current_workspace_id=uuid4()
        )

        assert result.operation == "created"
        assert service.remember.await_args.kwargs["_skip_daily_quota"] is False

    async def test_replacement_is_not_charged(self, service):
        existing = MagicMock()
        existing.id = uuid4()
        existing.delivery_mode = "on_recall"
        service.memory_repo.get_by_resource_id = AsyncMock(return_value=existing)
        self._stub_remember(service)
        service.forget = AsyncMock()

        result = await service.update_memory(
            self._request(), user_id="u", current_context_id=uuid4(), current_workspace_id=uuid4()
        )

        assert result.operation == "replaced"
        assert service.remember.await_args.kwargs["_skip_daily_quota"] is True


class TestExcludedUpdatePaths:
    """In-place updates never touch the daily counter."""

    @pytest.fixture(autouse=True)
    def _daily_quota_raises(self):
        with patch(
            "services.quota_service.QuotaService.check_memories_per_day",
            new=AsyncMock(side_effect=AssertionError("daily quota must not be charged")),
        ):
            yield

    async def test_update_in_place_does_not_charge(self, service):
        memory = _make_memory()
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
            patch(
                "services.memory_service.resolve_collection_name",
                new=AsyncMock(return_value="kagura_memories"),
            ),
        ):
            mock_perm_cls.return_value.can_access_memory = AsyncMock(return_value=True)
            request = UpdateMemoryRequest(memory_id=memory.id, tags=["updated"], importance=0.9)

            result = await service.update_memory(
                request, user_id="test_user", current_workspace_id=uuid4()
            )

        assert result.operation == "updated"

    async def test_patch_memory_does_not_charge(self, service):
        memory = _make_memory()
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
            patch(
                "services.memory_service.resolve_collection_name",
                new=AsyncMock(return_value="kagura_memories"),
            ),
            patch.object(
                service,
                "_fetch_declared_link_refs",
                new=AsyncMock(return_value=([], False, [], False)),
            ),
        ):
            mock_perm_cls.return_value.can_access_memory = AsyncMock(return_value=True)

            await service.patch_memory(
                memory_id=memory.id,
                request=PatchMemoryRequest(importance=0.8),
                user_id="test_user",
            )

        assert memory.importance == 0.8


class TestSleepConsolidationIsExcluded:
    """Sleep promotes/archives existing rows; it creates none."""

    async def test_promotion_does_not_charge(self):
        from services.sleep.consolidation import ConsolidationPhase
        from tests.services.sleep.test_consolidation import (
            ADOPTION_PROMOTE_MIN,
            _make_working_memory,
            _run_execute,
        )

        with (
            patch("services.sleep.consolidation.MemoryRepository"),
            patch("services.sleep.consolidation.GraphService"),
            patch(
                "services.quota_service.QuotaService.check_memories_per_day",
                new=AsyncMock(side_effect=AssertionError("daily quota must not be charged")),
            ),
        ):
            phase = ConsolidationPhase(AsyncMock(), AsyncMock())
            phase.memory_repo = AsyncMock()
            mems = [_make_working_memory(reference_count=ADOPTION_PROMOTE_MIN, age_days=5)]

            result = await _run_execute(phase, mems)

        assert result.details["rule_promoted"] == 1
        assert phase.memory_repo.promote_to_persistent.await_count == 1


# Paths that construct ``Memory`` rows but are deliberately NOT charged (see
# ``QuotaService.check_memories_per_day``): a context merge copies rows the
# workspace already paid for, admin recovery restores rows that already
# existed, and Sleep / neural only mutate existing rows.
UNCHARGED_SOURCES = [
    "services/context_service.py",
    "api/routes/admin.py",
    "services/agent_bootstrap_service.py",
    *sorted(p.relative_to(SRC).as_posix() for p in (SRC / "services" / "sleep").glob("*.py")),
    *sorted(p.relative_to(SRC).as_posix() for p in (SRC / "neural").glob("*.py")),
]


@pytest.mark.parametrize("relpath", UNCHARGED_SOURCES)
def test_excluded_modules_never_reference_the_daily_quota(relpath: str) -> None:
    assert "check_memories_per_day" not in (SRC / relpath).read_text(encoding="utf-8"), relpath
