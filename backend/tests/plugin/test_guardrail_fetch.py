"""SessionStart fetch, cache write, freshness and refresh tests over a loopback stub (#1619).

The stub is an ``http.server`` on ``127.0.0.1:0`` recording every request. The
hook runs through the real ``hooks.json`` command under ``sh -c``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.plugin.conftest import (
    CANARY_KEY,
    CLAUDE_PLUGIN_JSON,
    CONTEXT_ID,
    PluginEnv,
    RunHook,
    StubServer,
    bash_pre,
    command_for,
    free_closed_port,
    item,
    memory_id,
    payload,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only hook")


def _env(plugin_env: PluginEnv, url: str) -> dict[str, str]:
    return {**plugin_env.env, "CLAUDE_PLUGIN_OPTION_SERVER_URL": url}


def _start(plugin_env: PluginEnv, run_hook: RunHook, url: str, source: str = "startup") -> Any:
    return run_hook(payload("SessionStart", source=source), env=_env(plugin_env, url))


STANDARD = [
    item(1, "Run gh pr view first", "Bash|PowerShell", match=r"gh pr merge\b.*--delete-branch"),
    item(
        2,
        "ps hangs the tool",
        "Bash|PowerShell",
        match=r"\bps\b",
        action="block",
        authored_by_caller=True,
    ),
    item(
        3,
        "Wrapper hid the failure",
        "Bash",
        on="result",
        match=r"summarised",
        authored_by_caller=False,
    ),
]


# ---------------------------------------------------------------------------
# Request and cache shape
# ---------------------------------------------------------------------------


def test_request_shape_and_cache_bytes(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD, pinned=[item(9, "pinned invariant", "x", importance=0.9)])
    result = _start(plugin_env, run_hook, stub_server.url + "?profile=core")
    assert result.returncode == 0 and result.stderr == ""
    assert len(stub_server.requests) == 1
    req = stub_server.requests[0]
    assert req.path == "/mcp/w/00000000-0000-0000-0000-000000000000?profile=core"
    assert req.headers["Authorization"] == "Bearer " + CANARY_KEY
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["Accept"] == "application/json"
    assert req.headers["MCP-Protocol-Version"] == "2026-07-28"
    assert req.headers["Mcp-Method"] == "tools/call"
    assert req.headers["Mcp-Name"] == "load_guardrails"
    manifest = json.loads(CLAUDE_PLUGIN_JSON.read_text(encoding="utf-8"))
    assert req.headers["User-Agent"] == f"kagura-memory-plugin-hooks/{manifest['version']} (claude)"
    body = req.json
    assert body["jsonrpc"] == "2.0" and body["method"] == "tools/call"
    assert body["params"]["name"] == "load_guardrails"
    assert body["params"]["arguments"] == {"context_id": CONTEXT_ID}
    assert body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert set(body["params"]) == {"name", "arguments", "_meta"}

    raw = plugin_env.cache_path.read_bytes()
    assert raw.endswith(b"\n") and b"\n" not in raw[:-1]
    cache = json.loads(raw)
    assert list(cache) == [
        "format",
        "context_id",
        "fetched_at",
        "version",
        "pinned",
        "tool_triggered",
    ]
    assert (
        cache["format"] == 1
        and cache["context_id"] == CONTEXT_ID
        and cache["version"] == "3f9c1a7b2d4e6f80"
    )
    assert cache["fetched_at"].endswith("Z")
    datetime.strptime(cache["fetched_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert [t["memory_id"] for t in cache["tool_triggered"]] == [
        memory_id(1),
        memory_id(2),
        memory_id(3),
    ]
    assert list(cache["tool_triggered"][0]) == [
        "memory_id",
        "summary",
        "importance",
        "tool_trigger",
    ]
    assert list(cache["tool_triggered"][1]) == [
        "memory_id",
        "summary",
        "importance",
        "authored_by_caller",
        "tool_trigger",
    ]
    assert cache["tool_triggered"][1]["authored_by_caller"] is True
    assert cache["tool_triggered"][2]["authored_by_caller"] is False
    for entry in cache["tool_triggered"]:
        assert set(entry) <= {
            "memory_id",
            "summary",
            "importance",
            "authored_by_caller",
            "tool_trigger",
        }
        assert list(entry["tool_trigger"])[:2] == ["tool", "on"]
    assert list(cache["pinned"][0]) == ["memory_id", "summary", "importance"]
    assert oct(plugin_env.cache_path.stat().st_mode & 0o777) == "0o600"
    assert oct(plugin_env.guardrails_dir.stat().st_mode & 0o777) == "0o700"
    assert b"context_summary" not in raw and b"source_type" not in raw and b"created_at" not in raw

    context = result.specific["additionalContext"]
    assert context == (
        f"Kagura Memory: 3 tool guardrails active for context {CONTEXT_ID} (fetched); "
        "the kagura-memory plugin hook delivers each at its matching tool call."
    )
    message = result.json["systemMessage"]
    assert message.startswith("kagura-memory guardrails: +3 new — ")
    assert f"{memory_id(3)[:8]} Wrapper hid the failure (by another member)" in message
    assert "(by another member)" not in message.split(memory_id(2)[:8])[1].split(";")[0]


def test_unchanged_version_rewrites_fetched_at_only(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD)
    plugin_env.write_cache(
        json.loads(json.dumps(STANDARD)), fetched_at=datetime.now(UTC) - timedelta(days=8)
    )
    before = json.loads(plugin_env.cache_path.read_text())
    result = _start(plugin_env, run_hook, stub_server.url)
    assert len(stub_server.requests) == 1
    after = json.loads(plugin_env.cache_path.read_text())
    assert after["fetched_at"] != before["fetched_at"]
    assert after["tool_triggered"] == before["tool_triggered"]
    assert after["version"] == before["version"]
    assert "systemMessage" not in result.json
    # 8-day cache + unchanged version: a matching PreToolUse fires again (fetched_at advanced)
    assert (
        run_hook(bash_pre("ps"), env=_env(plugin_env, stub_server.url)).specific[
            "permissionDecision"
        ]
        == "deny"
    )


def test_diff_names_new_changed_removed(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    old = [item(1, "old summary one", "Bash"), item(2, "two", "Bash"), item(3, "three", "Bash")]
    plugin_env.write_cache(old, fetched_at=datetime.now(UTC) - timedelta(minutes=5), version="v1")
    new = [
        item(1, "new summary one " + "x" * 100, "Bash"),
        item(2, "two", "Bash"),
        item(4, "four", "Bash", authored_by_caller=False),
    ]
    stub_server.set_guardrails(new, version="v2")
    result = _start(plugin_env, run_hook, stub_server.url)
    message = result.json["systemMessage"]
    assert "+1 new, ~1 changed, -1 removed" in message
    assert f"{memory_id(4)[:8]} four (by another member)" in message
    assert f"{memory_id(1)[:8]} new summary one " + "x" * 64 in message
    assert f"{memory_id(3)[:8]} three" in message
    assert len(message) <= 1000


def test_truncated_list_is_reported(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD, truncated=True)
    result = _start(plugin_env, run_hook, stub_server.url)
    assert "list truncated at 50" in result.json["systemMessage"]


# ---------------------------------------------------------------------------
# Redirects and schemes
# ---------------------------------------------------------------------------


def test_redirect_is_never_followed(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer, second_stub: StubServer
) -> None:
    second_stub.set_guardrails(STANDARD)
    stub_server.status = 302
    stub_server.raw_body = b""
    stub_server.extra_headers = {"Location": second_stub.url}
    plugin_env.write_cache(
        [item(7, "kept", "Bash")], fetched_at=datetime.now(UTC) - timedelta(hours=1), version="keep"
    )
    before = plugin_env.cache_path.read_bytes()
    result = _start(plugin_env, run_hook, stub_server.url)
    assert len(stub_server.requests) == 1
    assert second_stub.requests == []
    assert plugin_env.cache_path.read_bytes() == before
    assert "server unreachable, using cache from 1h" in result.json["systemMessage"]
    assert "1 tool guardrails active" in result.specific["additionalContext"]
    assert "(cached 1h)" in result.specific["additionalContext"]


def test_http_non_loopback_is_rejected_without_a_request(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    result = _start(plugin_env, run_hook, "http://example.test/mcp/w/x")
    assert result.json == {
        "systemMessage": "kagura-memory guardrails: server_url is missing or invalid — configure the plugin (/plugin → kagura-memory)"
    }
    assert not plugin_env.guardrails_dir.exists()


def test_http_loopback_works(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD)
    result = _start(plugin_env, run_hook, stub_server.url.replace("127.0.0.1", "localhost"))
    assert len(stub_server.requests) == 1
    assert "3 tool guardrails active" in result.specific["additionalContext"]


@pytest.mark.parametrize(
    ("field", "value", "named"),
    [
        ("CLAUDE_PLUGIN_OPTION_CONTEXT_ID", "", "context_id"),
        ("CLAUDE_PLUGIN_OPTION_CONTEXT_ID", "not-a-uuid", "context_id"),
        ("CLAUDE_PLUGIN_OPTION_SERVER_URL", "ftp://example.test/x", "server_url"),
        ("CLAUDE_PLUGIN_OPTION_API_KEY", "", "api_key"),
    ],
)
def test_partial_or_invalid_config_prints_one_message_and_no_request(
    plugin_env: PluginEnv,
    run_hook: RunHook,
    stub_server: StubServer,
    field: str,
    value: str,
    named: str,
) -> None:
    stub_server.set_guardrails(STANDARD)
    env = _env(plugin_env, stub_server.url)
    if value:
        env[field] = value
    else:
        del env[field]
    result = run_hook(payload("SessionStart", source="startup"), env=env)
    assert result.json == {
        "systemMessage": f"kagura-memory guardrails: {named} is missing or invalid — configure the plugin (/plugin → kagura-memory)"
    }
    assert stub_server.requests == []
    assert run_hook(bash_pre("ps"), env=env).stdout == ""


def test_guardrails_param_warning(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD)
    result = _start(plugin_env, run_hook, stub_server.url + "?guardrails=" + CONTEXT_ID)
    assert len(stub_server.requests) == 1
    assert (
        "this URL also requests the server digest (?guardrails=); use ?guardrails=off"
        in result.json["systemMessage"]
    )
    assert "3 tool guardrails active" in result.specific["additionalContext"]
    stub_server.requests.clear()
    plugin_env.cache_path.unlink()
    result = _start(plugin_env, run_hook, stub_server.url + "?guardrails=off")
    assert "?guardrails=" not in result.json.get("systemMessage", "")


# ---------------------------------------------------------------------------
# Failures and fallback
# ---------------------------------------------------------------------------


def test_hanging_server_returns_within_deadline(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.hang = True
    plugin_env.write_cache(STANDARD, fetched_at=datetime.now(UTC) - timedelta(hours=2))
    started = time.monotonic()
    result = _start(plugin_env, run_hook, stub_server.url)
    assert time.monotonic() - started < 4.5
    assert "using cache from 2h" in result.json["systemMessage"]
    assert "3 tool guardrails active" in result.specific["additionalContext"]


def test_server_down_with_fresh_and_stale_cache(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    url = f"http://127.0.0.1:{free_closed_port()}/mcp/w/x"
    plugin_env.write_cache(STANDARD, fetched_at=datetime.now(UTC) - timedelta(hours=1))
    result = _start(plugin_env, run_hook, url)
    assert "server unreachable, using cache from 1h" in result.json["systemMessage"]
    assert "3 tool guardrails active" in result.specific["additionalContext"]
    assert (plugin_env.guardrails_dir / f"{CONTEXT_ID}.fetch-failed").exists()

    plugin_env.write_cache(STANDARD, fetched_at=datetime.now(UTC) - timedelta(hours=25))
    (plugin_env.guardrails_dir / f"{CONTEXT_ID}.fetch-failed").unlink()
    result = _start(plugin_env, run_hook, url)
    assert result.json == {
        "systemMessage": "kagura-memory guardrails: server unreachable and no usable cache"
    }
    assert not plugin_env.cache_path.exists()
    assert (plugin_env.guardrails_dir / f"{CONTEXT_ID}.json.stale").exists()
    assert run_hook(bash_pre("ps"), env=_env(plugin_env, url)).stdout == ""


@pytest.mark.parametrize(
    "shape",
    ["isError", "jsonrpc_error", "http500", "not_json", "format2", "status_error", "unknown_tool"],
)
def test_bad_responses_are_failures_and_leave_the_cache_alone(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer, shape: str
) -> None:
    from tests.plugin.conftest import load_guardrails_response

    if shape == "isError":
        env = load_guardrails_response(STANDARD)
        env["result"]["isError"] = True
        stub_server.response = env
    elif shape == "jsonrpc_error":
        stub_server.response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "unknown"},
        }
    elif shape == "unknown_tool":
        stub_server.response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32602, "message": "Unknown tool"},
        }
    elif shape == "http500":
        stub_server.status = 500
        stub_server.raw_body = json.dumps({"echo": "Bearer " + CANARY_KEY}).encode()
    elif shape == "not_json":
        stub_server.raw_body = b"<html>nope</html>"
    elif shape == "format2":
        stub_server.set_guardrails(STANDARD, fmt=2)
    elif shape == "status_error":
        stub_server.set_guardrails(STANDARD, status="error")
    plugin_env.write_cache(
        [item(7, "kept", "Bash")],
        fetched_at=datetime.now(UTC) - timedelta(minutes=10),
        version="keep",
    )
    before = plugin_env.cache_path.read_bytes()
    result = _start(plugin_env, run_hook, stub_server.url)
    assert plugin_env.cache_path.read_bytes() == before
    assert "server unreachable, using cache from 10m" in result.json["systemMessage"]
    assert CANARY_KEY not in result.stdout + result.stderr
    assert stub_server.url.split("/")[2] not in result.stdout


def test_fetch_failed_marker_suppresses_retries_for_60s(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD)
    plugin_env.guardrails_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (plugin_env.guardrails_dir / f"{CONTEXT_ID}.fetch-failed").write_text("")
    result = _start(plugin_env, run_hook, stub_server.url)
    assert stub_server.requests == []
    assert "no usable cache" in result.json["systemMessage"]
    old = time.time() - 120
    os.utime(plugin_env.guardrails_dir / f"{CONTEXT_ID}.fetch-failed", (old, old))
    _start(plugin_env, run_hook, stub_server.url)
    assert len(stub_server.requests) == 1
    assert not (plugin_env.guardrails_dir / f"{CONTEXT_ID}.fetch-failed").exists()


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact", "fork"])
def test_fresh_cache_means_zero_requests_for_every_source(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer, source: str
) -> None:
    stub_server.set_guardrails(STANDARD)
    plugin_env.write_cache(STANDARD, fetched_at=datetime.now(UTC) - timedelta(seconds=10))
    result = _start(plugin_env, run_hook, stub_server.url, source=source)
    assert stub_server.requests == []
    assert "3 tool guardrails active" in result.specific["additionalContext"]
    assert "(cached 10s)" in result.specific["additionalContext"]


def test_cache_older_than_60s_is_refetched(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD)
    plugin_env.write_cache(STANDARD, fetched_at=datetime.now(UTC) - timedelta(seconds=90))
    _start(plugin_env, run_hook, stub_server.url, source="resume")
    assert len(stub_server.requests) == 1


def test_skipped_items_are_named(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    bad = item(8, "bad", "Bash", match="(unclosed")
    stub_server.set_guardrails(STANDARD + [bad])
    result = _start(plugin_env, run_hook, stub_server.url)
    assert "3 tool guardrails active" in result.specific["additionalContext"]
    assert (
        f"1 skipped ({memory_id(8)[:8]}) — pattern unsupported by this client"
        in result.json["systemMessage"]
    )


def test_session_start_without_guardrails_prints_no_context(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails([])
    result = _start(plugin_env, run_hook, stub_server.url)
    assert result.stdout == ""
    assert plugin_env.cache_path.exists()


# ---------------------------------------------------------------------------
# --refresh
# ---------------------------------------------------------------------------


def test_refresh_writes_cache_and_prints_nothing(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD)
    body = payload(
        "PostToolUse", tool_name="mcp__kagura-memory__remember", tool_input={}, tool_response={}
    )
    result = run_hook(body, env=_env(plugin_env, stub_server.url), refresh=True)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    assert len(stub_server.requests) == 1
    assert json.loads(plugin_env.cache_path.read_text())["version"] == "3f9c1a7b2d4e6f80"
    assert not (plugin_env.state_dir / "refresh.lock").exists()


def test_refresh_skips_a_cache_younger_than_5s(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD)
    plugin_env.write_cache(STANDARD, fetched_at=datetime.now(UTC))
    body = payload(
        "PostToolUse", tool_name="mcp__kagura-memory__remember", tool_input={}, tool_response={}
    )
    run_hook(body, env=_env(plugin_env, stub_server.url), refresh=True)
    assert stub_server.requests == []


def test_concurrent_refreshes_make_one_request(
    plugin_env: PluginEnv, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD)
    stub_server.delay = 0.5  # keep the first fetch in flight while the burst arrives
    command = command_for("PostToolUse", refresh=True)
    env = _env(plugin_env, stub_server.url)
    plugin_env.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = json.dumps(
        payload(
            "PostToolUse", tool_name="mcp__kagura-memory__remember", tool_input={}, tool_response={}
        )
    ).encode()

    def run(_: int) -> int:
        return subprocess.run(
            ["sh", "-c", command],
            input=body,
            capture_output=True,
            cwd=plugin_env.project_dir,
            env=env,
            check=False,
        ).returncode

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(run, range(6)))
    assert codes == [0] * 6
    # refresh.lock coalesces the burst: the holder fetches, the others exit without a request.
    assert len(stub_server.requests) == 1
    assert json.loads(plugin_env.cache_path.read_text())["version"] == "3f9c1a7b2d4e6f80"


def test_stale_refresh_lock_is_replaced(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails(STANDARD)
    plugin_env.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = plugin_env.state_dir / "refresh.lock"
    lock.write_text("")
    body = payload(
        "PostToolUse", tool_name="mcp__kagura-memory__remember", tool_input={}, tool_response={}
    )
    run_hook(body, env=_env(plugin_env, stub_server.url), refresh=True)
    assert stub_server.requests == [], "a live lock is honoured"
    old = time.time() - 60
    os.utime(lock, (old, old))
    run_hook(body, env=_env(plugin_env, stub_server.url), refresh=True)
    assert len(stub_server.requests) == 1
    assert not lock.exists()


# ---------------------------------------------------------------------------
# Canary: the key and the host never leave the hook
# ---------------------------------------------------------------------------


def _all_bytes_under(root: Any) -> bytes:
    chunks = []
    for path in root.rglob("*"):
        if path.is_file():
            chunks.append(path.read_bytes())
    return b"\n".join(chunks)


def test_canary_never_leaks_on_any_error_path(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer, tmp_path: Any
) -> None:
    host = "127.0.0.1"
    stub_server.status = 500
    stub_server.raw_body = json.dumps({"echo": "Bearer " + CANARY_KEY}).encode()
    outputs = []
    env = _env(plugin_env, stub_server.url)
    outputs.append(run_hook(payload("SessionStart", source="startup"), env=env))
    stub_server.raw_body = b"garbage"
    stub_server.status = 200
    outputs.append(run_hook(payload("SessionStart", source="startup"), env=env))
    closed = _env(plugin_env, f"http://{host}:{free_closed_port()}/mcp")
    outputs.append(run_hook(payload("SessionStart", source="startup"), env=closed))
    plugin_env.guardrails_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    plugin_env.cache_path.write_text("{malformed", encoding="utf-8")
    os.chmod(plugin_env.cache_path, 0o600)
    outputs.append(run_hook(bash_pre("ps"), env=env))
    plugin_env.write_cache([item(2, "s", "Bash", match="ps", action="block")])
    if os.geteuid() != 0:
        plugin_env.state_dir.mkdir(mode=0o500, exist_ok=True)
        outputs.append(run_hook(bash_pre("ps"), env=env))
        os.chmod(plugin_env.state_dir, 0o700)
    outputs.append(
        run_hook(
            json.dumps(bash_pre("ps " + "x" * (4 * 1024 * 1024 + 1))), env=env, event="PreToolUse"
        )
    )
    for result in outputs:
        assert result.returncode == 0
        assert CANARY_KEY not in result.stdout and CANARY_KEY not in result.stderr
        assert f"{host}:{stub_server.port}" not in result.stdout + result.stderr
    everything = _all_bytes_under(plugin_env.root)
    assert CANARY_KEY.encode() not in everything
    assert stub_server.url.encode() not in everything
