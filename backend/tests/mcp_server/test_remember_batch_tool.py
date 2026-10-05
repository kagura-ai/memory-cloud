"""``remember_batch`` registration, argument validation and envelope shaping (#1853).

The writes themselves run on real Postgres in
tests/services/test_remember_write_options.py.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from mcp_server.tools import (
    _RATE_LIMIT_EXEMPT_TOOLS,
    _TOOLS_WITHOUT_CONTEXT_ID,
    get_tool_definitions,
)
from mcp_server.tools._annotations import TOOL_ANNOTATIONS
from mcp_server.tools._errors import _VERIFY_WITH
from mcp_server.tools._profiles import CORE_TOOLS
from mcp_server.tools.batch import (
    _envelope,
    _request_from_item,
    handle_remember_batch,
    item_error,
    usage_status,
)
from mcp_server.tools.guide import GUIDE_INDEX
from mcp_server.tools.memory import parse_write_options
from models.schemas import RememberRequest
from services.memory_service import (
    MAX_BATCH_ITEMS,
    DedupeUnavailableError,
    DuplicateCandidateError,
)
from utils.exceptions import AuthorizationError, QuotaExceededError

CTX = "00000000-0000-4000-8000-000000000001"
ITEM = {"summary": "a reusable conclusion", "content": "c", "type": "note"}


def test_remember_batch_is_a_core_additive_write_with_a_manual():
    assert "remember_batch" in CORE_TOOLS
    assert CORE_TOOLS.index("remember_batch") == CORE_TOOLS.index("remember") + 1
    hints = TOOL_ANNOTATIONS["remember_batch"]
    assert hints["readOnlyHint"] is False and hints["destructiveHint"] is False
    assert "remember_batch" not in _TOOLS_WITHOUT_CONTEXT_ID
    assert "remember_batch" not in _RATE_LIMIT_EXEMPT_TOOLS  # a write, metered like remember
    assert "remember_batch" in GUIDE_INDEX
    assert _VERIFY_WITH["remember_batch"] == "recall"
    tool = next(t for t in get_tool_definitions() if t["name"] == "remember_batch")
    assert tool["inputSchema"]["required"] == ["context_id", "items"]
    assert tool["inputSchema"]["properties"]["items"]["maxItems"] == MAX_BATCH_ITEMS


@pytest.mark.parametrize("name", ["remember", "remember_batch"])
def test_write_tools_offer_tags_normalize_and_dedupe(name):
    props = next(t for t in get_tool_definitions() if t["name"] == name)["inputSchema"][
        "properties"
    ]
    assert props["tags_normalize"]["type"] == "boolean"
    assert props["dedupe"]["enum"] == ["suggest", "check", "off"]


# --------------------------------------------------------------------- options


def test_write_options_default_and_validate():
    assert parse_write_options({}) == (False, "suggest")
    assert parse_write_options({"tags_normalize": True, "dedupe": "check"}) == (True, "check")
    for bad in ({"tags_normalize": "yes"}, {"dedupe": "maybe"}, {"dedupe": 1}):
        with pytest.raises(ValueError):
            parse_write_options(bad)


# --------------------------------------------------------------- item parsing


def test_item_parsing_builds_a_request_or_names_the_problem():
    assert isinstance(_request_from_item(ITEM, CTX), RememberRequest)
    assert _request_from_item("not an object", CTX)["error"] == "validation_error"
    assert _request_from_item({"summary": "s" * 20}, CTX)["error"] == "missing_fields"
    other = _request_from_item({**ITEM, "context_id": "0" * 32}, CTX)
    assert other["error"] == "validation_error" and "context_id" in other["message"]
    short = _request_from_item({**ITEM, "summary": "short"}, CTX)
    assert short["error"] == "validation_error" and "summary" in short["message"]
    assert "pydantic" not in short["message"]


def test_an_item_key_remember_does_not_declare_is_refused_not_dropped():
    """#1873: a typo used to be ignored and the item reported success (#1742 for remember)."""
    typo = _request_from_item({**ITEM, "importnace": 0.9, "tag": ["a"]}, CTX)
    assert typo["error"] == "invalid_argument"
    assert "'importnace' (did you mean 'importance'?)" in typo["message"]
    assert "'tag' (did you mean 'tags'?)" in typo["message"]
    for name in ("dedupe", "tags_normalize", "verbose"):
        per_item = _request_from_item({**ITEM, name: True}, CTX)
        assert per_item["error"] == "invalid_argument"
        assert f"'{name}' (batch-level)" in per_item["message"]
        assert "set them on remember_batch itself" in per_item["message"]
    # Client-controlled names are bounded in the echo.
    many = _request_from_item({**ITEM, **{f"k{i:02d}": 1 for i in range(15)}, "x" * 200: 1}, CTX)
    assert "and 6 more" in many["message"] and "x" * 65 not in many["message"]
    # Every argument remember declares (but the batch-level ones) is still an item key.
    assert isinstance(
        _request_from_item({**ITEM, "context_id": CTX, "importance": 0.9, "tags": ["a"]}, CTX),
        RememberRequest,
    )


