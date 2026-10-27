"""Admin DELETE /admin/users/{id} keeps the user's connector keys (#2352).

The user's live connector write keys go to each key's workspace owner after the
``users`` row lock and before the ``api_keys`` count and delete, so
``deleted.api_keys`` counts only the rows the delete removes and the response
reports the moved keys under ``reowned``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Delete

from api.routes.admin import delete_user


def _sql(stmt: Any) -> str:
    return " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())


async def _call() -> tuple[dict, list[Any], MagicMock]:
    order: list[Any] = []

    def _execute(stmt, *a, **k):
        order.append(stmt)
        result = MagicMock()
        result.scalar.return_value = 3
        result.scalars.return_value.all.return_value = []
        result.scalar_one_or_none.return_value = SimpleNamespace(email="t@example.com")
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _reown(session, user_id):
        order.append(("reown", user_id))
        return 2

    with (
        patch("services.system_admin_service.SystemAdminService") as admin_svc,
        patch("services.identity_link_service.hand_over_private_contexts", new=AsyncMock()),
        patch("services.file_storage_service.FileStorageService") as storage,
        patch(
            "services.billing_contract.owned_workspaces_under_contract",
            new=AsyncMock(return_value=[]),
        ),
        patch("api.routes.admin.reown_connector_keys", new=AsyncMock(side_effect=_reown)),
        patch("api.routes.admin.logger") as mock_logger,
    ):
        admin_svc.return_value.can_delete_admin = AsyncMock(return_value=(True, None))
        storage.return_value.purge_files_for_contexts = AsyncMock(return_value={})
        response = await delete_user("target-1", {"user_id": "admin-1"}, db)
    return response, order, mock_logger


def _index(order: list[Any], predicate) -> int:
    return next(i for i, item in enumerate(order) if predicate(item))


@pytest.mark.asyncio
async def test_reown_runs_after_the_user_lock_and_before_the_key_count_and_delete():
    response, order, _ = await _call()

    reown = order.index(("reown", "target-1"))
    user_lock = _index(
        order,
        lambda s: (
            not isinstance(s, tuple) and "FROM users" in _sql(s) and _sql(s).endswith("FOR UPDATE")
        ),
    )
    key_count = _index(
        order, lambda s: not isinstance(s, tuple) and "count(api_keys.id)" in _sql(s)
    )
    key_delete = _index(
        order,
        lambda s: isinstance(s, Delete) and s.table.name == "api_keys",
    )
    assert user_lock < reown < key_count < key_delete
    assert order.count(("reown", "target-1")) == 1


@pytest.mark.asyncio
async def test_response_reports_moved_and_deleted_keys():
    response, _, mock_logger = await _call()

    assert response["deleted"]["api_keys"] == 3
    assert response["reowned"] == {"connector_api_keys": 2}
    (call,) = [c for c in mock_logger.info.call_args_list if c.args[0] == "admin_delete_user"]
    assert call.kwargs["connector_api_keys_reowned"] == 2
    assert call.kwargs["api_keys_deleted"] == 3
