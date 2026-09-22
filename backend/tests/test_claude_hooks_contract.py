"""Guard tests for #1623: ``.claude/settings.json`` hooks use a channel Claude reads.

``.claude/settings.json`` is tracked, so every contributor who trusts the
folder runs the same hooks. Claude Code's hooks reference fixes which output
channels reach the model, and a ``PreToolUse`` hook has exactly two of them:

* **exit 2** — blocks the tool call; stderr becomes the deny reason Claude sees.
* **exit 0 + JSON on stdout** — ``hookSpecificOutput.additionalContext`` is
  injected as a system reminder next to the tool result.

Stderr from a hook that exits 0 goes to the debug log only. Plain-text stdout
on exit 0 is model context only for ``UserPromptSubmit`` / ``SessionStart``
style events, not ``PreToolUse``. The PR reminder hook shipped in the first
form (``echo ... >&2; exit 0``) and therefore never reached a model request.

These tests run each hook command the way Claude Code does (``sh -c`` with the
event payload on stdin), touch no network and write nothing into the repo.

Manual delivery check (the reminder is a system reminder, so it renders no chat
message; the debug log — ``claude --debug`` — is the evidence):

* old form: the reminder text appears only as hook stderr in the debug log;
* new form: the debug log shows the ``additionalContext`` value accepted and
  delivered next to the ``gh pr create`` tool result.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

# backend/tests/test_claude_hooks_contract.py -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"

# Every hook command in the file shells out to jq to read its stdin payload.
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not on PATH")


def _payload(tool_name: str, **tool_input: str) -> str:
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input}
    )


# Payloads that trigger each of the tracked PreToolUse hooks. The secret and
# SQL samples are assembled at runtime so the file itself does not trip the
# hooks it tests when it is written or edited.
_TRIGGERING_PAYLOADS: dict[str, str] = {
    "gh pr create": _payload("Bash", command="gh pr create --title x --body y"),
    "env file": _payload("Write", file_path="/tmp/x/.env", content="A=1"),
    "hardcoded secret": _payload(
        "Write", file_path="/tmp/x/a.py", content="key = " + "AKIA" + "A" * 16
    ),
    "f-string sql": _payload(
        "Write", file_path="/tmp/x/a.py", content="q = f" + '"' + "SELECT 1 {x}" + '"'
    ),
}


# Command shapes the PR reminder must fire on. The heredoc form is the usual
# one: encoded, its newlines are ``\\n`` escapes, which dash's ``echo`` (``sh``
# on Debian-family hosts) turns back into raw newlines, corrupting the payload
# before jq reads it. The hook therefore reads stdin directly, never via echo.
_REMINDER_PAYLOADS: dict[str, str] = {
    "gh pr create": _TRIGGERING_PAYLOADS["gh pr create"],
    "gh pr create heredoc": _payload(
        "Bash",
        command=(
            "git push -u origin HEAD\n"
            'gh pr create --title "x" --body "$(cat <<\'EOF\'\n'
            "## Summary\n\t- one\n\\ two\nEOF\n)"
        ),
    ),
}


def _pre_tool_use_hooks() -> list[tuple[str, str]]:
    """Return ``(matcher, command)`` for every command hook under ``hooks.PreToolUse``."""
    settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    return [
        (entry.get("matcher", ""), hook["command"])
        for entry in settings["hooks"]["PreToolUse"]
        for hook in entry["hooks"]
        if hook.get("type") == "command"
    ]


def _bash_hook_command() -> str:
    commands = [cmd for matcher, cmd in _pre_tool_use_hooks() if matcher == "Bash"]
    assert len(commands) == 1, f"expected one Bash PreToolUse hook, found {len(commands)}"
    return commands[0]


def _run_hook(command: str, payload: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run ``command`` under ``sh -c`` with ``payload`` on stdin, from a scratch cwd."""
    return subprocess.run(
        ["sh", "-c", command],
        input=payload,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        timeout=30,
    )


def _channel_violation(result: subprocess.CompletedProcess[str]) -> str | None:
    """Return why ``result`` breaks the PreToolUse output contract, or ``None``.

    The contract, from the hooks reference ("Exit code output"): a hook exits 0
    or 2 and nothing else, since any other code is a non-blocking error whose
    stderr the model never sees; exit 2 carries its reason on stderr; exit 0
    with text only on stderr goes to the debug log; and anything on stdout, on
    either status, must be one ``PreToolUse`` JSON object, because Claude Code
    parses stdout on every exit code and reports garbage as a hook error.
    """
    stdout, stderr = result.stdout.strip(), result.stderr.strip()
    if result.returncode not in (0, 2):
        return f"exit {result.returncode} is a non-blocking error the model never sees"
    if result.returncode == 2 and not stderr:
        return "exit 2 without a stderr reason blocks silently"
    if result.returncode == 0 and stderr and not stdout:
        return f"exit 0 with stderr only goes to the debug log: {stderr!r}"
    if stdout:
        try:
            event_name = json.loads(stdout)["hookSpecificOutput"]["hookEventName"]
        except (ValueError, KeyError, TypeError) as exc:
            return f"stdout is not PreToolUse JSON ({type(exc).__name__}): {stdout!r}"
        if event_name != "PreToolUse":
            return f"stdout JSON names hookEventName={event_name!r}, expected 'PreToolUse'"
    return None


