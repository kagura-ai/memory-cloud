"""Shared fixtures for the kagura-memory plugin hook tests (#1619).

The hook under test is ``plugins/kagura-memory/hooks/kagura_guardrails.py``,
declared by ``claude-hooks/hooks.json``. Two ways to exercise it:

* ``hook_module`` imports the script by path and calls its functions and
  ``main(argv, stdin, stdout, env)`` in-process (table-driven core tests);
* ``run_hook`` runs the exact command string read from ``hooks.json`` under
  ``sh -c`` with a synthetic payload on stdin, the way Claude Code does.

Nothing here touches the network except ``stub_server`` (loopback only) and
nothing is written outside ``tmp_path``.
"""

from __future__ import annotations

import http.server
import importlib.util
import io
import json
import os
import socket
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# backend/tests/plugin/conftest.py -> plugin -> tests -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_SCRIPT = REPO_ROOT / "plugins" / "kagura-memory" / "hooks" / "kagura_guardrails.py"
CLAUDE_HOOKS_JSON = REPO_ROOT / "claude-hooks" / "hooks.json"
CLAUDE_PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"

CONTEXT_ID = "550e8400-e29b-41d4-a716-446655440000"
OTHER_CONTEXT_ID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
# Assembled at runtime so the literal never appears in the repository.
CANARY_KEY = "kagura_" + "canary" + "_" + "9f2b7c4e1d0a"
SESSION_ID = "sess-abc123-main-session-id"
AGENT_ID = "agent-def456-subagent-id"


def _load_hook_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kagura_guardrails_under_test", HOOK_SCRIPT)
    assert spec is not None and spec.loader is not None, HOOK_SCRIPT
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def hook_module() -> ModuleType:
    """The hook script imported by path (never via ``sys.path``)."""
    return _load_hook_module()


def memory_id(n: int) -> str:
    """Deterministic UUID for fixture item ``n``."""
    return str(uuid.UUID(int=0x1000_0000_0000_0000_0000_0000_0000_0000 + n))


def item(
    n: int,
    summary: str,
    tool: str,
    *,
    on: str = "pre",
    match: str | None = None,
    action: str = "inform",
    importance: float = 0.8,
    authored_by_caller: bool | None = None,
) -> dict[str, Any]:
    """One cache item in the shared format (``tool_trigger`` key order as the server writes it)."""
    trigger: dict[str, Any] = {"tool": tool, "on": on}
    if match is not None:
        trigger["match"] = match
    trigger["action"] = action
    out: dict[str, Any] = {"memory_id": memory_id(n), "summary": summary, "importance": importance}
    if authored_by_caller is not None:
        out["authored_by_caller"] = authored_by_caller
    out["tool_trigger"] = trigger
    return out


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PluginEnv:
    """A fully configured Claude plugin environment rooted in ``tmp_path``."""

    root: Path
    data_dir: Path
    project_dir: Path
    home: Path
    env: dict[str, str]
    context_id: str = CONTEXT_ID

    @property
    def guardrails_dir(self) -> Path:
        return self.data_dir / "guardrails"

    @property
    def cache_path(self) -> Path:
        return self.guardrails_dir / f"{self.context_id}.json"

    @property
    def state_dir(self) -> Path:
        return self.guardrails_dir / "state"

    def write_cache(
        self,
        items: list[dict[str, Any]],
        *,
        pinned: list[dict[str, Any]] | None = None,
        fetched_at: datetime | str | None = None,
        version: str | None = "3f9c1a7b2d4e6f80",
        context_id: str | None = None,
        fmt: int = 1,
        mode: int = 0o600,
    ) -> Path:
        if fetched_at is None:
            fetched_at = datetime.now(UTC) - timedelta(minutes=1)
        if isinstance(fetched_at, datetime):
            fetched_at = iso_z(fetched_at)
        cache: dict[str, Any] = {
            "format": fmt,
            "context_id": context_id or self.context_id,
            "fetched_at": fetched_at,
        }
        if version is not None:
            cache["version"] = version
        cache["pinned"] = pinned or []
        cache["tool_triggered"] = items
        self.guardrails_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        os.chmod(self.cache_path, mode)
        return self.cache_path

    def markers(self, lane: str | None = None) -> list[Path]:
        if not self.state_dir.exists():
            return []
        out = []
        for path in self.state_dir.rglob("*"):
            if path.is_file() and path.parent.name in ("block", "inform"):
                if lane is None or path.parent.name == lane:
                    out.append(path)
        return sorted(out)

    def without_options(self) -> dict[str, str]:
        return {k: v for k, v in self.env.items() if not k.startswith("CLAUDE_PLUGIN_OPTION_")}


