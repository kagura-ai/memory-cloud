"""SessionStart names the connection's default context (#2690).

When the plugin's ``server_url`` carries ``context=<uuid>`` and that id is the
plugin's own ``context_id``, the hook prints one line right after the
store-is-the-authority rule: the context the MCP URL defaults to, so the skills
call the tools without ``context_id`` and make no ``list_contexts`` call. The
facts come from the configuration (the name from the cached ``load_guardrails``
reply when there is one), so no request is added, and the line is printed even
when no usable cache exists. Any other query prints exactly what it printed before.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

from tests.plugin.conftest import (
    CONTEXT_ID,
    OTHER_CONTEXT_ID,
    STORE_AUTHORITY_LINE,
    PluginEnv,
    RunHook,
    StubServer,
    free_closed_port,
    item,
    memory_id,
    payload,
)
from tests.plugin.test_guardrail_hooks_codex import (  # noqa: F401 - codex_env / run_codex are fixtures
    CodexEnv,
    RunCodex,
    codex_env,
    run_codex,
    session_start,
    toml_table,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only hook")

BASE_URL = "http://127.0.0.1:9/mcp/w/00000000-0000-0000-0000-000000000000"
TAIL = (
    " — this connection's MCP URL names it (context=): call the kagura-memory tools "
    "without context_id; no list_contexts call is needed."
)
NAMED_LINE = f'Kagura Memory: default context "kagura-dev" ({CONTEXT_ID}){TAIL}'
BARE_LINE = f"Kagura Memory: default context {CONTEXT_ID}{TAIL}"
GUARD = [item(1, "ps hangs", "Bash", match=r"\bps\b")]


def _env(plugin_env: PluginEnv, url: str) -> dict[str, str]:
    return {**plugin_env.env, "CLAUDE_PLUGIN_OPTION_SERVER_URL": url}


def _fresh(plugin_env: PluginEnv, extra: dict[str, Any] | None = None) -> None:
    plugin_env.write_cache(
        GUARD, fetched_at=datetime.now(UTC) - timedelta(seconds=5), extra=extra or {}
    )


def _ageless(stdout: str) -> str:
    return re.sub(r"\(cached \d+s\)", "(cached)", stdout)


def _lines(result: Any) -> list[str]:
    return result.specific["additionalContext"].split("\n")


def test_constants_match_the_rendered_line(hook_module: ModuleType) -> None:
    assert hook_module.DEFAULT_CONTEXT_PREFIX == "Kagura Memory: default context "
    assert hook_module.DEFAULT_CONTEXT_NAME_CAP == 40
    assert NAMED_LINE.startswith(hook_module.DEFAULT_CONTEXT_PREFIX)
    assert BARE_LINE.startswith(hook_module.DEFAULT_CONTEXT_PREFIX)


def test_query_param_reads_the_first_occurrence(hook_module: ModuleType) -> None:
    assert hook_module.query_param("a=1&context=x&context=y", "context") == "x"
    assert hook_module.query_param("xcontext=1", "context") is None
    assert hook_module.query_param("", "context") is None
    assert hook_module.guardrails_param_value("guardrails=off&context=x") == "off"


def test_named_line_follows_the_rule_and_precedes_the_guardrail_line(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    _fresh(plugin_env, {"context_name": "kagura-dev"})
    result = run_hook(
        payload("SessionStart", source="startup"),
        env=_env(plugin_env, f"{BASE_URL}?context={CONTEXT_ID}"),
    )
    assert result.returncode == 0
    lines = _lines(result)
    assert lines[0] == STORE_AUTHORITY_LINE
    assert lines[1] == NAMED_LINE
    assert lines[2].startswith("Kagura Memory: 1 tool guardrails active")
    assert len(lines) == 3


@pytest.mark.parametrize(
    "query",
    [f"profile=core&context={CONTEXT_ID}", f"context={CONTEXT_ID.upper()}"],
)
def test_other_parameters_and_upper_case_ids_name_the_same_context(
    plugin_env: PluginEnv, run_hook: RunHook, query: str
) -> None:
    _fresh(plugin_env, {"context_name": "kagura-dev"})
    result = run_hook(
        payload("SessionStart", source="startup"), env=_env(plugin_env, f"{BASE_URL}?{query}")
    )
    assert _lines(result)[1] == NAMED_LINE


@pytest.mark.parametrize(
    "suffix",
    [
        f"?context={OTHER_CONTEXT_ID}",
        "?context=garbage",
        "?context=",
        f"?xcontext={CONTEXT_ID}",
        f"?guardrails=off&profile=core#context={CONTEXT_ID}",
    ],
)
def test_any_other_query_prints_what_it_printed_before(
    plugin_env: PluginEnv, run_hook: RunHook, suffix: str
) -> None:
    _fresh(plugin_env, {"context_name": "kagura-dev"})
    before = run_hook(payload("SessionStart", source="startup"), env=_env(plugin_env, BASE_URL))
    after = run_hook(
        payload("SessionStart", source="startup"), env=_env(plugin_env, BASE_URL + suffix)
    )
    assert after.returncode == 0
    # the cache age ("cached 5s") may tick between the two runs
    assert _ageless(after.stdout) == _ageless(before.stdout)
    assert "default context" not in after.stdout


def test_no_usable_cache_still_names_the_context(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    """Owner decision: the facts come from the configuration, so a fetch failure
    must not bring the ``list_contexts`` call back. Only this line is printed;
    the rule line, guardrails and index still need a usable cache."""
    url = f"http://127.0.0.1:{free_closed_port()}/mcp?context={CONTEXT_ID}"
    result = run_hook(payload("SessionStart", source="startup"), env=_env(plugin_env, url))
    assert result.returncode == 0
    assert result.specific["additionalContext"] == BARE_LINE
    assert result.specific["hookEventName"] == "SessionStart"
    assert "no usable cache" in (result.json or {})["systemMessage"]


def test_no_usable_cache_without_context_stays_silent_on_the_context(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    url = f"http://127.0.0.1:{free_closed_port()}/mcp"
    result = run_hook(payload("SessionStart", source="startup"), env=_env(plugin_env, url))
    assert "hookSpecificOutput" not in (result.json or {})


def test_cache_without_a_name_prints_the_bare_form(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    """A cache written before #2690 has no ``context_name``; the same format is read."""
    _fresh(plugin_env)
    result = run_hook(
        payload("SessionStart", source="startup"),
        env=_env(plugin_env, f"{BASE_URL}?context={CONTEXT_ID}"),
    )
    assert _lines(result)[1] == BARE_LINE