def test_item_values_get_remembers_json_string_coercion():
    request = _request_from_item({**ITEM, "tags": '["a", "b"]', "details": '{"k": 1}'}, CTX)
    assert isinstance(request, RememberRequest)
    assert request.tags == ["a", "b"] and request.details == {"k": 1}


def test_item_errors_map_onto_the_remember_error_codes():
    assert item_error(DuplicateCandidateError({"memory_id": "m"})) == {
        "status": "duplicate_candidate",
        "candidate": {"memory_id": "m"},
    }
    assert item_error(DedupeUnavailableError("down"))["error"] == "dedupe_unavailable"
    assert item_error(QuotaExceededError("full"))["error"] == "quota_exceeded"
    assert item_error(AuthorizationError("no"))["error"] == "forbidden"
    assert item_error(ValueError("details.trigger must …"))["error"] == "validation_error"
    assert item_error(RuntimeError("boom"))["error"] == "internal_error"


# ------------------------------------------------------------------- envelopes


def _ctx() -> dict:
    return {"context_id": CTX, "context_name": "ctx", "context_display_name": None}


def _parse(blocks):
    (block,) = blocks
    return json.loads(block.text)


def test_envelope_status_reflects_how_many_items_were_written():
    ok = {"index": 0, "status": "success", "memory_id": "m"}
    bad = {"index": 1, "status": "error", "error": "quota_exceeded", "message": "full"}
    assert _parse(_envelope([ok], _ctx()))["status"] == "success"
    partial = _parse(_envelope([ok, bad], _ctx()))
    assert partial["status"] == "partial" and (partial["succeeded"], partial["failed"]) == (1, 1)
    none = _parse(_envelope([bad], _ctx()))
    assert (
        none["status"] == "error" and none["error"] == "batch_failed"
    )  # isError for the transport
    assert none["results"] == [bad] and none["context_name"] == "ctx"
    # Every refusal a dedupe="check" candidate: a decision, not an error (as for remember).
    cand = {"index": 0, "status": "duplicate_candidate", "candidate": {"memory_id": "m"}}
    only = _parse(_envelope([cand], _ctx()))
    assert only["status"] == "duplicate_candidate" and (only["candidates"], only["failed"]) == (
        1,
        0,
    )
    assert _parse(_envelope([ok, cand], _ctx()))["status"] == "partial"


def test_a_rolled_back_batch_whose_only_refusal_is_a_candidate_is_a_decision():
    """#1873: skipped items are not failures."""
    cand = {"index": 0, "status": "duplicate_candidate", "candidate": {"memory_id": "m"}}
    skipped = {"index": 1, "status": "skipped", "message": "batch rolled back: item 0 failed"}
    payload = _parse(_envelope([cand, skipped], _ctx()))
    assert payload["status"] == "duplicate_candidate"
    assert (payload["candidates"], payload["failed"], payload["skipped"]) == (1, 0, 1)
    bad = {"index": 0, "status": "error", "error": "validation_error", "message": "no"}
    failed = _parse(_envelope([bad, skipped], _ctx()))
    assert failed["error"] == "batch_failed"
    assert (failed["failed"], failed["skipped"]) == (1, 1)


