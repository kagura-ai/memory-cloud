"""Table-driven tests for the client-neutral guardrail core (#1619).

The core is exercised in-process through ``hook_module`` (imported by path).
Rows are parametrised over the adapters' subject functions where the rule is
shared; PR-B (#1620) adds Codex rows to the same tables, never a second suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.plugin.conftest import (
    AGENT_ID,
    CONTEXT_ID,
    SESSION_ID,
    PluginEnv,
    bash_pre,
    command_for,
    item,
    iso_z,
    memory_id,
    payload,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only hook")


# ---------------------------------------------------------------------------
# Matching rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "name", "aliases", "expected"),
    [
        ("Bash|PowerShell", "Bash", [], True),
        ("Bash|PowerShell", "Bashful", [], False),
        ("Bash|PowerShell", "PowerShell", [], True),
        ("mcp__.*__remember", "mcp__kagura-memory__remember", [], True),
        ("mcp__.*__remember", "mcp__kagura-memory__recall", [], False),
        ("Edit|Write", "apply_patch", ["Edit", "Write"], True),
        ("Edit|Write", "apply_patch", [], False),
        ("(?i)bash", "Bash", [], True),
        ("bash", "Bash", [], False),
    ],
)
def test_tool_full_match_with_aliases(
    hook_module: ModuleType, pattern: str, name: str, aliases: list[str], expected: bool
) -> None:
    items = [item(1, "s", pattern)]
    hit = hook_module.match_items(items, "pre", name, aliases, ["x"], None, time.monotonic() + 1)
    assert bool(hit) is expected


@pytest.mark.parametrize(
    ("match", "subject", "expected"),
    [
        (r"\d+", "٣", False),  # ASCII classes only
        (r"\d+", "3", True),
        (r"abc$", "abc\n", True),  # trailing newline stripped before matching
        (r"abc$", "abc\nmore", False),  # no MULTILINE
        (r"a.b", "a\nb", False),  # no DOTALL
        (r"(?i)GIT PUSH", "git push", True),
        (r"git push", "GIT PUSH", False),
        (r"one\ntwo", "one\r\ntwo", True),  # CRLF normalised
        (r"x$", "x\r\n", True),
    ],
)
def test_match_semantics(hook_module: ModuleType, match: str, subject: str, expected: bool) -> None:
    items = [item(1, "s", "Bash", match=match)]
    hit = hook_module.match_items(items, "pre", "Bash", [], [subject], None, time.monotonic() + 1)
    assert bool(hit) is expected


def test_any_of_several_subjects_matches(hook_module: ModuleType) -> None:
    items = [item(1, "s", "Edit|Write", match=r"\.env$")]
    subjects = ["src/a.py", "config/.env", "README.md"]
    assert hook_module.match_items(
        items, "pre", "apply_patch", ["Edit", "Write"], subjects, None, time.monotonic() + 1
    )


def test_subject_is_cut_at_8192_characters(hook_module: ModuleType) -> None:
    items = [item(1, "s", "Bash", match="NEEDLE")]
    inside = "a" * (8192 - 6) + "NEEDLE"
    outside = "a" * 8187 + "NEEDLE"  # the needle starts at 8187 and ends at 8193
    assert hook_module.match_items(items, "pre", "Bash", [], [inside], None, time.monotonic() + 1)
    assert not hook_module.match_items(
        items, "pre", "Bash", [], [outside], None, time.monotonic() + 1
    )
    assert hook_module.normalize_subject("x" * 9000) == "x" * 8192


def test_item_without_match_fires_on_tool_alone(hook_module: ModuleType) -> None:
    items = [item(1, "s", "mcp__.*__forget")]
    assert hook_module.match_items(
        items, "pre", "mcp__kagura-memory__forget", [], ['{"x":1}'], None, time.monotonic() + 1
    )


def test_on_filter(hook_module: ModuleType) -> None:
    items = [item(1, "s", "Bash", on="result", match="oops")]
    assert not hook_module.match_items(
        items, "pre", "Bash", [], ["oops"], None, time.monotonic() + 1
    )
    assert hook_module.match_items(
        items, "result", "Bash", [], ["oops"], None, time.monotonic() + 1
    )


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------


def test_compact_json_sorts_keys(hook_module: ModuleType) -> None:
    out = hook_module.compact_json({"summary": "s", "context_id": "c", "tags": ["日本"]})
    assert out == '{"context_id":"c","summary":"s","tags":["日本"]}'


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (bash_pre("gh pr merge 1 --delete-branch"), ["gh pr merge 1 --delete-branch"]),
        (payload("PreToolUse", tool_name="Bash", tool_input={"command": 5}), []),
        (
            payload("PreToolUse", tool_name="Write", tool_input={"file_path": "C:\\a\\b.py"}),
            ["C:/a/b.py"],
        ),
        (
            payload("PreToolUse", tool_name="Read", tool_input={"file_path": "/x/y"}),
            ["/x/y"],
        ),
        (
            payload(
                "PreToolUse", tool_name="NotebookEdit", tool_input={"notebook_path": "n\\b.ipynb"}
            ),
            ["n/b.ipynb"],
        ),
        (
            payload(
                "PreToolUse",
                tool_name="mcp__kagura-memory__remember",
                tool_input={"summary": "s", "context_id": "c"},
            ),
            ['{"context_id":"c","summary":"s"}'],
        ),
        (
            payload("PreToolUse", tool_name="Glob", tool_input={"pattern": "*.py"}),
            ['{"pattern":"*.py"}'],
        ),
    ],
)
def test_claude_pre_subjects(
    hook_module: ModuleType, event: dict[str, Any], expected: list[str]
) -> None:
    name, aliases, subjects = hook_module.ClaudeAdapter().subjects(event)
    assert name == event["tool_name"]
    assert aliases == []
    assert subjects == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Exit code 1\nboom", "Exit code 1\nboom"),
        (
            {"stdout": "out", "stderr": "err", "interrupted": False, "isImage": False},
            "err\nout",
        ),
        (
            {
                "content": [{"type": "text", "text": '{"status": "error", "error": "x"}'}],
                "isError": True,
            },
            '{"status": "error", "error": "x"}\ntext\nisError=true',
        ),
        ({"content": [{"type": "text", "text": "ok"}]}, "ok\ntext"),
        ({"b": ["z", {"y": "1"}], "a": "first"}, "first\nz\n1"),
        (None, ""),
        (42, ""),
    ],
)
def test_result_subject(hook_module: ModuleType, value: Any, expected: str) -> None:
    assert hook_module.result_subject(value) == expected


def test_claude_result_events_use_response_and_error(hook_module: ModuleType) -> None:
    adapter = hook_module.ClaudeAdapter()
    post = payload(
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": "x"},
        tool_response={"stdout": "o", "stderr": "e", "interrupted": False, "isImage": False},
    )
    assert adapter.subjects(post) == ("Bash", [], ["e\no"])
    failure = payload(
        "PostToolUseFailure",
        tool_name="mcp__kagura-memory__remember",
        tool_input={},
        error='{"status":"error"}',
    )
    assert adapter.subjects(failure) == ("mcp__kagura-memory__remember", [], ['{"status":"error"}'])


# ---------------------------------------------------------------------------
# Skip rules and cache validation
# ---------------------------------------------------------------------------


def _bad_items() -> list[dict[str, Any]]:
    good = item(1, "s", "Bash", match="x")
    return [
        {**good, "memory_id": "not-a-uuid"},
        {**good, "tool_trigger": "Bash"},
        {**good, "tool_trigger": {**good["tool_trigger"], "on": "later"}},
        {**good, "tool_trigger": {**good["tool_trigger"], "action": "ask_user"}},
        {**good, "tool_trigger": {**good["tool_trigger"], "tool": 7}},
        {**good, "tool_trigger": {**good["tool_trigger"], "match": ["x"]}},
        {**good, "tool_trigger": {**good["tool_trigger"], "match": "(unclosed"}},
        {**good, "tool_trigger": {**good["tool_trigger"], "tool": "(?P<n>x)(?P=n"}},
    ]


def test_skip_rules_never_fail(hook_module: ModuleType) -> None:
    items = _bad_items() + [item(9, "ok", "Bash", match="x")]
    matched = hook_module.match_items(items, "pre", "Bash", [], ["x"], None, time.monotonic() + 1)
    assert [m["memory_id"] for m in matched] == [memory_id(9)]
    for bad in _bad_items():
        assert not hook_module.item_is_compilable(bad)


def test_unknown_keys_ignored_and_defaults_applied(hook_module: ModuleType) -> None:
    raw = {
        "memory_id": memory_id(1),
        "summary": "s",
        "importance": 0.5,
        "future_key": {"x": 1},
        "tool_trigger": {"tool": "Bash", "match": "x", "extra": True},
    }
    trigger = hook_module.valid_trigger(raw)
    assert trigger is not None and trigger.on == "pre" and trigger.action == "inform"


@pytest.mark.parametrize(
    ("mutate", "expected_absent"),
    [
        (lambda c: c, False),
        (lambda c: {**c, "format": 2}, True),
        (lambda c: {k: v for k, v in c.items() if k != "format"}, True),
        (lambda c: {**c, "format": "1"}, True),
        (lambda c: {**c, "context_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8"}, True),
        (lambda c: {**c, "fetched_at": "yesterday"}, True),
        (lambda c: {**c, "fetched_at": iso_z(datetime.now(UTC) + timedelta(minutes=10))}, True),
        (lambda c: {**c, "fetched_at": iso_z(datetime.now(UTC) + timedelta(minutes=2))}, False),
        (lambda c: {**c, "fetched_at": iso_z(datetime.now(UTC) - timedelta(days=8))}, True),
        (lambda c: {**c, "fetched_at": iso_z(datetime.now(UTC) - timedelta(days=6))}, False),
        (lambda c: {**c, "unknown_top": 1}, False),
    ],
)
def test_load_cache_validation(
    hook_module: ModuleType, plugin_env: PluginEnv, mutate: Any, expected_absent: bool
) -> None:
    base = {
        "format": 1,
        "context_id": CONTEXT_ID,
        "fetched_at": iso_z(datetime.now(UTC) - timedelta(minutes=1)),
        "version": "v",
        "pinned": [],
        "tool_triggered": [item(1, "s", "Bash")],
    }
    plugin_env.guardrails_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    plugin_env.cache_path.write_text(json.dumps(mutate(base)), encoding="utf-8")
    os.chmod(plugin_env.cache_path, 0o600)
    loaded = hook_module.load_cache(
        str(plugin_env.cache_path), CONTEXT_ID, datetime.now(UTC), hook_module.TOOL_EVENT_MAX_AGE_S
    )
    assert (loaded is None) is expected_absent


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o606])
def test_load_cache_rejects_group_or_other_bits(
    hook_module: ModuleType, plugin_env: PluginEnv, mode: int
) -> None:
    plugin_env.write_cache([item(1, "s", "Bash")], mode=mode)
    assert (
        hook_module.load_cache(str(plugin_env.cache_path), CONTEXT_ID, datetime.now(UTC), None)
        is None
    )


def test_load_cache_rejects_oversized_and_corrupt(
    hook_module: ModuleType, plugin_env: PluginEnv
) -> None:
    plugin_env.guardrails_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    plugin_env.cache_path.write_text("{not json", encoding="utf-8")
    os.chmod(plugin_env.cache_path, 0o600)
    assert (
        hook_module.load_cache(str(plugin_env.cache_path), CONTEXT_ID, datetime.now(UTC), None)
        is None
    )
    with open(plugin_env.cache_path, "wb") as fh:
        fh.write(b"[" + b"1," * (2 * 1024 * 1024 + 10) + b"1]")
    assert (
        hook_module.load_cache(str(plugin_env.cache_path), CONTEXT_ID, datetime.now(UTC), None)
        is None
    )


# ---------------------------------------------------------------------------
# Two-phase compile and budgets
# ---------------------------------------------------------------------------


def test_two_phase_compile_skips_match_patterns_on_tool_miss(
    hook_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled: list[str] = []
    original = hook_module.compile_pattern

    def spy(pattern: str):
        compiled.append(pattern)
        return original(pattern)

    monkeypatch.setattr(hook_module, "compile_pattern", spy)
    items = [item(n, "s", "Bash", match=f"needle{n}") for n in range(50)]
    hook_module.match_items(items, "pre", "Read", [], ["/x"], None, time.monotonic() + 1)
    assert compiled == ["Bash"] * 50
    assert not any(p.startswith("needle") for p in compiled)


def _state(hook_module: ModuleType, plugin_env: PluginEnv) -> Any:
    plugin_env.guardrails_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return hook_module.State(str(plugin_env.guardrails_dir), SESSION_ID, None)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs setitimer")
def test_budget_interrupts_a_slow_pattern_and_still_delivers_the_fast_block(
    hook_module: ModuleType, plugin_env: PluginEnv
) -> None:
    slow = item(1, "slow", "Bash", match=r".*a.*b")  # the server rejects this; a cache may carry it
    fast = item(2, "fast", "Bash", match=r"gh pr merge", action="block")
    subject = "gh pr merge " + "a" * 8192  # the needle sits inside the 8,192-char window
    state = _state(hook_module, plugin_env)
    started = time.monotonic()
    matched = hook_module.match_items(
        [slow, fast], "pre", "Bash", [], [subject], state, started + hook_module.CALL_BUDGET_S
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, elapsed
    assert [m["memory_id"] for m in matched] == [memory_id(2)]
    assert (plugin_env.state_dir / state.sid16 / "slow" / f"{memory_id(1)}.1").exists()
    assert not (plugin_env.state_dir / state.sid16 / "slow" / memory_id(1)).exists()
    # second trip -> skipped for the session
    hook_module.match_items([slow], "pre", "Bash", [], [subject], state, time.monotonic() + 1)
    assert (plugin_env.state_dir / state.sid16 / "slow" / memory_id(1)).exists()
    assert state.is_slow(memory_id(1))
    # a following normal pattern still matches: the timer is cleared
    normal = item(3, "normal", "Bash", match="merge")
    assert hook_module.match_items(
        [slow, normal], "pre", "Bash", [], [subject], state, time.monotonic() + 1
    ) == [normal]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs setitimer")
def test_fifty_slow_patterns_respect_the_per_call_clock(
    hook_module: ModuleType, plugin_env: PluginEnv
) -> None:
    items = [item(n, "slow", "Bash", match=r".*a.*b") for n in range(50)]
    state = _state(hook_module, plugin_env)
    started = time.monotonic()
    hook_module.match_items(
        items, "pre", "Bash", [], ["a" * 8192], state, started + hook_module.CALL_BUDGET_S
    )
    assert time.monotonic() - started < 1.2
    assert list((plugin_env.state_dir / state.sid16 / "slow").glob("*.1"))


# ---------------------------------------------------------------------------
# Markers, caps, reset
# ---------------------------------------------------------------------------


def test_markers_never_contain_raw_ids_and_missing_session_yields_nothing(
    hook_module: ModuleType, plugin_env: PluginEnv, call_main: Any
) -> None:
    plugin_env.write_cache([item(1, "s", "Bash", match="ls")])
    result = call_main(bash_pre("ls -la", agent_id=AGENT_ID))
    assert result.json is not None
    for path in plugin_env.state_dir.rglob("*"):
        assert SESSION_ID not in str(path) and AGENT_ID not in str(path)
    plugin_env2 = plugin_env
    before = len(plugin_env2.markers())
    result = call_main(bash_pre("ls -la", session_id=None))
    assert result.stdout == ""
    assert len(plugin_env2.markers()) == before


def test_marker_race_delivers_exactly_once(plugin_env: PluginEnv) -> None:
    plugin_env.write_cache([item(1, "s", "Bash", match="ls"), item(2, "t", "Bash", match="ls")])
    command = command_for("PreToolUse")
    body = json.dumps(bash_pre("ls")).encode()

    def run(_: int) -> str:
        proc = subprocess.run(
            ["sh", "-c", command],
            input=body,
            capture_output=True,
            cwd=plugin_env.project_dir,
            env=plugin_env.env,
            check=False,
            timeout=60,
        )
        assert proc.returncode == 0
        return proc.stdout.decode()

    with ThreadPoolExecutor(max_workers=16) as pool:
        outputs = list(pool.map(run, range(16)))
    non_empty = [o for o in outputs if o.strip()]
    rendered_lines = sum(
        json.loads(o)["hookSpecificOutput"]["additionalContext"].count("Kagura Memory guardrail (")
        for o in non_empty
    )
    assert len(plugin_env.markers()) == 2
    assert rendered_lines == 2
    assert 1 <= len(non_empty) <= 2  # two hooks may split the two candidates between them
    log = (plugin_env.guardrails_dir / "deliveries.log").read_text().splitlines()
    assert len(log) == len(plugin_env.markers())


def test_eleven_informs_then_a_block_still_denies(plugin_env: PluginEnv, call_main: Any) -> None:
    informs = [item(n, f"inform {n}", "Bash", match=f"cmd{n}\\b") for n in range(1, 12)]
    block = item(50, "the block", "Bash", match="rm -rf", action="block")
    plugin_env.write_cache(informs + [block])
    for n in range(1, 12):
        call_main(bash_pre(f"cmd{n}"))
    delivered = len(plugin_env.markers("inform"))
    assert delivered == 10, "the per-key inform cap holds at 10"
    result = call_main(bash_pre("rm -rf /tmp/x"))
    assert result.specific["permissionDecision"] == "deny"
    assert "the block" in result.specific["permissionDecisionReason"]


def test_every_live_block_is_taken_and_informs_fill_to_three(
    plugin_env: PluginEnv, call_main: Any
) -> None:
    blocks = [item(n, f"block {n}", "Bash", match="danger", action="block") for n in range(1, 5)]
    informs = [item(n, f"inform {n}", "Bash", match="danger") for n in range(10, 12)]
    plugin_env.write_cache(blocks + informs)
    result = call_main(bash_pre("danger"))
    reason = result.specific["permissionDecisionReason"]
    lines = reason.split("\n")
    assert lines[0].startswith("Kagura Memory guardrails (")
    body = [ln for ln in lines if ln.startswith("Kagura Memory guardrail (")]
    assert len(body) == 4 and all("block" in ln for ln in body)
    assert len(plugin_env.markers("block")) == 4
    assert plugin_env.markers("inform") == []
    assert lines[-1].startswith("One-time note from the kagura-memory plugin hook")
    # The re-issued call is not denied again; the two informs the block lines displaced
    # were never marked, so they now reach the model as context next to the result.
    again = call_main(bash_pre("danger"))
    assert "permissionDecision" not in again.specific
    assert again.specific["additionalContext"].count("Kagura Memory guardrail (") == 2
    assert len(plugin_env.markers("inform")) == 2
    assert call_main(bash_pre("danger")).stdout == ""


def test_one_block_plus_five_informs(plugin_env: PluginEnv, call_main: Any) -> None:
    block = item(1, "block one", "Bash", match="danger", action="block")
    informs = [item(n, f"inform {n}", "Bash", match="danger") for n in range(10, 15)]
    plugin_env.write_cache([block] + informs)
    result = call_main(bash_pre("danger"))
    body = [
        ln
        for ln in result.specific["permissionDecisionReason"].split("\n")
        if ln.startswith("Kagura Memory guardrail (")
    ]
    assert len(body) == 3
    assert "block one" in body[0]
    assert len(plugin_env.markers("inform")) == 2
    assert len(plugin_env.markers("block")) == 1


def test_compact_reset_touches_main_only(plugin_env: PluginEnv, call_main: Any) -> None:
    plugin_env.write_cache([item(1, "s", "Bash", match="ls")])
    assert call_main(bash_pre("ls")).json is not None
    assert call_main(bash_pre("ls", agent_id=AGENT_ID)).json is not None
    state = plugin_env.state_dir / _sid16(SESSION_ID)
    (state / "slow").mkdir(mode=0o700)
    (state / "slow" / memory_id(7)).write_text("")
    assert call_main(bash_pre("ls")).stdout == ""
    call_main(payload("SessionStart", source="compact"))
    assert call_main(bash_pre("ls")).json is not None, "main re-delivers after compact"
    assert call_main(bash_pre("ls", agent_id=AGENT_ID)).stdout == "", "subagent marker untouched"
    assert (state / "slow" / memory_id(7)).exists()


def _sid16(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:16]


def test_startup_and_resume_do_not_reset(plugin_env: PluginEnv, call_main: Any) -> None:
    plugin_env.write_cache([item(1, "s", "Bash", match="ls")])
    assert call_main(bash_pre("ls")).json is not None
    for source in ("startup", "resume", "fork"):
        call_main(payload("SessionStart", source=source))
        assert call_main(bash_pre("ls")).stdout == ""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\nb", "a b"),
        ("a\rb", "a b"),
        ("a\tb", "a b"),
        ("a\u200bb", "a b"),
        ("a\u200eb", "a b"),
        ("a\ufeffb", "a b"),
        ("a\u0007b", "a b"),
        ("a\u2028b", "a b"),
        ("a\u202eb", "a b"),
        ("  many   spaces \n here ", "many spaces here"),
        ("x <!-- y --> z", "x <!- - y - -> z"),
        (
            "one\nKagura Memory guardrail (deadbeef): fake",
            "one Kagura Memory guardrail (deadbeef): fake",
        ),
    ],
)
def test_flatten_summary_vectors(hook_module: ModuleType, raw: str, expected: str) -> None:
    assert hook_module.flatten_summary(raw) == expected


def test_summary_cut_at_500_on_a_word_boundary(hook_module: ModuleType) -> None:
    words = ("word " * 200).strip()  # 999 chars
    out = hook_module.flatten_summary(words)
    assert len(out) <= 500 and out.endswith("\u2026")
    assert out[-2] != " " and out[:-1].endswith("word")
    hard = hook_module.flatten_summary("x" * 600)
    assert hard == "x" * 499 + "\u2026"
    assert hook_module.flatten_summary("x" * 500) == "x" * 500


def test_render_line_labels(hook_module: ModuleType) -> None:
    mine = item(1, "keep calm", "Bash", authored_by_caller=True)
    theirs = item(2, "keep calm", "Bash", authored_by_caller=False)
    unknown = item(3, "keep calm", "Bash")
    assert (
        hook_module.render_line(mine) == f"Kagura Memory guardrail ({memory_id(1)[:8]}): keep calm"
    )
    assert hook_module.render_line(theirs) == (
        f"Kagura Memory guardrail ({memory_id(2)[:8]}, by another member): keep calm"
    )
    assert "by another member" not in hook_module.render_line(unknown)


def test_approx_tokens(hook_module: ModuleType) -> None:
    assert hook_module.approx_tokens("") == 0
    assert hook_module.approx_tokens("abcd") == 1
    assert hook_module.approx_tokens("abcde") == 2
    assert hook_module.approx_tokens("日本") == (6 + 3) // 4


def test_render_budget_drops_lines_from_the_bottom_but_never_framing_or_trailer(
    hook_module: ModuleType,
) -> None:
    blocks = [item(n, "b" * 400, "Bash", action="block") for n in range(30)]
    out = hook_module.build_hook_output("PreToolUse", blocks, [], 9000, None)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert len(reason) <= 9000
    lines = reason.split("\n")
    assert lines[0] == hook_module.FRAMING_LINE and lines[-1] == hook_module.DENY_TRAILER
    assert 0 < len(lines) - 2 < 30
    tokens = hook_module.build_hook_output("PreToolUse", [], blocks, None, 2000)
    assert hook_module.approx_tokens(tokens["hookSpecificOutput"]["additionalContext"]) <= 2000


# ---------------------------------------------------------------------------
# Cross-adapter cache and misc helpers
# ---------------------------------------------------------------------------


def test_cache_written_by_project_response_reads_back_identically(
    hook_module: ModuleType, plugin_env: PluginEnv
) -> None:
    from tests.plugin.conftest import load_guardrails_response

    envelope = load_guardrails_response(
        [item(1, "s", "Bash", match="ls", authored_by_caller=False), item(2, "t", "Edit|Write")]
    )
    result = json.loads(envelope["result"]["content"][0]["text"])
    cache = hook_module.project_response(result, CONTEXT_ID, datetime.now(UTC))
    assert list(cache) == [
        "format",
        "context_id",
        "fetched_at",
        "version",
        "pinned",
        "tool_triggered",
    ]
    first = cache["tool_triggered"][0]
    assert list(first) == [
        "memory_id",
        "summary",
        "importance",
        "authored_by_caller",
        "tool_trigger",
    ]
    assert list(cache["tool_triggered"][1]) == [
        "memory_id",
        "summary",
        "importance",
        "tool_trigger",
    ]
    plugin_env.guardrails_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert hook_module.write_private_file(
        str(plugin_env.cache_path), hook_module.cache_bytes(cache)
    )
    assert oct(plugin_env.cache_path.stat().st_mode & 0o777) == "0o600"
    loaded = hook_module.load_cache(str(plugin_env.cache_path), CONTEXT_ID, datetime.now(UTC), None)
    assert loaded is not None and loaded.data == cache
    # any adapter reading this cache gets the same matches
    a = hook_module.match_items(
        loaded.tool_triggered, "pre", "Bash", [], ["ls"], None, time.monotonic() + 1
    )
    b = hook_module.match_items(
        loaded.tool_triggered,
        "pre",
        "apply_patch",
        ["Edit", "Write"],
        ["x"],
        None,
        time.monotonic() + 1,
    )
    assert [m["memory_id"] for m in a] == [memory_id(1)]
    assert [m["memory_id"] for m in b] == [memory_id(2)]


@pytest.mark.parametrize(
    ("url", "ok", "warn"),
    [
        ("https://example.test/mcp/w/x", True, False),
        ("https://example.test/mcp/w/x?profile=core", True, False),
        ("https://example.test/mcp/w/x?guardrails=off", True, False),
        ("https://example.test/mcp/w/x?guardrails=OFF ", True, False),
        (
            "https://example.test/mcp/w/x?guardrails=550e8400-e29b-41d4-a716-446655440000",
            True,
            True,
        ),
        ("https://example.test/mcp/w/x?profile=core&guardrails=typo", True, True),
        ("http://localhost:8080/mcp/w/x", True, False),
        ("http://127.0.0.1:8080/mcp/w/x", True, False),
        ("http://127.1.2.3/mcp", True, False),
        ("http://[::1]:8080/mcp", True, False),
        ("http://example.test/mcp/w/x", False, False),
        ("http://10.0.0.1/mcp", False, False),
        ("ftp://example.test/x", False, False),
        ("https:///x", False, False),
        ("not a url", False, False),
        ("https://user:pw@example.test/x", True, False),
    ],
)
def test_server_url_rules(hook_module: ModuleType, url: str, ok: bool, warn: bool) -> None:
    parts = hook_module.parse_server_url(url)
    assert (parts is not None) is ok
    if parts is not None:
        assert hook_module.url_requests_server_digest(parts.query) is warn


def test_canonical_uuid(hook_module: ModuleType) -> None:
    assert hook_module.canonical_uuid(CONTEXT_ID) == CONTEXT_ID
    assert hook_module.canonical_uuid(CONTEXT_ID.upper()) == CONTEXT_ID
    assert hook_module.canonical_uuid(CONTEXT_ID.replace("-", "")) is None
    assert hook_module.canonical_uuid("../../etc/passwd") is None
    assert hook_module.canonical_uuid(None) is None


def test_max_action_parsing(hook_module: ModuleType) -> None:
    assert hook_module.normalize_max_action(None) == ("block", True)
    assert hook_module.normalize_max_action("Block") == ("block", True)
    assert hook_module.normalize_max_action(" INFORM ") == ("inform", True)
    assert hook_module.normalize_max_action("bogus") == ("inform", False)


def test_deliveries_log_rotates(hook_module: ModuleType, plugin_env: PluginEnv) -> None:
    plugin_env.guardrails_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = plugin_env.guardrails_dir / "deliveries.log"
    log.write_bytes(b"x" * (hook_module.LOG_ROTATE_BYTES + 1))
    state = hook_module.State(str(plugin_env.guardrails_dir), SESSION_ID, None)
    hook_module.append_delivery_log(
        str(plugin_env.guardrails_dir), "claude", "PreToolUse", state, memory_id(1), "inform"
    )
    assert (plugin_env.guardrails_dir / "deliveries.log.1").exists()
    line = log.read_text().strip()
    assert line.startswith("ts=") and " client=claude event=PreToolUse sid=" in line
    assert f"id={memory_id(1)[:8]} lane=inform" in line
    assert SESSION_ID not in line


def test_prune_removes_old_sessions_and_stale_caches(
    hook_module: ModuleType, plugin_env: PluginEnv
) -> None:
    old_dir = plugin_env.state_dir / "0123456789abcdef"
    old_dir.mkdir(parents=True, mode=0o700)
    new_dir = plugin_env.state_dir / "fedcba9876543210"
    new_dir.mkdir(mode=0o700)
    stale = plugin_env.guardrails_dir / f"{CONTEXT_ID}.json.stale"
    stale.write_text("{}")
    ancient = time.time() - 8 * 86400
    os.utime(old_dir, (ancient, ancient))
    os.utime(stale, (ancient, ancient))
    hook_module.prune_state(str(plugin_env.guardrails_dir), time.time())
    assert not old_dir.exists() and new_dir.exists() and not stale.exists()


def test_state_dirs_are_private(plugin_env: PluginEnv, call_main: Any) -> None:
    plugin_env.write_cache([item(1, "s", "Bash", match="ls")])
    call_main(bash_pre("ls"))
    for path in [plugin_env.guardrails_dir, *plugin_env.state_dir.rglob("*")]:
        mode = path.stat().st_mode & 0o777
        assert mode in (0o700, 0o600), (path, oct(mode))
    assert Path(plugin_env.guardrails_dir / "deliveries.log").stat().st_mode & 0o077 == 0
