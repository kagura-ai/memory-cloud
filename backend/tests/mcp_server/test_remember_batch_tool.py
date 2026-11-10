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
    ParsedItem,
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
from utils.exceptions import AuthorizationError, ContentSizeExceededError, QuotaExceededError

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
    assert tool["inputSchema"]["required"] == ["items"]  # #2125: ?context= default
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
    parsed = _request_from_item(ITEM, CTX)
    assert isinstance(parsed, ParsedItem) and isinstance(parsed.request, RememberRequest)
    assert parsed.fitted == {}
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
        ParsedItem,
    )


def test_item_values_get_remembers_json_string_coercion():
    request = _request_from_item({**ITEM, "tags": '["a", "b"]', "details": '{"k": 1}'}, CTX).request
    assert isinstance(request, RememberRequest)
    assert request.tags == ["a", "b"] and request.details == {"k": 1}


def test_item_errors_map_onto_the_remember_error_codes():
    assert item_error(DuplicateCandidateError({"memory_id": "m"})) == {
        "status": "duplicate_candidate",
        "candidate": {"memory_id": "m"},
    }
    assert item_error(DedupeUnavailableError("down"))["error"] == "dedupe_unavailable"
    assert item_error(QuotaExceededError("full"))["error"] == "quota_exceeded"
    size = item_error(ContentSizeExceededError(actual=1_000_001, limit=1_000_000))
    assert size["error"] == "payload_too_large"
    assert (size["limit"], size["actual"]) == (1_000_000, 1_000_001)
    assert "Split" in size["help"]
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
@pytest.mark.parametrize(
    ("extra", "batch"),
    # #2753: an item's batch-level value refuses it only when it conflicts with the batch's.
    [({"dedupe": "check"}, {"dedupe": "suggest"}), ({"importnace": 0.9}, {})],
)
async def test_an_undeclared_item_key_refuses_an_atomic_batch_whole(extra, batch):
    payload = _parse(
        await handle_remember_batch(
            {"context_id": CTX, "items": [ITEM, {**ITEM, **extra}], "atomic": True, **batch},
            "user-1",
            None,
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
            {
                "context_id": str(ctx_id),
                "items": [OK_ITEM, {**OK_ITEM, "dedupe": "off"}],
                "dedupe": "suggest",  # #2753: a value that agrees with the batch's is accepted
            },
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


# ------------------------------------------- skill-style write, stubbed (#2690)


@pytest.mark.asyncio
@pytest.mark.parametrize("atomic", [False, True])
async def test_a_skill_style_write_normalizes_tags_and_reports_the_mapping(atomic):
    """The skills write with ``tags_normalize=true`` instead of calling ``list_tags``
    first: the flag reaches the service on both batch paths, and the service's
    ``tag_normalized`` hint reaches the item's ``lint``. The storage half (the
    established spelling wins) runs on Postgres in test_remember_write_options.py."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4 as _uuid4

    from models.schemas import WriteLintHint

    hint = WriteLintHint(
        code="tag_normalized",
        hint="Stored under the context's established spelling.",
        subject="Dev_Environment",
        replacement="dev-environment",
    )
    response = SimpleNamespace(memory_id=_uuid4(), scope="working", persistence=None, lint=[hint])
    service = MagicMock(
        tag_canonical_map=AsyncMock(return_value={"devenvironment": "dev-environment"}),
        remember=AsyncMock(return_value=response),
        remember_many=AsyncMock(return_value=[response]),
    )
    db = MagicMock(commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock())

    async def _db():
        yield db

    context = MagicMock(workspace_id=_uuid4())
    with (
        patch("db.base.get_db", new=_db),
        patch("services.memory_service.MemoryService", return_value=service),
        patch("mcp_server.tools.batch._resolve_context", new=AsyncMock(return_value=context)),
        patch("mcp_server.tools.batch._context_response_fields", return_value={"context_id": CTX}),
        patch("mcp_server.tools.batch._log_tool_usage", new=AsyncMock()),
        patch("mcp_server.tools.batch._touch_context_last_used", new=AsyncMock()),
        patch("mcp_server.tools.batch._plan_headroom_field", new=AsyncMock(return_value={})),
    ):
        payload = _parse(
            await handle_remember_batch(
                {
                    "context_id": CTX,
                    "items": [{**ITEM, "tags": ["Dev_Environment"]}],
                    "tags_normalize": True,
                    "atomic": atomic,
                },
                "user-1",
                None,
            )
        )
    assert payload["status"] == "success"
    assert payload["results"][0]["lint"] == [
        {
            "code": "tag_normalized",
            "hint": "Stored under the context's established spelling.",
            "subject": "Dev_Environment",
            "replacement": "dev-environment",
        }
    ]
    if atomic:
        (requests,), kwargs = service.remember_many.await_args
        assert requests[0].tags == ["Dev_Environment"]
        assert kwargs["tags_normalize"] is True
    else:
        (request,), kwargs = service.remember.await_args
        assert request.tags == ["Dev_Environment"]
        assert kwargs["tags_normalize"] is True
        assert kwargs["tag_canonical"] == {"devenvironment": "dev-environment"}


# ------------------------------------- batch-level arguments on an item (#2753)


def _stubbed_service(*, fail_remember=False):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4 as _uuid4

    def _response(*_args, **_kwargs):
        return SimpleNamespace(memory_id=_uuid4(), scope="working", persistence=None, lint=[])

    async def _many(requests, **_kwargs):
        return [_response() for _ in requests]

    return MagicMock(
        tag_canonical_map=AsyncMock(return_value={}),
        remember=AsyncMock(side_effect=_response),
        remember_many=AsyncMock(side_effect=_many),
    )


async def _run_stubbed(args, service):
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4 as _uuid4

    db = MagicMock(commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock())

    async def _db():
        yield db

    context = MagicMock(workspace_id=_uuid4())
    with (
        patch("db.base.get_db", new=_db),
        patch("services.memory_service.MemoryService", return_value=service),
        patch("mcp_server.tools.batch._resolve_context", new=AsyncMock(return_value=context)),
        patch("mcp_server.tools.batch._context_response_fields", return_value={"context_id": CTX}),
        patch("mcp_server.tools.batch._log_tool_usage", new=AsyncMock()),
        patch("mcp_server.tools.batch._touch_context_last_used", new=AsyncMock()),
        patch("mcp_server.tools.batch._plan_headroom_field", new=AsyncMock(return_value={})),
    ):
        return _parse(await handle_remember_batch({"context_id": CTX, **args}, "user-1", None))


def _write_kwargs(service, atomic):
    call = service.remember_many if atomic else service.remember
    return call.await_args.kwargs


class TestBatchLevelArgumentsOnAnItem:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("atomic", [False, True])
    async def test_agreeing_items_set_the_batch_value_and_say_so_once(self, atomic):
        service = _stubbed_service()
        items = [{**ITEM, "tags": ["Dev_Env"], "tags_normalize": True} for _ in range(3)]
        payload = await _run_stubbed({"items": items, "atomic": atomic}, service)
        assert payload["status"] == "success" and payload["succeeded"] == 3
        assert _write_kwargs(service, atomic)["tags_normalize"] is True
        (lint,) = payload["lint"]
        assert lint["code"] == "batch_level_argument" and lint["subject"] == "tags_normalize"
        assert "remember_batch" in lint["hint"] and "tags_normalize=true" in lint["hint"]
        if not atomic:
            service.tag_canonical_map.assert_awaited_once()  # normalized: the fold map is read

    @pytest.mark.asyncio
    @pytest.mark.parametrize("atomic", [False, True])
    async def test_items_that_repeat_the_batch_value_are_accepted_quietly(self, atomic):
        service = _stubbed_service()
        items = [{**ITEM, "tags_normalize": True, "verbose": False} for _ in range(2)]
        payload = await _run_stubbed(
            {"items": items, "atomic": atomic, "tags_normalize": True, "verbose": False}, service
        )
        assert payload["status"] == "success" and payload["succeeded"] == 2
        assert _write_kwargs(service, atomic)["tags_normalize"] is True
        assert "lint" not in payload

    @pytest.mark.asyncio
    async def test_an_item_that_repeats_the_default_is_adopted_too(self):
        service = _stubbed_service()
        payload = await _run_stubbed({"items": [ITEM, {**ITEM, "dedupe": "suggest"}]}, service)
        assert payload["status"] == "success"
        assert _write_kwargs(service, False)["dedupe"] == "suggest"
        assert [entry["subject"] for entry in payload["lint"]] == ["dedupe"]

    @pytest.mark.asyncio
    async def test_a_conflict_with_the_batch_value_refuses_only_that_item(self):
        service = _stubbed_service()
        items = [ITEM, {**ITEM, "tags_normalize": True}, {**ITEM, "tags_normalize": False}]
        payload = await _run_stubbed({"items": items, "tags_normalize": False}, service)
        assert [r["status"] for r in payload["results"]] == ["success", "error", "success"]
        refused = payload["results"][1]
        assert refused["error"] == "invalid_argument"
        assert "'tags_normalize' (batch-level)" in refused["message"]
        assert "The batch's tags_normalize is false" in refused["message"]
        assert _write_kwargs(service, False)["tags_normalize"] is False
        assert "lint" not in payload

    @pytest.mark.asyncio
    async def test_a_conflict_refuses_an_atomic_batch_whole(self):
        payload = _parse(
            await handle_remember_batch(
                {
                    "context_id": CTX,
                    "items": [ITEM, {**ITEM, "verbose": True}],
                    "atomic": True,
                    "verbose": False,
                },
                "user-1",
                None,
            )
        )
        assert payload["error"] == "batch_refused"
        assert [r["status"] for r in payload["results"]] == ["skipped", "error"]
        assert payload["results"][1]["error"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_items_that_disagree_without_a_batch_value_are_all_refused(self):
        service = _stubbed_service()
        items = [
            {**ITEM, "tags_normalize": True},
            ITEM,
            {**ITEM, "tags_normalize": False},
        ]
        payload = await _run_stubbed({"items": items}, service)
        assert [r["status"] for r in payload["results"]] == ["error", "success", "error"]
        for index in (0, 2):
            assert payload["results"][index]["error"] == "invalid_argument"
            assert "'tags_normalize' (batch-level)" in payload["results"][index]["message"]
        assert _write_kwargs(service, False)["tags_normalize"] is False
        assert "lint" not in payload

    @pytest.mark.asyncio
    async def test_an_invalid_agreeing_value_is_not_adopted(self):
        service = _stubbed_service()
        items = [{**ITEM, "dedupe": "always"}, {**ITEM, "dedupe": "always"}]
        payload = await _run_stubbed({"items": items}, service)
        assert payload["error"] == "batch_failed"
        assert {r["error"] for r in payload["results"]} == {"invalid_argument"}
        assert "lint" not in payload

    @pytest.mark.asyncio
    async def test_a_json_string_boolean_agrees_with_a_boolean(self):
        """Items get remember's JSON-string coercion, the batch-level values too."""
        service = _stubbed_service()
        items = [{**ITEM, "tags_normalize": "true"}, {**ITEM, "tags_normalize": True}]
        payload = await _run_stubbed({"items": items, "tags_normalize": True}, service)
        assert payload["status"] == "success" and payload["succeeded"] == 2

    @pytest.mark.asyncio
    async def test_an_unknown_key_is_still_refused_with_the_did_you_mean(self):
        service = _stubbed_service()
        items = [ITEM, {**ITEM, "tags_normalize": True, "importnace": 0.9}]
        payload = await _run_stubbed({"items": items, "tags_normalize": True}, service)
        refused = payload["results"][1]
        assert refused["error"] == "invalid_argument"
        assert "'importnace' (did you mean 'importance'?)" in refused["message"]
        assert "'tags_normalize'" not in refused["message"].split(". ")[0]

    @pytest.mark.asyncio
    async def test_an_adopted_verbose_reaches_every_item(self):
        from unittest.mock import patch

        service = _stubbed_service()
        items = [{**ITEM, "verbose": True}, ITEM]
        with patch(
            "mcp_server.tools.batch._persistence_response_field", return_value={}
        ) as persistence:
            payload = await _run_stubbed({"items": items}, service)
        assert payload["status"] == "success"
        assert [c.kwargs["verbose"] for c in persistence.call_args_list] == [True, True]
        assert [entry["subject"] for entry in payload["lint"]] == ["verbose"]


def test_the_description_and_the_guide_say_an_agreeing_value_is_accepted():
    tool = next(t for t in get_tool_definitions() if t["name"] == "remember_batch")
    assert "dedupe, tags_normalize and verbose are batch-level" in tool["description"]
    assert "an item that repeats one with the batch's value is accepted" in tool["description"]
    from mcp_server.tools.guide import GUIDE_TOPICS

    usage = GUIDE_TOPICS["remember_batch.usage"]
    assert "batch_level_argument" in usage
    assert "every item that carries one agrees" in usage


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [{}, {"tags_normalize": True}])
async def test_items_with_tags_normalize_are_written_in_one_call_and_normalized(live, batch):
    """#2753 on Postgres: the item-level tags_normalize no longer fails the batch."""
    from uuid import UUID
    from uuid import uuid4 as _uuid4

    from sqlalchemy import select

    from models.memory import Memory
    from services.tag_resolution import clear_vocabulary_cache

    owner = f"o-{_uuid4().hex[:6]}"
    ws, ctx = await _scope(live, owner)
    ctx_id = ctx.id
    seeded = _parse(
        await handle_remember_batch(
            {"context_id": str(ctx_id), "items": [{**OK_ITEM, "tags": ["dev-environment"]}]},
            owner,
            ws.id,
        )
    )
    assert seeded["status"] == "success"
    clear_vocabulary_cache()
    item = {**OK_ITEM, "tags": ["Dev_Environment"], "tags_normalize": True}
    payload = _parse(
        await handle_remember_batch(
            {"context_id": str(ctx_id), "items": [item, item], **batch}, owner, ws.id
        )
    )
    assert payload["status"] == "success" and payload["succeeded"] == 2
    assert ("lint" in payload) is (not batch)
    ids = [UUID(r["memory_id"]) for r in payload["results"]]
    rows = (await live.execute(select(Memory.tags).where(Memory.id.in_(ids)))).scalars().all()
    assert [list(tags) for tags in rows] == [["dev-environment"], ["dev-environment"]]