def test_usage_status_follows_the_dominant_outcome():
    ok = {"index": 0, "status": "success"}
    cand = {"index": 0, "status": "duplicate_candidate"}
    quota = {"index": 0, "status": "error", "error": "quota_exceeded"}
    bad = {"index": 0, "status": "error", "error": "validation_error"}
    boom = {"index": 0, "status": "error", "error": "internal_error"}
    skipped = {"index": 1, "status": "skipped"}
    assert usage_status([ok, quota]) == 200
    assert usage_status([cand]) == 200
    assert usage_status([quota, quota]) == 429
    assert usage_status([bad, skipped]) == 422
    assert usage_status([boom, skipped]) == 500
    unknown = {"index": 0, "status": "error", "error": "invalid_argument"}
    assert usage_status([unknown, bad]) == 422
    down = {"index": 0, "status": "error", "error": "dedupe_unavailable"}
    assert usage_status([down, skipped]) == 503  # as remember's


# ------------------------------------------------------- handler, before any DB


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "error", "needle"),
    [
        ({}, "missing_fields", "context_id"),
        ({"context_id": CTX}, "validation_error", "items"),
        ({"context_id": CTX, "items": []}, "validation_error", "items"),
        ({"context_id": CTX, "items": "x"}, "validation_error", "items"),
        ({"context_id": CTX, "items": [ITEM] * (MAX_BATCH_ITEMS + 1)}, "validation_error", "limit"),
        ({"context_id": CTX, "items": [ITEM], "atomic": "yes"}, "validation_error", "atomic"),
        ({"context_id": CTX, "items": [ITEM], "dedupe": "always"}, "validation_error", "dedupe"),
        (
            {"context_id": CTX, "items": [ITEM], "tags_normalize": 1},
            "validation_error",
            "tags_normalize",
        ),
    ],
)
async def test_handler_rejects_bad_arguments_before_touching_the_database(args, error, needle):
    payload = _parse(await handle_remember_batch(args, "user-1", None))
    assert payload["error"] == error
    assert needle in payload["message"]


@pytest.mark.asyncio
async def test_an_atomic_batch_with_an_invalid_item_is_refused_whole_before_any_write():
    payload = _parse(
        await handle_remember_batch(
            {"context_id": CTX, "items": [ITEM, {"summary": "no content"}, ITEM], "atomic": True},
            "user-1",
            None,
        )
    )
    assert payload["error"] == "batch_refused"
    assert [r["status"] for r in payload["results"]] == ["skipped", "error", "skipped"]
    assert payload["results"][1]["error"] == "missing_fields"
    # #1873: the same counters as every other batch envelope; skipped is not failed.
    assert (payload["count"], payload["succeeded"], payload["candidates"]) == (3, 0, 0)
    assert (payload["failed"], payload["skipped"]) == (1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"dedupe": "check"}, {"importnace": 0.9}])
async def test_an_undeclared_item_key_refuses_an_atomic_batch_whole(extra):
    payload = _parse(
        await handle_remember_batch(
            {"context_id": CTX, "items": [ITEM, {**ITEM, **extra}], "atomic": True}, "user-1", None
        )
    )
    assert payload["error"] == "batch_refused"
    assert [r["status"] for r in payload["results"]] == ["skipped", "error"]
    assert payload["results"][1]["error"] == "invalid_argument"


# ------------------------------------------------------- handler, real session


async def _scope(db, owner: str):
    from uuid import uuid4 as _uuid4

    from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole

    db.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=_uuid4(),
        name=f"rb-ws-{_uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=_uuid4(),
        workspace_id=ws.id,
        name=f"rb-ctx-{_uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    await db.commit()
    return ws, ctx


@pytest.fixture
async def live(db_session):
    """The handler's ``get_db()`` yields the test session; quota and the embedding task are stubbed."""
    from unittest.mock import AsyncMock, patch

    async def _db():
        yield db_session

    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(return_value=None),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    with (
        patch("db.base.get_db", new=_db),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.QuotaService", return_value=quota),
    ):
        yield db_session


async def _rows(db, ctx_id) -> int:
    from sqlalchemy import func, select

    from models.memory import Memory

    return (
        await db.execute(
            select(func.count()).select_from(Memory).where(Memory.context_id == ctx_id)
        )
    ).scalar_one()


OK_ITEM = {"summary": "a reusable conclusion for later", "content": "c", "type": "note"}
BAD_ITEM = {"summary": "a time memory with no trigger", "content": "c", "type": "time"}


