"""The user facts lane of recall (#3014), without a database.

When it runs (:func:`lane_applies`), which filters it keeps, that an error or a
timeout leaves the page as it was with one warning, the fixed query's vector
cache in ``hybrid_search``, which callers opt in, the binding filter's audit
row, and the MCP envelope. The lane against Postgres is pinned in
tests/integration/test_3014_user_facts_lane_db.py.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import MemoryResponse, RecallRequest, RecallResponse
from services import user_facts_lane
from services.user_facts_lane import (
    USER_FACTS_QUERY,
    USER_FACTS_ROWS,
    attach_user_facts,
    lane_applies,
    lane_filters,
)

SRC = Path(__file__).resolve().parents[2] / "src"


def _on(value=True):
    return SimpleNamespace(user_facts_lane=value)


def _response(**kw) -> RecallResponse:
    return RecallResponse(results=[], **kw)


def _applies(request: RecallRequest, config=None, **kw) -> bool:
    return lane_applies(
        request,
        search_config=_on() if config is None else config,
        context_ids=kw.get("context_ids"),
        selection=kw.get("selection", False),
        response=kw.get("response", _response()),
    )


# ------------------------------------------------------------- when it runs


def test_the_fixed_query_and_row_count():
    assert USER_FACTS_QUERY == (
        "facts about the user: health and physical limits, circumstances, constraints, needs"
    )
    assert USER_FACTS_ROWS == 2


def test_runs_when_the_switch_is_on():
    assert _applies(RecallRequest(query="q")) is True
    assert _applies(RecallRequest(query="q", filters={"tags": ["a"]})) is True


@pytest.mark.parametrize(
    "config", [None, _on(False), SimpleNamespace(), SimpleNamespace(user_facts_lane="yes")]
)
def test_off_or_unreadable_switch_skips(config):
    request = RecallRequest(query="q")
    assert (
        lane_applies(
            request, search_config=config, context_ids=None, selection=False, response=_response()
        )
        is False
    )


@pytest.mark.parametrize(
    ("request_kw", "kw"),
    [
        ({"filters": {"type": "fact"}}, {}),
        ({"filters": {"type": ["note", "fact"]}}, {}),
        ({"order": "date"}, {}),
        ({}, {"context_ids": [uuid4(), uuid4()]}),
        ({}, {"selection": True}),
        ({}, {"response": _response(degraded=True, degraded_reason="embedding_unavailable")}),
    ],
)
def test_skipped_cases(request_kw, kw):
    assert _applies(RecallRequest(query="q", **request_kw), **kw) is False


def test_lane_filters_keep_access_filters_only():
    request = RecallRequest(
        query="q",
        filters={
            "trust_tier": "trusted",
            "source_type": "manual",
            "source_uri_prefix": "https://example.com/",
            "tags": ["a"],
            "importance": 0.8,
            "scope": "persistent",
            "event_after": "2026-01-01",
            "context_id": str(uuid4()),
        },
    )
    assert lane_filters(request) == {
        "trust_tier": "trusted",
        "source_type": "manual",
        "source_uri_prefix": "https://example.com/",
        "type": "fact",
    }
    assert lane_filters(RecallRequest(query="q")) == {"type": "fact"}


# ---------------------------------------------------- never breaks a recall


def _service():
    service = MagicMock()
    service.db = MagicMock()
    service.db.commit = AsyncMock()
    return service


async def _attach(service, response):
    return await attach_user_facts(
        service,
        response,
        request=RecallRequest(query="q"),
        user_id="u-1",
        context_id=uuid4(),
        workspace_id=uuid4(),
        is_shared_context_read=False,
        content_trusted_only=True,
    )


@pytest.mark.asyncio
async def test_an_error_returns_the_page_with_one_warning():
    response = _response()
    with (
        patch.object(user_facts_lane, "_search", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(user_facts_lane, "logger") as log,
    ):
        out = await _attach(_service(), response)
    assert out is response
    assert out.user_facts is None
    log.warning.assert_called_once()
    assert log.warning.call_args.args == ("user_facts_lane_failed",)
    assert set(log.warning.call_args.kwargs) == {"context_id", "error_class"}
    assert log.warning.call_args.kwargs["error_class"] == "RuntimeError"


@pytest.mark.asyncio
async def test_a_timeout_returns_the_page_with_one_warning():
    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return []

    with (
        patch.object(user_facts_lane, "_search", slow),
        patch.object(user_facts_lane, "USER_FACTS_TIMEOUT_SECONDS", 0.01),
        patch.object(user_facts_lane, "logger") as log,
    ):
        out = await _attach(_service(), _response())
    assert out.user_facts is None
    log.warning.assert_called_once()
    assert log.warning.call_args.kwargs["error_class"] == "TimeoutError"


@pytest.mark.asyncio
async def test_rows_land_under_user_facts_after_the_recall_session_is_released():
    service = _service()
    row = MemoryResponse(
        memory_id=uuid4(),
        summary="The user injured a knee",
        type="fact",
        importance=0.5,
        scope="working",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        tags=[],
        context_summary=None,
        client="test",
        context=None,
    )
    order: list[str] = []
    service.db.commit = AsyncMock(side_effect=lambda: order.append("commit"))

    async def search(*args, **kwargs):
        order.append("search")
        return [row]

    with patch.object(user_facts_lane, "_search", search):
        out = await _attach(service, _response())
    assert out.user_facts == [row]
    assert order == ["commit", "search"]


@pytest.mark.asyncio
async def test_the_binding_filter_writes_no_audit_row():
    memory = SimpleNamespace(id=uuid4(), context_id=uuid4(), type="fact", source_type="manual")
    hits = [{"id": str(memory.id), "score": 0.5}]
    with patch(
        "services.agent_binding_service.filter_memory_rows_by_binding",
        AsyncMock(return_value=([], 1)),
    ) as rows_filter:
        kept = await user_facts_lane._binding_filter(
            MagicMock(), hits, {str(memory.id): memory}, user_id="u-1"
        )
    assert rows_filter.await_args.kwargs["operation"] is None
    assert kept == {} and hits == []


# ------------------------------------------------ what the page already shows


def test_a_version_folded_under_a_page_row_counts_as_shown():
    from models.schemas import FoldedVersions
    from services.user_facts_lane import _page_ids

    newest, older, plain = uuid4(), uuid4(), uuid4()
    head = SimpleNamespace(memory_id=newest, folded=FoldedVersions(count=1, memory_ids=[older]))
    other = SimpleNamespace(memory_id=plain, folded=None)

    # The older version is not a row of the page, but the page already stands
    # for it: the lane must not bring it back next to its newer version.
    assert _page_ids(SimpleNamespace(results=[head, other])) == {newest, older, plain}


# ------------------------------------------------------- the cached vector


def _embed_svc():
    svc = MagicMock()
    svc.provider = "openai"
    svc.model = "text-embedding-3-small"
    svc.dimensions = 1536
    svc.embed_with_usage = AsyncMock(return_value=([0.1] * 8, 12))
    svc.resolve_paid_by = AsyncMock(return_value="platform")
    return svc


async def _search_with(cache, embed_svc, writer_cls):
    from services.search_service import SearchService

    config = SimpleNamespace(
        context_id=uuid4(), fetch_factor=2, use_rerank=False, semantic_weight=0.6, bm25_weight=0.4
    )
    with (
        patch("services.search_service.LLMCallLogWriter", writer_cls),
        patch(
            "services.search_service.resolve_routing_from_config",
            return_value=("default", embed_svc),
        ),
        patch("services.search_service.search_memories_qdrant", new=AsyncMock(return_value=[])),
        patch("services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])),
        patch(
            "services.context_service.ContextService.is_context_shared",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "services.permission_service.PermissionService.is_workspace_member",
            new=AsyncMock(return_value=True),
        ),
    ):
        service = SearchService(db=AsyncMock())
        service._get_search_config = AsyncMock(return_value=config)
        await service.hybrid_search(
            query=USER_FACTS_QUERY,
            user_id="u-1",
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
            k=2,
            use_rerank=False,
            filters={"type": "fact"},
            query_vectors=cache,
        )


@pytest.mark.asyncio
async def test_the_fixed_query_is_embedded_once_per_model():
    cache: dict = {}
    embed_svc = _embed_svc()
    writer_cls = MagicMock()
    await _search_with(cache, embed_svc, writer_cls)
    await _search_with(cache, embed_svc, writer_cls)
    assert embed_svc.embed_with_usage.await_count == 1
    assert cache == {("openai", "text-embedding-3-small", 1536): [0.1] * 8}
    # The lane writes no llm_call_log row of its own.
    writer_cls.assert_not_called()
    other = _embed_svc()
    other.model = "another-model"
    await _search_with(cache, other, writer_cls)
    assert other.embed_with_usage.await_count == 1
    assert len(cache) == 2
    # The same model at another size is another vector: a context that stores
    # 512-wide vectors never gets the 1536-wide one.
    narrow = _embed_svc()
    narrow.dimensions = 512
    await _search_with(cache, narrow, writer_cls)
    assert narrow.embed_with_usage.await_count == 1
    assert ("openai", "text-embedding-3-small", 512) in cache


# ------------------------------------------------------------- the callers


def _recall_calls_opting_in() -> set[str]:
    """Files whose ``.recall(...)`` call passes ``user_facts_lane=True``."""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "recall"
            ):
                for kw in node.keywords:
                    if kw.arg == "user_facts_lane":
                        assert isinstance(kw.value, ast.Constant) and kw.value.value is True
                        found.add(str(path.relative_to(SRC)))
    return found


def test_only_the_mcp_tool_and_the_rest_route_opt_in():
    assert _recall_calls_opting_in() == {"mcp_server/tools/memory.py", "api/routes/memory.py"}


# ---------------------------------------------------------- the MCP reply


def _row(summary: str, content: str | None = None) -> MemoryResponse:
    return MemoryResponse(
        memory_id=uuid4(),
        summary=summary,
        type="fact",
        importance=0.5,
        scope="working",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        observed_at="2026-10-30",
        tags=[],
        content=content,
        context_summary=None,
        client="test",
        context=None,
    )


def _context():
    return SimpleNamespace(
        id=uuid4(), name="me", display_name="Me", is_private=True, is_locked=False
    )


def test_mcp_reply_carries_user_facts_after_count():
    from mcp_server.tools.memory import _recall_reply

    main = _row("Shopping list for the weekend")
    fact = _row("The user injured a knee", content="Stairs hurt.")
    reply = _recall_reply(
        RecallResponse(results=[main], user_facts=[fact]), _context(), "compact", 20_000
    )
    keys = list(reply)
    assert keys.index("user_facts") == keys.index("count") + 1
    assert reply["count"] == 1
    (item,) = reply["user_facts"]
    assert item["memory_id"] == str(fact.memory_id)
    assert item["content"] == "Stairs hurt."
    assert item["observed_at"] == "2026-10-30" and item["created_at"]


def test_mcp_reply_has_no_key_when_the_lane_did_not_run_and_an_empty_one_when_it_found_nothing():
    from mcp_server.tools.memory import _recall_reply

    assert "user_facts" not in _recall_reply(RecallResponse(results=[]), _context(), "full", 20_000)
    reply = _recall_reply(RecallResponse(results=[], user_facts=[]), _context(), "full", 20_000)
    assert reply["user_facts"] == []


def test_mcp_reply_over_budget_drops_lane_content_before_cutting_rows():
    from mcp_server.tools.memory import _recall_reply

    fact = _row("The user injured a knee", content="x" * 1_500)
    reply = _recall_reply(
        RecallResponse(results=[_row("A short main row here")], user_facts=[fact]),
        _context(),
        "compact",
        1_200,
    )
    assert reply["user_facts"][0]["summary"] == "The user injured a knee"
    assert "content" not in reply["user_facts"][0]


# ----------------------------------------------- #3020: Sleep-applied links


def _lane_memory():
    return SimpleNamespace(
        id=uuid4(),
        summary="The office is on floor 3",
        context_summary=None,
        type="fact",
        importance=0.5,
        scope="persistent",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        updated_at=None,
        details=None,
        client="test",
        tags=[],
        context=None,
        source_uri=None,
        source_type="manual",
        version=1,
        content="body",
        context_id=uuid4(),
    )


def test_a_lane_row_carries_superseded_by_from_the_shadow_map():
    memory, newer = _lane_memory(), uuid4()
    hit = {"id": str(memory.id), "score": 0.5}
    row = user_facts_lane._row(
        hit,
        memory,
        {},
        with_content=False,
        trusted_context_ids=None,
        shadow_map={memory.id: newer},
    )
    assert row.superseded_by == newer
    plain = user_facts_lane._row(
        hit, memory, {}, with_content=False, trusted_context_ids=None, shadow_map={}
    )
    assert plain.superseded_by is None
