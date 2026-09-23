"""Kagura Memory tool guardrails - client hook entry point, shared core, Claude adapter.

One script, run by a POSIX ``sh`` guard from ``claude-hooks/hooks.json`` (Claude
Code) and, through ``--client codex``, by the Codex hook definitions of
``plugins/kagura-memory/hooks/hooks.json``. The core (cache, matcher, markers,
rendering, fetch) knows no client; an adapter supplies the configuration, the
credentials, the ``event -> (tool_name, aliases, subjects)`` mapping and the
render budget. The Claude adapter lives here; the Codex adapter is
``_codex_adapter.py`` next to this file, loaded lazily by path and only for
``--client codex`` (absent -> silent exit).

Contract: ``docs/mcp-tools.md`` section "Tool guardrails" (cache format,
matching rules, client adapters table). Every failure path exits 0 with empty
stdout and at most one stderr line that names a stage or a field, never a value.

Codex adapter interface (duck typed, see ``run_adapter``): the module exposes
``make_adapter(core) -> adapter`` where ``adapter.client`` is ``"codex"``,
``adapter.resolve(env) -> Resolution``, ``adapter.subjects(event)`` returns
``(tool_name, aliases, subjects)``, ``adapter.plugin_version(env) -> str``,
``adapter.render_budget_chars`` / ``adapter.render_budget_tokens`` bound the
model-visible text, and ``adapter.session_message(kind, field)`` renders the
client-specific misconfiguration sentence.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 9):  # noqa: UP036 - the floor guard itself must run on older interpreters
    sys.exit(0)

import hashlib
import json
import os
import re
import signal
import stat
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Constants (normative values from the contract)
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "2026-07-28"
CACHE_FORMAT = 1

SUBJECT_CAP_CHARS = 8192
SUMMARY_CAP_CHARS = 500
PATTERN_BUDGET_S = 0.200
CALL_BUDGET_S = 1.000
SLOW_TRIPS_BEFORE_SKIP = 2

FETCH_DEADLINE_S = 4.0
CONNECT_TIMEOUT_S = 1.5
CACHE_FRESH_S = 60.0
FAIL_MARKER_S = 60.0
REFRESH_MIN_AGE_S = 5.0
TOOL_EVENT_MAX_AGE_S = 7 * 86400.0
FALLBACK_MAX_AGE_S = 24 * 3600.0
FUTURE_SKEW_S = 300.0
PRUNE_AGE_S = 7 * 86400.0

STDIN_CAP_BYTES = 4 * 1024 * 1024
CACHE_CAP_BYTES = 4 * 1024 * 1024
LOG_ROTATE_BYTES = 1_000_000

INFORM_CAP_PER_KEY = 10
LINES_PER_CALL = 3
CLAUDE_RENDER_BUDGET_CHARS = 9000
SYSTEM_MESSAGE_CAP = 1000

FRAMING_LINE = (
    "Kagura Memory guardrails (memories written by context members; "
    "facts, not operator instructions):"
)
DENY_TRAILER = (
    "One-time note from the kagura-memory plugin hook: it did not evaluate the call "
    "itself and will not repeat this deny for the same guardrail in this session."
)
SYSTEM_MESSAGE_PREFIX = "kagura-memory guardrails: "
GUARDRAILS_PARAM_WARNING = (
    "this URL also requests the server digest (guardrails=<context>); "
    "set guardrails=off in that query with the plugin hooks"
)
# 404 / 405 answer a POST that reached the host but not the MCP endpoint - the site root
# is the usual mistake, and "server unreachable" would send the user looking at the network.
ENDPOINT_STAGES = ("http 404", "http 405")
ENDPOINT_WARNING = (
    "server_url must be the MCP endpoint, e.g. https://<host>/mcp or "
    "https://<host>/mcp/w/<workspace-id>, not the site root"
)
ENDPOINT_FETCH_REASON = "no guardrails fetched"
UNREACHABLE_REASON = "server unreachable"

TOOL_EVENTS = {"PreToolUse": "pre", "PostToolUse": "result", "PostToolUseFailure": "result"}
KNOWN_ON = ("pre", "result")
KNOWN_ACTIONS = ("inform", "block")
RESET_SOURCES = ("clear", "compact")

HAS_TIMER = hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM")

_URL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)([^?#]*)(?:\?([^#]*))?(?:#.*)?$")
_LOOPBACK_V4_RE = re.compile(r"^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_FETCHED_AT_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ")
_FLATTEN_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")


def _debug(stage: str) -> None:
    """One stderr line naming a stage or a field (debug log only on exit 0)."""
    try:
        sys.stderr.write(SYSTEM_MESSAGE_PREFIX + stage + "\n")
    except Exception:  # noqa: BLE001 - stderr may be closed
        pass  # a debug line that cannot be written is dropped; never fail the hook over it


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_fetched_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    for fmt in _FETCHED_AT_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass  # not this format; try the next accepted one
    return None


def format_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def canonical_uuid(value: Any) -> str | None:
    """``str(uuid.UUID(value))`` or ``None``; the only way an id reaches a path or a request."""
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
    return str(parsed) if str(parsed) == value.lower() else None


def sha16(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def approx_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def normalize_subject(subject: str) -> str:
    return subject.replace("\r\n", "\n").rstrip("\n\r")[:SUBJECT_CAP_CHARS]


def result_subject(value: Any) -> str:
    """The ``on: result`` subject: a string as is, else every string leaf joined by newlines."""
    if isinstance(value, str):
        return value
    leaves: list[str] = []
    _collect_string_leaves(value, leaves, 0)
    text = "\n".join(leaves)
    if isinstance(value, dict) and value.get("isError") is True:
        text += "\nisError=true"
    return text


def _collect_string_leaves(value: Any, out: list[str], depth: int) -> None:
    if depth > 64:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key in sorted(value, key=str):
            _collect_string_leaves(value[key], out, depth + 1)
    elif isinstance(value, list):
        for entry in value:
            _collect_string_leaves(entry, out, depth + 1)


def flatten_summary(summary: Any) -> str:
    """Flatten a summary for one output line (same rule as the server digest)."""
    if not isinstance(summary, str):
        return ""
    chars = [" " if unicodedata.category(ch) in _FLATTEN_CATEGORIES else ch for ch in summary]
    text = " ".join("".join(chars).split())
    text = text.replace("<!--", "<!- -").replace("-->", "- ->")
    if len(text) <= SUMMARY_CAP_CHARS:
        return text
    cut = text.rfind(" ", 0, SUMMARY_CAP_CHARS)
    if cut <= 0:
        cut = SUMMARY_CAP_CHARS - 1
    return text[:cut].rstrip() + "…"


def id8(memory_id: str) -> str:
    return memory_id[:8]


def render_line(item: dict[str, Any]) -> str:
    memory_id = str(item.get("memory_id", ""))
    label = id8(memory_id)
    if item.get("authored_by_caller") is False:
        label += ", by another member"
    return f"Kagura Memory guardrail ({label}): {flatten_summary(item.get('summary'))}"


# ---------------------------------------------------------------------------
# URL rules
# ---------------------------------------------------------------------------


class UrlParts:
    __slots__ = ("scheme", "netloc", "host", "path", "query")

    def __init__(self, scheme: str, netloc: str, host: str, path: str, query: str) -> None:
        self.scheme = scheme
        self.netloc = netloc
        self.host = host
        self.path = path
        self.query = query


def parse_server_url(url: Any) -> UrlParts | None:
    """Split an endpoint URL; ``None`` unless https, or http on a loopback host."""
    if not isinstance(url, str):
        return None
    found = _URL_RE.match(url.strip())
    if not found:
        return None
    scheme = found.group(1).lower()
    netloc = found.group(2)
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:
            return None
        host = netloc[1:end]
    else:
        host = netloc.split(":", 1)[0]
    host = host.lower()
    if not host or any(ch in netloc for ch in "\r\n\t "):
        return None
    if scheme == "https":
        pass
    elif scheme == "http":
        if not is_loopback_host(host):
            return None
    else:
        return None
    return UrlParts(scheme, netloc, host, found.group(3) or "/", found.group(4) or "")


def is_loopback_host(host: str) -> bool:
    return host == "localhost" or host == "::1" or bool(_LOOPBACK_V4_RE.match(host))


def guardrails_param_value(query: str) -> str | None:
    """Value of the first ``guardrails=`` query parameter, or ``None`` when absent."""
    for pair in query.split("&"):
        key, _sep, value = pair.partition("=")
        if key == "guardrails":
            return value
    return None


def url_requests_server_digest(query: str) -> bool:
    value = guardrails_param_value(query)
    return value is not None and value.strip().lower() != "off"


# ---------------------------------------------------------------------------
# Configuration shared by the adapters
# ---------------------------------------------------------------------------


class Config:
    """What an adapter resolved: endpoint, header value, context, strongest action, data dir."""

    __slots__ = ("server_url", "url", "authorization", "context_id", "max_action", "data_dir")

    def __init__(
        self,
        server_url: str,
        url: UrlParts,
        authorization: str,
        context_id: str,
        max_action: str,
        data_dir: str,
    ) -> None:
        self.server_url = server_url
        self.url = url
        self.authorization = authorization
        self.context_id = context_id
        self.max_action = max_action
        self.data_dir = data_dir

    @property
    def guardrails_dir(self) -> str:
        return os.path.join(self.data_dir, "guardrails")

    @property
    def cache_path(self) -> str:
        return os.path.join(self.guardrails_dir, self.context_id + ".json")


class Resolution:
    """Outcome of ``adapter.resolve``: ``config`` or the reasons it is missing."""

    __slots__ = ("config", "configured", "bad_fields", "warnings")

    def __init__(
        self,
        config: Config | None,
        configured: bool,
        bad_fields: list[str],
        warnings: list[str],
    ) -> None:
        self.config = config
        self.configured = configured
        self.bad_fields = bad_fields
        self.warnings = warnings


def normalize_max_action(value: Any) -> tuple[str, bool]:
    """``(max_action, valid)``: missing -> block; unknown -> inform + invalid flag."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "block", True
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in KNOWN_ACTIONS:
            return folded, True
    return "inform", False


