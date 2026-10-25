"""#2124: recall returns content for the top N results (``full``).

Pins:

1. N selection — omitted → 3, explicit 0 / 1 / 10, never more than ``k``.
2. The per-item slice: 1,500 characters, ``content_truncated`` /
   ``content_next_offset`` only when cut (absent, never null, in the MCP item).
3. Content goes on the shipped rows in page order — a fold head carries its
   own content; folded members are not rows.
4. The MCP ``max_chars`` rule: content leaves the lowest-ranked rows first
   (``full_applied`` reports what is left), summaries are never cut for it.
5. The bootstrap lane gains no content; forget(query) and share keys ask for none.
6. REST and MCP parity: both honour ``full``; bad values are refused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mcp_server.tools import memory as mcp_memory
from models.schemas import MemoryResponse, RecallRequest, RecallResponse
from services.memory_service import MemoryService
from tests.mcp_server.test_recall_envelope import _context, _get_db_yielding, _handler_payload
from utils.recall_projection import (
    RECALL_CONTENT_CHARS,
    RECALL_DEFAULT_FULL,
    content_slice,
    effective_full,
    recall_result_item,
)
from utils.response_budget import json_chars

T0 = datetime(2026, 10, 1, tzinfo=UTC)
TRUSTED_CTX = uuid4()
EXTERNAL_CTX = uuid4()


def _mem(
    i: int, content: str | None = None, *, source_type=None, context_id=TRUSTED_CTX
) -> SimpleNamespace:
    return SimpleNamespace(
        context_id=context_id,
        id=uuid4(),
        summary=f"fact {i}",
        context_summary=None,
        content=f"content of fact {i}" if content is None else content,
        type="fact",
        importance=0.5,
        scope="working",
        created_at=T0 - timedelta(days=i),
        updated_at=None,
        client="test",
        tags=[],
        context=None,
        details=None,  # #2330
        source_uri=None,
        source_type=source_type,
        version=1,
        supersede_candidate=None,
    )


def _pool(mems):
    search_results = [
        {"id": str(m.id), "score": 1 - i / 100, "hybrid_score": 1 - i / 100}
        for i, m in enumerate(mems)
    ]
    return search_results, {str(m.id): m for m in mems}


async def _build(mems, *, fold_map=None, content_trusted_only=False, **request_kwargs):
    svc = MemoryService(MagicMock())
    svc._resolve_supersede_candidates = AsyncMock(return_value={})
    svc._recall_trusted_context_ids = AsyncMock(return_value={TRUSTED_CTX})
    search_results, memories = _pool(mems)
    return await svc._recall_build_responses(
        RecallRequest(query="q", **request_kwargs),
        search_results,
        memories,
        shadow_map={},
        contradiction_map={},
        user_id="u",
        fold_map=fold_map,
        content_trusted_only=content_trusted_only,
    )


def _response(**overrides) -> MemoryResponse:
    fields = {
        "memory_id": uuid4(),
        "summary": "JWT expiry caused 401",
        "context_summary": None,
        "type": "fact",
        "importance": 0.5,
        "scope": "working",
        "created_at": T0,
        "client": "mcp",
        "tags": [],
        "context": None,
        "score": 0.9,
    }
    fields.update(overrides)
    return MemoryResponse(**fields)


# ----------------------------------------------------------------- N selection


class TestEffectiveFull:
    @pytest.mark.parametrize(
        ("full", "k", "expected"),
        [(None, 5, 0), (0, 5, 0), (1, 5, 1), (3, 2, 2), (10, 5, 5), (10, 20, 10)],
    )
    def test_service_default_is_none_explicit_and_clamped_to_k(self, full, k, expected):
        assert effective_full(full, k) == expected

    def test_the_tool_default_is_three(self):
        assert RECALL_DEFAULT_FULL == 3


@pytest.mark.asyncio
class TestBuildResponses:
    async def test_full_three_puts_content_on_the_top_three(self):
        mems = [_mem(i) for i in range(5)]
        responses = await _build(mems, k=5, full=3)
        assert [r.content for r in responses] == [m.content for m in mems[:3]] + [None, None]
        assert all(r.content_truncated is None for r in responses)

    @pytest.mark.parametrize(("full", "with_content"), [(0, 0), (1, 1), (10, 4)])
    async def test_explicit_full(self, full, with_content):
        responses = await _build([_mem(i) for i in range(4)], k=4, full=full)
        assert sum(r.content is not None for r in responses) == with_content
        assert all(r.content is not None for r in responses[:with_content])

    async def test_long_content_is_cut_with_its_continuation_offset(self):
        long = "x" * (RECALL_CONTENT_CHARS + 10)
        exact = "y" * RECALL_CONTENT_CHARS
        responses = await _build([_mem(0, long), _mem(1, exact)], k=5, full=3)
        assert responses[0].content == long[:RECALL_CONTENT_CHARS]
        assert responses[0].content_truncated is True
        assert responses[0].content_next_offset == RECALL_CONTENT_CHARS
        assert responses[1].content == exact
        assert responses[1].content_truncated is None
        assert responses[1].content_next_offset is None

    async def test_an_unhydrated_hit_does_not_use_up_a_slot(self):
        mems = [_mem(i) for i in range(4)]
        svc = MemoryService(MagicMock())
        svc._resolve_supersede_candidates = AsyncMock(return_value={})
        search_results, memories = _pool(mems)
        del memories[str(mems[0].id)]  # no PostgreSQL row → skipped
        responses = await svc._recall_build_responses(
            RecallRequest(query="q", k=4, full=2),
            search_results,
            memories,
            shadow_map={},
            contradiction_map={},
            user_id="u",
        )
        assert [r.content is not None for r in responses] == [True, True, False]

    async def test_a_fold_head_carries_its_own_content(self):
        head, other = _mem(0, "newest version"), _mem(1)
        older = uuid4()  # folded out of the pool — not a row
        responses = await _build([head, other], k=5, full=1, fold_map={head.id: [older]})
        assert responses[0].memory_id == head.id
        assert responses[0].content == "newest version"
        assert responses[0].folded.memory_ids == [older]
        assert responses[1].content is None


def test_content_slice():
    assert content_slice("short") == ("short", None)
    text = "a" * (RECALL_CONTENT_CHARS + 1)
    assert content_slice(text) == (text[:RECALL_CONTENT_CHARS], RECALL_CONTENT_CHARS)


# ------------------------------------------------------------------ projection


class TestProjection:
    @pytest.mark.parametrize("verbosity", ["compact", "full"])
    def test_content_follows_the_summary_and_flags_only_when_cut(self, verbosity):
        plain = recall_result_item(_response(content="body"), verbosity)
        assert plain["content"] == "body"
        assert "content_truncated" not in plain and "content_next_offset" not in plain
        keys = list(plain)
        assert keys.index("content") == keys.index("summary") + 1

        cut = recall_result_item(
            _response(content="b" * 10, content_truncated=True, content_next_offset=1500),
            verbosity,
        )
        assert cut["content_truncated"] is True
        assert cut["content_next_offset"] == 1500

    @pytest.mark.parametrize("verbosity", ["compact", "full"])
    def test_no_content_keys_without_content(self, verbosity):
        item = recall_result_item(_response(), verbosity)
        assert not {"content", "content_truncated", "content_next_offset"} & set(item)


# ------------------------------------------------------------- max_chars rule


def _envelope(n: int, content_chars: int) -> dict:
    result = RecallResponse(
        results=[
            _response(summary=f"summary {i} " * 10, content="c" * content_chars)
            if i < 3
            else _response(summary=f"summary {i} " * 10)
            for i in range(n)
        ]
    )
    return mcp_memory._recall_envelope(result, _context(), "compact")


class TestFitRecallContent:
    def test_fits_untouched(self):
        env = _envelope(5, 1_500)
        assert mcp_memory._fit_recall_content(env, 20_000) is env
        assert "full_applied" not in env

    def test_lowers_n_from_the_bottom_and_keeps_summaries(self):
        env = _envelope(5, 4_000)  # 3 x 4,000 of content does not fit 10,000
        fitted = mcp_memory._fit_recall_content(env, 10_000)
        assert json_chars(fitted) <= 10_000
        with_content = [i for i, item in enumerate(fitted["results"]) if "content" in item]
        assert with_content == [0, 1]
        assert fitted["full_applied"] == 2
        assert [r["summary"] for r in fitted["results"]] == [r["summary"] for r in env["results"]]
        assert fitted["count"] == 5
        # The input envelope is not mutated.
        assert all("content" in env["results"][i] for i in range(3))

    def test_falls_to_zero_then_rows_are_cut_as_before(self):
        env = _envelope(60, 4_000)
        fitted = mcp_memory._fit_recall_content(env, 10_000)
        assert fitted["full_applied"] == 0
        bounded = mcp_memory._bound_recall_envelope(fitted, 10_000)
        assert json_chars(bounded) <= 10_000
        assert bounded["truncated"] is True

    def test_measure_is_exact(self):
        """The running total matches a real re-serialization at the stop point."""
        env = _envelope(5, 3_000)
        for budget in range(10_000, json_chars(env) + 1, 777):
            fitted = mcp_memory._fit_recall_content(env, budget)
            assert json_chars(fitted) <= budget or fitted.get("full_applied") == 0


# --------------------------------------------------------------- MCP handler


def _result_with_content() -> RecallResponse:
    return RecallResponse(results=[_response(content="body"), _response()])


@pytest.mark.asyncio
class TestHandler:
    async def test_content_reaches_the_item(self):
        payload = await _handler_payload({"full": 1}, _result_with_content())
        assert payload["results"][0]["content"] == "body"
        assert "content" not in payload["results"][1]
        assert "full_applied" not in payload

    async def test_full_is_passed_to_the_service(self):
        captured = {}

        async def recall(request, **_):
            captured["full"] = request.full
            return _result_with_content()

        service = MagicMock()
        service.recall = recall
        with (
            patch("db.base.get_db", new=_get_db_yielding(AsyncMock())),
            patch(
                "mcp_server.tools.memory._resolve_context_for_read",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch("mcp_server.tools.memory._context_response_fields", return_value={}),
            patch("mcp_server.tools.memory._touch_context_last_used", new=AsyncMock()),
            patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
            patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
        ):
            # Omitted → the tool's default (3); the service itself ships none.
            for value, expected in ((None, 3), (0, 0), (7, 7)):
                args = {"query": "q", "context_id": str(uuid4())}
                if value is not None:
                    args["full"] = value
                await mcp_memory.handle_recall(args, "u", None)
                assert captured["full"] == expected

    async def test_bad_full_is_an_invalid_argument(self):
        for bad in (-1, 101, True, "3", 1.5, [3]):
            result = await mcp_memory.handle_recall(
                {"query": "q", "context_id": str(uuid4()), "full": bad}, "u", None
            )
            payload = json.loads(result[0].text)
            assert payload["error"] == "invalid_argument", bad
            assert "full must be an integer between 0 and 100" in payload["message"]


# ------------------------------------------------------------- REST / schema


class TestRecallRequest:
    def test_default_is_none_and_bounds_are_enforced(self):
        assert RecallRequest(query="q").full is None
        assert RecallRequest(query="q", full=0).full == 0
        assert RecallRequest(query="q", full=100).full == 100
        for bad in (-1, 101, True, "3"):
            with pytest.raises(ValidationError):
                RecallRequest(query="q", full=bad)

    def test_rest_serialization_omits_absent_content(self):
        """The REST routes use response_model_exclude_none: no null content keys."""
        body = RecallResponse(results=[_response(content="body"), _response()]).model_dump(
            mode="json", exclude_none=True
        )
        assert body["results"][0]["content"] == "body"
        assert "content" not in body["results"][1]
        assert "content_truncated" not in body["results"][0]


@pytest.mark.asyncio
async def test_rest_recall_passes_full_through():
    from api.routes.memory import recall as rest_recall

    service = MagicMock()
    service.recall = AsyncMock(return_value=RecallResponse(results=[]))
    request = RecallRequest(query="q", full=2)
    await rest_recall(
        request,
        {"user_id": "u", "current_workspace_id": uuid4()},
        service,
        db=MagicMock(),
    )
    assert service.recall.await_args.args[0].full == 2


@pytest.mark.asyncio
async def test_share_key_recall_asks_for_no_content():
    from api.routes.share_keys import share_recall

    service = MagicMock()
    service.db = MagicMock()
    service.recall = AsyncMock(return_value=RecallResponse(results=[]))
    principal = {
        "share_key_context_id": uuid4(),
        "user_id": "u",
        "current_workspace_id": uuid4(),
    }
    with patch("api.routes.share_keys.ensure_context_not_capacity_locked", new=AsyncMock()):
        await share_recall(RecallRequest(query="q", full=5), principal, service)
    assert service.recall.await_args.args[0].full == 0


# ------------------------------------------------------------ other callers


@pytest.mark.asyncio
async def test_bootstrap_requests_no_content_and_drops_it_anyway():
    from services.agent_bootstrap_service import (
        AgentBootstrapService,
        BootstrapParams,
        BootstrapPrincipal,
    )

    svc = AgentBootstrapService(MagicMock())
    with patch("services.memory_service.MemoryService") as ms_cls:
        ms_cls.return_value.recall = AsyncMock(
            return_value=RecallResponse(
                results=[_response(content="x", content_truncated=True, content_next_offset=1)]
            )
        )
        body = await svc._recall(
            _context(),
            BootstrapParams(agent_id=uuid4(), query="auth"),
            BootstrapPrincipal(user_id="u", workspace_id=uuid4(), principal_type="agent"),
        )
        request = ms_cls.return_value.recall.await_args.args[0]
    assert request.full == 0
    assert not {"content", "content_truncated", "content_next_offset"} & set(body["results"][0])


def test_forget_by_query_asks_for_no_content():
    import inspect

    source = inspect.getsource(MemoryService.forget)
    assert "full=0" in source


def test_a_reply_inside_the_bound_reserve_loses_content_not_rows():
    """The bound step cuts a reply within ~32 characters of max_chars, so the
    content fit must aim below that reserve (review finding on #2124)."""
    env = _envelope(5, 1_500)
    size = json_chars(env)
    budget = size + 10  # fits max_chars, but not the bound step's reserve
    fitted = mcp_memory._fit_recall_content(env, budget)
    assert fitted["full_applied"] == 2
    bounded = mcp_memory._bound_recall_envelope(fitted, budget)
    assert "truncated" not in bounded
    assert bounded["count"] == 5
    assert json_chars(bounded) <= budget


class TestRecallAuditMetadata:
    def test_lists_the_results_that_shipped_content(self):
        from services.memory_service import _recall_audit_metadata

        with_content = [_response(content="a"), _response(content="b")]
        responses = [*with_content, _response()]
        metadata = _recall_audit_metadata(responses, 0)
        assert metadata == {
            "content_memory_ids": [str(r.memory_id) for r in with_content],
            "content_count": 2,
        }

    def test_none_without_content_or_filtering_and_capped_ids(self):
        from services.memory_access_event_writer import MAX_METADATA_MEMORY_IDS
        from services.memory_service import _recall_audit_metadata

        assert _recall_audit_metadata([_response()], 0) is None
        many = [_response(content="x") for _ in range(MAX_METADATA_MEMORY_IDS + 5)]
        metadata = _recall_audit_metadata(many, 3)
        assert len(metadata["content_memory_ids"]) == MAX_METADATA_MEMORY_IDS
        assert metadata["content_count"] == MAX_METADATA_MEMORY_IDS + 5
        assert metadata["binding_row_filtered_count"] == 3


# ------------------------------------------------- trusted-only default (owner)


@pytest.mark.asyncio
class TestTrustedOnlyWhenDefaulted:
    """A defaulted full (the MCP tool's 3) inlines content on trusted rows only;
    an untrusted row keeps summary-only and leaves its slot to the next trusted
    row. An explicit full is a deliberate request: every row is eligible."""

    async def test_defaulted_full_skips_a_connector_row_in_position_one(self):
        mems = [_mem(0, source_type="connector")] + [_mem(i) for i in range(1, 6)]
        responses = await _build(mems, k=6, full=3, content_trusted_only=True)
        assert [r.content is not None for r in responses] == [
            False,
            True,
            True,
            True,
            False,
            False,
        ]

    async def test_defaulted_full_skips_a_row_in_an_external_context(self):
        mems = [_mem(0), _mem(1, context_id=EXTERNAL_CTX), _mem(2), _mem(3)]
        responses = await _build(mems, k=4, full=2, content_trusted_only=True)
        assert [r.content is not None for r in responses] == [True, False, True, False]

    async def test_explicit_full_gives_the_connector_row_content(self):
        mems = [_mem(0, source_type="connector")] + [_mem(i) for i in range(1, 5)]
        responses = await _build(mems, k=5, full=3)
        assert [r.content is not None for r in responses] == [True, True, True, False, False]

    async def test_a_trusted_filter_needs_no_context_lookup(self):
        svc = MemoryService(MagicMock())
        svc._resolve_supersede_candidates = AsyncMock(return_value={})
        svc._recall_trusted_context_ids = AsyncMock(return_value=set())
        search_results, memories = _pool([_mem(0), _mem(1)])
        responses = await svc._recall_build_responses(
            RecallRequest(query="q", k=2, full=3, filters={"trust_tier": "trusted"}),
            search_results,
            memories,
            shadow_map={},
            contradiction_map={},
            user_id="u",
            content_trusted_only=True,
        )
        svc._recall_trusted_context_ids.assert_not_awaited()
        assert all(r.content is not None for r in responses)


@pytest.mark.asyncio
async def test_the_handler_marks_only_a_defaulted_full_as_trusted_only():
    captured = []

    async def recall(request, **kwargs):
        captured.append((request.full, kwargs.get("content_trusted_only")))
        return _result_with_content()

    service = MagicMock()
    service.recall = recall
    with (
        patch("db.base.get_db", new=_get_db_yielding(AsyncMock())),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._touch_context_last_used", new=AsyncMock()),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        await mcp_memory.handle_recall({"query": "q", "context_id": str(uuid4())}, "u", None)
        await mcp_memory.handle_recall(
            {"query": "q", "context_id": str(uuid4()), "full": 3}, "u", None
        )
    assert captured == [(3, True), (3, False)]
