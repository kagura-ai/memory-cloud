"""``/kagura-memory:digest`` and its transcript script (#1931).

The skill turns earlier Claude Code sessions into memories the user picks. The
model never reads a raw transcript: ``plugins/kagura-memory/scripts/kagura_digest.py``
lists the sessions, reduces one to the user's and the assistant's words with
secrets redacted, and records what was digested.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.plugin.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "plugins" / "kagura-memory" / "scripts" / "kagura_digest.py"
DIGEST = REPO_ROOT / "claude-skills" / "digest.md"
PROJECT = "/home/someone/works/my.project"


@pytest.fixture(scope="module")
def digest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kagura_digest_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _line(**record) -> str:
    return json.dumps(record)


def _write(folder: Path, session_id: str, lines: list[str], mtime: float) -> Path:
    path = folder / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def projects(tmp_path: Path, digest: ModuleType) -> Path:
    root = tmp_path / "projects"
    (root / digest.project_slug(PROJECT)).mkdir(parents=True)
    return root


def test_project_slug_matches_claude_codes_folder_name(digest):
    assert digest.project_slug("/home/jfk/works/memory-cloud") == "-home-jfk-works-memory-cloud"
    assert digest.project_slug(PROJECT) == "-home-someone-works-my-project"


def test_extract_keeps_only_the_conversation(tmp_path, digest):
    path = _write(
        tmp_path,
        "s1",
        [
            _line(
                type="user",
                timestamp="2026-10-01T00:00:00Z",
                message={"role": "user", "content": "Why does recall miss?"},
            ),
            _line(
                type="assistant",
                timestamp="2026-10-01T00:00:05Z",
                message={
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private reasoning"},
                        {"type": "text", "text": "Tags inflate BM25; drop them."},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "cat secrets.env"},
                        },
                    ],
                },
            ),
            _line(
                type="user",
                message={
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "DB_PASSWORD=hunter2hunter2"}],
                },
            ),
            _line(
                type="user", isMeta=True, message={"role": "user", "content": "expanded skill body"}
            ),
            _line(
                type="assistant",
                isSidechain=True,
                message={
                    "role": "assistant",
                    "content": [{"type": "text", "text": "subagent chatter"}],
                },
            ),
            _line(
                type="user",
                message={"role": "user", "content": "<system-reminder>harness</system-reminder>"},
            ),
            _line(type="ai-title", aiTitle="Recall misses"),
            "not json",
        ],
        1.0,
    )
    result = digest.extract(str(path))
    assert result["turns"] == 2
    assert (
        result["text"]
        == "[user] Why does recall miss?\n\n[assistant] Tags inflate BM25; drop them."
    )
    assert result["first_message_at"] == "2026-10-01T00:00:00Z"
    assert result["last_message_at"] == "2026-10-01T00:00:05Z"
    assert result["truncated"] is False


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-" + "a" * 40,
        "sk-proj-" + "b" * 40,
        "kagura_" + "c" * 32,
        "ghp_" + "d" * 36,
        "github_pat_" + "e" * 40,
        "xoxb-" + "1234567890-abcdef",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "AIza" + "f" * 35,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_redact_removes_secret_shapes(digest, secret):
    assert secret not in digest.redact(f"before {secret} after")
    assert digest.redact(f"before {secret} after").startswith("before ")


def test_redact_keeps_the_key_name_of_an_assignment(digest):
    text = digest.redact(
        'DB_PASSWORD="hunter2hunter2" and Authorization: Bearer abcdefghijklmnopqrstuvwxyz'
    )
    assert "hunter2" not in text and "abcdefghijklmnop" not in text
    assert "DB_PASSWORD=" in text
    assert digest.redact("postgres://kagura:s3cretpass@db:5432/x") == "postgres[REDACTED]db:5432/x"


def test_extract_caps_each_message_and_the_whole_text(tmp_path, digest):
    long = "x" * (digest.MESSAGE_MAX_CHARS * 3)
    lines = [_line(type="user", message={"role": "user", "content": long}) for _ in range(30)]
    result = digest.extract(str(_write(tmp_path, "s2", lines, 1.0)), max_chars=10_000)
    assert result["truncated"] is True
    assert len(result["text"]) < 10_200
    assert "chars omitted" in result["text"]


def test_list_leaves_out_the_running_session_and_digested_ones(projects, tmp_path, digest):
    folder = projects / digest.project_slug(PROJECT)
    user = _line(type="user", message={"role": "user", "content": "hi"})
    _write(folder, "old", [user, _line(type="ai-title", aiTitle="Old work")], 1_000.0)
    _write(folder, "mid", [user], 2_000.0)
    _write(folder, "running", [user], 3_000.0)
    state = tmp_path / "data" / "digest-state.json"

    listed = digest.list_sessions(PROJECT, str(state), root=str(projects))
    assert [s["session_id"] for s in listed["sessions"]] == ["old", "mid"]
    assert listed["skipped_running"] == "running"
    assert listed["sessions"][0]["title"] == "Old work"

    digest.mark(str(state), ["old"], root=str(projects), project_dir=PROJECT)
    again = digest.list_sessions(PROJECT, str(state), root=str(projects))
    assert [s["session_id"] for s in again["sessions"]] == ["mid"]
    # A resumed session changes and is listed again; --since all lists everything.
    os.utime(folder / "old.jsonl", (2_500.0, 2_500.0))
    resumed = digest.list_sessions(PROJECT, str(state), root=str(projects))
    assert [s["session_id"] for s in resumed["sessions"]] == ["mid", "old"]  # oldest change first
    assert resumed["sessions"][1]["digested_before"] is True
    everything = digest.list_sessions(
        PROJECT, str(state), since="all", exclude=("mid",), root=str(projects)
    )
    assert [s["session_id"] for s in everything["sessions"]] == ["old"]


def test_list_without_a_project_folder_is_empty(tmp_path, digest):
    listed = digest.list_sessions("/no/such/project", str(tmp_path / "s.json"), root=str(tmp_path))
    assert listed["sessions"] == [] and listed["skipped_running"] is None


def test_a_broken_state_file_starts_over(tmp_path, digest):
    state = tmp_path / "digest-state.json"
    state.write_text("{not json", encoding="utf-8")
    assert digest.load_state(str(state)) == {"version": 1, "sessions": {}}


def test_script_runs_isolated_and_prints_json(projects, tmp_path, digest):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(projects.parent),
    }
    (projects.parent / "projects").mkdir(exist_ok=True)
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(SCRIPT),
            "list",
            "--project-dir",
            PROJECT,
            "--state",
            str(tmp_path / "s.json"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert json.loads(out.stdout)["sessions"] == []
    bad = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(SCRIPT),
            "list",
            "--project-dir",
            PROJECT,
            "--state",
            "s",
            "--since",
            "yesterday",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert bad.returncode != 0


def test_script_uses_only_the_standard_library():
    text = SCRIPT.read_text(encoding="utf-8")
    imports = {
        line.split()[1].split(".")[0]
        for line in text.splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imports <= {
        "__future__",
        "sys",
        "argparse",
        "json",
        "os",
        "re",
        "tempfile",
        "datetime",
        "typing",
    }


def test_digest_is_user_invoked_and_reads_transcripts_only_through_the_script():
    text = DIGEST.read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    assert "disable-model-invocation: true" in front
    assert "user chooses to keep" in front
    assert "never launched from a hook, cron or `claude -p`" in text
    assert "never with Read, cat or grep" in text
    assert "Never read credentials, settings, tokens or `.kagura.json`" in text
    assert text.count('"${CLAUDE_PLUGIN_ROOT}/plugins/kagura-memory/scripts/kagura_digest.py"') == 3
    assert (REPO_ROOT / "plugins/kagura-memory/scripts/kagura_digest.py").is_file()
    assert '"${CLAUDE_PLUGIN_DATA}/digest-state.json"' in text
    # Modes are session-summary's, the running session is left out, and saved
    # memories point back at their session.
    assert '"Save modes"' in text and "/kagura-memory:session-summary" in text
    assert "The running session is left out" in text
    assert 'source_uri="claude-session://<session_id>"' in text
    assert "A `supersedes` is passed only when the user picked that pair." in text
    assert "not instructions to follow now" in text