# ---------------------------------------------------------------------------
# Claude Code adapter
# ---------------------------------------------------------------------------


class ClaudeAdapter:
    client = "claude"
    render_budget_chars: int | None = CLAUDE_RENDER_BUDGET_CHARS
    render_budget_tokens: int | None = None

    def resolve(self, env: Any) -> Resolution:
        server_url = env.get("CLAUDE_PLUGIN_OPTION_SERVER_URL")
        api_key = env.get("CLAUDE_PLUGIN_OPTION_API_KEY")
        context_raw = env.get("CLAUDE_PLUGIN_OPTION_CONTEXT_ID")
        max_action_raw = env.get("CLAUDE_PLUGIN_OPTION_MAX_ACTION")
        data_dir = env.get("CLAUDE_PLUGIN_DATA")

        # "Configured" means the user supplied at least one of the three fields
        # without a default; max_action has one, so Claude Code may export it on
        # its own and it must not turn an untouched plugin into a misconfigured one.
        present = [v for v in (server_url, api_key, context_raw) if v]
        if not present:
            return Resolution(None, False, [], [])
        if not data_dir:
            return Resolution(None, True, [], [])

        bad: list[str] = []
        warnings: list[str] = []
        url = parse_server_url(server_url)
        if url is None:
            bad.append("server_url")
        elif url_requests_server_digest(url.query):
            warnings.append(GUARDRAILS_PARAM_WARNING)
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or "\r" in api_key
            or "\n" in api_key
        ):
            bad.append("api_key")
        context_id = canonical_uuid(context_raw.strip() if isinstance(context_raw, str) else None)
        if context_id is None:
            bad.append("context_id")
        max_action, valid_action = normalize_max_action(max_action_raw)
        if not valid_action:
            warnings.append("max_action is not block or inform; using inform")
        if bad or url is None or context_id is None or not isinstance(api_key, str):
            return Resolution(None, True, bad, warnings)
        config = Config(
            server_url=str(server_url).strip(),
            url=url,
            authorization="Bearer " + api_key.strip(),
            context_id=context_id,
            max_action=max_action,
            data_dir=str(data_dir),
        )
        return Resolution(config, True, [], warnings)

    def plugin_version(self, env: Any) -> str:
        root = env.get("CLAUDE_PLUGIN_ROOT")
        if not root:
            return "unknown"
        try:
            with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
                version = json.load(fh).get("version")
        except (OSError, ValueError, AttributeError):
            return "unknown"
        return version if isinstance(version, str) and version else "unknown"

    def session_message(self, kind: str, fields: list[str]) -> str:
        if kind == "misconfigured":
            names = ", ".join(fields)
            return f"{names} is missing or invalid — configure the plugin (/plugin → kagura-memory)"
        return kind

    def subjects(self, event: dict[str, Any]) -> tuple[str | None, list[str], list[str]]:
        name = event.get("tool_name")
        if not isinstance(name, str):
            return None, [], []
        hook_event = event.get("hook_event_name")
        if hook_event == "PreToolUse":
            tool_input = event.get("tool_input")
            return name, [], claude_pre_subjects(name, tool_input)
        if hook_event == "PostToolUse":
            return name, [], [result_subject(event.get("tool_response"))]
        if hook_event == "PostToolUseFailure":
            return name, [], [result_subject(event.get("error"))]
        return name, [], []


