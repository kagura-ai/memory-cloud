"""MCP tool results are serialized once, as UTF-8 compact JSON (Issue #1599).

A tool result is paid for in the calling model's context window. With the
stdlib default (``ensure_ascii=True``) every non-ASCII character is emitted as
a 6-character ``\\uXXXX`` escape *inside* the ``TextContent`` string, so the
model reads the escapes — Japanese text costs several times what it should.

These tests pin:

- the shared serializer (``_dumps``) and the two response helpers built on it
- that no handler in ``mcp_server.tools`` bypasses it (AST guard)
- the acceptance surfaces named by the issue — recall, reference, load_pinned,
  list_contexts, get_context_info and error envelopes — end to end through the
  real handlers
- that the transport layer, which deliberately stays ASCII, still turns a tool
  text carrying a lone surrogate into a valid HTTP body
"""

import ast
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools import _helpers
from mcp_server.tools._helpers import _error_response, _success_response

JA_SUMMARY = "認証エラーの対処法: JWT の期限切れはリフレッシュトークンで再認証する"
JA_CONTEXT = "認証まわりの障害対応で参照する"

TOOLS_DIR = Path(_helpers.__file__).parent


def _assert_raw_utf8(text: str, *needles: str) -> None:
    """The tool text carries the characters themselves, not their escapes."""
    assert "\\u" not in text, f"\\u escape leaked into tool text: {text[:200]}"
    for needle in needles:
        assert needle in text


# ============================================================================
# The serializer
# ============================================================================