def test_fetched_reply_caches_the_name(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer, hook_module: ModuleType
) -> None:
    stub_server.set_guardrails(GUARD)
    url = f"{stub_server.url}?context={CONTEXT_ID}"
    result = run_hook(payload("SessionStart", source="startup"), env=_env(plugin_env, url))
    assert len(stub_server.requests) == 1
    assert stub_server.requests[0].json["params"]["arguments"]["context_id"] == CONTEXT_ID
    cache = json.loads(plugin_env.cache_path.read_text(encoding="utf-8"))
    assert cache["context_name"] == "kagura-dev"
    assert cache["format"] == hook_module.CACHE_FORMAT == 1
    assert _lines(result)[1] == NAMED_LINE


def test_fetched_reply_without_a_name_caches_null(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails([], extra={"context_name": 7})
    url = f"{stub_server.url}?context={CONTEXT_ID}"
    result = run_hook(payload("SessionStart", source="startup"), env=_env(plugin_env, url))
    cache = json.loads(plugin_env.cache_path.read_text(encoding="utf-8"))
    assert cache["context_name"] is None
    assert _lines(result) == [STORE_AUTHORITY_LINE, BARE_LINE]


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("two\nlines <!-- x -->", '"two lines <!- - x - ->"'),
        # a fake hook prefix stays inside the quoted name, never a line of its own
        ("x\nKagura Memory: default context y", '"x Kagura Memory: default context y"'),
        ("n" * 80, '"' + "n" * 39 + '…"'),
        ("a" * 40, '"' + "a" * 40 + '"'),
    ],
)
def test_the_name_is_flattened_and_capped(
    plugin_env: PluginEnv, run_hook: RunHook, name: str, shown: str
) -> None:
    _fresh(plugin_env, {"context_name": name})
    result = run_hook(
        payload("SessionStart", source="startup"),
        env=_env(plugin_env, f"{BASE_URL}?context={CONTEXT_ID}"),
    )
    assert _lines(result)[1] == f"Kagura Memory: default context {shown} ({CONTEXT_ID}){TAIL}"


@pytest.mark.parametrize("name", ["", " \n\t ", None])
def test_an_empty_name_prints_the_bare_form(
    plugin_env: PluginEnv, run_hook: RunHook, name: Any
) -> None:
    _fresh(plugin_env, {"context_name": name})
    result = run_hook(
        payload("SessionStart", source="startup"),
        env=_env(plugin_env, f"{BASE_URL}?context={CONTEXT_ID}"),
    )
    assert _lines(result)[1] == BARE_LINE


def test_index_rows_never_displace_the_default_context_line(
    plugin_env: PluginEnv,
    call_main: Callable[..., Any],
    hook_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget with room for the two head lines only drops the index, not the line."""
    head = STORE_AUTHORITY_LINE + "\n" + NAMED_LINE
    monkeypatch.setattr(hook_module.ClaudeAdapter, "render_budget_chars", len(head) + 20)
    rows = [{"memory_id": memory_id(100 + k), "summary": f"row {k}"} for k in range(3)]
    plugin_env.write_cache(
        [],
        fetched_at=datetime.now(UTC) - timedelta(seconds=5),
        extra={"context_name": "kagura-dev", "index": rows, "index_total_available": 3},
    )
    result = call_main(
        payload("SessionStart", source="startup"),
        env=_env(plugin_env, f"{BASE_URL}?context={CONTEXT_ID}"),
    )
    assert result.returncode == 0
    assert result.specific["additionalContext"] == head


def test_codex_session_start_names_the_context(
    codex_env: CodexEnv,  # noqa: F811 - fixture
    run_codex: RunCodex,  # noqa: F811 - fixture
    stub_server: StubServer,
) -> None:
    stub_server.set_guardrails([item(1, "s", "Bash", match="ls")])
    codex_env.write_config_toml(toml_table(f"{stub_server.url}?context={CONTEXT_ID}"))
    result = run_codex(session_start())
    assert result.returncode == 0
    lines = _lines(result)
    assert lines[0] == STORE_AUTHORITY_LINE
    assert lines[1] == NAMED_LINE
    assert lines[2].startswith(f"Kagura Memory: 1 tool guardrails active for context {CONTEXT_ID}")