def claude_pre_subjects(tool_name: str, tool_input: Any) -> list[str]:
    inputs = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in ("Bash", "PowerShell"):
        command = inputs.get("command")
        return [command] if isinstance(command, str) else []
    if tool_name in ("Write", "Edit", "Read"):
        path = inputs.get("file_path")
        return [path.replace("\\", "/")] if isinstance(path, str) else []
    if tool_name == "NotebookEdit":
        path = inputs.get("notebook_path")
        return [path.replace("\\", "/")] if isinstance(path, str) else []
    return [compact_json(tool_input)]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class LoadedCache:
    __slots__ = ("data", "fetched_at", "age_seconds")

    def __init__(self, data: dict[str, Any], fetched_at: datetime, age_seconds: float) -> None:
        self.data = data
        self.fetched_at = fetched_at
        self.age_seconds = age_seconds

    @property
    def tool_triggered(self) -> list[dict[str, Any]]:
        items = self.data.get("tool_triggered")
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _stat_is_private(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size > CACHE_CAP_BYTES:
        return False
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return False
    if os.name == "posix" and (st.st_mode & 0o077):
        return False
    return True


def load_cache(
    path: str, context_id: str, now: datetime, max_age_s: float | None
) -> LoadedCache | None:
    """Read and validate the cache; ``None`` means "absent" (fail open)."""
    if not _stat_is_private(path):
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read(CACHE_CAP_BYTES + 1)
        if len(raw) > CACHE_CAP_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    fmt = data.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt < 1 or fmt > CACHE_FORMAT:
        return None
    if data.get("context_id") != context_id:
        return None
    fetched_at = parse_fetched_at(data.get("fetched_at"))
    if fetched_at is None:
        return None
    age = (now - fetched_at).total_seconds()
    if age < -FUTURE_SKEW_S:
        return None
    if max_age_s is not None and age > max_age_s:
        return None
    return LoadedCache(data, fetched_at, age)


def _private_dir(path: str) -> bool:
    """``path`` is a directory this user owns, reached without a symlink, with mode 0700.

    A looser mode on a directory we own is tightened through a directory file
    descriptor (``O_NOFOLLOW`` + ``fchmod``), so the chmod cannot be redirected
    by swapping the entry for a symlink in between. Anything else - a symlink, a
    regular file, another owner, a chmod that fails - is refused and the caller
    fails open. Mode and owner checks are POSIX only.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if os.name != "posix":
        return True
    if st.st_uid != os.getuid():
        return False
    if not (st.st_mode & 0o077):
        return True
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return False
    try:
        current = os.fstat(fd)
        if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.getuid():
            return False
        os.fchmod(fd, 0o700)
        return not (os.fstat(fd).st_mode & 0o077)
    except OSError:
        return False
    finally:
        os.close(fd)


def ensure_dir(path: str) -> bool:
    """Create ``path`` (and missing parents) with mode 0700.

    ``True`` only when ``path`` is, or has just become, a private directory as
    ``_private_dir`` defines it; a pre-existing entry that cannot be brought to
    that state is refused (``False``) and the caller fails open.
    """
    if os.path.lexists(path):
        return _private_dir(path)
    parent = os.path.dirname(path)
    if parent and parent != path and not os.path.lexists(parent) and not ensure_dir(parent):
        return False
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return _private_dir(path)
    except OSError:
        return False
    return True


def write_private_file(path: str, payload: bytes) -> bool:
    """Write ``payload`` to ``<path>.tmp-<pid>`` (0600) and ``os.replace`` it into place."""
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # best-effort cleanup of the temp file; the write already failed
        return False
    return True


def cache_bytes(cache: dict[str, Any]) -> bytes:
    return (
        json.dumps(cache, ensure_ascii=False, separators=(",", ":"), sort_keys=False) + "\n"
    ).encode("utf-8")


def _project_item(entry: Any, with_trigger: bool) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("memory_id"), str):
        return None
    out: dict[str, Any] = {
        "memory_id": entry["memory_id"],
        "summary": entry.get("summary"),
        "importance": entry.get("importance"),
    }
    if isinstance(entry.get("authored_by_caller"), bool):
        out["authored_by_caller"] = entry["authored_by_caller"]
    if with_trigger:
        out["tool_trigger"] = entry.get("tool_trigger")
    return out


def project_response(
    result: dict[str, Any], context_id: str, fetched_at: datetime
) -> dict[str, Any]:
    """Project a ``load_guardrails`` result onto the format-1 cache shape."""
    pinned_src = result.get("pinned")
    triggered_src = result.get("tool_triggered")
    pinned = [p for p in (_project_item(e, False) for e in (pinned_src or [])) if p]
    triggered = [t for t in (_project_item(e, True) for e in (triggered_src or [])) if t]
    return {
        "format": CACHE_FORMAT,
        "context_id": context_id,
        "fetched_at": iso_z(fetched_at),
        "version": result.get("version"),
        "pinned": pinned,
        "tool_triggered": triggered,
    }


# ---------------------------------------------------------------------------
# Items and matching
# ---------------------------------------------------------------------------


class Trigger:
    __slots__ = ("memory_id", "tool", "on", "match", "action")

    def __init__(self, memory_id: str, tool: str, on: str, match: str | None, action: str) -> None:
        self.memory_id = memory_id
        self.tool = tool
        self.on = on
        self.match = match
        self.action = action


def valid_trigger(item: Any) -> Trigger | None:
    """Apply the skip rules (never fail the hook); ``None`` skips the item."""
    if not isinstance(item, dict):
        return None
    memory_id = canonical_uuid(item.get("memory_id"))
    if memory_id is None:
        return None
    trigger = item.get("tool_trigger")
    if not isinstance(trigger, dict):
        return None
    tool = trigger.get("tool")
    on = trigger.get("on", "pre")
    match = trigger.get("match")
    action = trigger.get("action", "inform")
    if not isinstance(tool, str) or not tool:
        return None
    if on not in KNOWN_ON or action not in KNOWN_ACTIONS:
        return None
    if match is not None and not isinstance(match, str):
        return None
    return Trigger(memory_id, tool, on, match, action)


def compile_pattern(pattern: str) -> re.Pattern[str] | None:
    flags = re.ASCII
    if pattern.startswith("(?i)"):
        pattern = pattern[4:]
        flags |= re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except (re.error, RecursionError, OverflowError, TypeError, ValueError):
        return None


def item_is_compilable(item: Any) -> bool:
    trigger = valid_trigger(item)
    if trigger is None or compile_pattern(trigger.tool) is None:
        return False
    return trigger.match is None or compile_pattern(trigger.match) is not None


class PatternBudget(Exception):
    """Raised by the SIGALRM handler when one pattern exceeds its budget."""


def _on_alarm(_signum: int, _frame: Any) -> None:
    raise PatternBudget()


def run_with_budget(fn: Callable[[], Any], seconds: float) -> Any:
    """Run ``fn`` under an ITIMER_REAL budget; the timer is always cleared."""
    if not HAS_TIMER:
        return fn()
    try:
        previous = signal.signal(signal.SIGALRM, _on_alarm)
    except ValueError:  # not the main thread (tests only)
        return fn()
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class State:
    """Per-session marker directory: ``state/<sid16>/<main|agent16>/<lane>/<memory_id>``."""

    def __init__(self, guardrails_dir: str, session_id: str, agent_id: str | None) -> None:
        self.sid16 = sha16(session_id)
        self.key = sha16(agent_id) if isinstance(agent_id, str) and agent_id else "main"
        self.guardrails_dir = guardrails_dir
        self.state_dir = os.path.join(guardrails_dir, "state")
        self.session_dir = os.path.join(self.state_dir, self.sid16)
        self.key_dir = os.path.join(self.session_dir, self.key)
        self.slow_dir = os.path.join(self.session_dir, "slow")

    def marker_path(self, lane: str, memory_id: str) -> str:
        return os.path.join(self.key_dir, lane, memory_id)

    def delivered(self, memory_id: str) -> bool:
        return os.path.exists(self.marker_path("block", memory_id)) or os.path.exists(
            self.marker_path("inform", memory_id)
        )

    def _ensure_chain(self, *leaves: str) -> bool:
        """Every directory we own on the way down is private, not only the leaf.

        Tool events never pass ``guardrails/`` or ``state/`` to ``ensure_dir`` on
        their own, and a loose or symlinked directory above the markers would
        expose (or redirect) everything below it.
        """
        for directory in (self.guardrails_dir, self.state_dir, self.session_dir) + leaves:
            if not ensure_dir(directory):
                return False
        return True

    def take(self, lane: str, memory_id: str) -> bool:
        lane_dir = os.path.join(self.key_dir, lane)
        if not self._ensure_chain(self.key_dir, lane_dir):
            return False
        return _create_exclusive(os.path.join(lane_dir, memory_id))

    def inform_count(self) -> int:
        try:
            return len(os.listdir(os.path.join(self.key_dir, "inform")))
        except OSError:
            return 0

    def is_slow(self, memory_id: str) -> bool:
        return os.path.exists(os.path.join(self.slow_dir, memory_id))

    def record_slow(self, memory_id: str) -> None:
        if not self._ensure_chain(self.slow_dir):
            return
        first = os.path.join(self.slow_dir, memory_id + ".1")
        if os.path.exists(first):
            _create_exclusive(os.path.join(self.slow_dir, memory_id))
        else:
            _create_exclusive(first)

    def slow_ids(self) -> list[str]:
        try:
            names = os.listdir(self.slow_dir)
        except OSError:
            return []
        return sorted(n for n in names if not n.endswith(".1"))

    def reset_main(self) -> None:
        import shutil

        shutil.rmtree(os.path.join(self.session_dir, "main"), ignore_errors=True)


def _create_exclusive(path: str) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    os.close(fd)
    return True


def _try_lock(fd: int) -> bool:
    """Exclusive non-blocking advisory lock on ``fd``; ``False`` when held elsewhere or unsupported."""
    try:
        import fcntl
    except ImportError:  # no flock on this platform (win32)
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def match_items(
    items: list[dict[str, Any]],
    on: str,
    tool_name: str,
    aliases: list[str],
    subjects: list[str],
    state: State | None,
    deadline: float,
) -> list[dict[str, Any]]:
    """Cache-order matches for ``on``; two-phase compile; per-pattern and per-call budgets."""
    names = [tool_name] + [a for a in aliases if isinstance(a, str)]
    normalized = [normalize_subject(s) for s in subjects if isinstance(s, str)]
    matched: list[dict[str, Any]] = []
    for item in items:
        if time.monotonic() > deadline:
            break
        trigger = valid_trigger(item)
        if trigger is None or trigger.on != on:
            continue
        if state is not None and state.is_slow(trigger.memory_id):
            continue
        tool_re = compile_pattern(trigger.tool)
        if tool_re is None:
            continue
        try:
            hit = any(
                run_with_budget(lambda n=n, r=tool_re: r.fullmatch(n), PATTERN_BUDGET_S)
                for n in names
            )
        except PatternBudget:
            if state is not None:
                state.record_slow(trigger.memory_id)
            continue
        if not hit:
            continue
        if trigger.match is None:
            matched.append(item)
            continue
        match_re = compile_pattern(trigger.match)
        if match_re is None:
            continue
        try:
            found = any(
                run_with_budget(lambda s=s, r=match_re: r.search(s), PATTERN_BUDGET_S)
                for s in normalized
            )
        except PatternBudget:
            if state is not None:
                state.record_slow(trigger.memory_id)
            continue
        if found:
            matched.append(item)
    return matched


def render_text(lines: list[str], deny: bool) -> str:
    """The model-visible text: framing line, guardrail lines, deny trailer when denying."""
    return "\n".join([FRAMING_LINE] + lines + ([DENY_TRAILER] if deny else []))


def within_budget(text: str, budget_chars: int | None, budget_tokens: int | None) -> bool:
    if budget_chars is not None and len(text) > budget_chars:
        return False
    return budget_tokens is None or approx_tokens(text) <= budget_tokens


def select_candidates(
    matched: list[dict[str, Any]],
    on: str,
    max_action: str,
    state: State,
    budget_chars: int | None = None,
    budget_tokens: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Decide what this call delivers, then take the markers - in that order.

    Live blocks first (cache order), then informs up to the per-call and per-key
    caps. The client's render budget is applied while selecting, before any
    marker is taken: a candidate whose line would push the output over the
    budget is not marked and stays live for a later matching call (a block cut
    this way denies the re-issued call once more). The alternative - marking
    it and letting the renderer drop the line - would lose a guardrail the
    model never saw for the whole session. A candidate whose ``O_EXCL`` marker
    fails (race lost) is dropped and the next one is tried, so nothing is
    printed without a marker and nothing is marked without being printed.
    """
    blocks: list[dict[str, Any]] = []
    informs: list[dict[str, Any]] = []
    for raw in matched:
        trigger = valid_trigger(raw)
        if trigger is None or state.delivered(trigger.memory_id):
            continue
        # Markers, the log and the rendered id8 all use the canonical (lower-case)
        # UUID, so a differently cased id in the cache cannot bypass once-per-key.
        item = dict(raw, memory_id=trigger.memory_id)
        if on == "pre" and trigger.action == "block" and max_action == "block":
            blocks.append(item)
        else:
            informs.append(item)
    inform_count = state.inform_count()
    lines: list[str] = []
    taken_blocks: list[dict[str, Any]] = []
    for item in blocks:
        candidate = lines + [render_line(item)]
        if not within_budget(render_text(candidate, True), budget_chars, budget_tokens):
            break
        if state.take("block", str(item["memory_id"])):
            taken_blocks.append(item)
            lines = candidate
    deny = bool(taken_blocks)
    room = min(LINES_PER_CALL - len(taken_blocks), INFORM_CAP_PER_KEY - inform_count)
    taken_informs: list[dict[str, Any]] = []
    for item in informs:
        if len(taken_informs) >= room:
            break
        candidate = lines + [render_line(item)]
        if not within_budget(render_text(candidate, deny), budget_chars, budget_tokens):
            break
        if state.take("inform", str(item["memory_id"])):
            taken_informs.append(item)
            lines = candidate
    return taken_blocks, taken_informs


def build_hook_output(
    event_name: str,
    blocks: list[dict[str, Any]],
    informs: list[dict[str, Any]],
    budget_chars: int | None,
    budget_tokens: int | None,
) -> dict[str, Any]:
    lines = [render_line(i) for i in blocks] + [render_line(i) for i in informs]
    deny = bool(blocks)
    # ``select_candidates`` already fitted the taken candidates to the budget; this
    # trim is the last line of defence only and drops nothing in normal operation.
    text = render_text(lines, deny)
    while lines and not within_budget(text, budget_chars, budget_tokens):
        lines.pop()
        text = render_text(lines, deny)
    specific: dict[str, Any] = {"hookEventName": event_name}
    if deny:
        specific["permissionDecision"] = "deny"
        specific["permissionDecisionReason"] = text
    else:
        specific["additionalContext"] = text
    return {"hookSpecificOutput": specific}


def append_delivery_log(
    guardrails_dir: str, client: str, event: str, state: State, memory_id: str, lane: str
) -> None:
    path = os.path.join(guardrails_dir, "deliveries.log")
    line = (
        f"ts={iso_z(now_utc())} client={client} event={event} sid={state.sid16} "
        f"key={state.key} id={id8(memory_id)} lane={lane}\n"
    )
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_ROTATE_BYTES:
            os.replace(path, path + ".1")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("ascii", "replace"))
        finally:
            os.close(fd)
    except OSError:
        pass  # the log is diagnostic only; a failed append must not fail the delivery


