"""Unit tests: bounded reference() responses with selective retrieval (#1685).

reference() used to return content, details, context and links in full, so one
large memory produced a response of any size. These tests pin the contract:

* a small memory comes back exactly as before (same keys, order and text);
* the serialized tool result never exceeds ``max_chars`` CHARACTERS (Python str
  code points of the compact JSON text — not tokens, not UTF-8 bytes);
* a field that does not fit is sliced (content) or left out (details, context,
  links) with explicit markers, never cut silently;
* following ``*_next_offset`` reproduces content and details exactly;
* every continuation goes through the same context / memory authorization.

The service is mocked; DB-backed reference() behaviour is covered elsewhere.
These are RESPONSE budgets — the tools/list definition budgets live in
``test_tool_definition_budget.py``.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._constants import (
    REFERENCE_DEFAULT_MAX_CHARS,
    REFERENCE_MAX_CHARS_LIMIT,
    REFERENCE_MIN_MAX_CHARS,
)
from mcp_server.tools._helpers import _ContextNotFoundError
from mcp_server.tools.memory import handle_reference
from models.schemas import LinkedMemoryRef, ReferenceResponse
from utils.exceptions import NotFoundException

MEM_ID = uuid4()
CTX_ID = uuid4()

# Characters that change size when JSON-escaped (quote, backslash, newline, a
# control character) mixed with multi-byte text, so slicing has to measure the
# escaped length rather than assume one output character per input character.
_MIXED = 'ascii "quoted" back\\slash\nnew line\ttab\x01ctl 日本語テキスト 🍣 '


def _big_text(n: int) -> str:
    return (_MIXED * (n // len(_MIXED) + 1))[:n]


def _response(content: str = "full layer-3 content", details=None, context=None, links=1):
    return ReferenceResponse(
        memory_id=MEM_ID,
        summary="seed summary for a bounded reference test",
        context_summary="ctx",
        content=content,
        details={"k": "v"} if details is None else details,
        type="note",
        scope="working",
        importance=0.8,
        tags=["a"],
        context={"file_path": "x.py"} if context is None else context,
        created_at=datetime(2026, 6, 1, 12, 0, 0),
        updated_at=datetime(2026, 6, 2, 9, 30, 0),
        client="api",
        source_uri="vault://v/note.md",
        source_type="vault",
        outgoing_links=[
            LinkedMemoryRef(
                memory_id=uuid4(),
                summary=f"linked memory number {i}",
                type="code",
                importance=0.5,
                weight=1.0,
                created_at=datetime(2026, 5, 1, 0, 0, 0),
            )
            for i in range(links)
        ],
        outgoing_has_more=False,
        incoming_links=[],
        incoming_has_more=False,
    )


async def _call(args: dict, *, service_result=None, service_error=None, resolver=None):
    """Run handle_reference with the service mocked; return (text, service mock)."""
    svc = MagicMock(reference=AsyncMock(return_value=service_result, side_effect=service_error))

    async def gen():
        yield AsyncMock()

    full_args = {"memory_id": str(MEM_ID), "context_id": str(CTX_ID), **args}
    with ExitStack() as stack:
        stack.enter_context(patch("db.base.get_db", new=gen))
        stack.enter_context(
            patch(
                "mcp_server.tools.memory._resolve_context_for_read",
                new=resolver or AsyncMock(return_value=MagicMock(last_used_at=None)),
            )
        )
        stack.enter_context(patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()))
        stack.enter_context(patch("services.memory_service.MemoryService", return_value=svc))
        result = await handle_reference(args=full_args, user_id="u", workspace_id=uuid4())
    assert len(result) == 1
    return result[0].text, svc


def _memory(text: str) -> dict:
    payload = json.loads(text)
    assert payload["status"] == "success", payload
    return payload["memory"]


def _error(text: str) -> dict:
    payload = json.loads(text)
    assert payload["status"] == "error", payload
    return payload


def _legacy_text(result: ReferenceResponse) -> str:
    """The exact text reference() returned before #1685, rebuilt independently."""
    memory = {
        "memory_id": str(result.memory_id),
        "summary": result.summary,
        "context_summary": result.context_summary,
        "content": result.content,
        "details": result.details,
        "type": result.type,
        "scope": result.scope,
        "importance": result.importance,
        "tags": result.tags,
        "context": result.context,
        "created_at": "2026-06-01T12:00:00Z",
        "updated_at": "2026-06-02T09:30:00Z",
        "client": result.client,
        "source_uri": result.source_uri,
        "source_type": result.source_type,
        "outgoing_links": [ref.model_dump(mode="json") for ref in result.outgoing_links],
        "outgoing_has_more": result.outgoing_has_more,
        "incoming_links": [ref.model_dump(mode="json") for ref in result.incoming_links],
        "incoming_has_more": result.incoming_has_more,
        "supersede_candidate": None,
    }
    return json.dumps(
        {"status": "success", "memory": memory}, ensure_ascii=False, separators=(",", ":")
    )