# ---------------------------------------------------------------------------
# PR reminder hook (matcher: Bash)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sample", sorted(_REMINDER_PAYLOADS))
def test_pr_reminder_emits_additional_context_json(sample: str, tmp_path: Path) -> None:
    """``gh pr create`` yields exit 0, silent stderr and one PreToolUse JSON object."""
    result = _run_hook(_bash_hook_command(), _REMINDER_PAYLOADS[sample], tmp_path)

    assert result.returncode == 0
    assert result.stderr == "", f"reminder leaked to stderr: {result.stderr!r}"
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, f"expected exactly one JSON line, got {result.stdout!r}"
    assert lines[0].startswith("{") and lines[0].endswith("}")

    output = json.loads(lines[0])["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    context = output["additionalContext"]
    assert isinstance(context, str) and context.strip()


@pytest.mark.parametrize(
    "command",
    ["git status", "gh pr merge 1 --squash", "gh pr view 1"],
)
def test_pr_reminder_is_silent_for_other_commands(command: str, tmp_path: Path) -> None:
    """Non-matching commands, including merge, produce no output on either stream."""
    result = _run_hook(_bash_hook_command(), _payload("Bash", command=command), tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# Regression guard over every PreToolUse hook
# ---------------------------------------------------------------------------


def test_every_pre_tool_use_hook_has_a_triggering_payload(tmp_path: Path) -> None:
    """Each hook fires on at least one sample, so the channel guard below is not vacuous."""
    for matcher, command in _pre_tool_use_hooks():
        fired = any(
            result.returncode != 0 or result.stdout.strip() or result.stderr.strip()
            for result in (
                _run_hook(command, payload, tmp_path) for payload in _TRIGGERING_PAYLOADS.values()
            )
        )
        assert fired, f"no sample payload triggers PreToolUse hook (matcher={matcher!r})"


@pytest.mark.parametrize("sample", sorted(_TRIGGERING_PAYLOADS))
def test_every_pre_tool_use_hook_uses_a_model_visible_channel(sample: str, tmp_path: Path) -> None:
    """A PreToolUse hook either blocks (exit 2, stderr reason) or prints JSON on stdout.

    Any other exit code, exit 0 with text only on stderr, or non-JSON stdout
    on either status never reaches the model. ``PostToolUse`` hooks are
    excluded: they shell out to formatters and the memory sync script.
    """
    for matcher, command in _pre_tool_use_hooks():
        result = _run_hook(command, _TRIGGERING_PAYLOADS[sample], tmp_path)
        violation = _channel_violation(result)
        assert violation is None, (
            f"PreToolUse hook (matcher={matcher!r}) on {sample!r}: {violation}"
        )


# ---------------------------------------------------------------------------
# The channel guard itself, on synthetic hooks
# ---------------------------------------------------------------------------

_PRE_TOOL_USE_JSON = json.dumps(
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "x"}}
)
_OTHER_EVENT_JSON = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse"}})

_ACCEPTED_HOOKS: dict[str, str] = {
    "silent exit 0": "exit 0",
    "PreToolUse JSON on exit 0": f"echo '{_PRE_TOOL_USE_JSON}'; exit 0",
    "stderr reason on exit 2": "echo reason >&2; exit 2",
    "stderr reason plus PreToolUse JSON on exit 2": (
        f"echo '{_PRE_TOOL_USE_JSON}'; echo reason >&2; exit 2"
    ),
}

_REJECTED_HOOKS: dict[str, str] = {
    "exit 1 with stderr": "echo oops >&2; exit 1",
    "exit 1 silent": "exit 1",
    "exit 1 with PreToolUse JSON": f"echo '{_PRE_TOOL_USE_JSON}'; exit 1",
    "exit 0 with stderr only": "echo reminder >&2; exit 0",
    "exit 0 with plain-text stdout": "echo reminder; exit 0",
    "exit 0 with JSON for another event": f"echo '{_OTHER_EVENT_JSON}'; exit 0",
    "exit 0 with JSON missing hookSpecificOutput": "echo '{\"continue\": true}'; exit 0",
    "exit 2 without a reason": "exit 2",
    "exit 2 with malformed stdout": "echo '{not json'; echo reason >&2; exit 2",
    "exit 2 with plain-text stdout": "echo reminder; echo reason >&2; exit 2",
}


@pytest.mark.parametrize("command", list(_ACCEPTED_HOOKS.values()), ids=list(_ACCEPTED_HOOKS))
def test_channel_guard_accepts_model_visible_hooks(command: str, tmp_path: Path) -> None:
    result = _run_hook(command, _TRIGGERING_PAYLOADS["gh pr create"], tmp_path)
    assert _channel_violation(result) is None


@pytest.mark.parametrize("command", list(_REJECTED_HOOKS.values()), ids=list(_REJECTED_HOOKS))
def test_channel_guard_rejects_hooks_the_model_cannot_see(command: str, tmp_path: Path) -> None:
    result = _run_hook(command, _TRIGGERING_PAYLOADS["gh pr create"], tmp_path)
    assert _channel_violation(result) is not None