@pytest.fixture
def plugin_env(tmp_path: Path) -> PluginEnv:
    data_dir = tmp_path / "plugin-data"
    project_dir = tmp_path / "project"
    home = tmp_path / "home"
    for path in (data_dir, project_dir, home):
        path.mkdir(mode=0o700)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "CLAUDE_PLUGIN_DATA": str(data_dir),
        "CLAUDE_PROJECT_DIR": str(project_dir),
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": "http://127.0.0.1:9/mcp/w/00000000-0000-0000-0000-000000000000",
        "CLAUDE_PLUGIN_OPTION_API_KEY": CANARY_KEY,
        "CLAUDE_PLUGIN_OPTION_CONTEXT_ID": CONTEXT_ID,
    }
    return PluginEnv(root=tmp_path, data_dir=data_dir, project_dir=project_dir, home=home, env=env)


def payload(
    event: str,
    *,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
    session_id: str | None = SESSION_ID,
    agent_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"hook_event_name": event, "cwd": "/tmp/project"}
    if session_id is not None:
        body["session_id"] = session_id
    if agent_id is not None:
        body["agent_id"] = agent_id
        body["agent_type"] = "Explore"
    if tool_name is not None:
        body["tool_name"] = tool_name
        body["tool_input"] = tool_input or {}
        body["tool_use_id"] = "toolu_01ABC"
    body.update(extra)
    return body


def bash_pre(command: str, **kw: Any) -> dict[str, Any]:
    return payload("PreToolUse", tool_name="Bash", tool_input={"command": command}, **kw)


def hook_commands() -> dict[str, list[dict[str, Any]]]:
    """``{event: [handler, ...]}`` for every command handler in ``claude-hooks/hooks.json``."""
    data = json.loads(CLAUDE_HOOKS_JSON.read_text(encoding="utf-8"))
    out: dict[str, list[dict[str, Any]]] = {}
    for event, groups in data["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                entry = dict(handler)
                entry["matcher"] = group.get("matcher")
                out.setdefault(event, []).append(entry)
    return out


def command_for(event: str, *, refresh: bool = False) -> str:
    handlers = hook_commands()[event]
    wanted = [h for h in handlers if bool(h.get("async")) is refresh]
    assert len(wanted) == 1, (event, refresh, handlers)
    return wanted[0]["command"]


@dataclass
class HookResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def json(self) -> dict[str, Any] | None:
        if not self.stdout.strip():
            return None
        lines = self.stdout.strip().splitlines()
        assert len(lines) == 1, f"expected one JSON line, got {self.stdout!r}"
        return json.loads(lines[0])

    @property
    def specific(self) -> dict[str, Any]:
        out = self.json
        assert out is not None, "empty stdout"
        return out["hookSpecificOutput"]


RunHook = Callable[..., HookResult]


@pytest.fixture
def run_hook(plugin_env: PluginEnv) -> RunHook:
    """Run the real ``hooks.json`` command under ``sh -c`` with ``payload`` on stdin."""

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
        command = command_for(event, refresh=refresh)
        proc = subprocess.run(
            ["sh", "-c", command],
            input=data,
            capture_output=True,
            cwd=cwd or plugin_env.project_dir,
            env=env if env is not None else plugin_env.env,
            check=False,
            timeout=timeout,
        )
        return HookResult(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    return _run


@pytest.fixture
def call_main(hook_module: ModuleType, plugin_env: PluginEnv) -> Callable[..., HookResult]:
    """Call ``main()`` in-process with a payload; same result shape as ``run_hook``."""

    def _call(
        body: dict[str, Any] | str,
        *,
        env: dict[str, str] | None = None,
        argv: list[str] | None = None,
    ) -> HookResult:
        text = json.dumps(body) if isinstance(body, dict) else body
        stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")
        stdout = io.StringIO()
        code = hook_module.main(
            argv or ["kagura_guardrails.py", "--client", "claude"],
            stdin,
            stdout,
            env if env is not None else plugin_env.env,
        )
        return HookResult(code, stdout.getvalue(), "")

    return _call


# ---------------------------------------------------------------------------
# Stub MCP server
# ---------------------------------------------------------------------------


@dataclass
class Recorded:
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class StubServer:
    """A loopback ``http.server`` that answers ``tools/call`` with a canned envelope."""

    host: str = "127.0.0.1"
    port: int = 0
    requests: list[Recorded] = field(default_factory=list)
    status: int = 200
    response: Any = None
    raw_body: bytes | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    hang: bool = False
    _server: http.server.ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp/w/00000000-0000-0000-0000-000000000000"

    def set_guardrails(
        self,
        tool_triggered: list[dict[str, Any]],
        *,
        pinned: list[dict[str, Any]] | None = None,
        version: str = "3f9c1a7b2d4e6f80",
        truncated: bool = False,
        fmt: int = 1,
        status: str = "success",
    ) -> None:
        self.response = load_guardrails_response(
            tool_triggered,
            pinned=pinned,
            version=version,
            truncated=truncated,
            fmt=fmt,
            status=status,
        )

    def start(self) -> StubServer:
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # silence
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append(Recorded(self.path, dict(self.headers.items()), body))
                if stub.hang:
                    threading.Event().wait(10)
                    return
                if stub.raw_body is not None:
                    payload_bytes = stub.raw_body
                else:
                    payload_bytes = json.dumps(stub.response or {}).encode("utf-8")
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload_bytes)))
                for key, value in stub.extra_headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload_bytes)

        self._server = http.server.ThreadingHTTPServer((self.host, 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def load_guardrails_response(
    tool_triggered: list[dict[str, Any]],
    *,
    pinned: list[dict[str, Any]] | None = None,
    version: str = "3f9c1a7b2d4e6f80",
    truncated: bool = False,
    fmt: int = 1,
    status: str = "success",
) -> dict[str, Any]:
    """A JSON-RPC ``tools/call`` envelope carrying a ``load_guardrails`` result.

    Items carry the full server item shape (extra fields the cache must drop).
    """

    def full(entry: dict[str, Any], *, with_trigger: bool) -> dict[str, Any]:
        out = {
            "memory_id": entry["memory_id"],
            "summary": entry["summary"],
            "context_summary": None if with_trigger else "why it matters",
            "type": "troubleshooting",
            "importance": entry["importance"],
            "delivery_mode": "on_recall",
            "tool_trigger": entry.get("tool_trigger") if with_trigger else None,
            "source_type": "manual",
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
        }
        if "authored_by_caller" in entry:
            out["authored_by_caller"] = entry["authored_by_caller"]
        return out

    result = {
        "status": status,
        "format": fmt,
        "version": version,
        "pinned": [full(p, with_trigger=False) for p in (pinned or [])],
        "tool_triggered": [full(t, with_trigger=True) for t in tool_triggered],
        "total_available": len(pinned or []) + len(tool_triggered),
        "truncated": truncated,
        "cap": 50,
        "pinned_cap": 100,
        "pinned_total_available": len(pinned or []),
        "pinned_truncated": False,
        "tool_triggered_total_available": len(tool_triggered),
        "tool_triggered_truncated": truncated,
        "context_id": CONTEXT_ID,
        "context_name": "kagura-dev",
        "context_display_name": "Kagura Dev",
        "context_is_private": False,
        "context_is_locked": False,
    }
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
    }


@pytest.fixture
def stub_server() -> Iterator[StubServer]:
    stub = StubServer().start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture
def second_stub() -> Iterator[StubServer]:
    stub = StubServer().start()
    try:
        yield stub
    finally:
        stub.stop()


def free_closed_port() -> int:
    """A loopback port that nothing listens on (bound then released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