@pytest.mark.asyncio
async def test_a_partial_batch_reports_each_item_and_keeps_the_context_fields(live):
    from uuid import uuid4 as _uuid4

    owner = f"o-{_uuid4().hex[:6]}"
    ws, ctx = await _scope(live, owner)
    ctx_id, ctx_name = ctx.id, ctx.name
    payload = _parse(
        await handle_remember_batch(
            {"context_id": str(ctx_id), "items": [OK_ITEM, BAD_ITEM, OK_ITEM]}, owner, ws.id
        )
    )
    assert payload["status"] == "partial"
    assert [r["status"] for r in payload["results"]] == ["success", "error", "success"]
    assert payload["results"][1]["error"] == "validation_error"
    assert payload["context_name"] == ctx_name  # read before the item's rollback expired the row
    assert (payload["succeeded"], payload["failed"]) == (2, 1)
    assert await _rows(live, ctx_id) == 2


@pytest.mark.asyncio
async def test_an_atomic_batch_failure_rolls_back_through_the_handler(live):
    from uuid import uuid4 as _uuid4

    owner = f"o-{_uuid4().hex[:6]}"
    ws, ctx = await _scope(live, owner)
    ctx_id, ctx_name = ctx.id, ctx.name
    payload = _parse(
        await handle_remember_batch(
            {"context_id": str(ctx_id), "items": [OK_ITEM, BAD_ITEM, OK_ITEM], "atomic": True},
            owner,
            ws.id,
        )
    )
    assert payload["error"] == "batch_failed"
    assert [r["status"] for r in payload["results"]] == ["skipped", "error", "skipped"]
    assert payload["context_name"] == ctx_name
    assert await _rows(live, ctx_id) == 0


@pytest.mark.asyncio
async def test_remember_dedupe_check_reply_carries_the_candidate_and_the_context(live):
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4 as _uuid4

    from mcp_server.tools.memory import handle_remember
    from services.memory_service import MemoryService

    owner = f"o-{_uuid4().hex[:6]}"
    ws, ctx = await _scope(live, owner)
    ctx_id, ctx_name = ctx.id, ctx.name
    candidate = {"memory_id": str(_uuid4()), "summary": "the same fact", "similarity": 0.9}
    with patch.object(
        MemoryService, "_find_duplicate_candidate", new=AsyncMock(return_value=candidate)
    ):
        payload = _parse(
            await handle_remember(
                {**OK_ITEM, "context_id": str(ctx_id), "dedupe": "check"}, owner, ws.id
            )
        )
    assert payload["status"] == "duplicate_candidate"
    assert payload["candidate"] == candidate
    assert payload["context_name"] == ctx_name
    assert await _rows(live, ctx_id) == 0


# ------------------------------------------------------- follow-ups (#1873)


@pytest.mark.asyncio
async def test_an_undeclared_item_key_is_a_per_item_error_without_atomic(live):
    from uuid import uuid4 as _uuid4

    owner = f"o-{_uuid4().hex[:6]}"
    ws, ctx = await _scope(live, owner)
    ctx_id = ctx.id
    payload = _parse(
        await handle_remember_batch(
            {"context_id": str(ctx_id), "items": [OK_ITEM, {**OK_ITEM, "dedupe": "off"}]},
            owner,
            ws.id,
        )
    )
    assert payload["status"] == "partial"
    assert [r["status"] for r in payload["results"]] == ["success", "error"]
    assert payload["results"][1]["error"] == "invalid_argument"
    assert await _rows(live, ctx_id) == 1


@pytest.mark.asyncio
async def test_an_atomic_check_candidate_is_a_decision_through_the_handler(live):
    from unittest.mock import patch
    from uuid import uuid4 as _uuid4

    from services.memory_service import MemoryService

    owner = f"o-{_uuid4().hex[:6]}"
    ws, ctx = await _scope(live, owner)
    ctx_id = ctx.id
    candidate = {"memory_id": str(_uuid4()), "summary": "the same fact", "similarity": 0.9}

    async def find(self, *, summary, **_):
        return candidate if "dup" in summary else None

    dup = {"summary": "a dup of an existing memory", "content": "c", "type": "note"}
    with patch.object(MemoryService, "_find_duplicate_candidate", new=find):
        blocks = await handle_remember_batch(
            {
                "context_id": str(ctx_id),
                "items": [OK_ITEM, dup, OK_ITEM],
                "atomic": True,
                "dedupe": "check",
            },
            owner,
            ws.id,
        )
    payload = _parse(blocks)
    assert payload["status"] == "duplicate_candidate"  # was the batch_failed error envelope
    assert not getattr(blocks, "is_error", False)
    assert [r["status"] for r in payload["results"]] == [
        "skipped",
        "duplicate_candidate",
        "skipped",
    ]
    assert payload["results"][1]["candidate"] == candidate
    assert (payload["candidates"], payload["failed"], payload["skipped"]) == (1, 0, 2)
    assert await _rows(live, ctx_id) == 0