# ------------------------------------------------------------- small memories


@pytest.mark.asyncio
async def test_small_memory_is_returned_exactly_as_before():
    result = _response()
    text, _ = await _call({}, service_result=result)
    assert text == _legacy_text(result)


@pytest.mark.asyncio
async def test_small_memory_is_unchanged_under_an_explicit_budget():
    result = _response()
    text, _ = await _call({"max_chars": REFERENCE_MIN_MAX_CHARS}, service_result=result)
    assert text == _legacy_text(result)


def test_default_budget_constants_are_ordered():
    assert REFERENCE_MIN_MAX_CHARS < REFERENCE_DEFAULT_MAX_CHARS < REFERENCE_MAX_CHARS_LIMIT
    assert REFERENCE_DEFAULT_MAX_CHARS == 20_000


# ------------------------------------------------------------ field selection


@pytest.mark.asyncio
async def test_fields_selects_heavy_fields_and_keeps_light_ones():
    text, _ = await _call({"fields": ["details"]}, service_result=_response())
    memory = _memory(text)
    assert memory["details"] == {"k": "v"}
    for dropped in ("content", "context", "outgoing_links", "incoming_links", "outgoing_has_more"):
        assert dropped not in memory
    # Light fields always come back.
    for kept in ("memory_id", "summary", "context_summary", "tags", "updated_at", "source_uri"):
        assert kept in memory


@pytest.mark.asyncio
async def test_empty_fields_returns_only_light_fields():
    text, _ = await _call({"fields": []}, service_result=_response())
    memory = _memory(text)
    assert not {"content", "details", "context", "outgoing_links"} & memory.keys()
    assert memory["summary"] == "seed summary for a bounded reference test"


def test_fields_sent_as_a_json_string_is_coerced_at_dispatch():
    """Quirky clients stringify arrays; dispatch coerces them by the schema type."""
    from mcp_server.tools._arg_coercion import coerce_mcp_arguments

    args = coerce_mcp_arguments("reference", {"fields": '["content"]'})
    assert args["fields"] == ["content"]


# ------------------------------------------------------------- large memories


@pytest.mark.asyncio
async def test_issue_reproduction_is_bounded_with_explicit_markers():
    """The audit's 524,288 + 524,288 character memory, at the default budget."""
    content = _big_text(524_288)
    details = {"raw": _big_text(524_288)}
    text, _ = await _call({}, service_result=_response(content=content, details=details))

    assert len(text) <= REFERENCE_DEFAULT_MAX_CHARS
    memory = _memory(text)  # still valid JSON
    # content: a prefix slice, explicitly marked
    assert memory["content_truncated"] is True
    assert memory["content_offset"] == 0
    assert memory["content_total_chars"] == len(content)
    assert content.startswith(memory["content"])
    assert memory["content_next_offset"] == len(memory["content"]) > 0
    # details: never sliced mid-structure — left out, with its size and the offset to start at
    assert "details" not in memory
    assert memory["details_omitted"] is True
    assert memory["details_total_chars"] == len(
        json.dumps(details, ensure_ascii=False, separators=(",", ":"))
    )
    assert memory["details_next_offset"] == 0
    # small whole-or-omitted fields still fit
    assert memory["context"] == {"file_path": "x.py"}
    assert len(memory["outgoing_links"]) == 1


