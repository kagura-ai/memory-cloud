"""SessionStart session index (#2334): the hook prints a small context's index.

``load_guardrails`` returns ``index`` (one 80-character pointer row per live
memory, newest first) when the context is small and ``index: null`` above the
server's cap. The hook caches the rows next to the guardrail lanes and prints
them after the guardrail line as ``Memory index (N rows …)`` with one
``- <summary> (mem: <id8>)`` line each, inside the client's render budget.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

from tests.plugin.conftest import (
    PluginEnv,
    RunHook,
    StubServer,
    item,
    memory_id,
    payload,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only hook")


def index_rows(n: int, summary_len: int = 40) -> list[dict[str, Any]]:
    return [
        {
            "memory_id": memory_id(100 + k),
            "summary": f"row {k:02d} " + "x" * (summary_len - 7),
            "type": "decision",
            "updated_at": "2026-09-01T00:00:00Z",
        }
        for k in range(n)
    ]


def _fresh(plugin_env: PluginEnv, rows: Any, total: Any = None, items: Any = None) -> None:
    extra: dict[str, Any] = {"index": rows}
    if total is not None:
        extra["index_total_available"] = total
    plugin_env.write_cache(
        items or [],
        fetched_at=datetime.now(UTC) - timedelta(seconds=5),
        extra=extra,
    )


def _context(result: Any) -> str:
    return result.specific.get("additionalContext", "") if result.json else ""


def test_small_context_lists_every_memory_on_one_line(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    rows = index_rows(12)
    _fresh(plugin_env, rows, 12)
    result = run_hook(payload("SessionStart", source="startup"))
    assert result.returncode == 0
    lines = _context(result).split("\n")
    assert lines[0].startswith("Memory index (12 rows, newest first;")
    assert lines[1:] == [f"- {r['summary']} (mem: {r['memory_id'][:8]})" for r in rows]


def test_index_follows_the_guardrail_line(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _fresh(plugin_env, index_rows(2), 2, items=[item(1, "ps hangs", "Bash", match=r"\bps\b")])
    lines = _context(run_hook(payload("SessionStart", source="startup"))).split("\n")
    assert lines[0].startswith("Kagura Memory: 1 tool guardrails active")
    assert lines[1].startswith("Memory index (2 rows")
    assert len(lines) == 4


@pytest.mark.parametrize("cached", [None, "absent", []])
def test_no_index_leaves_the_output_unchanged(
    plugin_env: PluginEnv, run_hook: RunHook, cached: Any
) -> None:
    """``index: null`` (a context above the cap), a cache without the key and an
    empty index all print exactly what the hook printed before #2334."""
    guard = [item(1, "ps hangs", "Bash", match=r"\bps\b")]
    fetched = datetime.now(UTC) - timedelta(seconds=5)
    if cached == "absent":
        plugin_env.write_cache(guard, fetched_at=fetched)
    else:
        plugin_env.write_cache(
            guard, fetched_at=fetched, extra={"index": cached, "index_total_available": 500}
        )
    context = _context(run_hook(payload("SessionStart", source="startup")))
    assert "Memory index" not in context
    assert context.startswith("Kagura Memory: 1 tool guardrails active")
    assert "\n" not in context


def test_old_format_1_cache_without_index_is_read(
    plugin_env: PluginEnv, run_hook: RunHook, hook_module: ModuleType
) -> None:
    """A cache written before #2334 keeps working: same format, no index key."""
    plugin_env.write_cache(
        [item(1, "ps hangs", "Bash", match=r"\bps\b", action="block")],
        fetched_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    loaded = hook_module.load_cache(
        str(plugin_env.cache_path), plugin_env.context_id, datetime.now(UTC), None
    )
    assert loaded is not None and "index" not in loaded.data
    assert hook_module.CACHE_FORMAT == 1
    assert run_hook(payload("SessionStart", source="startup")).returncode == 0


def test_budget_cut_keeps_the_newest_rows_and_names_the_rest(
    plugin_env: PluginEnv, run_hook: RunHook, hook_module: ModuleType
) -> None:
    rows = index_rows(200, summary_len=80)
    _fresh(plugin_env, rows, 200)
    context = _context(run_hook(payload("SessionStart", source="startup")))
    assert len(context) <= hook_module.CLAUDE_RENDER_BUDGET_CHARS
    lines = context.split("\n")
    shown = lines[1:-1]
    assert 0 < len(shown) < 200
    assert shown == [f"- {r['summary']} (mem: {r['memory_id'][:8]})" for r in rows[: len(shown)]]
    assert lines[-1] == f"- {200 - len(shown)} more — recall to search"


def test_server_side_cut_is_named_from_the_total(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _fresh(plugin_env, index_rows(3), 10)
    lines = _context(run_hook(payload("SessionStart", source="startup"))).split("\n")
    assert lines[0].startswith("Memory index (10 rows")
    assert lines[-1] == "- 7 more — recall to search"


def test_rows_are_flattened_and_capped(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    rows = [
        {"memory_id": memory_id(1), "summary": "two\nlines <!-- note -->"},
        {"memory_id": memory_id(2), "summary": "y" * 300},
        {"memory_id": 5, "summary": "not a row"},
    ]
    _fresh(plugin_env, rows)
    lines = _context(run_hook(payload("SessionStart", source="startup"))).split("\n")
    assert lines[0].startswith("Memory index (2 rows")
    assert lines[1] == f"- two lines <!- - note - -> (mem: {memory_id(1)[:8]})"
    assert lines[2] == f"- {'y' * 79}… (mem: {memory_id(2)[:8]})"
    assert len(lines) == 3


def test_fetched_index_is_cached_and_printed(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    server_rows = [{**r, "summary_cut": True} for r in index_rows(3)]
    stub_server.set_guardrails(
        [], extra={"index": server_rows, "index_total_available": 3, "index_truncated": False}
    )
    env = {**plugin_env.env, "CLAUDE_PLUGIN_OPTION_SERVER_URL": stub_server.url}
    result = run_hook(payload("SessionStart", source="startup"), env=env)
    assert len(stub_server.requests) == 1
    cache = json.loads(plugin_env.cache_path.read_text(encoding="utf-8"))
    assert cache["format"] == 1
    assert cache["index"] == [
        {"memory_id": r["memory_id"], "summary": r["summary"]} for r in server_rows
    ]
    assert cache["index_total_available"] == 3
    lines = _context(result).split("\n")
    assert lines[0].startswith("Memory index (3 rows")
    assert len(lines) == 4


def test_server_without_the_lane_caches_null(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails([])
    env = {**plugin_env.env, "CLAUDE_PLUGIN_OPTION_SERVER_URL": stub_server.url}
    result = run_hook(payload("SessionStart", source="startup"), env=env)
    cache = json.loads(plugin_env.cache_path.read_text(encoding="utf-8"))
    assert cache["index"] is None and cache["index_total_available"] is None
    assert "Memory index" not in result.stdout