@pytest.mark.asyncio
async def test_a_timeout_after_the_commit_reports_the_stored_items_not_a_rollback(live):
    import asyncio
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4 as _uuid4

    from services.memory_service import MemoryService

    owner = f"o-{_uuid4().hex[:6]}"
    ws, ctx = await _scope(live, owner)
    ctx_id, ctx_name = ctx.id, ctx.name
    with (
        patch.object(
            MemoryService,
            "_create_declared_links",
            new=AsyncMock(side_effect=[None, asyncio.CancelledError(), None]),
        ),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()) as embed,
    ):
        blocks = await handle_remember_batch(
            {"context_id": str(ctx_id), "items": [OK_ITEM, OK_ITEM, OK_ITEM], "atomic": True},
            owner,
            ws.id,
        )
    payload = _parse(blocks)
    assert payload["status"] == "success" and payload["committed_after_timeout"] is True
    assert "rolled back" not in blocks[0].text
    assert "Do NOT send the batch again" in payload["message"]
    assert len({r["memory_id"] for r in payload["results"]}) == 3
    assert payload["succeeded"] == 3 and payload["context_name"] == ctx_name
    assert embed.call_count == 3
    assert await _rows(live, ctx_id) == 3


@pytest.mark.asyncio
async def test_an_atomic_batch_over_the_daily_quota_is_refused_as_quota_exceeded(live):
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4 as _uuid4

    owner = f"o-{_uuid4().hex[:6]}"
    ws, ctx = await _scope(live, owner)
    ctx_id, ctx_name = ctx.id, ctx.name
    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        reserve_memories_per_day=AsyncMock(
            side_effect=QuotaExceededError("Daily memory-creation quota exceeded.", requested=3)
        ),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    with (
        patch("services.quota_service.QuotaService", return_value=quota),
        patch("mcp_server.tools.batch._log_tool_usage", new=AsyncMock()) as usage,
    ):
        payload = _parse(
            await handle_remember_batch(
                {"context_id": str(ctx_id), "items": [OK_ITEM] * 3, "atomic": True}, owner, ws.id
            )
        )
    assert payload["error"] == "quota_exceeded"
    assert payload["requested"] == 3 and payload["count"] == 3
    assert payload["context_name"] == ctx_name
    assert usage.await_args.args[4] == 429
    quota.release_memories_per_day.assert_not_awaited()  # nothing was reserved
    assert await _rows(live, ctx_id) == 0


@pytest.mark.asyncio
async def test_remember_dedupe_unavailable_reply_has_no_exception_text_and_is_logged(live):
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4 as _uuid4

    from mcp_server.tools.memory import handle_remember
    from services.memory_service import MemoryService

    owner = f"o-{_uuid4().hex[:6]}"
    ws, ctx = await _scope(live, owner)
    ctx_id = ctx.id
    with (
        patch.object(
            MemoryService,
            "_find_duplicate_candidate",
            new=AsyncMock(side_effect=DedupeUnavailableError("upstream said: key sk-secret-123")),
        ),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()) as usage,
    ):
        blocks = await handle_remember(
            {**OK_ITEM, "context_id": str(ctx_id), "dedupe": "check"}, owner, ws.id
        )
    payload = _parse(blocks)
    assert payload["error"] == "dedupe_unavailable"
    assert "detail" not in payload and "sk-secret-123" not in blocks[0].text  # #1684
    usage.assert_awaited_once()
    assert usage.await_args.args[2:5] == ("remember", usage.await_args.args[3], 503)
    assert await _rows(live, ctx_id) == 0
