"""#2959: the memories a ``reference`` miss names, as the service finds them.

``MemoryService.similar_id_candidates`` looks, inside the bounds of the first
8 hex characters of the asked id, at the nearest ids below and above it in the
named context (two primary-key range queries), ranks them by how long a
leading hex part they share with the asked id, and keeps those the caller
could read with a direct ``reference``. It returns plain values, never rows.

The database is mocked here: these pin the SQL and the order of the checks.
``tests/integration/test_reference_id_hint_db.py`` runs it on real rows.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services.memory_service import MemoryService
from utils.exceptions import CapacityLockedError


def _sql(statement) -> str:
    return " ".join(
        str(
            statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        ).split()
    )


def _sharing(asked: UUID, shared: int, *, step: int = 1) -> UUID:
    """An id whose first ``shared`` hex characters are ``asked``'s and whose
    next one differs by ``step``."""
    head = asked.hex[:shared]
    differing = format((int(asked.hex[shared], 16) + step) % 16, "x")
    return UUID(head + differing + uuid4().hex[shared + 1 :])


def _row(memory_id: UUID, *, summary: str = "s", mtype: str = "note") -> SimpleNamespace:
    return SimpleNamespace(
        id=memory_id,
        user_id="author",
        workspace_id=uuid4(),
        context_id=uuid4(),
        type=mtype,
        source_type="manual",
        summary=summary,
    )


def _service(below: list, above: list) -> tuple[MemoryService, list]:
    """A service whose two range queries answer ``below`` then ``above``."""
    statements: list = []
    answers = iter([below, above])

    async def execute(statement, *_args, **_kwargs):
        statements.append(statement)
        return MagicMock(all=MagicMock(return_value=next(answers)))

    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    service = MemoryService(db=db)
    service._ensure_capacity = AsyncMock()
    return service, statements


@pytest.mark.asyncio
class TestTheLookup:
    async def test_two_primary_key_ranges_bounded_by_the_first_8_hex_characters(self):
        asked, context_id, workspace_id = uuid4(), uuid4(), uuid4()
        service, statements = _service([], [])

        await service.similar_id_candidates(
            asked, "caller", context_id=context_id, workspace_id=workspace_id
        )

        prefix = asked.hex[:8]
        low = str(UUID(prefix + "0" * 24))
        high = str(UUID(prefix + "f" * 24))
        assert len(statements) == 2
        below, above = (_sql(s) for s in statements)
        # The nearest ids on each side of the asked one share its longest
        # leading part, so one query walks down from it and one walks up.
        assert below.count(f"memories.id >= '{low}' AND memories.id < '{asked}'") == 1
        assert below.count("ORDER BY memories.id DESC LIMIT 10") == 1
        assert above.count(f"memories.id > '{asked}' AND memories.id <= '{high}'") == 1
        assert above.count("ORDER BY memories.id ASC LIMIT 10") == 1
        for sql in (below, above):
            assert sql.count(f"memories.context_id = '{context_id}'") == 1
            assert sql.count(f"memories.workspace_id = '{workspace_id}'") == 1
            assert sql.count("memories.deleted_at IS NULL") == 1
            # Plain columns: never the content or a whole row.
            assert sql.count("memories.content") == 0
            assert sql.count("memories.summary") == 1

    async def test_no_row_in_the_range_means_no_permission_or_lock_check(self):
        service, statements = _service([], [])
        permission = MagicMock()

        with patch("services.permission_service.PermissionService", permission):
            found = await service.similar_id_candidates(
                uuid4(), "caller", context_id=uuid4(), workspace_id=uuid4()
            )

        assert found == []
        assert len(statements) == 2
        permission.assert_not_called()
        service._ensure_capacity.assert_not_awaited()

    async def test_longest_shared_part_first_each_through_the_read_gate(self):
        asked = UUID("8" * 32)
        long_match = _row(_sharing(asked, 18), summary="eighteen")
        # Lower in id order than the asked id and the 18-character match:
        # ranking by id would put it first.
        short_match = _row(_sharing(asked, 8, step=-1), summary="eight")
        denied = _row(_sharing(asked, 12), summary="hidden", mtype="decision")
        service, _ = _service([short_match], [long_match, denied])
        gate = MagicMock()
        gate.can_access_memory = AsyncMock(side_effect=lambda **kw: kw["memory_type"] != "decision")
        key_ws = uuid4()

        with patch("services.permission_service.PermissionService", return_value=gate) as cls:
            found = await service.similar_id_candidates(
                asked,
                "caller",
                context_id=uuid4(),
                workspace_id=uuid4(),
                key_workspace_id=key_ws,
            )

        assert found == [(long_match.id, "eighteen"), (short_match.id, "eight")]
        assert all(type(memory_id) is UUID and type(s) is str for memory_id, s in found)
        cls.assert_called_once()  # one gate for the whole loop
        checked = [c.kwargs for c in gate.can_access_memory.await_args_list]
        assert [c["memory_type"] for c in checked] == ["note", "decision", "note"]
        for kwargs, row in zip(checked, [long_match, denied, short_match], strict=True):
            # No access event for a memory nobody asked for.
            assert kwargs["operation"] is None
            assert kwargs["memory_id"] is None
            assert kwargs["user_id"] == "caller"
            assert kwargs["memory_user_id"] == row.user_id
            assert kwargs["workspace_id"] == row.workspace_id
            assert kwargs["context_id"] == row.context_id
            assert kwargs["memory_source_type"] == "manual"
            assert kwargs["key_workspace_id"] == key_ws
        service._ensure_capacity.assert_awaited_once()

    async def test_at_most_three(self):
        asked = UUID("8" * 32)
        rows = [_row(_sharing(asked, n)) for n in (9, 10, 11, 12, 13)]
        service, _ = _service(rows[:2], rows[2:])
        gate = MagicMock(can_access_memory=AsyncMock(return_value=True))

        with patch("services.permission_service.PermissionService", return_value=gate):
            found = await service.similar_id_candidates(
                asked, "caller", context_id=uuid4(), workspace_id=uuid4()
            )

        assert [memory_id for memory_id, _ in found] == [rows[4].id, rows[3].id, rows[2].id]

    async def test_a_capacity_locked_workspace_gets_no_candidates(self):
        asked = uuid4()
        service, _ = _service([_row(_sharing(asked, 8))], [])
        service._ensure_capacity = AsyncMock(side_effect=CapacityLockedError.for_outsider())
        workspace_id = uuid4()
        permission = MagicMock()

        with patch("services.permission_service.PermissionService", permission):
            found = await service.similar_id_candidates(
                asked, "caller", context_id=uuid4(), workspace_id=workspace_id
            )

        assert found == []
        service._ensure_capacity.assert_awaited_once_with("caller", workspace_id=workspace_id)
        permission.assert_not_called()