@pytest.mark.asyncio
async def test_small_details_stay_whole_next_to_large_content():
    content = _big_text(100_000)
    text, _ = await _call({}, service_result=_response(content=content))
    memory = _memory(text)
    assert len(text) <= REFERENCE_DEFAULT_MAX_CHARS
    assert memory["details"] == {"k": "v"}
    assert memory["content_truncated"] is True


@pytest.mark.asyncio
async def test_large_context_and_links_are_omitted_with_markers():
    context = {"blob": "x" * 30_000}
    text, _ = await _call({}, service_result=_response(context=context, links=50))
    memory = _memory(text)
    assert len(text) <= REFERENCE_DEFAULT_MAX_CHARS
    assert "context" not in memory
    assert memory["context_omitted"] is True
    assert memory["context_next_offset"] == 0
    # 50 links fit in 20k here; force links out with a tighter budget
    text, _ = await _call(
        {"max_chars": REFERENCE_MIN_MAX_CHARS},
        service_result=_response(context=context, links=50),
    )
    memory = _memory(text)
    assert len(text) <= REFERENCE_MIN_MAX_CHARS
    assert memory["links_omitted"] is True
    assert memory["links_total_chars"] > REFERENCE_MIN_MAX_CHARS
    assert "outgoing_links" not in memory and "incoming_has_more" not in memory


@pytest.mark.parametrize("max_chars", [REFERENCE_MIN_MAX_CHARS, 12_345, REFERENCE_MAX_CHARS_LIMIT])
@pytest.mark.asyncio
async def test_every_budget_is_respected(max_chars):
    result = _response(content=_big_text(300_000), details={"raw": _big_text(300_000)})
    text, _ = await _call({"max_chars": max_chars}, service_result=result)
    assert len(text) <= max_chars
    # the budget is filled, not merely respected
    assert len(text) > max_chars - 100


# ----------------------------------------------------------------- continuation


async def _follow_content(result: ReferenceResponse, max_chars: int) -> tuple[str, int]:
    """Page content from 0 via content_next_offset; return (joined text, calls)."""
    pieces, offset, calls = [], 0, 0
    while offset is not None:
        text, _ = await _call(
            {"content_offset": offset, "max_chars": max_chars}, service_result=result
        )
        calls += 1
        assert len(text) <= max_chars
        memory = _memory(text)
        assert memory["content_offset"] == offset
        assert "details" not in memory  # an offset alone selects just that field
        pieces.append(memory["content"])
        offset = memory["content_next_offset"]
        assert memory["content_truncated"] is (offset is not None)
    return "".join(pieces), calls


async def _follow_json(result: ReferenceResponse, field: str, max_chars: int) -> str:
    pieces, offset = [], 0
    while offset is not None:
        text, _ = await _call(
            {f"{field}_offset": offset, "max_chars": max_chars}, service_result=result
        )
        assert len(text) <= max_chars
        memory = _memory(text)
        assert field not in memory
        pieces.append(memory[f"{field}_json"])
        offset = memory[f"{field}_next_offset"]
    return "".join(pieces)


@pytest.mark.asyncio
async def test_following_content_reproduces_it_exactly():
    content = _big_text(524_288)
    result = _response(content=content)
    joined, calls = await _follow_content(result, REFERENCE_MAX_CHARS_LIMIT)
    assert joined == content
    assert calls > 1


@pytest.mark.asyncio
async def test_following_content_at_the_default_budget_reproduces_it_exactly():
    content = _big_text(70_000)
    joined, _ = await _follow_content(_response(content=content), REFERENCE_DEFAULT_MAX_CHARS)
    assert joined == content


@pytest.mark.asyncio
async def test_following_details_reproduces_the_object_exactly():
    details = {
        "raw": _big_text(524_288),
        "nested": {"list": [1, 2.5, None, True, "日本語"], "empty": {}},
        'quote"key': "value",
    }
    result = _response(details=details)
    joined = await _follow_json(result, "details", REFERENCE_MAX_CHARS_LIMIT)
    assert json.loads(joined) == details
    assert joined == json.dumps(details, ensure_ascii=False, separators=(",", ":"))


