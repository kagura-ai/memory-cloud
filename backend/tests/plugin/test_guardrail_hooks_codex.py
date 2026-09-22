"""Codex adapter tests over the real ``plugins/kagura-memory/hooks/hooks.json`` command (#1620).

Every subprocess test runs the exact command string under ``sh -c`` the way Codex
does it: ``hooks/src/engine/discovery.rs`` replaces ``${KEY}`` textually for every
plugin variable before ``$SHELL -lc`` runs and ``engine/command_runner.rs`` exports
the same variables to the process — the command names ``$PLUGIN_ROOT`` without
braces, so only the exported variable carries the path. Synthetic Codex payload on
stdin, a fixture ``PLUGIN_DATA`` and a fixture ``CODEX_HOME``. Network only through
the loopback stub; nothing is written outside ``tmp_path``.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.plugin.conftest import (
    CANARY_KEY,
    CLAUDE_HOOKS_JSON,
    CONTEXT_ID,
    HOOK_SCRIPT,
    REPO_ROOT,
    HookResult,
    PluginEnv,
    StubServer,
    free_closed_port,
    item,
    memory_id,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only hook")

CODEX_PLUGIN_ROOT = REPO_ROOT / "plugins" / "kagura-memory"
CODEX_HOOKS_JSON = CODEX_PLUGIN_ROOT / "hooks" / "hooks.json"
CODEX_ADAPTER = CODEX_PLUGIN_ROOT / "hooks" / "_codex_adapter.py"
FIXTURES = Path(__file__).parent / "fixtures" / "codex"

SESSION_ID = "019a2b3c-4d5e-7f80-9a1b-2c3d4e5f6071"
AGENT_ID = "019a2b3c-4d5e-7f80-9a1b-2c3d4e5f6099"
BEARER_VAR = "KAGURA_TEST_KEY"

ALLOWED_TOP = {"hookSpecificOutput", "systemMessage"}
ALLOWED_SPECIFIC = {
    "hookEventName",
    "permissionDecision",
    "permissionDecisionReason",
    "additionalContext",
}
# Assembled at runtime so this file never contains the tokens it forbids.
FORBIDDEN_OUTPUT_TOKENS = [
    '"' + "allow" + '"',
    '"' + "ask" + '"',
    '"' + "defer" + '"',
    "updated" + "Input",
    "updated" + "MCPToolOutput",
    '"' + "continue" + '"',
    "stop" + "Reason",
    "suppress" + "Output",
    '"' + "decision" + '"',
    '"' + "reason" + '"',
]
CODEX_ENV_ALLOWLIST = {"PLUGIN_ROOT", "PLUGIN_DATA", "CLAUDE_PLUGIN_DATA", "CODEX_HOME", "HOME"}
# The variables Codex both substitutes textually (``${KEY}``) and exports (discovery.rs:262-270).
CODEX_PLUGIN_VARIABLES = ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "PLUGIN_DATA", "CLAUDE_PLUGIN_DATA")


# ---------------------------------------------------------------------------
# Codex environment, payloads and the real command
# ---------------------------------------------------------------------------


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def codex_payload(
    event: str,
    *,
    tool_name: str | None = None,
    tool_input: Any = None,
    tool_response: Any = None,
    session_id: str | None = SESSION_ID,
    agent_id: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """A payload with every field Codex serialises for the event (``schema.rs``)."""
    body: dict[str, Any] = {
        "transcript_path": None,
        "cwd": "/home/user/project",
        "hook_event_name": event,
        "model": "gpt-5-codex",
        "permission_mode": "default",
    }
    if session_id is not None:
        body["session_id"] = session_id
    if event == "SessionStart":
        body["source"] = source or "startup"
        return body
    body["turn_id"] = "019a2b3c-4d5e-7f80-9a1b-2c3d4e5f6072"
    if agent_id is not None:
        body["agent_id"] = agent_id
        body["agent_type"] = "explorer"
    if tool_name is not None:
        body["tool_name"] = tool_name
        body["tool_input"] = tool_input if tool_input is not None else {}
        body["tool_use_id"] = "call_01"
    if event == "PostToolUse":
        body["tool_response"] = tool_response
    return body


def bash(command: str, **kw: Any) -> dict[str, Any]:
    return codex_payload("PreToolUse", tool_name="Bash", tool_input={"command": command}, **kw)


def session_start(source: str = "startup") -> dict[str, Any]:
    return codex_payload("SessionStart", source=source)


def codex_handlers() -> list[tuple[str, str | None, dict[str, Any]]]:
    data = json.loads(CODEX_HOOKS_JSON.read_text(encoding="utf-8"))
    return [
        (event, group.get("matcher"), handler)
        for event, groups in data["hooks"].items()
        for group in groups
        for handler in group["hooks"]
    ]


def codex_command(event: str, *, refresh: bool = False, env: dict[str, str] | None = None) -> str:
    """The handler command as Codex hands it to ``$SHELL -lc``: ``${KEY}`` replaced
    textually for every plugin variable in ``env`` (``discovery.rs``). The command
    names ``$PLUGIN_ROOT`` without braces, so this pass changes nothing and the shell
    expands the exported variable instead."""
    wanted = [h for e, _m, h in codex_handlers() if e == event and bool(h.get("async")) is refresh]
    assert len(wanted) == 1, (event, refresh)
    command = wanted[0]["command"]
    for key in CODEX_PLUGIN_VARIABLES:
        value = (env or {}).get(key)
        if value is not None:
            command = command.replace("${" + key + "}", value)
    return command


def toml_table(
    url: str,
    *,
    name: str = "kagura-memory",
    bearer_var: str | None = BEARER_VAR,
    extra: str = "",
) -> str:
    lines = [f"[mcp_servers.{name}]", f'url = "{url}"']
    if bearer_var is not None:
        lines.append(f'bearer_token_env_var = "{bearer_var}"')
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n"


class CodexEnv(PluginEnv):
    """A Codex plugin environment: ``PLUGIN_DATA``, ``CODEX_HOME`` and the user config."""

    codex_home: Path

    def write_config_json(self, **fields: Any) -> Path:
        body = {"context_id": self.context_id}
        body.update(fields)
        path = self.data_dir / "config.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def write_config_toml(self, text: str) -> Path:
        self.codex_home.mkdir(mode=0o700, exist_ok=True)
        path = self.codex_home / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def without(self, *names: str) -> dict[str, str]:
        return {k: v for k, v in self.env.items() if k not in names}


@pytest.fixture
def codex_env(tmp_path: Path) -> CodexEnv:
    data_dir = tmp_path / "plugins-data" / "kagura-memory-kagura-memory-cloud"
    project_dir = tmp_path / "project"
    home = tmp_path / "home"
    codex_home = home / ".codex"
    for path in (data_dir, project_dir, home, codex_home):
        path.mkdir(mode=0o700, parents=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PLUGIN_ROOT": str(CODEX_PLUGIN_ROOT),
        "CLAUDE_PLUGIN_ROOT": str(CODEX_PLUGIN_ROOT),
        "PLUGIN_DATA": str(data_dir),
        "CLAUDE_PLUGIN_DATA": str(data_dir),
        "CODEX_HOME": str(codex_home),
        BEARER_VAR: CANARY_KEY,
    }
    out = CodexEnv(root=tmp_path, data_dir=data_dir, project_dir=project_dir, home=home, env=env)
    out.codex_home = codex_home
    out.write_config_json()
    out.write_config_toml(toml_table(f"http://127.0.0.1:{free_closed_port()}/mcp/w/x"))
    return out


RunCodex = Callable[..., HookResult]


@pytest.fixture
def run_codex(codex_env: CodexEnv) -> RunCodex:
    def _run(
        body: dict[str, Any] | str | bytes,
        *,
        env: dict[str, str] | None = None,
        event: str | None = None,
        refresh: bool = False,
        cwd: Path | None = None,
        timeout: float = 30,
    ) -> HookResult:
        if isinstance(body, dict):
            event = event or body["hook_event_name"]
            data = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
        assert event is not None
        run_env = env if env is not None else codex_env.env
        proc = subprocess.run(
            ["sh", "-c", codex_command(event, refresh=refresh, env=run_env)],
            input=data,
            capture_output=True,
            cwd=cwd or codex_env.project_dir,
            env=run_env,
            check=False,
            timeout=timeout,
        )
        return HookResult(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    return _run


def call_codex_main(
    hook_module: ModuleType,
    body: dict[str, Any] | str,
    env: dict[str, str],
    *,
    refresh: bool = False,
) -> HookResult:
    """In-process ``main()`` with ``--client codex`` (adapter loaded by path)."""
    text = json.dumps(body) if isinstance(body, dict) else body
    stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")
    stdout = io.StringIO()
    argv = ["kagura_guardrails.py", "--client", "codex"] + (["--refresh"] if refresh else [])
    code = hook_module.main(argv, stdin, stdout, env)
    return HookResult(code, stdout.getvalue(), "")


def _assert_contract(result: HookResult, event: str) -> None:
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
            assert specific["permissionDecisionReason"].strip()
    for token in FORBIDDEN_OUTPUT_TOKENS:
        assert token not in result.stdout, token


def _silent(result: HookResult) -> None:
    assert result.returncode == 0
    assert result.stdout == "", result.stdout


def _standard_cache(codex_env: CodexEnv) -> None:
    codex_env.write_cache(
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
                "Bash",
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
            item(5, "Never edit .env in place", "Edit|Write", match=r"\.env$"),
            item(
                6,
                "Python files need a test",
                "Edit|Write",
                match=r"\.py$",
                authored_by_caller=False,
            ),
            item(
                7,
                "Remember with importance 1 only for principles",
                "mcp__.*__remember",
                match=r'"importance":1[,}]',
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Fixtures carry the documented Codex fields
# ---------------------------------------------------------------------------


COMMON_FIELDS = {
    "session_id",
    "transcript_path",
    "cwd",
    "hook_event_name",
    "model",
    "permission_mode",
}


@pytest.mark.parametrize("name", sorted(p.stem for p in FIXTURES.glob("*.json")))
def test_fixture_carries_the_documented_codex_fields(name: str) -> None:
    body = fixture(name)
    assert COMMON_FIELDS <= set(body), name
    if body["hook_event_name"] == "SessionStart":
        assert body["source"] in {"startup", "resume", "clear", "compact", "fork"}
        assert "agent_id" not in body
        return
    assert {"turn_id", "tool_name", "tool_input", "tool_use_id"} <= set(body), name
    if body["hook_event_name"] == "PostToolUse":
        assert "tool_response" in body
    if "agent_id" in body:
        assert "agent_type" in body


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------


def test_bash_inform_adds_context(codex_env: CodexEnv, run_codex: RunCodex) -> None:
    _standard_cache(codex_env)
    result = run_codex(bash("gh pr merge 12 --squash --delete-branch"))
    _assert_contract(result, "PreToolUse")
    assert result.stderr == ""
    lines = result.specific["additionalContext"].split("\n")
    assert lines[0].startswith("Kagura Memory guardrails (memories written by context members")
    assert lines[1] == (
        f"Kagura Memory guardrail ({memory_id(1)[:8]}): "
        "Run gh pr view first; merge fails from a worktree"
    )
    assert "permissionDecision" not in result.specific


def test_bash_block_denies_once_without_the_command(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    _standard_cache(codex_env)
    result = run_codex(fixture("pre_tool_use_bash"))
    _assert_contract(result, "PreToolUse")
    specific = result.specific
    assert specific["permissionDecision"] == "deny"
    reason = specific["permissionDecisionReason"]
    assert "ps aux" not in reason and "Command:" not in reason and "Tool:" not in reason
    lines = reason.split("\n")
    assert lines[0].startswith("Kagura Memory guardrails (")
    assert lines[1].startswith(f"Kagura Memory guardrail ({memory_id(2)[:8]}): ps hangs the tool")
    assert lines[-1].startswith("One-time note from the kagura-memory plugin hook")
    assert "additionalContext" not in specific
    again = run_codex(fixture("pre_tool_use_bash"))
    assert again.returncode == 0 and again.stdout == "" and again.stderr == ""
    assert [p.name for p in codex_env.markers("block")] == [memory_id(2)]


@pytest.mark.parametrize(
    ("value", "expect_deny", "expect_message"),
    [("inform", False, False), ("Block", True, False), ("bogus", False, True)],
)
def test_max_action_variants(
    codex_env: CodexEnv, run_codex: RunCodex, value: str, expect_deny: bool, expect_message: bool
) -> None:
    _standard_cache(codex_env)
    codex_env.write_config_json(max_action=value)
    result = run_codex(bash("ps -ef"))
    _assert_contract(result, "PreToolUse")
    assert ("permissionDecision" in result.specific) is expect_deny
    if not expect_deny:
        assert memory_id(2)[:8] in result.specific["additionalContext"]
    start = run_codex(session_start())
    message = (start.json or {}).get("systemMessage", "")
    assert ("max_action" in message) is expect_message


def test_apply_patch_update_matches_edit_or_write_on_the_path(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    _standard_cache(codex_env)
    result = run_codex(fixture("pre_tool_use_apply_patch_update"))
    _assert_contract(result, "PreToolUse")
    context = result.specific["additionalContext"]
    assert (
        f"Kagura Memory guardrail ({memory_id(6)[:8]}, by another member): Python files" in context
    )
    assert "Never edit .env" not in context


def test_apply_patch_two_files_and_move_to_yield_a_subject_each(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    codex_env.write_cache(
        [
            item(1, "docs live in docs/", "Edit|Write", match=r"^docs/.*\.md$"),
            item(2, "old_name is frozen", "Write", match=r"old_name\.py$"),
            item(3, "new_name needs a test", "Edit", match=r"^src/new_name\.py$"),
        ]
    )
    result = run_codex(fixture("pre_tool_use_apply_patch_two_files"))
    context = result.specific["additionalContext"]
    for n in (1, 2, 3):
        assert memory_id(n)[:8] in context


def test_apply_patch_backslash_paths_are_normalised(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    codex_env.write_cache([item(1, "pkg is generated", "Edit|Write", match=r"src/pkg/a\.py")])
    result = run_codex(fixture("pre_tool_use_apply_patch_backslash"))
    assert memory_id(1)[:8] in result.specific["additionalContext"]


def test_apply_patch_without_headers_and_non_string_bash_command_are_silent(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    codex_env.write_cache(
        [item(1, "s", "Edit|Write", match=r"."), item(2, "t", "Bash", match=r".")]
    )
    _silent(
        run_codex(
            codex_payload(
                "PreToolUse",
                tool_name="apply_patch",
                tool_input={"command": "*** Begin Patch\n*** End Patch"},
            )
        )
    )
    _silent(
        run_codex(codex_payload("PreToolUse", tool_name="Bash", tool_input={"command": ["ps"]}))
    )


def test_mcp_remember_matches_the_compact_json_arguments(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    _standard_cache(codex_env)
    result = run_codex(fixture("pre_tool_use_mcp_remember"))
    _assert_contract(result, "PreToolUse")
    context = result.specific["additionalContext"]
    assert "Remember with importance 1 only for principles" in context
    assert "Payload too large" not in context, "result items never fire on pre"


def test_once_per_key_with_two_agents_and_compact_reset(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    _standard_cache(codex_env)
    assert run_codex(bash("ps")).specific["permissionDecision"] == "deny"
    assert run_codex(bash("ps", agent_id=AGENT_ID)).specific["permissionDecision"] == "deny"
    second_agent = "019a2b3c-4d5e-7f80-9a1b-2c3d4e5f60aa"
    assert run_codex(bash("ps", agent_id=second_agent)).specific["permissionDecision"] == "deny"
    _silent(run_codex(bash("ps")))
    _silent(run_codex(bash("ps", agent_id=AGENT_ID)))
    assert len(codex_env.markers("block")) == 3
    for path in codex_env.state_dir.rglob("*"):
        assert SESSION_ID not in str(path) and AGENT_ID not in str(path)
    compact = run_codex(fixture("session_start_compact"))
    _assert_contract(compact, "SessionStart")  # server down -> "using cache" notice, no fetch
    assert run_codex(bash("ps")).specific["permissionDecision"] == "deny", "main re-delivers"
    _silent(run_codex(bash("ps", agent_id=AGENT_ID)))


@pytest.mark.parametrize("source", ["startup", "resume", "fork"])
def test_other_session_start_sources_do_not_reset(
    codex_env: CodexEnv, run_codex: RunCodex, source: str
) -> None:
    _standard_cache(codex_env)
    run_codex(bash("ps"))
    run_codex(session_start(source))
    _silent(run_codex(bash("ps")))


def test_non_matching_calls_are_silent(codex_env: CodexEnv, run_codex: RunCodex) -> None:
    _standard_cache(codex_env)
    for body in (
        bash("git status"),
        codex_payload("PreToolUse", tool_name="view_image", tool_input={"path": "/tmp/x.png"}),
        codex_payload("PreToolUse", tool_name="mcp__other__recall", tool_input={"q": "ps"}),
    ):
        result = run_codex(body)
        assert result.returncode == 0 and result.stdout == "" and result.stderr == ""


# ---------------------------------------------------------------------------
# PostToolUse and the two handlers on identical stdin
# ---------------------------------------------------------------------------


def test_post_tool_use_bash_object_uses_the_string_leaf_rule(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    _standard_cache(codex_env)
    result = run_codex(fixture("post_tool_use_bash_nonzero"))
    _assert_contract(result, "PostToolUse")
    assert memory_id(3)[:8] in result.specific["additionalContext"]


def test_post_tool_use_mcp_status_error_inside_content(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    _standard_cache(codex_env)
    result = run_codex(fixture("post_tool_use_mcp_error"))
    _assert_contract(result, "PostToolUse")
    assert memory_id(4)[:8] in result.specific["additionalContext"]


def test_both_post_tool_use_handlers_on_identical_remember_stdin(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer
) -> None:
    """The ``*`` handler prints context and never fetches; ``--refresh`` alone hits the server."""
    _standard_cache(codex_env)
    codex_env.write_config_toml(toml_table(stub_server.url))
    stub_server.set_guardrails([item(9, "new one", "Bash", match="x")], version="new-version")
    body = fixture("post_tool_use_mcp_error")
    plain = run_codex(body)
    assert memory_id(4)[:8] in plain.specific["additionalContext"]
    assert stub_server.requests == []
    refresh = run_codex(body, refresh=True)
    _silent(refresh)
    assert refresh.stderr == ""
    assert len(stub_server.requests) == 1
    cache = json.loads(codex_env.cache_path.read_text(encoding="utf-8"))
    assert cache["version"] == "new-version"
    assert oct(codex_env.cache_path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse", "SessionStart"])
def test_hook_event_name_echoes_input(codex_env: CodexEnv, run_codex: RunCodex, event: str) -> None:
    codex_env.write_cache([item(1, "pre", "Bash"), item(2, "post", "Bash", on="result")])
    body = codex_payload(event, tool_name="Bash", tool_input={"command": "x"}, tool_response="out")
    result = run_codex(body)
    _assert_contract(result, event)
    if event != "SessionStart":
        assert result.specific["hookEventName"] == event


def test_twenty_live_blocks_stay_under_the_codex_token_budget(
    codex_env: CodexEnv, run_codex: RunCodex, hook_module: ModuleType
) -> None:
    """The core fits every block line to the Codex token budget before taking its marker:
    the blocks that do not fit stay unmarked and deny the re-issued call, until all 20 are
    delivered; nothing is marked without being printed and nothing printed is unmarked."""
    blocks = [item(n, "b" * 480, "Bash", match="danger", action="block") for n in range(1, 21)]
    codex_env.write_cache(blocks)
    rendered_per_call: list[int] = []
    for _call in range(20):
        result = run_codex(bash("danger"))
        _assert_contract(result, "PreToolUse")
        if result.stdout == "":
            break
        reason = result.specific["permissionDecisionReason"]
        assert hook_module.approx_tokens(reason) <= 2000
        lines = reason.split("\n")
        assert lines[0] == hook_module.FRAMING_LINE and lines[-1] == hook_module.DENY_TRAILER
        body = lines[1:-1]
        assert 0 < len(body) < 20
        rendered_per_call.append(len(body))
        assert len(codex_env.markers("block")) == sum(rendered_per_call), (
            "marker count == rendered block lines"
        )
    assert len(rendered_per_call) >= 2 and rendered_per_call[0] > 1, rendered_per_call
    assert sum(rendered_per_call) == 20 and len(codex_env.markers("block")) == 20
    _silent(run_codex(bash("danger")))


def test_every_fixture_exits_zero_with_one_json_object_or_nothing(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    _standard_cache(codex_env)
    for path in sorted(FIXTURES.glob("*.json")):
        body = fixture(path.stem)
        result = run_codex(body)
        _assert_contract(result, body["hook_event_name"])
        assert result.stdout.count("\n") <= 1, path.name


# ---------------------------------------------------------------------------
# SessionStart credentials over the stub
# ---------------------------------------------------------------------------


def _one_request(stub: StubServer) -> Any:
    assert len(stub.requests) == 1, [r.path for r in stub.requests]
    return stub.requests[0]


def test_bearer_token_env_var_sends_one_post(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer
) -> None:
    stub_server.set_guardrails([item(1, "s", "Bash", match="ls")])
    codex_env.write_config_toml(toml_table(stub_server.url + "?profile=core"))
    result = run_codex(fixture("session_start_startup"))
    _assert_contract(result, "SessionStart")
    request = _one_request(stub_server)
    assert request.path == "/mcp/w/00000000-0000-0000-0000-000000000000?profile=core"
    assert request.headers["Authorization"] == "Bearer " + CANARY_KEY
    assert request.headers["User-Agent"].startswith("kagura-memory-plugin-hooks/")
    assert request.headers["User-Agent"].endswith("(codex)")
    assert request.json["params"]["name"] == "load_guardrails"
    assert request.json["params"]["arguments"] == {"context_id": CONTEXT_ID}
    context = result.specific["additionalContext"]
    assert context.startswith(
        f"Kagura Memory: 1 tool guardrails active for context {CONTEXT_ID} (fetched)"
    )
    assert "systemMessage" in (result.json or {})
    assert codex_env.cache_path.is_file()
    assert oct(codex_env.cache_path.stat().st_mode & 0o777) == "0o600"
    # the tool event now matches from the fresh cache without touching the server
    assert memory_id(1)[:8] in run_codex(bash("ls -la")).specific["additionalContext"]
    assert len(stub_server.requests) == 1


def test_user_agent_carries_the_codex_plugin_version(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer
) -> None:
    stub_server.set_guardrails([])
    codex_env.write_config_toml(toml_table(stub_server.url))
    run_codex(session_start())
    version = json.loads((CODEX_PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())[
        "version"
    ]
    assert (
        _one_request(stub_server).headers["User-Agent"]
        == f"kagura-memory-plugin-hooks/{version} (codex)"
    )


def test_env_http_headers_authorization_is_sent_verbatim(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer
) -> None:
    stub_server.set_guardrails([])
    codex_env.write_config_toml(
        toml_table(
            stub_server.url,
            bearer_var=None,
            extra='env_http_headers = { Authorization = "KAGURA_TEST_AUTH" }',
        )
    )
    env = {**codex_env.env, "KAGURA_TEST_AUTH": "Bearer " + CANARY_KEY}
    run_codex(session_start(), env=env)
    assert _one_request(stub_server).headers["Authorization"] == "Bearer " + CANARY_KEY


def test_http_headers_authorization_literal_is_sent_verbatim(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer
) -> None:
    stub_server.set_guardrails([])
    codex_env.write_config_toml(
        toml_table(
            stub_server.url,
            bearer_var=None,
            extra='http_headers = { Authorization = "Bearer literal-token" }',
        )
    )
    run_codex(session_start())
    assert _one_request(stub_server).headers["Authorization"] == "Bearer literal-token"


def test_mcp_server_setting_selects_another_table(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer, second_stub: StubServer
) -> None:
    stub_server.set_guardrails([])
    second_stub.set_guardrails([])
    codex_env.write_config_toml(
        toml_table(stub_server.url) + toml_table(second_stub.url, name="other")
    )
    codex_env.write_config_json(mcp_server="other")
    run_codex(session_start())
    assert stub_server.requests == [] and len(second_stub.requests) == 1


@pytest.mark.parametrize(
    ("extra", "bearer_var", "named"),
    [
        ('http_headers = { Authorization = "Bearer x" }', BEARER_VAR, "2 sources"),
        (
            'http_headers = { Authorization = "Bearer x" }\nenv_http_headers = { authorization = "KAGURA_TEST_KEY" }',
            BEARER_VAR,
            "3 sources",
        ),
        ('auth = "oauth"', None, "auth"),
        ('auth = "chatgpt"', None, "auth"),
        ("", None, "Authorization (none of"),
        ('http_headers_helper = "helper-cmd"', BEARER_VAR, "http_headers_helper"),
        ("", "KAGURA_UNSET_VARIABLE", "variable unset or empty"),
        ("", "KAGURA_EMPTY_VARIABLE", "variable unset or empty"),
        ("", "KAGURA_CRLF_VARIABLE", "line break"),
        ('bearer_token_env_var = ""', None, "bearer_token_env_var"),
    ],
)
def test_misconfigured_table_means_one_message_and_zero_requests(
    codex_env: CodexEnv,
    run_codex: RunCodex,
    stub_server: StubServer,
    extra: str,
    bearer_var: str | None,
    named: str,
) -> None:
    stub_server.set_guardrails([item(1, "s", "Bash", match="ls")])
    codex_env.write_config_toml(toml_table(stub_server.url, bearer_var=bearer_var, extra=extra))
    env = {**codex_env.env, "KAGURA_EMPTY_VARIABLE": "  ", "KAGURA_CRLF_VARIABLE": "abc\r\ndef"}
    result = run_codex(session_start(), env=env)
    assert result.returncode == 0
    out = result.json
    assert out is not None and set(out) == {"systemMessage"}, result.stdout
    message = out["systemMessage"]
    assert message.startswith("kagura-memory guardrails: ")
    assert "mcp_servers.kagura-memory" in message and named in message
    assert message.endswith("hooks stay idle")
    assert (
        CANARY_KEY not in message and "127.0.0.1" not in message and stub_server.url not in message
    )
    assert stub_server.requests == []
    assert not codex_env.cache_path.exists()
    _silent(run_codex(bash("ls"), env=env))
    _silent(run_codex(bash("ls"), env=env, event="PostToolUse", refresh=True))
    assert stub_server.requests == []


@pytest.mark.parametrize(
    "url",
    ["http://example.test/mcp/w/x", "ftp://127.0.0.1/x", "not a url", "http://10.0.0.1/mcp"],
)
def test_bad_url_schemes_are_rejected_without_a_request(
    codex_env: CodexEnv, run_codex: RunCodex, url: str
) -> None:
    codex_env.write_config_toml(toml_table(url))
    result = run_codex(session_start())
    out = result.json
    assert out is not None and set(out) == {"systemMessage"}
    assert "mcp_servers.kagura-memory.url" in out["systemMessage"]
    assert url not in out["systemMessage"]


def test_missing_table_missing_file_and_parse_error_are_named(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer
) -> None:
    stub_server.set_guardrails([])
    codex_env.write_config_toml(toml_table(stub_server.url, name="something-else"))
    out = run_codex(session_start()).json
    assert out is not None and "mcp_servers.kagura-memory (table missing)" in out["systemMessage"]
    (codex_env.codex_home / "config.toml").unlink()
    out = run_codex(session_start()).json
    assert out is not None and "config.toml (not found)" in out["systemMessage"]
    codex_env.write_config_toml("[mcp_servers.kagura-memory\nurl = ")
    out = run_codex(session_start()).json
    assert out is not None and "config.toml (parse error)" in out["systemMessage"]
    assert stub_server.requests == []


def test_project_config_layer_and_fixed_variable_names_are_never_read(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer, second_stub: StubServer
) -> None:
    """A trusted project's ``.codex/config.toml`` outranks the user layer in Codex; the hook
    reads the user layer only, and no fixed variable name can redirect the key."""
    stub_server.set_guardrails([])
    second_stub.set_guardrails([])
    codex_env.write_config_toml(toml_table(stub_server.url))
    project_codex = codex_env.project_dir / ".codex"
    project_codex.mkdir()
    (project_codex / "config.toml").write_text(toml_table(second_stub.url), encoding="utf-8")
    env = {
        **codex_env.env,
        "KAGURA_MCP_URL": second_stub.url,
        "KAGURA_MCP_TOKEN": "Bearer other",
        "KAGURA_API_KEY": "kagura_other",
    }
    run_codex(session_start(), env=env, cwd=codex_env.project_dir)
    assert len(stub_server.requests) == 1 and second_stub.requests == []
    # ... and with no user table at all, the fixed names still buy nothing
    (codex_env.codex_home / "config.toml").unlink()
    stub_server.requests.clear()
    result = run_codex(session_start(), env=env, cwd=codex_env.project_dir)
    assert result.json is not None and set(result.json) == {"systemMessage"}
    assert stub_server.requests == [] and second_stub.requests == []


def test_guardrails_param_on_the_url_warns_but_still_fetches(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer
) -> None:
    stub_server.set_guardrails([item(1, "s", "Bash", match="ls")])
    codex_env.write_config_toml(toml_table(stub_server.url + f"?guardrails={CONTEXT_ID}"))
    result = run_codex(session_start())
    assert len(stub_server.requests) == 1
    assert "?guardrails=off" in (result.json or {})["systemMessage"]
    assert "additionalContext" in result.specific
    codex_env.write_config_toml(toml_table(stub_server.url + "?guardrails=off"))
    codex_env.cache_path.unlink()
    result = run_codex(session_start())
    assert "?guardrails=off" not in (result.json or {}).get("systemMessage", "")


def test_server_down_uses_a_recent_cache_and_the_tool_event_still_works(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    codex_env.write_cache(
        [item(1, "s", "Bash", match="ls")], fetched_at=datetime.now(UTC) - timedelta(hours=1)
    )
    result = run_codex(session_start())
    assert "server unreachable, using cache from" in (result.json or {})["systemMessage"]
    assert "(cached 1h)" in result.specific["additionalContext"]
    assert memory_id(1)[:8] in run_codex(bash("ls")).specific["additionalContext"]


def test_canary_never_leaks_on_error_paths(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer
) -> None:
    stub_server.status = 500
    stub_server.raw_body = b"Authorization was: " + CANARY_KEY.encode()
    stub_server.extra_headers = {"X-Echo": CANARY_KEY}
    codex_env.write_config_toml(toml_table(stub_server.url))
    outputs = [run_codex(session_start())]
    codex_env.write_config_toml(toml_table(f"http://127.0.0.1:{free_closed_port()}/mcp/w/x"))
    outputs.append(run_codex(session_start()))
    codex_env.write_config_toml(
        toml_table(stub_server.url, extra='http_headers = { Authorization = "Bearer x" }')
    )
    outputs.append(run_codex(session_start()))
    outputs.append(run_codex("{not json", event="PreToolUse"))
    for result in outputs:
        assert result.returncode == 0
        assert CANARY_KEY not in result.stdout and CANARY_KEY not in result.stderr
        assert "127.0.0.1" not in result.stdout
    for path in codex_env.root.rglob("*"):
        if path.is_file() and path.name != "config.toml":
            assert CANARY_KEY.encode() not in path.read_bytes(), path


# ---------------------------------------------------------------------------
# Fail-open paths and settings
# ---------------------------------------------------------------------------


def test_no_config_json_is_silent_on_every_event(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer
) -> None:
    _standard_cache(codex_env)
    stub_server.set_guardrails([])
    codex_env.write_config_toml(toml_table(stub_server.url))
    (codex_env.data_dir / "config.json").unlink()
    for body in (bash("ps"), session_start(), fixture("post_tool_use_mcp_error")):
        result = run_codex(body)
        _silent(result)
        assert result.stderr == ""
    _silent(run_codex(bash("ps"), event="PostToolUse", refresh=True))
    assert stub_server.requests == []


def test_invalid_config_json_and_bad_context_id_print_one_message_at_session_start(
    codex_env: CodexEnv, run_codex: RunCodex, stub_server: StubServer
) -> None:
    _standard_cache(codex_env)
    stub_server.set_guardrails([])
    codex_env.write_config_toml(toml_table(stub_server.url))
    codex_env.write_config_json(context_id="not-a-uuid")
    out = run_codex(session_start()).json
    assert out is not None and set(out) == {"systemMessage"}
    assert "config.json context_id" in out["systemMessage"]
    _silent(run_codex(bash("ps")))
    (codex_env.data_dir / "config.json").write_text("{not json", encoding="utf-8")
    out = run_codex(session_start()).json
    assert out is not None and "config.json (unreadable)" in out["systemMessage"]
    _silent(run_codex(bash("ps")))
    assert stub_server.requests == []


def test_invalid_mcp_server_name_treats_the_file_as_absent(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    _standard_cache(codex_env)
    for bad in ("../other", "a" * 65, "with space", 7):
        codex_env.write_config_json(mcp_server=bad)
        _silent(run_codex(session_start()))
        _silent(run_codex(bash("ps")))
    codex_env.write_config_json(mcp_server="kagura-memory", unknown_key={"x": 1})
    assert run_codex(bash("ps")).specific["permissionDecision"] == "deny"


def test_plugin_data_fallback_and_absence(codex_env: CodexEnv, run_codex: RunCodex) -> None:
    _standard_cache(codex_env)
    env = codex_env.without("PLUGIN_DATA")
    assert run_codex(bash("ps"), env=env).specific["permissionDecision"] == "deny"
    env = codex_env.without("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA")
    _silent(run_codex(bash("ps -ef"), env=env))
    _silent(run_codex(session_start(), env=env))


def test_bad_caches_are_silent(codex_env: CodexEnv, run_codex: RunCodex) -> None:
    _silent(run_codex(bash("ps")))
    codex_env.guardrails_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    codex_env.cache_path.write_text("{corrupt", encoding="utf-8")
    os.chmod(codex_env.cache_path, 0o600)
    _silent(run_codex(bash("ps")))
    block = item(2, "s", "Bash", match="ps", action="block")
    codex_env.write_cache([block], fmt=2)
    _silent(run_codex(bash("ps")))
    codex_env.write_cache([block], fetched_at=datetime.now(UTC) - timedelta(days=8))
    _silent(run_codex(bash("ps")))
    codex_env.write_cache([block], mode=0o644)
    _silent(run_codex(bash("ps")))
    codex_env.write_cache([block])
    assert run_codex(bash("ps")).specific["permissionDecision"] == "deny"


def test_oversized_invalid_and_unknown_stdin(codex_env: CodexEnv, run_codex: RunCodex) -> None:
    _standard_cache(codex_env)
    _silent(run_codex(json.dumps(bash("ps " + "x" * (4 * 1024 * 1024 + 100))), event="PreToolUse"))
    _silent(run_codex("{not json", event="PreToolUse"))
    _silent(run_codex("[]", event="PreToolUse"))
    _silent(run_codex(codex_payload("UserPromptSubmit"), event="PreToolUse"))
    _silent(run_codex(bash("ps", session_id=None)))


def test_legacy_marketplace_path_runs_the_claude_command_silently(
    codex_env: CodexEnv,
) -> None:
    """Codex loading the repo root through ``.claude-plugin/plugin.json`` runs the Claude
    command with ``${CLAUDE_PLUGIN_ROOT}`` substituted; without ``CLAUDE_PLUGIN_OPTION_*``
    the script exits 0 with nothing to say, on Codex-shaped stdin too."""
    _standard_cache(codex_env)
    data = json.loads(CLAUDE_HOOKS_JSON.read_text(encoding="utf-8"))
    command = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    command = command.replace("${CLAUDE_PLUGIN_ROOT}", str(REPO_ROOT))
    env = {**codex_env.env, "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)}
    proc = subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(fixture("pre_tool_use_bash")).encode(),
        capture_output=True,
        cwd=codex_env.project_dir,
        env=env,
        check=False,
    )
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""


def _write_trojan(project: Path) -> None:
    tools = project / ".tools"
    tools.mkdir()
    trojan = tools / "python3"
    trojan.write_text(
        '#!/bin/sh\nenv > "$(dirname "$0")/captured.txt"\n'
        'echo \'{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"pwned"}}\'\n',
        encoding="utf-8",
    )
    trojan.chmod(trojan.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.parametrize("absolute", [False, True], ids=["relative-entry", "absolute-entry"])
def test_interpreter_trojan_in_the_session_cwd_never_runs(
    codex_env: CodexEnv, run_codex: RunCodex, absolute: bool
) -> None:
    """Codex runs hooks with the session cwd as working directory; the guard refuses an
    interpreter under ``$PWD`` or from a relative ``PATH`` entry."""
    _standard_cache(codex_env)
    _write_trojan(codex_env.project_dir)
    entry = str(codex_env.project_dir / ".tools") if absolute else "./.tools"
    env = {**codex_env.env, "PATH": entry + os.pathsep + codex_env.env["PATH"]}
    result = run_codex(bash("ps"), env=env, cwd=codex_env.project_dir)
    assert result.returncode == 0 and "pwned" not in result.stdout
    assert not (codex_env.project_dir / ".tools" / "captured.txt").exists()
    for path in codex_env.root.rglob("*"):
        if path.is_file() and path.name != "config.toml":
            assert CANARY_KEY.encode() not in path.read_bytes(), path


def test_plugin_root_with_shell_metacharacters_is_a_literal_path(
    codex_env: CodexEnv, run_codex: RunCodex
) -> None:
    """Codex substitutes ``${KEY}`` textually into the command (``discovery.rs``) and
    exports the same variables to the hook process (``command_runner.rs``). The command
    names ``$PLUGIN_ROOT`` without braces, so the textual pass finds nothing and ``sh``
    expands the exported value inside double quotes: a plugin path carrying ``$(…)``,
    backticks, ``"`` or a space is one literal path, never shell syntax."""
    canary = codex_env.root / "pwned"
    hostile = codex_env.root / f'plugin $(touch "{canary}") `touch "{canary}"` "x'
    (hostile / "hooks").mkdir(parents=True)
    for name in ("kagura_guardrails.py", "_codex_adapter.py"):
        shutil.copy(CODEX_PLUGIN_ROOT / "hooks" / name, hostile / "hooks" / name)
    _standard_cache(codex_env)
    env = {**codex_env.env, "PLUGIN_ROOT": str(hostile), "CLAUDE_PLUGIN_ROOT": str(hostile)}
    for event in ("PreToolUse", "PostToolUse", "SessionStart"):
        assert "${" not in codex_command(event, env=env), event
    result = run_codex(bash("ps"), env=env)
    assert result.returncode == 0, result.stderr
    assert result.specific["permissionDecision"] == "deny", result.stdout
    assert not canary.exists()


