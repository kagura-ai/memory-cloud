"""#2043 PR2: ``GET /api/v1/memory/{id}/revisions`` and ``version`` on
``POST /api/v1/memory/reference``.

The service is mocked; the route's job is the query validation, the cursor
decode and passing ``version`` through. The capacity classification is in
``test_capacity_lock_route_table.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.schemas import MemoryRevisionItem, MemoryRevisionsResponse
from utils.exceptions import NotFoundException, ValidationError


def _client(service: MagicMock) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.main import memory_cloud_exception_handler
    from api.routes.memory import get_memory_service, router
    from auth.dependencies import get_user_from_api_key_or_session
    from utils.exceptions import MemoryCloudException

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/memory")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: {"user_id": "u1"}
    app.dependency_overrides[get_memory_service] = lambda: service
    return TestClient(app)


def _page(memory_id: Any) -> MemoryRevisionsResponse:
    return MemoryRevisionsResponse(
        memory_id=memory_id,
        current_version=2,
        revisions=[
            MemoryRevisionItem(
                version=2,
                kind="updated",
                at=datetime(2026, 10, 2, 9, 0),
                changed_fields=["importance"],
                writer="api",
                by_you=False,
            )
        ],
        has_more=True,
        next_cursor="41",
    )


def test_revisions_page_and_cursor() -> None:
    memory_id = uuid4()
    service = MagicMock(list_revisions=AsyncMock(return_value=_page(memory_id)))

    with _client(service) as client:
        response = client.get(
            f"/api/v1/memory/{memory_id}/revisions", params={"limit": 5, "cursor": "42"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["current_version"] == 2
    assert body["next_cursor"] == "41"
    assert body["revisions"][0]["at"] == "2026-10-02T09:00:00Z"  # UTC on the wire
    assert body["revisions"][0]["changed_fields"] == ["importance"]
    assert "_cursor" not in body["revisions"][0]
    assert service.list_revisions.await_args.args == (memory_id,)
    assert service.list_revisions.await_args.kwargs == {
        "user_id": "u1",
        "before_id": 42,
        "limit": 5,
        "key_workspace_id": None,
    }


def test_revisions_defaults() -> None:
    memory_id = uuid4()
    service = MagicMock(list_revisions=AsyncMock(return_value=_page(memory_id)))
    with _client(service) as client:
        assert client.get(f"/api/v1/memory/{memory_id}/revisions").status_code == 200
    assert service.list_revisions.await_args.kwargs == {
        "user_id": "u1",
        "before_id": None,
        "limit": 20,
        "key_workspace_id": None,
    }


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"cursor": "abc"}, {"cursor": "-1"}, {"cursor": "1" * 19}],
)
def test_revisions_refuses_bad_query(params: dict[str, Any]) -> None:
    service = MagicMock(list_revisions=AsyncMock())
    with _client(service) as client:
        response = client.get(f"/api/v1/memory/{uuid4()}/revisions", params=params)
    assert response.status_code == 422
    service.list_revisions.assert_not_awaited()


def test_revisions_not_found() -> None:
    service = MagicMock(list_revisions=AsyncMock(side_effect=NotFoundException("Memory", "x")))
    with _client(service) as client:
        assert client.get(f"/api/v1/memory/{uuid4()}/revisions").status_code == 404


def test_reference_passes_version_and_maps_an_unknown_one_to_422() -> None:
    service = MagicMock(
        reference=AsyncMock(
            side_effect=ValidationError("no such version", field="version", current_version=3)
        )
    )
    memory_id = uuid4()
    with _client(service) as client:
        response = client.post(
            "/api/v1/memory/reference", json={"memory_id": str(memory_id), "version": 9}
        )
        assert (
            client.post(
                "/api/v1/memory/reference", json={"memory_id": str(memory_id), "version": 0}
            ).status_code
            == 422
        )
    assert response.status_code == 422
    assert response.json()["details"]["current_version"] == 3
    assert service.reference.await_args.kwargs == {
        "user_id": "u1",
        "version": 9,
        "key_workspace_id": None,
    }


def test_reference_a_historical_type_the_filter_denies_is_the_live_denials_404() -> None:
    """#2116: the REST route mirrors the MCP tool — the real service under a
    per-memory type filter that denies version 1's type answers the same 404
    body a denied live row does."""
    from unittest.mock import patch

    from tests.services.test_memory_revisions_read import _type_changed_service

    def call(*, live_allowed: bool) -> tuple[int, Any]:
        memory, service, perm = _type_changed_service({"note"} if live_allowed else set())
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
            _client(service) as client,
        ):
            response = client.post(
                "/api/v1/memory/reference", json={"memory_id": str(memory.id), "version": 1}
            )
        return response.status_code, response.text.replace(str(memory.id), "<id>")

    historical_denied = call(live_allowed=True)
    assert historical_denied[0] == 404
    assert historical_denied == call(live_allowed=False)


def test_revisions_a_historical_type_the_filter_denies_names_no_field() -> None:
    """#2169: the REST mirror of the MCP tool — under a type filter that
    denies version 1's type, the edit out of it is listed (200, same paging)
    with an empty ``changed_fields``; an unfiltered caller sees the names."""
    from unittest.mock import patch

    from tests.services.test_memory_revisions_read import (
        TYPE_CHANGED_PAGE,
        _type_changed_list_service,
    )

    def call(allowed: set[str]) -> dict[str, Any]:
        memory, service, perm = _type_changed_list_service(allowed)
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch(
                "services.memory_versions.list_revision_rows",
                new=AsyncMock(return_value=(TYPE_CHANGED_PAGE, True)),
            ),
            patch(
                "services.memory_versions.type_changes_above",
                new=AsyncMock(return_value=[(2, "decision")]),
            ),
            patch(
                "services.identity_link_service.linked_user_ids",
                new=AsyncMock(return_value=frozenset({"u1"})),
            ),
            _client(service) as client,
        ):
            response = client.get(f"/api/v1/memory/{memory.id}/revisions")
        assert response.status_code == 200
        return response.json()

    filtered = call({"note"})
    unfiltered = call({"note", "decision"})
    assert [r["changed_fields"] for r in filtered["revisions"]] == [["summary"], [], []]
    assert [r["changed_fields"] for r in unfiltered["revisions"]] == [
        ["summary"],
        ["summary", "type"],
        [],
    ]
    assert [r["version"] for r in filtered["revisions"]] == [3, 2, 1]
    for key in ("current_version", "has_more", "next_cursor"):
        assert filtered[key] == unfiltered[key]
