"""No raw query text in INFO logs (#1721, Directory policy 1.D).

Policy 1.D: a connector must not collect extraneous conversation data, even
for logging. The recall, forget-by-query, REST recall and public-search log
lines used to carry ``query=request.query`` at INFO. They now carry the
query's length and the keyed hash ``memory_access_events.query_hash`` uses
(HMAC-SHA256 under ``audit_hmac_key``), so a log line can still be correlated
with an access event without holding the text.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import structlog

from config.settings import get_settings
from models.schemas import ForgetRequest, RecallRequest, RecallResponse
from utils.hashing import hmac_sha256_hex
from utils.query_log import query_log_fields

SECRET_QUERY = "my private diagnosis and the address I moved to"


def _expected_hash(query: str) -> str:
    return hmac_sha256_hex(query, get_settings().audit_hmac_key)


def _event(logs: list[dict], name: str) -> dict:
    matches = [entry for entry in logs if entry["event"] == name]
    assert len(matches) == 1, f"expected one {name!r}, got {logs!r}"
    return matches[0]


def _assert_redacted(entry: dict, query: str) -> None:
    assert "query" not in entry
    assert entry["query_len"] == len(query)
    assert entry["query_hash"] == _expected_hash(query)
    assert query not in repr(entry)


# ------------------------------------------------------------------ helper


def test_query_log_fields_carry_length_and_the_access_event_hash():
    fields = query_log_fields(SECRET_QUERY)

    assert fields == {"query_len": len(SECRET_QUERY), "query_hash": _expected_hash(SECRET_QUERY)}


def test_query_log_fields_match_the_memory_access_event_hash():
    """Same key and primitive as ``emit_memory_access_event``'s query_hash, so
    a log line joins to its access-event row."""
    import inspect

    from services import memory_access_event_writer

    source = inspect.getsource(memory_access_event_writer)
    assert "hmac_sha256_hex(query, get_settings().audit_hmac_key)" in source
    assert query_log_fields("q")["query_hash"] == hmac_sha256_hex(
        "q", get_settings().audit_hmac_key
    )


@pytest.mark.parametrize("query", [None, ""])
def test_query_log_fields_without_a_query(query):
    assert query_log_fields(query) == {"query_len": 0, "query_hash": None}


# ----------------------------------------------------------------- service


@pytest.mark.asyncio
async def test_service_recall_request_log_has_no_raw_query():
    from services.memory_service import MemoryService

    service = MemoryService(MagicMock())

    with structlog.testing.capture_logs() as logs, pytest.raises(ValueError):
        # No workspace/context: the guard raises right after the log line.
        await service.recall(RecallRequest(query=SECRET_QUERY, k=3), "user-1")

    entry = _event(logs, "recall_request")
    assert entry["log_level"] == "info"
    assert entry["user_id"] == "user-1"
    assert entry["k"] == 3
    _assert_redacted(entry, SECRET_QUERY)


@pytest.mark.asyncio
async def test_forget_by_query_log_has_no_raw_query():
    from services.memory_service import MemoryService

    service = MemoryService(MagicMock())
    ws, ctx, mid = uuid4(), uuid4(), uuid4()
    row = MagicMock(id=mid, workspace_id=ws, context_id=ctx, deleted_at=None)
    service.context_service.get_context = AsyncMock(return_value=MagicMock(id=ctx, workspace_id=ws))
    service.recall = AsyncMock(
        return_value=SimpleNamespace(
            results=[SimpleNamespace(memory_id=mid)], degraded=None, degraded_reason=None
        )
    )
    service.memory_repo.get = AsyncMock(return_value=row)
    service.memory_repo.update = AsyncMock()
    service._may_delete_guardrail = AsyncMock(return_value=True)
    service.db.commit = AsyncMock()

    with (
        patch("services.memory_service.resolve_collection_name", AsyncMock(return_value="c")),
        patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()),
        patch("repositories.neural_edge.NeuralEdgeRepository") as edge_cls,
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
        structlog.testing.capture_logs() as logs,
    ):
        edge_cls.return_value.delete_node_edges = AsyncMock(return_value=0)
        await service.forget(
            ForgetRequest(query=SECRET_QUERY, k=5), "user-1", current_context_id=ctx
        )

    entry = _event(logs, "memories_soft_deleted_by_query")
    assert entry["log_level"] == "info"
    assert entry["count"] == 1
    assert entry["user_id"] == "user-1"
    _assert_redacted(entry, SECRET_QUERY)


# ------------------------------------------------------------------- REST


@pytest.mark.asyncio
async def test_rest_recall_route_log_has_no_raw_query():
    from api.routes.memory import recall

    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=RecallResponse(results=[]))
    request = RecallRequest(query=SECRET_QUERY, k=4, filters={"context_id": str(uuid4())})

    with structlog.testing.capture_logs() as logs:
        await recall(
            request=request,
            user={"user_id": "u1", "current_workspace_id": uuid4()},
            memory_service=svc,
        )

    entry = _event(logs, "recall_request")
    assert entry["log_level"] == "info"
    assert entry["user_id"] == "u1"
    assert entry["k"] == 4
    _assert_redacted(entry, SECRET_QUERY)


@pytest.mark.asyncio
async def test_public_search_log_has_no_raw_query():
    from api.routes.public_search import PublicSearchRequest, public_search
    from utils.exceptions import NotFoundException

    db = MagicMock()
    db.get = AsyncMock(return_value=None)  # stop right after the log line
    context_id = uuid4()

    with structlog.testing.capture_logs() as logs, pytest.raises(NotFoundException):
        await public_search(
            context_id=context_id,
            request=PublicSearchRequest(query=SECRET_QUERY, limit=7),
            user=None,
            api_key=None,
            db=db,
        )

    entry = _event(logs, "public_search_request")
    assert entry["log_level"] == "info"
    assert entry["context_id"] == context_id
    assert entry["limit"] == 7
    assert entry["has_user"] is False
    _assert_redacted(entry, SECRET_QUERY)


# -------------------------------------------------------------------- LLM


@pytest.mark.asyncio
async def test_llm_json_parse_retry_log_has_length_not_content():
    """The sleep / analysis LLM output is built from stored memory text; the
    parse-failure warning logs its length, never a preview of it."""
    from services.llm_providers.base import ProviderResponse, Usage
    from services.llm_service import LLMService

    usage = Usage(total=1, input=1, output=0, cached=0, cache_write=0)
    bad = ProviderResponse(content=f"not json: {SECRET_QUERY}", usage=usage)
    good = ProviderResponse(content='{"ok": true}', usage=usage)
    provider = AsyncMock()
    provider.complete_json.side_effect = [bad, good]
    service = LLMService(AsyncMock())

    with (
        patch.object(service, "_get_provider", AsyncMock(return_value=provider)),
        structlog.testing.capture_logs() as logs,
    ):
        await service.complete_json(user_id="user-1", prompt="p", model="m", provider="openai")

    entry = _event(logs, "llm_json_parse_failed_retrying")
    assert entry["log_level"] == "warning"
    assert entry["content_len"] == len(bad.content)
    assert "content_preview" not in entry
    assert SECRET_QUERY not in repr(entry)


# ------------------------------------------------------------------- guard


# Name parts that mark a text-bearing log field, and parts that mark a field
# derived from the text without carrying it (a length, count or hash).
_TEXT_PARTS = {
    "query",
    "queries",
    "summary",
    "summaries",
    "content",
    "contents",
    "text",
    "prompt",
    "preview",
    "snippet",
    "excerpt",
    "transcript",
}
_DERIVED_PARTS = {"len", "length", "hash", "count", "size", "chars", "tokens", "revision"}


def _is_text_name(name: str) -> bool:
    parts = set(name.lower().split("_"))
    return bool(parts & _TEXT_PARTS) and not parts & _DERIVED_PARTS


def _carries_text(value) -> bool:
    """The value is a text-named variable or attribute, sliced or put in an
    f-string (``content[:200]``, ``request.query``, ``f"q={query}"``). A call
    (``len(query)``, ``query_log_fields(query)``) derives from it and is fine."""
    import ast

    if isinstance(value, ast.Subscript):
        return _carries_text(value.value)
    if isinstance(value, ast.Name):
        return _is_text_name(value.id)
    if isinstance(value, ast.Attribute):
        return _is_text_name(value.attr)
    if isinstance(value, ast.JoinedStr):
        return any(
            isinstance(part, ast.FormattedValue) and _carries_text(part.value)
            for part in value.values
        )
    return False


def _info_or_higher_text_fields(tree) -> list[tuple[int, str]]:
    import ast

    levels = {"info", "warning", "warn", "error", "exception", "critical"}
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in levels
            and "log" in ast.unparse(node.func.value).lower()
        ):
            continue
        for kw in node.keywords:
            if kw.arg is not None and (_is_text_name(kw.arg) or _carries_text(kw.value)):
                found.append((node.lineno, f"{kw.arg}={ast.unparse(kw.value)}"))
        for arg in node.args:
            if _carries_text(arg):
                found.append((node.lineno, ast.unparse(arg)))
    return found


@pytest.mark.parametrize(
    "call",
    [
        'logger.info("e", query=request.query)',
        'logger.warning("e", content_preview=content[:200])',
        'logger.error("e", raw_query=q)',
        'logger.info("e", q=request.query)',
        'logger.info("e", summary_text=memory.summary[:80])',
        'logger.info(f"searching {query}")',
        'self.logger.exception("e", prompt=prompt)',
    ],
)
def test_log_guard_catches_text_fields(call):
    import ast

    assert _info_or_higher_text_fields(ast.parse(call))


@pytest.mark.parametrize(
    "call",
    [
        'logger.info("e", query_len=len(q), query_hash=h)',
        'logger.info("e", **query_log_fields(request.query))',
        'logger.error("e", query_length=len(query))',
        'logger.info("e", prompt_revision=EDGE_DISCOVERY_PROMPT_REVISION)',
        'logger.warning("e", content_len=len(content))',
        'logger.debug("e", query=query[:50])',
        'logger.info("e", message=exc.message)',
    ],
)
def test_log_guard_allows_derived_fields_and_debug(call):
    import ast

    assert _info_or_higher_text_fields(ast.parse(call)) == []


def test_no_info_or_higher_log_call_carries_query_summary_or_content_text():
    """Static guard over ``src``: no ``logger.info|warning|error|exception|
    critical(...)`` call passes query, summary, content, prompt or preview
    text — by field name (``query=``, ``content_preview=``, ``raw_query=``)
    or by value (``request.query``, ``content[:200]``, an f-string over them).
    Lengths, counts and keyed hashes are fine. DEBUG is off in production and
    stays allowed."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    offenders = [
        f"{path.relative_to(src)}:{line} {field}"
        for path in sorted(src.rglob("*.py"))
        for line, field in _info_or_higher_text_fields(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []
