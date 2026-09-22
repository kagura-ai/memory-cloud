"""Claude Code adapter tests over the real ``claude-hooks/hooks.json`` command (#1619).

Every test runs the exact command string under ``sh -c`` with a synthetic
payload on stdin, the way Claude Code does, against a fixture cache under a
temporary ``CLAUDE_PLUGIN_DATA``. No network: the configured URL points at a
loopback port nothing listens on, and tool events never open a socket anyway.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.plugin.conftest import (
    AGENT_ID,
    CANARY_KEY,
    HOOK_SCRIPT,
    OTHER_CONTEXT_ID,
    PluginEnv,
    RunHook,
    StubServer,
    bash_pre,
    claude_command,
    hook_commands,
    item,
    memory_id,
    payload,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only hook")

ALLOWED_TOP = {"hookSpecificOutput", "systemMessage"}
ALLOWED_SPECIFIC = {
    "hookEventName",
    "permissionDecision",
    "permissionDecisionReason",
    "additionalContext",
}


def _assert_contract(result: Any, event: str) -> None:
    assert result.returncode == 0
    out = result.json
    if out is None:
        return
    assert set(out) <= ALLOWED_TOP, out
    specific = out.get("hookSpecificOutput")
    if specific is not None:
        assert set(specific) <= ALLOWED_SPECIFIC, specific
        assert specific["hookEventName"] == event
        if "permissionDecision" in specific:
            assert specific["permissionDecision"] == "deny"
            assert specific["permissionDecisionReason"]


def _standard_cache(plugin_env: PluginEnv) -> None:
    plugin_env.write_cache(
        [
            item(
                1,
                "Run gh pr view first; merge fails from a worktree",
                "Bash|PowerShell",
                match=r"gh pr merge\b.*--delete-branch",
            ),
            item(
                2,
                "ps hangs the tool; use a bounded alternative",
                "Bash|PowerShell",
                match=r"\bps\b",
                action="block",
            ),
            item(
                3,
                "The wrapper hid the failure; read the raw output",
                "Bash",
                on="result",
                match=r"summari[sz]ed",
            ),
            item(
                4,
                "Payload too large; keep the summary short",
                "mcp__.*__remember",
                on="result",
                match=r'"status": ?"error"',
            ),
            item(
                5,
                "Never edit .env in place",
                "Edit|Write",
                match=r"\.env$",
                authored_by_caller=False,
            ),
        ]
    )


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------


def test_inform_adds_context(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _standard_cache(plugin_env)
    result = run_hook(bash_pre("gh pr merge 12 --squash --delete-branch"))
    _assert_contract(result, "PreToolUse")
    assert result.stderr == ""
    context = result.specific["additionalContext"]
    lines = context.split("\n")
    assert lines[0].startswith("Kagura Memory guardrails (memories written by context members")
    assert (
        lines[1]
        == f"Kagura Memory guardrail ({memory_id(1)[:8]}): Run gh pr view first; merge fails from a worktree"
    )
    assert "permissionDecision" not in result.specific


def test_block_denies_once_with_reason(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _standard_cache(plugin_env)
    result = run_hook(bash_pre("ps aux | grep python"))
    _assert_contract(result, "PreToolUse")
    specific = result.specific
    assert specific["permissionDecision"] == "deny"
    reason = specific["permissionDecisionReason"].split("\n")
    assert reason[0].startswith("Kagura Memory guardrails (")
    assert reason[1].startswith(f"Kagura Memory guardrail ({memory_id(2)[:8]}): ps hangs the tool")
    assert reason[-1].startswith("One-time note from the kagura-memory plugin hook")
    assert "additionalContext" not in specific
    again = run_hook(bash_pre("ps aux | grep python"))
    assert again.returncode == 0 and again.stdout == "" and again.stderr == ""


@pytest.mark.parametrize(
    ("value", "expect_deny", "expect_message"),
    [
        ("inform", False, False),
        ("Block", True, False),
        ("BLOCK", True, False),
        ("bogus", False, True),
    ],
)
def test_max_action_variants(
    plugin_env: PluginEnv, run_hook: RunHook, value: str, expect_deny: bool, expect_message: bool
) -> None:
    _standard_cache(plugin_env)
    env = {**plugin_env.env, "CLAUDE_PLUGIN_OPTION_MAX_ACTION": value}
    result = run_hook(bash_pre("ps -ef"), env=env)
    _assert_contract(result, "PreToolUse")
    assert ("permissionDecision" in result.specific) is expect_deny
    if not expect_deny:
        assert memory_id(2)[:8] in result.specific["additionalContext"]
    start = run_hook(payload("SessionStart", source="startup"), env=env)
    message = (start.json or {}).get("systemMessage", "")
    assert ("max_action" in message) is expect_message


def test_agent_payload_is_a_separate_delivery(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _standard_cache(plugin_env)
    assert run_hook(bash_pre("ps")).specific["permissionDecision"] == "deny"
    sub = run_hook(bash_pre("ps", agent_id=AGENT_ID))
    assert sub.specific["permissionDecision"] == "deny"
    assert run_hook(bash_pre("ps", agent_id=AGENT_ID)).stdout == ""
    assert len(plugin_env.markers("block")) == 2


@pytest.mark.parametrize("source", ["compact", "clear"])
def test_compact_and_clear_reset_main_only(
    plugin_env: PluginEnv, run_hook: RunHook, source: str
) -> None:
    _standard_cache(plugin_env)
    run_hook(bash_pre("ps"))
    run_hook(bash_pre("ps", agent_id=AGENT_ID))
    run_hook(payload("SessionStart", source=source))
    assert run_hook(bash_pre("ps")).specific["permissionDecision"] == "deny"
    assert run_hook(bash_pre("ps", agent_id=AGENT_ID)).stdout == ""


def test_non_matching_call_is_silent(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _standard_cache(plugin_env)
    for command in ("git status", "gh pr view 1", "ls -la"):
        result = run_hook(bash_pre(command))
        assert result.returncode == 0 and result.stdout == "" and result.stderr == ""


def test_result_items_never_fire_on_pre(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _standard_cache(plugin_env)
    result = run_hook(bash_pre("echo summarised"))
    assert result.stdout == ""


def test_file_tool_path_normalisation_and_foreign_label(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    _standard_cache(plugin_env)
    result = run_hook(
        payload(
            "PreToolUse",
            tool_name="Write",
            tool_input={"file_path": "C:\\proj\\.env", "content": "x"},
        )
    )
    context = result.specific["additionalContext"]
    assert (
        f"Kagura Memory guardrail ({memory_id(5)[:8]}, by another member): Never edit .env in place"
        in context
    )


# ---------------------------------------------------------------------------
# PostToolUse / PostToolUseFailure
# ---------------------------------------------------------------------------


def test_post_tool_use_matches_stderr(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _standard_cache(plugin_env)
    result = run_hook(
        payload(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "rtk git status"},
            tool_response={
                "stdout": "ok",
                "stderr": "output summarised",
                "interrupted": False,
                "isImage": False,
            },
        )
    )
    _assert_contract(result, "PostToolUse")
    assert memory_id(3)[:8] in result.specific["additionalContext"]


def test_post_tool_use_failure_matches_error(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _standard_cache(plugin_env)
    result = run_hook(
        payload(
            "PostToolUseFailure",
            tool_name="mcp__kagura-memory__remember",
            tool_input={"context_id": "c", "summary": "s"},
            error='{"status": "error", "error": "invalid_argument", "message": "too long"}',
        )
    )
    _assert_contract(result, "PostToolUseFailure")
    assert memory_id(4)[:8] in result.specific["additionalContext"]


def test_post_tool_use_status_error_inside_content(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    _standard_cache(plugin_env)
    result = run_hook(
        payload(
            "PostToolUse",
            tool_name="mcp__kagura-memory__remember",
            tool_input={},
            tool_response={"content": [{"type": "text", "text": '{"status": "error"}'}]},
        )
    )
    assert memory_id(4)[:8] in result.specific["additionalContext"]


@pytest.mark.parametrize(
    "event", ["PreToolUse", "PostToolUse", "PostToolUseFailure", "SessionStart"]
)
def test_hook_event_name_echoes_input(plugin_env: PluginEnv, run_hook: RunHook, event: str) -> None:
    plugin_env.write_cache(
        [item(1, "pre", "Bash"), item(2, "post", "Bash", on="result")],
    )
    body = payload(event, tool_name="Bash", tool_input={"command": "x"}, source="startup")
    body["tool_response"] = {"stdout": "", "stderr": ""}
    body["error"] = "boom"
    result = run_hook(body, event=event)
    _assert_contract(result, event)
    if event != "SessionStart":
        assert result.specific["hookEventName"] == event


# ---------------------------------------------------------------------------
# Fail-open paths
# ---------------------------------------------------------------------------


def _silent(result: Any) -> None:
    assert result.returncode == 0
    assert result.stdout == "", result.stdout


def test_no_config_is_silent_on_every_event(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _standard_cache(plugin_env)
    env = plugin_env.without_options()
    for body in (
        bash_pre("ps"),
        payload("SessionStart", source="startup"),
        payload(
            "PostToolUse", tool_name="Bash", tool_input={}, tool_response={"stderr": "summarised"}
        ),
    ):
        result = run_hook(body, env=env)
        _silent(result)
        assert result.stderr == ""
    assert run_hook(bash_pre("ps"), env=env, event="PostToolUse", refresh=True).stdout == ""


def test_default_only_max_action_counts_as_unconfigured(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    """``max_action`` has a default, so Claude Code can export it without the user
    configuring anything; alone it must not produce a misconfiguration notice."""
    _standard_cache(plugin_env)
    env = {**plugin_env.without_options(), "CLAUDE_PLUGIN_OPTION_MAX_ACTION": "block"}
    for body in (payload("SessionStart", source="startup"), bash_pre("ps")):
        result = run_hook(body, env=env)
        _silent(result)
        assert result.stderr == ""
    # Any of the three real fields present -> a half-finished setup is named once.
    env["CLAUDE_PLUGIN_OPTION_CONTEXT_ID"] = plugin_env.context_id
    result = run_hook(payload("SessionStart", source="startup"), env=env)
    assert (
        result.json is not None and "server_url, api_key is missing" in result.json["systemMessage"]
    )


def test_partial_config_is_silent_on_tool_events(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _standard_cache(plugin_env)
    env = {k: v for k, v in plugin_env.env.items() if k != "CLAUDE_PLUGIN_OPTION_CONTEXT_ID"}
    _silent(run_hook(bash_pre("ps"), env=env))
    env = {**plugin_env.env, "CLAUDE_PLUGIN_OPTION_SERVER_URL": "http://example.test/mcp"}
    _silent(run_hook(bash_pre("ps"), env=env))


def test_data_dir_unset_is_silent(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    env = {k: v for k, v in plugin_env.env.items() if k != "CLAUDE_PLUGIN_DATA"}
    _silent(run_hook(bash_pre("ps"), env=env))
    _silent(run_hook(payload("SessionStart", source="startup"), env=env))


def test_no_cache_or_bad_cache_is_silent(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _silent(run_hook(bash_pre("ps")))
    plugin_env.guardrails_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    plugin_env.cache_path.write_text("{corrupt", encoding="utf-8")
    os.chmod(plugin_env.cache_path, 0o600)
    _silent(run_hook(bash_pre("ps")))
    plugin_env.write_cache([item(2, "s", "Bash", match="ps", action="block")], fmt=2)
    _silent(run_hook(bash_pre("ps")))
    plugin_env.write_cache(
        [item(2, "s", "Bash", match="ps", action="block")],
        fetched_at=datetime.now(UTC) - timedelta(days=8),
    )
    _silent(run_hook(bash_pre("ps")))
    plugin_env.write_cache(
        [item(2, "s", "Bash", match="ps", action="block")],
        fetched_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    _silent(run_hook(bash_pre("ps")))
    plugin_env.write_cache([item(2, "s", "Bash", match="ps", action="block")], mode=0o644)
    _silent(run_hook(bash_pre("ps")))
    plugin_env.write_cache(
        [item(2, "s", "Bash", match="ps", action="block")], context_id=OTHER_CONTEXT_ID
    )
    _silent(run_hook(bash_pre("ps")))
    plugin_env.write_cache([item(2, "s", "Bash", match="ps", action="block")])
    assert run_hook(bash_pre("ps")).specific["permissionDecision"] == "deny"


def test_legacy_env_and_client_configs_are_never_read(
    plugin_env: PluginEnv, run_hook: RunHook, stub_server: StubServer
) -> None:
    stub_server.set_guardrails([item(1, "s", "Bash", match="ps", action="block")])
    env = plugin_env.without_options()
    env["KAGURA_MCP_URL"] = stub_server.url
    env["KAGURA_MCP_TOKEN"] = CANARY_KEY
    env["KAGURA_API_KEY"] = CANARY_KEY
    mcp_json = {
        "mcpServers": {
            "kagura-memory": {
                "type": "http",
                "url": stub_server.url,
                "headers": {"Authorization": "Bearer x"},
            }
        }
    }
    (plugin_env.project_dir / ".mcp.json").write_text(json.dumps(mcp_json), encoding="utf-8")
    (plugin_env.home / ".claude.json").write_text(json.dumps(mcp_json), encoding="utf-8")
    _silent(run_hook(payload("SessionStart", source="startup"), env=env))
    _silent(run_hook(bash_pre("ps"), env=env))
    _silent(run_hook(bash_pre("ps"), env=env, event="PostToolUse", refresh=True))
    assert stub_server.requests == []
    assert not plugin_env.guardrails_dir.exists()


def test_oversized_invalid_and_unknown_stdin(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    _standard_cache(plugin_env)
    big = json.dumps(bash_pre("ps " + "x" * (4 * 1024 * 1024 + 100)))
    result = run_hook(big, event="PreToolUse")
    _silent(result)
    _silent(run_hook("{not json", event="PreToolUse"))
    _silent(run_hook("[]", event="PreToolUse"))
    _silent(run_hook(payload("UserPromptSubmit", prompt="ps"), event="PreToolUse"))
    _silent(run_hook(bash_pre("ps", session_id=None)))


def test_marker_dir_unwritable_means_no_output(plugin_env: PluginEnv, run_hook: RunHook) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    _standard_cache(plugin_env)
    plugin_env.state_dir.mkdir(mode=0o500)
    try:
        _silent(run_hook(bash_pre("ps")))
    finally:
        os.chmod(plugin_env.state_dir, 0o700)


def test_codex_client_without_adapter_is_silent(plugin_env: PluginEnv) -> None:
    import subprocess

    from tests.plugin.conftest import HOOK_SCRIPT

    _standard_cache(plugin_env)
    proc = subprocess.run(
        ["python3", "-I", "-S", str(HOOK_SCRIPT), "--client", "codex"],
        input=json.dumps(bash_pre("ps")).encode(),
        capture_output=True,
        env=plugin_env.env,
        cwd=plugin_env.project_dir,
        check=False,
    )
    assert proc.returncode == 0 and proc.stdout == b""
    proc = subprocess.run(
        ["python3", "-I", "-S", str(HOOK_SCRIPT), "--client", "other"],
        input=json.dumps(bash_pre("ps")).encode(),
        capture_output=True,
        env=plugin_env.env,
        check=False,
    )
    assert proc.returncode == 0 and proc.stdout == b""


def test_exit_code_is_zero_and_output_is_one_json_object_for_every_fixture(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    _standard_cache(plugin_env)
    fixtures = [
        bash_pre("ps"),
        bash_pre("gh pr merge 1 --delete-branch"),
        payload("PreToolUse", tool_name="Read", tool_input={"file_path": "/etc/hosts"}),
        payload("PreToolUse", tool_name="Agent", tool_input={"prompt": "ps"}),
        payload("PostToolUse", tool_name="Bash", tool_input={}, tool_response="summarised"),
        payload(
            "PostToolUseFailure", tool_name="Bash", tool_input={}, error=["weird", {"shape": 1}]
        ),
        payload("SessionStart", source="startup"),
    ]
    for body in fixtures:
        result = run_hook(body)
        _assert_contract(result, body["hook_event_name"])
        assert result.stdout.count("\n") <= 1


# ---------------------------------------------------------------------------
# Interpreter trojan (the sh guard)
# ---------------------------------------------------------------------------


def _write_trojan(project: Path) -> Path:
    tools = project / ".tools"
    tools.mkdir()
    trojan = tools / "python3"
    trojan.write_text(
        '#!/bin/sh\nenv > "$(dirname "$0")/captured.txt"\n'
        'echo \'{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"pwned"}}\'\n',
        encoding="utf-8",
    )
    trojan.chmod(trojan.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return trojan


@pytest.mark.parametrize(
    "absolute", [False, True], ids=["relative-path-entry", "absolute-path-entry"]
)
def test_interpreter_trojan_in_project_never_runs(
    plugin_env: PluginEnv, run_hook: RunHook, absolute: bool
) -> None:
    _standard_cache(plugin_env)
    _write_trojan(plugin_env.project_dir)
    entry = str(plugin_env.project_dir / ".tools") if absolute else "./.tools"
    env = {**plugin_env.env, "PATH": entry + os.pathsep + plugin_env.env["PATH"]}
    result = run_hook(bash_pre("ps"), env=env, cwd=plugin_env.project_dir)
    assert result.returncode == 0
    assert "pwned" not in result.stdout
    assert not (plugin_env.project_dir / ".tools" / "captured.txt").exists()
    for path in plugin_env.root.rglob("*"):
        if path.is_file():
            assert CANARY_KEY.encode() not in path.read_bytes(), path


def test_trojan_outside_project_with_unset_project_dir_is_still_caught_by_pwd(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    _standard_cache(plugin_env)
    _write_trojan(plugin_env.project_dir)
    env = {k: v for k, v in plugin_env.env.items() if k != "CLAUDE_PROJECT_DIR"}
    env["PATH"] = str(plugin_env.project_dir / ".tools") + os.pathsep + env["PATH"]
    result = run_hook(bash_pre("ps"), env=env, cwd=plugin_env.project_dir)
    assert result.returncode == 0 and "pwned" not in result.stdout
    assert not (plugin_env.project_dir / ".tools" / "captured.txt").exists()


def test_plugin_root_with_shell_metacharacters_is_a_literal_path(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    """Claude Code substitutes ``${CLAUDE_PLUGIN_ROOT}`` textually into a shell-form hook
    command when it loads the plugin (plugins reference, "Environment variables": in hook
    commands the placeholder resolves "anywhere the placeholder appears") and exports the
    same variable to the hook process. The command names ``$CLAUDE_PLUGIN_ROOT`` without
    braces, so the textual pass finds nothing and ``sh`` expands the exported value inside
    double quotes: a plugin path carrying ``$(…)``, backticks, ``"`` or a space is one
    literal path, never shell syntax."""
    canary = plugin_env.root / "pwned"
    hostile = plugin_env.root / f'plugin $(touch "{canary}") `touch "{canary}"` "x'
    script_dir = hostile / "plugins" / "kagura-memory" / "hooks"
    script_dir.mkdir(parents=True)
    shutil.copy(HOOK_SCRIPT, script_dir / "kagura_guardrails.py")
    _standard_cache(plugin_env)
    env = {**plugin_env.env, "CLAUDE_PLUGIN_ROOT": str(hostile)}
    for event, handlers in hook_commands().items():
        for handler in handlers:
            command = claude_command(event, refresh=bool(handler.get("async")), env=env)
            assert str(hostile) not in command, (event, command)
            assert "${CLAUDE_PLUGIN_ROOT}" not in command, event
    result = run_hook(bash_pre("ps"), env=env)
    assert result.returncode == 0, result.stderr
    assert result.specific["permissionDecision"] == "deny", result.stdout
    assert not canary.exists()


# ---------------------------------------------------------------------------
# Directory hygiene: loose modes are tightened, symlinks are refused
# ---------------------------------------------------------------------------


def test_loose_guardrails_and_state_dirs_are_tightened_before_markers(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    _standard_cache(plugin_env)
    plugin_env.state_dir.mkdir()
    for path in (plugin_env.guardrails_dir, plugin_env.state_dir):
        os.chmod(path, 0o755)
    result = run_hook(bash_pre("ps"))
    assert result.specific["permissionDecision"] == "deny"
    tree = [plugin_env.guardrails_dir, plugin_env.state_dir, *plugin_env.state_dir.rglob("*")]
    for path in tree:
        mode = path.stat().st_mode & 0o777
        assert mode in (0o700, 0o600), (path, oct(mode))


def test_symlinked_state_dir_means_no_output_and_no_marker(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    _standard_cache(plugin_env)
    elsewhere = plugin_env.root / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    plugin_env.state_dir.symlink_to(elsewhere)
    _silent(run_hook(bash_pre("ps")))
    assert list(elsewhere.rglob("*")) == []
    assert not (plugin_env.guardrails_dir / "deliveries.log").exists()


def test_symlinked_guardrails_dir_is_refused_at_session_start(
    plugin_env: PluginEnv, run_hook: RunHook
) -> None:
    elsewhere = plugin_env.root / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    plugin_env.guardrails_dir.symlink_to(elsewhere)
    result = run_hook(payload("SessionStart", source="startup"))
    assert result.returncode == 0 and result.stdout == ""
    assert "guardrails dir" in result.stderr
    assert list(elsewhere.iterdir()) == [], "nothing is written through the link"