@pytest.mark.asyncio
async def test_following_context_reproduces_the_object_exactly():
    context = {"blob": _big_text(45_000), "n": 1}
    joined = await _follow_json(_response(context=context), "context", REFERENCE_DEFAULT_MAX_CHARS)
    assert json.loads(joined) == context


@pytest.mark.asyncio
async def test_details_page_of_a_memory_without_details_is_null():
    result = _response()
    result.details = None
    text, _ = await _call({"details_offset": 0}, service_result=result)
    memory = _memory(text)
    assert memory["details_json"] == "null"
    assert memory["details_truncated"] is False
    assert memory["details_next_offset"] is None


@pytest.mark.asyncio
async def test_offset_at_the_end_returns_an_empty_final_page():
    text, _ = await _call({"content_offset": 20}, service_result=_response())
    memory = _memory(text)
    assert memory["content"] == ""
    assert memory["content_total_chars"] == 20
    assert memory["content_next_offset"] is None


@pytest.mark.asyncio
async def test_offset_with_explicit_fields_returns_the_page_and_the_other_fields():
    content = _big_text(50_000)
    text, _ = await _call(
        {"fields": ["content", "details"], "content_offset": 10},
        service_result=_response(content=content),
    )
    memory = _memory(text)
    assert memory["content_offset"] == 10
    assert content[10:].startswith(memory["content"])
    assert memory["details"] == {"k": "v"}
    assert "context" not in memory


@pytest.mark.asyncio
async def test_content_is_still_reported_next_to_a_details_page():
    """A details page has priority; selected content is sliced or marked, never dropped."""
    content, details = _big_text(50_000), {"raw": _big_text(50_000)}
    text, _ = await _call(
        {"fields": ["content", "details"], "details_offset": 0},
        service_result=_response(content=content, details=details),
    )
    memory = _memory(text)
    assert len(text) <= REFERENCE_DEFAULT_MAX_CHARS
    assert memory["details_offset"] == 0 and memory["details_truncated"] is True
    assert memory["content_omitted"] is True
    assert memory["content_total_chars"] == len(content)
    assert memory["content_next_offset"] == 0

    small = _response()
    text, _ = await _call(
        {"fields": ["content", "details"], "details_offset": 0}, service_result=small
    )
    memory = _memory(text)
    assert memory["content"] == small.content
    assert json.loads(memory["details_json"]) == small.details


@pytest.mark.asyncio
async def test_all_fields_named_explicitly_match_the_default_shape():
    result = _response()
    text, _ = await _call(
        {"fields": ["links", "context", "details", "content"]}, service_result=result
    )
    assert text == _legacy_text(result)


# ------------------------------------------------------------ argument errors


@pytest.mark.parametrize(
    "args",
    [
        {"content_offset": -1},
        {"content_offset": "10"},
        {"content_offset": 1.5},
        {"content_offset": True},
        {"details_offset": -5},
        {"context_offset": [0]},
        {"max_chars": REFERENCE_MIN_MAX_CHARS - 1},
        {"max_chars": REFERENCE_MAX_CHARS_LIMIT + 1},
        {"max_chars": "20000"},
        {"fields": ["content", "summary"]},
        {"fields": "content"},
        {"fields": [1]},
        {"content_offset": 0, "details_offset": 0},
        {"fields": ["details"], "content_offset": 0},
    ],
)
@pytest.mark.asyncio
async def test_invalid_arguments_are_refused_before_any_read(args):
    text, svc = await _call(args, service_result=_response())
    assert _error(text)["error"] == "invalid_argument"
    svc.reference.assert_not_awaited()


@pytest.mark.parametrize("field", ["content", "details", "context"])
@pytest.mark.asyncio
async def test_offset_beyond_the_end_is_invalid_argument(field):
    result = _response()
    total = (
        len(result.content)
        if field == "content"
        else len(json.dumps(getattr(result, field), ensure_ascii=False, separators=(",", ":")))
    )
    text, _ = await _call({f"{field}_offset": total + 1}, service_result=result)
    error = _error(text)
    assert error["error"] == "invalid_argument"
    assert error[f"{field}_total_chars"] == total
    assert f"{field}_offset" in error["message"]


# ------------------------------------------------------------ authorization