def prune_state(guardrails_dir: str, now_ts: float) -> None:
    import shutil

    state_dir = os.path.join(guardrails_dir, "state")
    try:
        names = os.listdir(state_dir)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(state_dir, name)
        try:
            if os.path.isdir(path) and now_ts - os.stat(path).st_mtime > PRUNE_AGE_S:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass  # best-effort maintenance: an entry that vanished or cannot be read is skipped
    try:
        for name in os.listdir(guardrails_dir):
            if name.endswith(".stale"):
                path = os.path.join(guardrails_dir, name)
                if now_ts - os.stat(path).st_mtime > PRUNE_AGE_S:
                    os.unlink(path)
    except OSError:
        pass  # best-effort maintenance: pruning .stale files must not fail SessionStart


# ---------------------------------------------------------------------------
# Fetch - one stateless tools/call, no redirects
# ---------------------------------------------------------------------------


def fetch_guardrails(
    url: UrlParts, authorization: str, context_id: str, user_agent: str, deadline_s: float
) -> tuple[dict[str, Any] | None, str]:
    """POST one ``tools/call load_guardrails``; ``(result, "ok")`` or ``(None, <stage>)``.

    ``http.client`` never follows a redirect, so any 3xx is a plain non-200
    failure and ``Location`` is never read. Nothing about the request or the
    response is ever printed.
    """
    import http.client
    import socket
    import ssl

    started = time.monotonic()
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "load_guardrails",
                "arguments": {"context_id": context_id},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
    ).encode("utf-8")
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "load_guardrails",
        "User-Agent": user_agent,
    }
    connect_timeout = min(CONNECT_TIMEOUT_S, deadline_s)
    try:
        if url.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                url.netloc, timeout=connect_timeout, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(url.netloc, timeout=connect_timeout)
    except (ValueError, OSError, http.client.HTTPException):
        return None, "connect"
    raw = b""
    try:
        conn.connect()
        remaining = deadline_s - (time.monotonic() - started)
        if remaining <= 0 or conn.sock is None:
            return None, "connect"
        conn.sock.settimeout(remaining)
        target = url.path + ("?" + url.query if url.query else "")
        conn.request("POST", target, body=body, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            return None, f"http {response.status}"
        while True:
            remaining = deadline_s - (time.monotonic() - started)
            if remaining <= 0:
                return None, "connect"
            conn.sock.settimeout(remaining)
            chunk = response.read(65536)
            if not chunk:
                break
            raw += chunk
            if len(raw) > CACHE_CAP_BYTES:
                return None, "http body"
    except (OSError, socket.timeout, http.client.HTTPException, ValueError):
        return None, "connect"
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing a half-open socket may raise anything
            pass  # the outcome was decided above; a close failure changes nothing
    return parse_tools_call_envelope(raw)


def parse_tools_call_envelope(raw: bytes) -> tuple[dict[str, Any] | None, str]:
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "jsonrpc parse"
    if not isinstance(envelope, dict):
        return None, "jsonrpc parse"
    if "error" in envelope or not isinstance(envelope.get("result"), dict):
        return None, "jsonrpc error"
    result = envelope["result"]
    if result.get("isError") is True:
        return None, "isError"
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return None, "content"
    try:
        payload = json.loads(content[0].get("text", ""))
    except (TypeError, ValueError):
        return None, "content"
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None, "status"
    fmt = payload.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt < 1 or fmt > CACHE_FORMAT:
        return None, "format"
    return payload, "ok"


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _read_stdin(stdin: Any) -> str | None:
    try:
        buffer = getattr(stdin, "buffer", None)
        if buffer is not None:
            raw = buffer.read(STDIN_CAP_BYTES + 1)
            if raw is None or len(raw) > STDIN_CAP_BYTES:
                return None
            return raw.decode("utf-8", "replace")
        text = stdin.read(STDIN_CAP_BYTES + 1)
        if text is None or len(text) > STDIN_CAP_BYTES:
            return None
        return text
    except (OSError, ValueError):
        return None


def _emit(stdout: Any, obj: dict[str, Any]) -> None:
    stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    try:
        stdout.flush()
    except (OSError, ValueError):
        pass  # the reader closed the pipe or the stream is detached; the write already happened


def _diff_messages(old: LoadedCache | None, new: dict[str, Any]) -> list[str]:
    def by_id(items: list[Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for entry in items:
            if isinstance(entry, dict) and isinstance(entry.get("memory_id"), str):
                out[entry["memory_id"]] = entry
        return out

    new_items = by_id(new.get("tool_triggered") or [])
    old_items = by_id(old.tool_triggered) if old is not None else {}
    added = [new_items[k] for k in new_items if k not in old_items]
    removed = [old_items[k] for k in old_items if k not in new_items]
    changed = [
        new_items[k]
        for k in new_items
        if k in old_items
        and (
            old_items[k].get("summary") != new_items[k].get("summary")
            or old_items[k].get("tool_trigger") != new_items[k].get("tool_trigger")
        )
    ]
    if not added and not removed and not changed:
        return []
    counts = []
    if added:
        counts.append(f"+{len(added)} new")
    if changed:
        counts.append(f"~{len(changed)} changed")
    if removed:
        counts.append(f"-{len(removed)} removed")
    names = []
    for entry in added + changed + removed:
        label = f"{id8(str(entry['memory_id']))} {flatten_summary(entry.get('summary'))[:80]}"
        if entry.get("authored_by_caller") is False:
            label += " (by another member)"
        names.append(label)
    return [", ".join(counts) + " — " + "; ".join(names)]


def _touch(path: str, stage: str = "") -> None:
    """(Re)start the negative-cache marker; it carries the failure stage when that is one
    the fallback words differently (``ENDPOINT_STAGES``), else nothing - the generic case."""
    tag = stage.encode("ascii") if stage in ENDPOINT_STAGES else b""
    # The marker is written, not only touched: O_NOFOLLOW + a regular-file check keep the
    # truncation off whatever a symlink or FIFO in its place points at (O_NONBLOCK: no hang).
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return
            os.ftruncate(fd, 0)
            if tag:
                os.write(fd, tag)
            if os.utime in os.supports_fd:
                os.utime(fd, None)
            else:
                os.utime(path, None)
        finally:
            os.close(fd)
    except OSError:
        pass  # the negative-cache marker is an optimisation; without it we simply retry sooner


def _marker_stage(path: str) -> str:
    """The stage a marker recorded, or ``""`` (generic) - also for a marker written by an
    earlier version, which is empty. Only a value in ``ENDPOINT_STAGES`` is ever returned."""
    # O_NONBLOCK: a FIFO in its place must not hang the hook; the fstat below then refuses it.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        raw = os.read(fd, 32)
    except OSError:
        return ""
    finally:
        os.close(fd)
    stage = raw.decode("ascii", "replace").strip()
    return stage if stage in ENDPOINT_STAGES else ""


def _file_age(path: str, now_ts: float) -> float | None:
    try:
        return now_ts - os.stat(path).st_mtime
    except OSError:
        return None


def handle_session_start(adapter: Any, event: dict[str, Any], env: Any, stdout: Any) -> int:
    resolution = adapter.resolve(env)
    messages: list[str] = list(resolution.warnings)
    if not resolution.configured:
        return 0
    config = resolution.config
    if config is None:
        if resolution.bad_fields:
            messages.insert(0, adapter.session_message("misconfigured", resolution.bad_fields))
        if messages:
            _emit(stdout, {"systemMessage": _system_message(messages)})
        return 0
    if not ensure_dir(config.guardrails_dir):
        _debug("guardrails dir")
        return 0
    now = now_utc()
    now_ts = time.time()
    prune_state(config.guardrails_dir, now_ts)

    session_id = event.get("session_id")
    state = State(config.guardrails_dir, session_id, None) if isinstance(session_id, str) else None
    if state is not None and event.get("source") in RESET_SOURCES:
        state.reset_main()

    old = load_cache(config.cache_path, config.context_id, now, None)
    fail_marker = os.path.join(config.guardrails_dir, config.context_id + ".fetch-failed")
    fail_age = _file_age(fail_marker, now_ts)
    need_fetch = old is None or old.data.get("version") is None or old.age_seconds > CACHE_FRESH_S
    blocked = fail_age is not None and 0 <= fail_age < FAIL_MARKER_S

    cache: LoadedCache | None = None
    source_desc = ""
    if need_fetch and not blocked:
        result, stage = fetch_guardrails(
            config.url,
            config.authorization,
            config.context_id,
            user_agent(adapter, env),
            FETCH_DEADLINE_S,
        )
        if result is not None:
            new = project_response(result, config.context_id, now)
            messages.extend(_diff_messages(old, new))
            if result.get("tool_triggered_truncated") is True:
                messages.append(f"list truncated at {result.get('cap')}")
            if write_private_file(config.cache_path, cache_bytes(new)):
                try:
                    os.unlink(fail_marker)
                except OSError:
                    pass  # usually absent; a marker that stays expires on its own after 60 s
            cache = LoadedCache(new, now, 0.0)
            source_desc = "fetched"
        else:
            _debug(stage)
            _touch(fail_marker, stage)
            cache, source_desc = _fallback_cache(config, old, messages, stage)
    elif need_fetch:
        # Negative cache: no request, but the failure keeps the wording it had when recorded.
        cache, source_desc = _fallback_cache(config, old, messages, _marker_stage(fail_marker))
    else:
        cache = old
        source_desc = f"cached {format_age(old.age_seconds if old else 0.0)}"

    if cache is not None:
        items = cache.tool_triggered
        compilable = [i for i in items if item_is_compilable(i)]
        skipped = [i for i in items if not item_is_compilable(i)]
        slow_ids = state.slow_ids() if state is not None else []
        skipped_ids = [id8(str(i.get("memory_id", ""))) for i in skipped] + [
            id8(s) for s in slow_ids
        ]
        if skipped_ids:
            listed = ", ".join(skipped_ids)
            messages.append(
                f"{len(skipped_ids)} skipped ({listed}) — pattern unsupported by this client"
            )
        output: dict[str, Any] = {}
        if compilable:
            output["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    f"Kagura Memory: {len(compilable)} tool guardrails active for context "
                    f"{config.context_id} ({source_desc}); the kagura-memory plugin hook "
                    "delivers each at its matching tool call."
                ),
            }
        if messages:
            output["systemMessage"] = _system_message(messages)
        if output:
            _emit(stdout, output)
        return 0
    if messages:
        _emit(stdout, {"systemMessage": _system_message(messages)})
    return 0


def _fallback_cache(
    config: Config, old: LoadedCache | None, messages: list[str], stage: str = ""
) -> tuple[LoadedCache | None, str]:
    if stage in ENDPOINT_STAGES:
        messages.append(ENDPOINT_WARNING)
        reason = ENDPOINT_FETCH_REASON
    else:
        reason = UNREACHABLE_REASON
    if old is not None and old.age_seconds <= FALLBACK_MAX_AGE_S:
        age = format_age(old.age_seconds)
        messages.append(f"{reason}, using cache from {age}")
        return old, f"cached {age}"
    if os.path.exists(config.cache_path):
        try:
            os.replace(config.cache_path, config.cache_path + ".stale")
        except OSError:
            pass  # the rename is bookkeeping; the too-old cache is not used either way
    messages.append(f"{reason} and no usable cache")
    return None, ""


def user_agent(adapter: Any, env: Any) -> str:
    return f"kagura-memory-plugin-hooks/{adapter.plugin_version(env)} ({adapter.client})"


def _system_message(messages: list[str]) -> str:
    text = SYSTEM_MESSAGE_PREFIX + "; ".join(m for m in messages if m)
    if len(text) > SYSTEM_MESSAGE_CAP:
        text = text[: SYSTEM_MESSAGE_CAP - 1] + "…"
    return text


def handle_refresh(adapter: Any, env: Any) -> int:
    resolution = adapter.resolve(env)
    config = resolution.config
    if config is None:
        return 0
    if not ensure_dir(config.guardrails_dir):
        _debug("guardrails dir")
        return 0
    now = now_utc()
    old = load_cache(config.cache_path, config.context_id, now, None)
    if old is not None and old.age_seconds < REFRESH_MIN_AGE_S:
        return 0
    state_dir = os.path.join(config.guardrails_dir, "state")
    if not ensure_dir(state_dir):
        _debug("state dir")
        return 0
    # One refresh in flight per data directory: an advisory flock on a lock file
    # that is never removed. The kernel drops the lock when this process exits,
    # so a crashed holder leaves nothing stale and no takeover is needed - a
    # path-based takeover cannot be made atomic (two contenders can both replace
    # a stale lock, and one then unlinks the other's live lock). Without flock
    # (win32) there is no in-session refresh; SessionStart still fetches.
    lock = os.path.join(state_dir, "refresh.lock")
    try:
        lock_fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return 0
    try:
        if not _try_lock(lock_fd):
            return 0
        result, stage = fetch_guardrails(
            config.url,
            config.authorization,
            config.context_id,
            user_agent(adapter, env),
            FETCH_DEADLINE_S,
        )
        if result is None:
            _debug(stage)
            return 0
        write_private_file(
            config.cache_path, cache_bytes(project_response(result, config.context_id, now_utc()))
        )
    finally:
        os.close(lock_fd)  # releases the flock
    return 0


def handle_tool_event(
    adapter: Any, event: dict[str, Any], env: Any, stdout: Any, started: float
) -> int:
    event_name = event.get("hook_event_name")
    on = TOOL_EVENTS.get(event_name if isinstance(event_name, str) else "")
    if on is None:
        return 0
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0
    config = adapter.resolve(env).config
    if config is None:
        return 0
    cache = load_cache(config.cache_path, config.context_id, now_utc(), TOOL_EVENT_MAX_AGE_S)
    if cache is None:
        return 0
    items = [i for i in cache.tool_triggered if isinstance(i.get("tool_trigger"), dict)]
    items = [i for i in items if i["tool_trigger"].get("on", "pre") == on]
    if not items:
        return 0
    if not HAS_TIMER:
        return 0
    tool_name, aliases, subjects = adapter.subjects(event)
    if not isinstance(tool_name, str):
        return 0
    agent_id = event.get("agent_id")
    state = State(
        config.guardrails_dir, session_id, agent_id if isinstance(agent_id, str) else None
    )
    matched = match_items(items, on, tool_name, aliases, subjects, state, started + CALL_BUDGET_S)
    if not matched:
        return 0
    blocks, informs = select_candidates(
        matched,
        on,
        config.max_action,
        state,
        adapter.render_budget_chars,
        adapter.render_budget_tokens,
    )
    if not blocks and not informs:
        return 0
    for lane, taken in (("block", blocks), ("inform", informs)):
        for item in taken:
            append_delivery_log(
                config.guardrails_dir,
                adapter.client,
                str(event_name),
                state,
                str(item["memory_id"]),
                lane,
            )
    _emit(
        stdout,
        build_hook_output(
            str(event_name),
            blocks,
            informs,
            adapter.render_budget_chars,
            adapter.render_budget_tokens,
        ),
    )
    return 0


def run_adapter(
    adapter: Any, argv: list[str], stdin: Any, stdout: Any, env: Any, started: float
) -> int:
    """Dispatch one hook invocation for ``adapter``; always returns 0."""
    refresh = "--refresh" in argv[1:]
    text = _read_stdin(stdin)
    if refresh:
        return handle_refresh(adapter, env)
    if text is None:
        return 0
    try:
        event = json.loads(text)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    event_name = event.get("hook_event_name")
    if event_name == "SessionStart":
        return handle_session_start(adapter, event, env, stdout)
    if event_name in TOOL_EVENTS:
        return handle_tool_event(adapter, event, env, stdout, started)
    return 0


def _this_module() -> Any:
    module = sys.modules.get(__name__)
    if module is not None:
        return module
    import types

    module = types.ModuleType(__name__)
    module.__dict__.update(globals())
    return module


def _load_codex_adapter() -> Any:
    """Load ``_codex_adapter.py`` next to this file by path (``-I`` leaves it off ``sys.path``)."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_codex_adapter.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("_codex_adapter", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_adapter(_this_module())


def _client_from_argv(argv: list[str]) -> str:
    args = argv[1:]
    for index, arg in enumerate(args):
        if arg == "--client" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--client="):
            return arg.split("=", 1)[1]
    return "claude"


def main(argv: list[str], stdin: Any, stdout: Any, env: Any) -> int:
    started = time.monotonic()
    try:
        client = _client_from_argv(argv)
        if client == "claude":
            adapter: Any = ClaudeAdapter()
        elif client == "codex":
            adapter = _load_codex_adapter()
            if adapter is None:
                return 0
        else:
            return 0
        return run_adapter(adapter, argv, stdin, stdout, env, started)
    except PatternBudget:
        _debug("budget")
    except Exception as exc:  # noqa: BLE001 - fail open, name the stage only
        _debug("unhandled " + type(exc).__name__)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass  # a replaced or detached stream keeps its own encoding
    # Fail open: exit 0 whatever happens. ``main`` already turns every Exception
    # into one stderr line; this is the last resort for anything it re-raises,
    # and a Ctrl-C mid-hook is treated the same way (exit 0, no traceback).
    # SystemExit is left alone - nothing after the interpreter-floor guard
    # raises it, and it would carry its own code.
    try:
        main(sys.argv, sys.stdin, sys.stdout, os.environ)
    except KeyboardInterrupt:
        pass  # interrupted mid-hook: still exit 0 and print no traceback
    except Exception:  # noqa: BLE001 - never a non-zero exit
        pass  # main() already named the stage on stderr where it could
    sys.exit(0)