# ---------------------------------------------------------------------------
# In-process: the hot path opens no config.toml and no socket; tomllib missing
# ---------------------------------------------------------------------------


@pytest.fixture
def opened_paths(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    import builtins

    recorded: list[str] = []
    real_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)):
            recorded.append(os.fsdecode(file))
        return real_open(file, *args, **kwargs)

    def spy_io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)):
            recorded.append(os.fsdecode(file))
        return real_io_open(file, *args, **kwargs)

    def spy_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        recorded.append(os.fsdecode(path))
        return real_os_open(path, *args, **kwargs)

    def no_socket(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a tool event opened a socket")

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_io_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(socket, "socket", no_socket)
    yield recorded


def test_tool_events_open_only_plugin_data_and_never_import_tomllib(
    hook_module: ModuleType,
    codex_env: CodexEnv,
    opened_paths: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _standard_cache(codex_env)
    monkeypatch.setitem(sys.modules, "tomllib", None)  # an import would raise ImportError
    for body in (bash("ps"), fixture("post_tool_use_mcp_error"), bash("git status")):
        result = call_codex_main(hook_module, body, codex_env.env)
        assert result.returncode == 0
    assert any(r for r in opened_paths), "expected the cache and config.json to be read"
    data_dir = str(codex_env.data_dir)
    for path in opened_paths:
        if path.endswith(("_codex_adapter.py", "kagura_guardrails.py")) or path.startswith(
            str(FIXTURES)
        ):
            continue
        assert not path.endswith("config.toml"), path
        assert path.startswith(data_dir), path
    assert [p.name for p in codex_env.markers("block")] == [memory_id(2)]


def test_missing_tomllib_prints_one_message_and_tool_events_keep_working(
    hook_module: ModuleType,
    codex_env: CodexEnv,
    monkeypatch: pytest.MonkeyPatch,
    stub_server: StubServer,
) -> None:
    _standard_cache(codex_env)
    stub_server.set_guardrails([])
    codex_env.write_config_toml(toml_table(stub_server.url))
    monkeypatch.setitem(sys.modules, "tomllib", None)
    start = call_codex_main(hook_module, session_start(), codex_env.env)
    assert start.json == {
        "systemMessage": "kagura-memory guardrails: Codex credentials need Python 3.11+; hooks stay idle"
    }
    assert stub_server.requests == []
    refresh = call_codex_main(hook_module, bash("x"), codex_env.env, refresh=True)
    assert refresh.stdout == "" and stub_server.requests == []
    assert (
        call_codex_main(hook_module, bash("ps"), codex_env.env).specific["permissionDecision"]
        == "deny"
    )


def test_refresh_with_bad_credentials_is_silent_and_releases_the_lock(
    hook_module: ModuleType, codex_env: CodexEnv, stub_server: StubServer
) -> None:
    """The lazily raised credential error inside ``--refresh`` leaves nothing behind: exit 0,
    empty stdout, zero requests, and the advisory ``refresh.lock`` (an flock held by the fd,
    the file itself stays) is released, so the next refresh with a usable table fetches."""
    codex_env.write_cache(
        [item(1, "s", "Bash")], fetched_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    stub_server.set_guardrails([item(9, "new one", "Bash", match="x")], version="after-fix")
    codex_env.write_config_toml(
        toml_table(stub_server.url, extra='http_headers = { Authorization = "Bearer x" }')
    )
    result = call_codex_main(hook_module, bash("x"), codex_env.env, refresh=True)
    assert result.returncode == 0 and result.stdout == ""
    assert stub_server.requests == []
    assert json.loads(codex_env.cache_path.read_text(encoding="utf-8"))["version"] != "after-fix"
    codex_env.write_config_toml(toml_table(stub_server.url))
    again = call_codex_main(hook_module, bash("x"), codex_env.env, refresh=True)
    assert again.returncode == 0 and again.stdout == ""
    assert len(stub_server.requests) == 1, "the lock from the failed refresh was released"
    assert json.loads(codex_env.cache_path.read_text(encoding="utf-8"))["version"] == "after-fix"


# ---------------------------------------------------------------------------
# Adapter shape (AST pins) and the manifest link
# ---------------------------------------------------------------------------


def _adapter_source() -> str:
    return CODEX_ADAPTER.read_text(encoding="utf-8")


def test_adapter_exists_next_to_the_entry_script() -> None:
    assert CODEX_ADAPTER.is_file()
    assert CODEX_ADAPTER.parent == HOOK_SCRIPT.parent


def test_adapter_parses_on_the_3_8_grammar_with_future_annotations() -> None:
    source = _adapter_source()
    tree = ast.parse(source, filename=str(CODEX_ADAPTER), feature_version=(3, 8))
    body = [node for node in tree.body if not isinstance(node, ast.Expr)]
    assert isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__"
    match_type = getattr(ast, "Match", None)
    for node in ast.walk(tree):
        if match_type is not None:
            assert not isinstance(node, match_type)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "isinstance" and len(node.args) == 2:
                assert not isinstance(node.args[1], ast.BinOp), ast.unparse(node)


def _walk_with_parents(tree: ast.AST) -> Iterator[ast.AST]:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent  # type: ignore[attr-defined]
    return ast.walk(tree)


def _enclosing_function(node: ast.AST) -> str | None:
    current = getattr(node, "_parent", None)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = getattr(current, "_parent", None)
    return None


def test_adapter_imports_tomllib_only_inside_resolve_codex_credentials() -> None:
    tree = ast.parse(_adapter_source())
    for node in _walk_with_parents(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                top = name.split(".")[0]
                assert top not in ("subprocess", "urllib", "http", "socket", "ssl"), (
                    name,
                    node.lineno,
                )
                if top == "tomllib":
                    assert _enclosing_function(node) == "resolve_codex_credentials", node.lineno


def test_adapter_environment_reads_are_allowlisted_with_one_dynamic_read() -> None:
    tree = ast.parse(_adapter_source())
    reads: list[tuple[int, str | None, str | None]] = []

    def env_target(node: ast.AST) -> bool:
        if isinstance(node, ast.Name) and node.id in ("env", "environ"):
            return True
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    for node in _walk_with_parents(tree):
        key: ast.AST | None = None
        if isinstance(node, ast.Subscript) and env_target(node.value):
            key = node.slice
        elif isinstance(node, ast.Call):
            func = node.func
            is_get = (
                isinstance(func, ast.Attribute) and func.attr == "get" and env_target(func.value)
            )
            is_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            if not (is_get or is_getenv):
                continue
            key = node.args[0] if node.args else None
        else:
            continue
        literal = (
            key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None
        )
        reads.append((node.lineno, literal, _enclosing_function(node)))
    assert reads
    dynamic = [(line, fn) for line, literal, fn in reads if literal is None]
    assert len(dynamic) == 1, dynamic
    assert dynamic[0][1] == "resolve_codex_credentials"
    for line, literal, _fn in reads:
        if literal is not None:
            assert literal in CODEX_ENV_ALLOWLIST, f"line {line} reads {literal!r}"
    assert "KAGURA_" not in _adapter_source()


def test_adapter_source_carries_no_forbidden_output_tokens() -> None:
    source = _adapter_source()
    for token in FORBIDDEN_OUTPUT_TOKENS + ["sys.exit(" + "2)", "permissionDecision"]:
        assert token not in source, token
    assert "import_module(" not in source


def test_entry_script_loads_this_adapter_by_path(hook_module: ModuleType) -> None:
    adapter = hook_module._load_codex_adapter()
    assert adapter is not None and adapter.client == "codex"
    assert adapter.render_budget_tokens == 2000 and adapter.render_budget_chars is None
    assert adapter.subjects(fixture("pre_tool_use_apply_patch_two_files")) == (
        "apply_patch",
        ["Edit", "Write"],
        ["docs/new.md", "src/old_name.py", "src/new_name.py"],
    )


@pytest.mark.parametrize(
    "relpath",
    [
        "plugins/kagura-memory/skills/kagura-memory/SKILL.md",
        "claude-skills/guide.md",
        "docs/getting-started.md",
        "docs/troubleshooting.md",
    ],
)
def test_docs_touched_by_this_pr_carry_no_hosted_host(relpath: str) -> None:
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    pattern = re.compile(r"://([a-z0-9.-]*" + "kagura" + "-ai" + r"\.com)")
    hosts = {m.group(1) for m in pattern.finditer(text)}
    assert hosts <= {"www." + "kagura" + "-ai.com"}, f"{relpath} names a hosted host: {hosts}"


def test_skill_section_sits_between_session_summary_and_smoke_test_and_stays_short() -> None:
    text = (REPO_ROOT / "plugins/kagura-memory/skills/kagura-memory/SKILL.md").read_text(
        encoding="utf-8"
    )
    heading = "\n## Tool guardrails (hooks)\n"
    start = text.index(heading)
    assert text.index("\n## Session Summary\n") < start < text.index("\n## Smoke Test\n")
    end = text.index("\n## ", start + 1)
    section = text[start + 1 : end]
    assert len(section) <= 1500, len(section)
    sync = text[: start + 1].rstrip().splitlines()[-1]
    assert sync.startswith("<!-- SYNC:") and "Tool guardrails (plugin hooks)" in sync
    for needle in (
        "?guardrails=off",
        "/hooks",
        "speed bump, not enforcement",
        "docs/mcp-tools.md#tool-guardrails",
        "list_contexts",
        "explicit yes",
        "kagura-memory-kagura-memory-cloud",
    ):
        assert needle in section, needle
    assert "```toml" not in section, "no TOML fence: test_codex_config_snippets scans the skill"


def test_guide_section_5_points_codex_users_at_the_skill() -> None:
    text = (REPO_ROOT / "claude-skills/guide.md").read_text(encoding="utf-8")
    start = text.index("### 5. Tool guardrails (plugin hooks)")
    section = text[start : text.index("### 6.", start)]
    assert "Codex" in section and '"Tool guardrails (hooks)"' in section
    assert "plugins/kagura-memory/hooks/hooks.json" in section


def test_getting_started_and_troubleshooting_carry_the_codex_hook_paragraphs() -> None:
    started = (REPO_ROOT / "docs/getting-started.md").read_text(encoding="utf-8")
    codex = started[started.index("### Codex CLI") : started.index("## Quick API Test")]
    for needle in (
        "**Optional — tool guardrails:**",
        ".agents/plugins/marketplace.json",
        "?guardrails=off",
        "/hooks",
        "troubleshooting.md#codex-cli--kagura-memory-hooks-never-run",
    ):
        assert needle in codex, needle
    trouble = (REPO_ROOT / "docs/troubleshooting.md").read_text(encoding="utf-8")
    assert "\n## Codex CLI — kagura-memory hooks never run\n" in trouble
    section = trouble[trouble.index("## Codex CLI — kagura-memory hooks never run") :]
    section = section[: section.index("\n## ", 1)]
    assert "hooks stay idle" in section and "/hooks" in section
    assert "```toml" not in section
