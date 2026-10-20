"""#2043 PR2: the ``list_revisions`` handler and ``reference(version=N)``.

The service is mocked (its behaviour is in
``tests/services/test_memory_revisions_read.py``); here: argument validation,
the uniform not-found, the envelope, ``max_chars`` bounding with a cursor that
continues from the last item kept, and the historical reference projection.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.memory import handle_reference
from mcp_server.tools.revisions import handle_list_revisions
from models.schemas import (
    LinkedMemoryRef,
    MemoryRevisionItem,
    MemoryRevisionsResponse,
    ReferenceResponse,
)
from services.memory_versions import UnknownVersionError
from utils.exceptions import NotFoundException, ValidationError

_CTX = str(uuid4())


def _payload(result: list[Any]) -> dict[str, Any]:
    assert len(result) == 1
    return json.loads(result[0].text)


def _page(n: int, *, has_more: bool = False, memory_id: Any = None) -> MemoryRevisionsResponse:
    items = []
    for i in range(n):
        item = MemoryRevisionItem(
            version=n - i,
            kind="updated" if i < n - 1 else "created",
            at=datetime(2026, 10, 1, 12, i % 60),
            changed_fields=["summary", "tags"] if i < n - 1 else [],
            writer="api",
            by_you=i % 2 == 0,
            session_id="sess-x" if i % 2 == 0 else None,
        )
        item._cursor = str(1000 - i)
        items.append(item)
    return MemoryRevisionsResponse(
        memory_id=memory_id or uuid4(),
        current_version=n,
        revisions=items,
        has_more=has_more,
        next_cursor=items[-1]._cursor if has_more else None,
    )


def _run(handler, args: dict[str, Any], service: MagicMock):
    async def gen():
        yield AsyncMock()

    module = handler.__module__
    stack = ExitStack()
    stack.enter_context(patch("db.base.get_db", new=gen))
    stack.enter_context(
        patch(
            f"{module}._resolve_context_for_read",
            new=AsyncMock(return_value=MagicMock(last_used_at=None)),
        )
    )
    stack.enter_context(patch(f"{module}._log_tool_usage", new=AsyncMock()))
    stack.enter_context(
        patch(f"{module}._context_response_fields", return_value={"context_id": _CTX})
    )
    stack.enter_context(patch("services.memory_service.MemoryService", return_value=service))
    return stack


async def _list(args: dict[str, Any], service: MagicMock) -> dict[str, Any]:
    with _run(handle_list_revisions, args, service):
        return _payload(await handle_list_revisions(args, user_id="u", workspace_id=uuid4()))


class TestListRevisionsArguments:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("args", "error"),
        [
            ({"context_id": _CTX}, "memory_id_required"),
            ({"context_id": _CTX, "memory_id": "nope"}, "invalid_memory_id_format"),
            ({"context_id": _CTX, "memory_id": str(uuid4()), "cursor": "abc"}, "validation_error"),
            ({"context_id": _CTX, "memory_id": str(uuid4()), "cursor": 5}, "validation_error"),
            (
                {"context_id": _CTX, "memory_id": str(uuid4()), "cursor": "1" * 19},
                "validation_error",
            ),
            ({"context_id": _CTX, "memory_id": str(uuid4()), "limit": "5"}, "validation_error"),
            ({"context_id": _CTX, "memory_id": str(uuid4()), "max_chars": 10}, "validation_error"),
        ],
    )
    async def test_refused_before_any_read(self, args: dict[str, Any], error: str) -> None:
        service = MagicMock(list_revisions=AsyncMock())
        payload = await _list(args, service)
        assert payload["error"] == error
        service.list_revisions.assert_not_awaited()


class TestListRevisions:
    @pytest.mark.asyncio
    async def test_envelope_and_paging_arguments(self) -> None:
        memory_id = uuid4()
        page = _page(3, has_more=True, memory_id=memory_id)
        service = MagicMock(list_revisions=AsyncMock(return_value=page))

        payload = await _list(
            {"context_id": _CTX, "memory_id": str(memory_id), "limit": 3, "cursor": "1200"},
            service,
        )

        assert payload["status"] == "success"
        assert payload["memory_id"] == str(memory_id)
        assert payload["current_version"] == 3
        assert payload["count"] == 3
        assert payload["has_more"] is True
        assert payload["next_cursor"] == "998"
        assert payload["context_id"] == _CTX
        first, second, last = payload["revisions"]
        assert first == {
            "version": 3,
            "kind": "updated",
            "at": "2026-10-01T12:00:00Z",
            "changed_fields": ["summary", "tags"],
            "writer": "api",
            "by_you": True,
            "session_id": "sess-x",
        }
        assert "session_id" not in second and "agent_id" not in second  # absent, never null
        assert last["kind"] == "created"
        assert "truncated" not in payload
        kwargs = service.list_revisions.await_args.kwargs
        assert (kwargs["user_id"], kwargs["before_id"], kwargs["limit"]) == ("u", 1200, 3)

    @pytest.mark.asyncio
    async def test_limit_is_clamped_to_one_hundred(self) -> None:
        service = MagicMock(list_revisions=AsyncMock(return_value=_page(1)))
        await _list({"context_id": _CTX, "memory_id": str(uuid4()), "limit": 500}, service)
        assert service.list_revisions.await_args.kwargs["limit"] == 100

    @pytest.mark.asyncio
    async def test_a_cut_page_continues_from_the_last_item_kept(self) -> None:
        page = _page(100)
        service = MagicMock(list_revisions=AsyncMock(return_value=page))

        payload = await _list(
            {"context_id": _CTX, "memory_id": str(uuid4()), "limit": 100, "max_chars": 10_000},
            service,
        )

        kept = payload["count"]
        assert 0 < kept < 100
        assert len(payload["revisions"]) == kept
        assert payload["truncated"] is True and payload["has_more"] is True
        assert payload["next_cursor"] == page.revisions[kept - 1]._cursor
        assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= 10_000

    @pytest.mark.asyncio
    async def test_a_denied_or_forgotten_memory_is_the_uniform_not_found(self) -> None:
        service = MagicMock(list_revisions=AsyncMock(side_effect=NotFoundException("Memory", "x")))
        payload = await _list({"context_id": _CTX, "memory_id": str(uuid4())}, service)
        assert payload["error"] == "memory_not_found"


# ---------------------------------------------------------- reference(version)


def _historical(mem_id: Any) -> ReferenceResponse:
    return ReferenceResponse(
        memory_id=mem_id,
        summary="old summary",
        context_summary=None,
        content="old content",
        details=None,
        type="note",
        scope="working",
        importance=0.5,
        tags=["a"],
        context=None,
        created_at=datetime(2026, 10, 1),
        updated_at=datetime(2026, 10, 2),
        version=2,
        historical=True,
        current_version=5,
        unavailable_fields=["details"],
        client="api",
    )


async def _reference(args: dict[str, Any], service: MagicMock) -> dict[str, Any]:
    with _run(handle_reference, args, service):
        return _payload(await handle_reference(args, user_id="u", workspace_id=uuid4()))


class TestReferenceVersion:
    @pytest.mark.asyncio
    async def test_a_historical_read_leaves_out_links_and_names_the_live_version(self) -> None:
        mem_id = uuid4()
        service = MagicMock(reference=AsyncMock(return_value=_historical(mem_id)))

        payload = await _reference(
            {"context_id": _CTX, "memory_id": str(mem_id), "version": 2}, service
        )

        m = payload["memory"]
        assert (m["version"], m["historical"], m["current_version"]) == (2, True, 5)
        assert m["unavailable_fields"] == ["details"]
        assert m["summary"] == "old summary" and m["content"] == "old content"
        for key in (
            "outgoing_links",
            "outgoing_has_more",
            "incoming_links",
            "incoming_has_more",
            "supersede_candidate",
        ):
            assert key not in m
        assert service.reference.await_args.kwargs["version"] == 2

    @pytest.mark.asyncio
    async def test_fields_links_alone_still_answers(self) -> None:
        mem_id = uuid4()
        service = MagicMock(reference=AsyncMock(return_value=_historical(mem_id)))
        payload = await _reference(
            {"context_id": _CTX, "memory_id": str(mem_id), "version": 2, "fields": ["links"]},
            service,
        )
        assert payload["status"] == "success"
        assert "outgoing_links" not in payload["memory"]

    @pytest.mark.asyncio
    async def test_the_current_version_keeps_todays_shape(self) -> None:
        mem_id = uuid4()
        current = _historical(mem_id).model_copy(
            update={
                "historical": False,
                "current_version": None,
                "unavailable_fields": None,
                "version": 5,
                "outgoing_links": [
                    LinkedMemoryRef(
                        memory_id=uuid4(),
                        summary="l",
                        importance=0.5,
                        weight=1.0,
                        created_at=datetime(2026, 1, 1),
                    )
                ],
            }
        )
        service = MagicMock(reference=AsyncMock(return_value=current))
        payload = await _reference(
            {"context_id": _CTX, "memory_id": str(mem_id), "version": 5}, service
        )
        m = payload["memory"]
        assert "historical" not in m and "current_version" not in m
        assert len(m["outgoing_links"]) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", [0, -2, "3", True, 1.5])
    async def test_a_malformed_version_is_refused_before_any_read(self, version: Any) -> None:
        service = MagicMock(reference=AsyncMock())
        payload = await _reference(
            {"context_id": _CTX, "memory_id": str(uuid4()), "version": version}, service
        )
        assert payload["error"] == "invalid_argument"
        service.reference.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unknown_version_is_validation_error_with_the_current_version(self) -> None:
        service = MagicMock(reference=AsyncMock(side_effect=UnknownVersionError(9, 4)))
        payload = await _reference(
            {"context_id": _CTX, "memory_id": str(uuid4()), "version": 9}, service
        )
        assert payload["error"] == "validation_error"
        assert payload["current_version"] == 4
        assert payload["received"] == 9


@pytest.mark.asyncio
async def test_a_historical_type_the_filter_denies_is_the_live_denials_not_found() -> None:
    """#2116: the real service under a per-memory type filter (#1299) that
    allows today's type but not version 1's answers exactly what a denied
    live row answers — no new error shape."""
    from tests.services.test_memory_revisions_read import _type_changed_service

    async def call(*, live_allowed: bool) -> dict[str, Any]:
        memory, service, perm = _type_changed_service({"note"} if live_allowed else set())
        args = {"context_id": _CTX, "memory_id": str(memory.id), "version": 1}
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
        ):
            payload = await _reference(args, service)
        payload["message"] = payload["message"].replace(str(memory.id), "<id>")
        return payload

    historical_denied = await call(live_allowed=True)
    assert historical_denied["error"] == "memory_not_found"
    assert historical_denied == await call(live_allowed=False)


@pytest.mark.asyncio
async def test_another_validation_error_is_not_reported_as_a_bad_version() -> None:
    service = MagicMock(reference=AsyncMock(side_effect=ValidationError("something else")))
    with pytest.raises(ValidationError):
        await _reference({"context_id": _CTX, "memory_id": str(uuid4())}, service)


@pytest.mark.asyncio
async def test_list_revisions_scopes_the_memory_to_the_named_context() -> None:
    service = MagicMock(list_revisions=AsyncMock(return_value=_page(1)))
    await _list({"context_id": _CTX, "memory_id": str(uuid4())}, service)
    assert str(service.list_revisions.await_args.kwargs["context_id"]) == _CTX