class TestDumps:
    def test_non_ascii_is_emitted_as_is(self):
        text = _helpers._dumps({"summary": JA_SUMMARY})
        _assert_raw_utf8(text, JA_SUMMARY)

    def test_compact_separators(self):
        assert _helpers._dumps({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'

    def test_round_trips(self):
        obj = {"summary": JA_SUMMARY, "tags": ["認証", "auth"], "n": 1.5, "none": None}
        assert json.loads(_helpers._dumps(obj)) == obj

    def test_structural_characters_are_still_escaped(self):
        # ensure_ascii=False must not weaken JSON string escaping: quotes,
        # backslashes and control characters stay escaped.
        obj = {"s": 'a"b\\c\nd\x00'}
        text = _helpers._dumps(obj)
        assert json.loads(text) == obj
        assert "\n" not in text

    def test_shorter_than_the_stdlib_default(self):
        obj = {"summary": JA_SUMMARY, "context_summary": JA_CONTEXT}
        assert len(_helpers._dumps(obj)) < len(json.dumps(obj)) / 2


class TestResponseHelpers:
    def test_success_response_is_utf8(self):
        text = _success_response(summary=JA_SUMMARY)[0].text
        _assert_raw_utf8(text, JA_SUMMARY)
        assert json.loads(text) == {"status": "success", "summary": JA_SUMMARY}

    def test_error_response_is_utf8(self):
        text = _error_response("validation_error", "k は整数で指定してください", help="例: 5")[
            0
        ].text
        _assert_raw_utf8(text, "k は整数で指定してください", "例: 5")
        assert json.loads(text)["error"] == "validation_error"


# ============================================================================
# Guard: nothing in the tools package bypasses the serializer
# ============================================================================


def _raw_json_dumps_calls(path: Path) -> list[tuple[str, int]]:
    """Return ``(enclosing_function, lineno)`` for each ``json.dumps`` /
    bare ``dumps`` call in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.Call):
            target = node.func
            is_json_dumps = (
                isinstance(target, ast.Attribute)
                and target.attr == "dumps"
                and isinstance(target.value, ast.Name)
                and target.value.id == "json"
            )
            is_bare_dumps = isinstance(target, ast.Name) and target.id == "dumps"
            if is_json_dumps or is_bare_dumps:
                hits.append((func, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, "<module>")
    return hits


def test_no_raw_json_dumps_in_tools_package():
    """Every tool result goes through ``_dumps``.

    A new handler that reaches for ``json.dumps`` silently reintroduces the
    ``\\uXXXX`` tax for every non-ASCII user. The only permitted call is the
    one inside ``_helpers._dumps`` itself.
    """
    offenders = []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        for func, lineno in _raw_json_dumps_calls(path):
            if path.name == "_helpers.py" and func == "_dumps":
                continue
            offenders.append(f"{path.name}:{lineno} (in {func})")
    assert not offenders, (
        "raw json.dumps in mcp_server/tools — serialize tool results with "
        "_helpers._dumps instead (#1599): " + ", ".join(offenders)
    )


def test_guard_detects_a_raw_call(tmp_path):
    # The guard must actually see what it claims to forbid.
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import json\n"
        "from json import dumps\n"
        "def handler():\n"
        "    return json.dumps({})\n"
        "def other():\n"
        "    return dumps({})\n"
    )
    assert _raw_json_dumps_calls(sample) == [("handler", 4), ("other", 6)]


# ============================================================================
# Acceptance surfaces, through the real handlers
# ============================================================================


def _get_db_yielding(db):
    async def mock_get_db():
        yield db

    return mock_get_db


@contextlib.contextmanager
def _patched_memory_handler(service):
    db = AsyncMock()
    with (
        patch("db.base.get_db", new=_get_db_yielding(db)),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._touch_context_last_used", new=AsyncMock()),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        yield


@pytest.mark.asyncio
async def test_recall_returns_japanese_as_is():
    from mcp_server.tools.memory import handle_recall
    from models.schemas import MemoryResponse, RecallResponse, RelatedTagItem

    now = datetime(2026, 9, 1, tzinfo=UTC)
    recall_result = RecallResponse(
        results=[
            MemoryResponse(
                memory_id=uuid4(),
                summary=JA_SUMMARY,
                context_summary=JA_CONTEXT,
                type="learning",
                importance=0.8,
                scope="persistent",
                created_at=now,
                client="mcp",
                tags=["認証", "auth"],
                context=None,
                score=0.87,
            )
        ],
        related_tags=[RelatedTagItem(tag="認証", count=3, sample_summary=JA_SUMMARY)],
    )
    service = MagicMock()
    service.recall = AsyncMock(return_value=recall_result)

    with _patched_memory_handler(service):
        result = await handle_recall(
            {"query": "認証エラー", "context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )

    text = result[0].text
    _assert_raw_utf8(text, JA_SUMMARY, JA_CONTEXT, "認証")
    assert json.loads(text)["results"][0]["summary"] == JA_SUMMARY


@pytest.mark.asyncio
async def test_reference_returns_japanese_as_is():
    from mcp_server.tools.memory import handle_reference

    now = datetime(2026, 9, 1, tzinfo=UTC)
    reference_result = SimpleNamespace(
        memory_id=uuid4(),
        summary=JA_SUMMARY,
        context_summary=JA_CONTEXT,
        content="本文: リフレッシュトークンのローテーションを有効にする",
        details={"手順": ["再認証", "時計ずれの確認"]},
        type="learning",
        scope="persistent",
        importance=0.8,
        tags=["認証"],
        context=None,
        created_at=now,
        updated_at=None,
        client="mcp",
        source_uri=None,
        source_type=None,
        outgoing_links=[],
        outgoing_has_more=False,
        incoming_links=[],
        incoming_has_more=False,
        supersede_candidate=None,
    )
    service = MagicMock()
    service.reference = AsyncMock(return_value=reference_result)

    with _patched_memory_handler(service):
        result = await handle_reference(
            {"memory_id": str(uuid4()), "context_id": str(uuid4())},
            user_id="u1",
            workspace_id=None,
        )

    text = result[0].text
    _assert_raw_utf8(text, JA_SUMMARY, "本文: リフレッシュトークン", "手順", "時計ずれの確認")
    assert json.loads(text)["memory"]["details"] == {"手順": ["再認証", "時計ずれの確認"]}


@pytest.mark.asyncio
async def test_load_pinned_returns_japanese_as_is():
    from mcp_server.tools.memory import handle_load_pinned

    pinned = SimpleNamespace(
        memories=[
            SimpleNamespace(
                memory_id=uuid4(),
                summary="本番の active color は green",
                context_summary="デプロイ前に必ず確認する",
                type="guardrail",
                importance=0.9,
                delivery_mode="always",
            )
        ],
        total_available=1,
        truncated=False,
        cap=100,
    )
    service = MagicMock()
    service.load_pinned = AsyncMock(return_value=pinned)

    with _patched_memory_handler(service):
        result = await handle_load_pinned(
            {"context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )

    _assert_raw_utf8(result[0].text, "本番の active color は green", "デプロイ前に必ず確認する")


@pytest.mark.asyncio
async def test_list_contexts_returns_japanese_as_is():
    from mcp_server.tools.context import handle_list_contexts

    ja_context = SimpleNamespace(
        id=uuid4(),
        name="開発メモ",
        summary="開発の知見をためるコンテキスト",
        is_private=True,
        is_locked=False,
        last_used_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    db = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = []  # no per-context search configs
    db.execute = AsyncMock(return_value=exec_result)
    context_service = MagicMock()
    context_service.list_contexts = AsyncMock(return_value=[ja_context])

    with (
        patch("db.base.get_db", new=_get_db_yielding(db)),
        patch(
            "services.context_service.ContextService",
            new=MagicMock(return_value=context_service),
        ),
        patch("mcp_server.tools.context._log_tool_usage", new=AsyncMock()),
    ):
        result = await handle_list_contexts({}, user_id="u1", workspace_id=None)

    # The context NAME is asserted (not the summary) so this stays true whatever
    # per-context fields list_contexts chooses to return by default.
    _assert_raw_utf8(result[0].text, "開発メモ")
    assert json.loads(result[0].text)["status"] == "success"


@pytest.mark.asyncio
async def test_get_context_info_returns_japanese_as_is():
    from mcp_server.tools.context import handle_get_context_info

    ja_context = SimpleNamespace(
        id=uuid4(),
        name="dev",
        display_name="開発メモ",
        summary="開発の知見をためるコンテキスト",
        usage_guide="作業の前に recall、終わったら remember する",
        is_private=True,
        is_locked=False,
        workspace_id=None,
    )
    db = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None  # no per-context search config
    db.execute = AsyncMock(return_value=exec_result)
    stats = SimpleNamespace(
        total_count=1,
        working_count=0,
        persistent_count=1,
        by_type={"学び": 1},
        by_importance={},
        recent_activity={},
    )
    service = MagicMock()
    service.get_stats = AsyncMock(return_value=stats)

    with (
        patch("db.base.get_db", new=_get_db_yielding(db)),
        patch(
            "mcp_server.tools.context._resolve_context_for_read",
            new=AsyncMock(return_value=ja_context),
        ),
        patch("mcp_server.tools.context._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        result = await handle_get_context_info(
            {"context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )

    text = result[0].text
    payload = json.loads(text)
    assert payload["status"] == "success"
    # The standing instructions block is non-ASCII-heavy too (arrows, bullets),
    # so the whole text — not only the user-authored fields — must be raw UTF-8.
    _assert_raw_utf8(
        text, "開発メモ", "開発の知見をためるコンテキスト", "作業の前に recall", "学び"
    )


@pytest.mark.asyncio
async def test_handler_error_envelope_is_utf8():
    from mcp_server.tools.memory import handle_recall_upcoming

    # A validation error that echoes caller input back.
    result = await handle_recall_upcoming(
        {"context_id": str(uuid4()), "k": "五"}, user_id="u1", workspace_id=None
    )
    text = result[0].text
    assert json.loads(text)["error"] == "validation_error"
    _assert_raw_utf8(text, "五")


@pytest.mark.asyncio
async def test_dispatch_crash_envelope_is_utf8():
    """The catch-all in ``execute_tool_call`` builds its own envelope — it must
    use the shared serializer too."""
    from mcp_server import tools as mcp_tools

    boom = AsyncMock(side_effect=RuntimeError("保存に失敗しました"))
    # workspace_id=None skips the rate-limit lookup, so only the registry needs
    # a stand-in. Patching the attribute restores whatever was there (built or
    # still None) on exit.
    registry = {**mcp_tools._build_registry(), "recall": boom}
    with patch.object(mcp_tools, "_TOOL_REGISTRY", registry):
        result = await mcp_tools.execute_tool_call(
            tool_name="recall",
            arguments={"query": "q", "context_id": str(uuid4())},
            user_id="u1",
            workspace_id=None,
        )
    text = result[0].text
    assert json.loads(text) == {"status": "error", "error": "保存に失敗しました"}
    _assert_raw_utf8(text, "保存に失敗しました")


# ============================================================================
# Transport boundary: stays ASCII, so a lone surrogate cannot break the body
# ============================================================================


@pytest.mark.asyncio
async def test_lone_surrogate_in_tool_text_still_yields_a_valid_http_body():
    """``_dumps`` passes a lone surrogate through (it is not valid UTF-8 on its
    own, so ``text.encode("utf-8")`` would raise). The JSON-RPC layer that wraps
    the tool text is decoded by the client's JSON parser, not read by the model,
    so it keeps ``ensure_ascii=True`` — which escapes the surrogate and keeps
    the HTTP body encodable. This drives the real send helper."""
    from mcp_server.transport import _send_jsonrpc_result

    tool_text = _success_response(summary="壊れた \ud800 文字")[0].text
    assert "\ud800" in tool_text  # the tool text really carries the raw surrogate
    with pytest.raises(UnicodeEncodeError):
        tool_text.encode("utf-8")

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await _send_jsonrpc_result(
        send, None, 1, {"content": [{"type": "text", "text": tool_text}], "isError": False}
    )

    body = sent[-1]["body"]
    assert isinstance(body, bytes)
    decoded = json.loads(body.decode("utf-8"))  # valid UTF-8, valid JSON
    assert decoded["result"]["content"][0]["text"] == tool_text  # lossless
