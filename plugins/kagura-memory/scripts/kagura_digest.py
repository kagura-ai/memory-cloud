"""Kagura Memory digest - turn past Claude Code transcripts into compact text.

Run by ``/kagura-memory:digest`` (``claude-skills/digest.md``) with ``python3 -I -S``,
standard library only. The model never reads a raw transcript: this script lists
the project's sessions, reduces one transcript to the user's and the assistant's
words (no tool calls, tool output or thinking), redacts secret shapes and caps
the length, and records which sessions were digested.

Subcommands (all print one JSON document on stdout):

* ``list --project-dir DIR --state FILE [--since last|all|YYYY-MM-DD]
  [--exclude-session ID]`` - this project's sessions, oldest first. ``last``
  (default) keeps sessions not digested yet, or changed since they were.
  The newest transcript is the running session and is always left out.
* ``extract FILE [--max-chars N]`` - the compact text of one transcript.
* ``mark --state FILE SESSION_ID...`` - record sessions as digested.

Reads transcripts only; never reads credentials, settings or tokens, and sends
nothing anywhere.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 9):  # noqa: UP036 - the floor guard itself must run on older interpreters
    sys.exit("kagura_digest: python 3.9+ required")
import argparse
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any

STATE_VERSION = 1
DEFAULT_MAX_CHARS = 40_000
MESSAGE_MAX_CHARS = 2_000
REDACTED = "[REDACTED]"

# Secret shapes. A match is replaced whole; the key name of an assignment is kept.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bkagura_[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)://[^/\s:@]+:[^/\s@]+@"),
]
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)[A-Z0-9_]*)"
    r"(\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}\3"
)


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def project_slug(project_dir: str) -> str:
    """Claude Code's folder name for a project: every non-alphanumeric becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(project_dir))


def projects_root() -> str:
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(config, "projects")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {"version": STATE_VERSION, "sessions": {}}
    if not isinstance(state, dict) or not isinstance(state.get("sessions"), dict):
        return {"version": STATE_VERSION, "sessions": {}}
    return state


def save_state(path: str, state: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".digest-state.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _iter_records(path: str):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _title(path: str) -> str | None:
    title = None
    for record in _iter_records(path):
        if record.get("type") == "ai-title" and isinstance(record.get("aiTitle"), str):
            title = record["aiTitle"]
    return title


def list_sessions(
    project_dir: str,
    state_path: str,
    since: str = "last",
    exclude: tuple[str, ...] = (),
    root: str | None = None,
) -> dict[str, Any]:
    folder = os.path.join(root or projects_root(), project_slug(project_dir))
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".jsonl")]
    except OSError:
        return {"project_folder": folder, "sessions": [], "skipped_running": None}
    files = sorted(
        ((os.path.getmtime(os.path.join(folder, n)), n) for n in names),
        key=lambda pair: pair[0],
    )
    running = files.pop()[1][: -len(".jsonl")] if files else None
    digested = load_state(state_path)["sessions"]
    since_ts = None
    if since not in ("last", "all"):
        since_ts = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    sessions = []
    for mtime, name in files:
        session_id = name[: -len(".jsonl")]
        if session_id in exclude:
            continue
        if since_ts is not None and mtime < since_ts:
            continue
        seen = digested.get(session_id)
        if since == "last" and isinstance(seen, dict) and seen.get("mtime", 0) >= mtime:
            continue
        path = os.path.join(folder, name)
        sessions.append(
            {
                "session_id": session_id,
                "path": path,
                "modified": _iso(mtime),
                "size_bytes": os.path.getsize(path),
                "title": _title(path),
                "digested_before": seen is not None,
            }
        )
    return {"project_folder": folder, "sessions": sessions, "skipped_running": running}


def _text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                out.append(block["text"])
    return out


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n[... {len(text) - limit} chars omitted ...]\n{text[-tail:]}"


def extract(path: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    turns: list[str] = []
    session_id = os.path.basename(path)[: -len(".jsonl")]
    first = last = None
    for record in _iter_records(path):
        role = record.get("type")
        if role not in ("user", "assistant") or record.get("isSidechain") or record.get("isMeta"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        text = "\n".join(t.strip() for t in _text_blocks(message.get("content")) if t.strip())
        # Command wrappers and system reminders are harness text, not the conversation.
        text = re.sub(
            r"<(system-reminder|command-[a-z]+|local-command-stdout)>.*?</\1>",
            "",
            text,
            flags=re.S,
        )
        text = text.strip()
        if not text:
            continue
        stamp = record.get("timestamp")
        if isinstance(stamp, str):
            first = first or stamp
            last = stamp
        turns.append(f"[{role}] {_clip(redact(text), MESSAGE_MAX_CHARS)}")
    body = "\n\n".join(turns)
    truncated = len(body) > max_chars
    return {
        "session_id": session_id,
        "first_message_at": first,
        "last_message_at": last,
        "turns": len(turns),
        "truncated": truncated,
        "text": _clip(body, max_chars),
    }


def mark(
    state_path: str,
    session_ids: list[str],
    root: str | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    state = load_state(state_path)
    folder = (
        os.path.join(root or projects_root(), project_slug(project_dir)) if project_dir else None
    )
    now = datetime.now(tz=timezone.utc).timestamp()
    for session_id in session_ids:
        mtime = now
        if folder:
            try:
                mtime = os.path.getmtime(os.path.join(folder, f"{session_id}.jsonl"))
            except OSError:
                pass
        state["sessions"][session_id] = {"digested_at": _iso(now), "mtime": mtime}
    state["version"] = STATE_VERSION
    save_state(state_path, state)
    return {"marked": session_ids, "state": state_path}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kagura_digest")
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--project-dir", required=True)
    p_list.add_argument("--state", required=True)
    p_list.add_argument("--since", default="last")
    p_list.add_argument("--exclude-session", action="append", default=[])
    p_extract = sub.add_parser("extract")
    p_extract.add_argument("path")
    p_extract.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    p_mark = sub.add_parser("mark")
    p_mark.add_argument("--state", required=True)
    p_mark.add_argument("--project-dir")
    p_mark.add_argument("session_ids", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "list":
        if args.since not in ("last", "all") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
            parser.error("--since takes last, all or YYYY-MM-DD")
        result = list_sessions(
            args.project_dir, args.state, args.since, tuple(args.exclude_session)
        )
    elif args.command == "extract":
        result = extract(args.path, args.max_chars)
    else:
        result = mark(args.state, args.session_ids, project_dir=args.project_dir)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