@pytest.mark.parametrize(
    "continuation",
    [
        {"content_offset": 100},
        {"details_offset": 0},
        {"context_offset": 0},
        {"fields": ["details"]},
    ],
)
@pytest.mark.asyncio
async def test_continuation_for_an_inaccessible_context_is_refused_the_same_way(continuation):
    deny = AsyncMock(side_effect=_ContextNotFoundError(CTX_ID, "Context not found or no access."))
    plain, plain_svc = await _call({}, service_result=_response(), resolver=deny)
    paged, paged_svc = await _call(continuation, service_result=_response(), resolver=deny)
    assert _error(paged)["error"] == "context_not_found"
    assert paged == plain
    plain_svc.reference.assert_not_awaited()
    paged_svc.reference.assert_not_awaited()


@pytest.mark.parametrize(
    "continuation",
    [
        {"content_offset": 100},
        {"details_offset": 0},
        {"context_offset": 0},
        {"fields": ["details"]},
    ],
)
@pytest.mark.asyncio
async def test_continuation_for_another_contexts_memory_is_refused_the_same_way(continuation):
    """The memory-level check (service.reference) runs on every continuation."""
    missing = NotFoundException("Memory", str(MEM_ID))
    plain, _ = await _call({}, service_error=missing)
    paged, paged_svc = await _call(continuation, service_error=missing)
    assert _error(paged)["error"] == "memory_not_found"
    assert paged == plain
    paged_svc.reference.assert_awaited_once()
    assert paged_svc.reference.await_args.kwargs == {"user_id": "u"}


# ------------------------------------------------------------------ definition


def test_definition_advertises_the_same_bounds_as_the_handler():
    from mcp_server.tools import get_tool_definitions

    tool = next(t for t in get_tool_definitions() if t["name"] == "reference")
    props = tool["inputSchema"]["properties"]
    assert props["max_chars"]["minimum"] == REFERENCE_MIN_MAX_CHARS
    assert props["max_chars"]["maximum"] == REFERENCE_MAX_CHARS_LIMIT
    assert f"default {REFERENCE_DEFAULT_MAX_CHARS}" in props["max_chars"]["description"]
    assert "not tokens" in props["max_chars"]["description"]
    assert props["fields"]["items"]["enum"] == ["content", "details", "context", "links"]
    for field in ("content", "details", "context"):
        assert props[f"{field}_offset"]["minimum"] == 0
    assert tool["inputSchema"]["required"] == ["memory_id", "context_id"]


# ------------------------------------------------------------- invariants


def _accounted_for(memory: dict, field: str) -> bool:
    """A selected heavy field is present, paged, or explicitly marked omitted."""
    key = "outgoing_links" if field == "links" else field
    return key in memory or f"{field}_json" in memory or memory.get(f"{field}_omitted") is True


@pytest.mark.parametrize(
    "sizes", [(10, 10, 10, 1), (30_000, 10, 25_000, 50), (150_000, 60_000, 10, 0)]
)
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"fields": ["content", "details"]},
        {"fields": ["details", "context", "links"]},
        {"fields": ["content", "details", "context", "links"], "details_offset": 3},
        {"fields": ["content", "context"], "context_offset": 0},
        {"content_offset": 7},
    ],
)
@pytest.mark.parametrize("max_chars", [REFERENCE_MIN_MAX_CHARS, REFERENCE_DEFAULT_MAX_CHARS])
@pytest.mark.asyncio
async def test_budget_and_markers_hold_for_every_combination(sizes, args, max_chars):
    content_n, details_n, context_n, links = sizes
    result = _response(
        content=_big_text(content_n),
        details={"raw": _big_text(details_n)},
        context={"blob": _big_text(context_n)},
        links=links,
    )
    text, _ = await _call({**args, "max_chars": max_chars}, service_result=result)
    assert len(text) <= max_chars
    memory = _memory(text)
    offset_fields = [f for f in ("content", "details", "context") if f"{f}_offset" in args]
    selected = args.get("fields", offset_fields or ["content", "details", "context", "links"])
    for field in ("content", "details", "context", "links"):
        assert _accounted_for(memory, field) is (field in selected), field
