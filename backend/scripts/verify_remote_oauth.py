#!/usr/bin/env python3
"""Verify the Remote MCP OAuth + MCP flow of a Kagura Memory Cloud deployment (#1686).

Runs discovery, Dynamic Client Registration, one browser consent, the PKCE token
exchange with its negative cases, authenticated MCP over Streamable HTTP,
refresh, revocation and cleanup against ``--base-url``, and writes redacted
evidence. Everything except the consent is automatic; see ``--help`` and
``docs/ops/remote-oauth-verification.md``.

Standalone: Python 3.11 standard library plus ``httpx``. It never imports the
server code; the only checkout files it reads are two string constants
(``APP_VERSION`` and the static MCP instructions text), parsed with ``ast``.
"""

from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import hashlib
import ipaddress
import json
import queue
import re
import secrets
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

SCRIPT_VERSION = "1.0.0"
EVIDENCE_SCHEMA = "kagura-memory-cloud/remote-oauth-verification/v1"

# The redirect URI Claude registers for remote MCP connectors. Step R2 only
# registers it; that client is never used further.
CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"

DEFAULT_SCOPE = "memory:read memory:write"
# A loopback DCR registration must name a recognised client (see
# ``detect_dcr_provider`` in api/routes/oauth.py); the run id is appended.
DEFAULT_CLIENT_NAME = "Claude remote OAuth verification"
# A scope no server defines: the refresh-widening and extended scope checks.
UNKNOWN_SCOPE = "verification:unknown-scope"
READ_ONLY_SCOPE = "memory:read"
# A read-like REST call that needs ``memory:write`` for an OAuth bearer (POST).
# Without a context filter the server answers 422 even if the scope check let
# it through, so the probe never reads or writes data.
REST_SCOPE_PROBE_PATH = "/api/v1/memory/recall"
SYSTEM_INFO_PATH = "/api/v1/system/info"
READ_TOOL = "list_contexts"

LEGACY_PROTOCOL_VERSION = "2025-03-26"
MODERN_PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
TOOL_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

# Hosts that stay in the evidence as-is: loopback and the Claude callback host.
_KEEP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "claude.ai"})

# Dict keys whose values never reach the evidence (presence and length only).
SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "code",
        "code_verifier",
        "code_challenge",
        "client_secret",
        "plaintext_secret",
        "registration_access_token",
        "device_code",
        "state",
        "cookie",
        "set-cookie",
        "authorization",
        "mcp-session-id",
        "session_id",
        "password",
    }
)
# Query parameters masked in any URL recorded as evidence.
SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "code",
        "state",
        "code_challenge",
        "code_verifier",
        "access_token",
        "refresh_token",
        "client_secret",
        "session_id",
        "return_to",
    }
)
# Shorter values are not treated as secrets (they would match ordinary words);
# every secret this script handles is far longer.
_MIN_SECRET_LENGTH = 8

_URL_HOST_RE = re.compile(r"(?i)\b(https?)://([^/\s\"'<>?#]+)")
_AUTH_PARAM_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)="([^"]*)"')

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_INFO = "info"


# ============================================================================
# PKCE (RFC 7636)
# ============================================================================


def s256_challenge(code_verifier: str) -> str:
    """Return the RFC 7636 §4.2 S256 ``code_challenge`` for ``code_verifier``.

    Args:
        code_verifier: The verifier (43-128 unreserved characters).

    Returns:
        ``BASE64URL(SHA256(ASCII(code_verifier)))`` without padding.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_code_verifier() -> str:
    """Return a fresh 64-character RFC 7636 ``code_verifier``."""
    return secrets.token_urlsafe(48)[:64]


# ============================================================================
# Redaction
# ============================================================================


def fingerprint(value: str) -> str:
    """Return a short, non-reversible fingerprint that correlates a value."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def mask(value: Any) -> dict[str, Any]:
    """Describe a sensitive value by presence and length only."""
    if value is None or value == "":
        return {"redacted": True, "present": False}
    return {"redacted": True, "present": True, "length": len(str(value))}


def _is_mask(value: Any) -> bool:
    return isinstance(value, dict) and value.get("redacted") is True


def redact_url(url: str) -> str:
    """Mask the values of sensitive query parameters in ``url``.

    Parameter names stay visible, so the evidence shows what was carried.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if not parts.query:
        return url
    pairs = []
    for item in parts.query.split("&"):
        name, sep, value = item.partition("=")
        if sep and name.lower() in SENSITIVE_QUERY_PARAMS:
            value = "<redacted>"
        pairs.append(f"{name}{sep}{value}")
    return parts._replace(query="&".join(pairs)).geturl()


class Redactor:
    """Scrubs secrets and the target host out of everything recorded.

    Applied to the whole evidence document before it is written or rendered:

    1. Values under a key in ``SENSITIVE_KEYS`` become presence/length only.
    2. Every secret the run registered (tokens, codes, verifiers, state,
       client ids, session ids) is replaced wherever it appears in a string.
    3. Unless ``record_host`` is set, the target host becomes ``<label>`` and
       any other non-loopback host ``<label-other-N>``.

    ``dumps`` and ``check`` refuse text that still carries a registered secret.
    """

    def __init__(self, base_url: str, label: str, record_host: bool) -> None:
        parts = urlsplit(base_url)
        self._netloc = parts.netloc.lower()
        self._hostname = (parts.hostname or "").lower()
        # The origin as it appears percent-encoded in a query (``resource=``).
        self._encoded_origin = quote(f"{parts.scheme}://{self._netloc}", safe="")
        self._encoded_label = quote(f"{parts.scheme}://", safe="") + f"<{label}>"
        self._label = label
        self._record_host = record_host
        self._secrets: dict[str, str] = {}
        self._other_hosts: dict[str, str] = {}

    def register(self, value: str | None, kind: str) -> None:
        """Remember ``value`` as a secret of type ``kind``."""
        if value and len(value) >= _MIN_SECRET_LENGTH:
            self._secrets.setdefault(value, kind)

    def _replace_host(self, match: re.Match[str]) -> str:
        scheme, netloc = match.group(1), match.group(2).lower()
        if netloc == self._netloc:
            return f"{scheme}://<{self._label}>"
        try:
            hostname = urlsplit(f"{scheme}://{netloc}").hostname or ""
        except ValueError:
            hostname = ""
        if hostname in _KEEP_HOSTS:
            return match.group(0)
        alias = self._other_hosts.setdefault(
            netloc, f"<{self._label}-other-{len(self._other_hosts) + 1}>"
        )
        return f"{scheme}://{alias}"

    def scrub_text(self, text: str) -> str:
        """Return ``text`` with registered secrets and hosts replaced."""
        # Longest first, so a secret that contains another is replaced whole.
        for value in sorted(self._secrets, key=len, reverse=True):
            if value in text:
                text = text.replace(value, f"<redacted:{self._secrets[value]}>")
        if self._record_host:
            return text
        text = re.sub(re.escape(self._encoded_origin), self._encoded_label, text, flags=re.I)
        text = _URL_HOST_RE.sub(self._replace_host, text)
        if self._hostname and not is_loopback_host(self._hostname):
            text = re.sub(re.escape(self._hostname), f"<{self._label}>", text, flags=re.I)
        return text

    def scrub(self, obj: Any) -> Any:
        """Recursively scrub a JSON-compatible value."""
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for key, value in obj.items():
                if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                    out[key] = value if _is_mask(value) else mask(value)
                else:
                    out[key] = self.scrub(value)
            return out
        if isinstance(obj, (list, tuple)):
            return [self.scrub(item) for item in obj]
        if isinstance(obj, str):
            return self.scrub_text(obj)
        return obj

    def check(self, text: str) -> None:
        """Raise ``RuntimeError`` if ``text`` still carries a registered secret."""
        leaked = sorted({kind for value, kind in self._secrets.items() if value in text})
        if leaked:
            raise RuntimeError(f"refusing to emit evidence: unredacted {', '.join(leaked)}")

    def dumps(self, obj: Any) -> str:
        """Scrub ``obj`` and serialize it as indented JSON, verified clean."""
        text = json.dumps(self.scrub(obj), indent=2, ensure_ascii=False)
        self.check(text)
        return text


# ============================================================================
# Redirect handling (loopback listener + pasted URL)
# ============================================================================


class ConsentAborted(Exception):
    """The operator typed ``abort``."""


class ConsentSkipped(Exception):
    """The operator typed ``skip`` for an optional check."""


class ConsentTimeout(Exception):
    """No redirect arrived in time."""


class RedirectParseError(ValueError):
    """A redirect URL could not be read as an OAuth authorization response."""


class StateMismatchError(ValueError):
    """The redirect's ``state`` does not match the one sent."""


@dataclass
class RedirectParams:
    """The authorization response carried by a redirect (RFC 6749 §4.1.2)."""

    code: str | None
    state: str | None
    error: str | None
    error_description: str | None
    iss: str | None
    via: str


def parse_redirect(value: str, redirect_uri: str, via: str = "paste") -> RedirectParams:
    """Read an authorization response from a callback URL or a pasted one.

    Accepts the full redirect URL, which must start with ``redirect_uri``
    (same scheme, host, port and path), or just its query string.

    Args:
        value: The URL (or ``?query``) the browser landed on.
        redirect_uri: The redirect URI of the authorization request.
        via: ``"loopback"`` or ``"paste"``, kept as evidence.

    Returns:
        The parsed parameters.

    Raises:
        RedirectParseError: The URL is for another address, a parameter is
            repeated, or it carries neither ``code`` nor ``error``.
    """
    text = value.strip()
    if not text:
        raise RedirectParseError("empty redirect")
    if text.startswith("?") or ("://" not in text and "=" in text):
        query = text.lstrip("?")
    else:
        got, want = urlsplit(text), urlsplit(redirect_uri)
        if (got.scheme.lower(), got.netloc.lower(), got.path) != (
            want.scheme.lower(),
            want.netloc.lower(),
            want.path,
        ):
            raise RedirectParseError("the URL does not start with the registered redirect URI")
        query = got.query
    params = parse_qs(query, keep_blank_values=True)
    repeated = sorted(name for name, values in params.items() if len(values) > 1)
    if repeated:
        raise RedirectParseError(f"repeated parameter(s): {', '.join(repeated)}")
    flat = {name: values[0] for name, values in params.items()}
    if not flat.get("code") and not flat.get("error"):
        raise RedirectParseError("the URL carries neither code nor error")
    return RedirectParams(
        code=flat.get("code") or None,
        state=flat.get("state"),
        error=flat.get("error") or None,
        error_description=flat.get("error_description"),
        iss=flat.get("iss"),
        via=via,
    )


def check_state(params: RedirectParams, expected: str) -> None:
    """Raise ``StateMismatchError`` unless the redirect echoes ``expected``."""
    if params.state is None or not secrets.compare_digest(
        params.state.encode("utf-8"), expected.encode("utf-8")
    ):
        raise StateMismatchError("state in the redirect does not match the request")


class EventSource:
    """One queue for loopback callbacks and lines typed on stdin.

    A single stdin reader lives for the whole run, so a later prompt never
    loses a line to an earlier, abandoned read.
    """

    def __init__(self) -> None:
        self.queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._stdin_started = False

    def put(self, kind: str, value: str) -> None:
        """Queue an event: ``"callback"``, ``"paste"`` or ``"eof"``."""
        self.queue.put((kind, value))

    def start_stdin(self, stream: TextIO) -> None:
        """Start the background stdin reader (once)."""
        if self._stdin_started:
            return
        self._stdin_started = True

        def pump() -> None:
            while True:
                line = stream.readline()
                if line == "":
                    self.put("eof", "")
                    return
                self.put("paste", line)

        threading.Thread(target=pump, name="stdin-pump", daemon=True).start()

    def get(self, timeout: float) -> tuple[str, str] | None:
        """Return the next event, or ``None`` after ``timeout`` seconds."""
        try:
            return self.queue.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None


class CallbackServer:
    """Loopback redirect listener (RFC 8252 §7.3), bound to 127.0.0.1 only."""

    def __init__(self, events: EventSource, port: int = 0) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if self.path.startswith("/favicon"):
                    self.send_response(404)
                    self.end_headers()
                    return
                events.put(
                    "callback", f"http://127.0.0.1:{self.server.server_address[1]}{self.path}"
                )
                body = b"<!doctype html><title>Verification</title><p>Redirect received. You can close this tab.</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return  # the request line carries the code: never log it

        self._httpd = HTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        """The bound port."""
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        """Serve in a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and close the socket."""
        if self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()


def wait_for_redirect(
    events: EventSource,
    accept_paths: tuple[str, ...],
    timeout: float,
    allow_empty: bool = False,
) -> tuple[str, str]:
    """Wait for the first loopback callback on ``accept_paths`` or a pasted line.

    A closed stdin is not an error: callbacks can still arrive until the timeout.

    Returns:
        ``(via, value)`` with ``via`` ``"loopback"`` or ``"paste"``. With
        ``allow_empty`` an empty line returns ``("paste", "")``.

    Raises:
        ConsentAborted: The operator typed ``abort``.
        ConsentSkipped: The operator typed ``skip``.
        ConsentTimeout: Nothing arrived in time.
    """
    deadline = time.monotonic() + timeout
    while True:
        event = events.get(deadline - time.monotonic())
        if event is None:
            raise ConsentTimeout(f"no redirect within {int(timeout)} s")
        kind, value = event
        if kind == "callback":
            if urlsplit(value).path in accept_paths:
                return "loopback", value
            continue
        if kind != "paste":
            continue
        line = value.strip()
        if line.lower() == "abort":
            raise ConsentAborted("aborted by the operator")
        if line.lower() == "skip":
            raise ConsentSkipped("skipped by the operator")
        if line or allow_empty:
            return "paste", line


# ============================================================================
# Small helpers
# ============================================================================


def read_module_constant(path: Path, name: str) -> str | None:
    """Return the string literal assigned to ``name`` at module level in ``path``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return None
            return value if isinstance(value, str) else None
    return None


def utc_now() -> str:
    """Current UTC time, ISO 8601 with a ``Z`` suffix."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_www_authenticate(header: str | None) -> dict[str, str]:
    """Parse a challenge into ``{"scheme": ..., <auth-param>: <value>, ...}``."""
    if not header:
        return {}
    scheme, _, rest = header.strip().partition(" ")
    return {"scheme": scheme, **dict(_AUTH_PARAM_RE.findall(rest))}


def json_or_none(resp: httpx.Response) -> Any:
    """Decode a JSON (or single-event SSE) body, or return ``None``."""
    text = resp.text
    if "text/event-stream" in resp.headers.get("content-type", ""):
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = data[-1] if data else ""
    try:
        return json.loads(text)
    except ValueError:
        return None


def json_dict(resp: httpx.Response) -> dict[str, Any]:
    """The decoded body when it is a JSON object, else ``{}``."""
    body = json_or_none(resp)
    return body if isinstance(body, dict) else {}


def rpc_result(resp: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC ``result`` object of an MCP answer, else ``{}``."""
    result = json_dict(resp).get("result")
    return result if isinstance(result, dict) else {}


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and loopback IP literals."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def normalize_base_url(value: str) -> str:
    """Validate ``--base-url``: http(s) origin only; plain http only on loopback."""
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("--base-url must be an absolute http(s) URL")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("--base-url must not carry a path, query or fragment")
    if parts.scheme == "http" and not is_loopback_host(parts.hostname):
        raise ValueError("plain http is only accepted for a loopback --base-url")
    return f"{parts.scheme}://{parts.netloc}"


def count_contexts(text: str) -> int | None:
    """Number of contexts in a ``list_contexts`` result, if it parses."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("contexts"), list):
        return len(data["contexts"])
    return len(data) if isinstance(data, list) else None


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(report: dict[str, Any]) -> str:
    """Render a scrubbed evidence document as a Markdown summary.

    Args:
        report: The evidence document after ``Redactor.scrub``.

    Returns:
        Markdown suitable for a GitHub issue comment.
    """
    summary = report.get("summary", {})
    target = report.get("target", {})
    deployed = report.get("deployed") or {}
    checkout = report.get("checkout") or {}
    lines = [
        f"### Remote OAuth verification — {target.get('label', 'target')}",
        "",
        f"- Result: **{summary.get('result', '?')}** ({summary.get('passed', 0)} passed, "
        f"{summary.get('failed', 0)} failed, {summary.get('skipped', 0)} skipped, "
        f"{summary.get('info', 0)} info; required not passed: "
        f"{summary.get('required_not_passed', 0)})",
        f"- Run `{report.get('run_id', '?')}`: {report.get('started_at', '?')} → "
        f"{report.get('finished_at', '?')}",
        f"- Deployed version: {deployed.get('version') or 'unknown'} (environment: "
        f"{deployed.get('environment') or 'unknown'}); checkout version: "
        f"{checkout.get('version') or 'unknown'}",
        f"- Target: {target.get('scheme', '?')}, "
        f"{'loopback' if target.get('is_loopback') else 'non-loopback'}, host "
        f"{'recorded' if target.get('host') else 'not recorded'}; consent rounds: "
        f"{report.get('consent_rounds', 0)}",
        f"- Script: `backend/scripts/verify_remote_oauth.py` v{report.get('script_version', '?')}",
        "",
        "| Step | Check | Required | Result | Evidence |",
        "|---|---|---|---|---|",
    ]
    for step in report.get("steps", []):
        lines.append(
            f"| {_cell(step.get('id', ''))} | {_cell(step.get('title', ''))} | "
            f"{'yes' if step.get('required') else 'no'} | "
            f"{_cell(str(step.get('status', '')).upper())} | {_cell(step.get('summary', ''))} |"
        )
    lines += [
        "",
        "Tokens, codes, verifiers, state, client ids, cookies and session ids are "
        "recorded by presence and length only.",
    ]
    return "\n".join(lines) + "\n"


# ============================================================================
# Steps
# ============================================================================


class StepFailed(Exception):
    """Ends a step as failed with a reason."""


class _SkipStep(Exception):
    pass


@dataclass
class Step:
    """One recorded check."""

    id: str
    section: str
    title: str
    required: bool
    status: str = STATUS_SKIP
    summary: str = ""
    note: str = ""
    started_at: str = ""
    duration_ms: int = 0
    checks: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    observation_only: bool = False

    def check(self, name: str, ok: bool) -> bool:
        """Record a sub-check; a false one fails the step."""
        self.checks.append({"name": name, "ok": bool(ok)})
        return bool(ok)

    def info(self, summary: str) -> None:
        """End the step as ``info``: an observation, not a pass/fail result."""
        self.observation_only = True
        self.summary = summary

    def skip(self, reason: str) -> None:
        """End the step as skipped."""
        raise _SkipStep(reason)

    def as_dict(self) -> dict[str, Any]:
        """The step as evidence."""
        out: dict[str, Any] = {
            "id": self.id,
            "section": self.section,
            "title": self.title,
            "required": self.required,
            "status": self.status,
            "summary": self.summary,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
        }
        if self.note:
            out["note"] = self.note
        if self.checks:
            out["checks"] = self.checks
        if self.evidence:
            out["evidence"] = self.evidence
        return out


@dataclass
class TokenPair:
    """Tokens issued during the run; the values stay in memory only."""

    label: str
    access_token: str
    refresh_token: str | None
    scope: str | None
    revoked: bool = False


@dataclass
class Config:
    """Command-line options."""

    base_url: str
    label: str = "target"
    record_host: bool = False
    scope: str = DEFAULT_SCOPE
    client_name: str = DEFAULT_CLIENT_NAME
    mcp_path: str = "/mcp"
    callback_port: int = 0
    consent_timeout: float = 600.0
    expect_version: str | None = None
    discovery_only: bool = False
    extended_authorize_checks: bool = False


MCP_FOLLOW_UP_STEPS = (
    ("M2", "notifications/initialized is accepted", True),
    ("M3", "tools/list: every tool has a title and the four hints", True),
    ("M4", f"Safe read call ({READ_TOOL})", True),
    ("M5", "Session reuse (ping on the same Mcp-Session-Id)", True),
    ("M6", "Stateless per-request era (MCP 2026-07-28), if supported", False),
    ("M7", "Unknown Mcp-Session-Id answers 404 with re-initialize guidance", False),
)
REFRESH_STEPS = (
    ("S2", "Refresh cannot widen the granted scope", True),
    ("F1", "Refresh grant issues a new token pair", True),
    ("F2", "The previous refresh token no longer works", True),
    ("F3", "The previous access token after refresh", False),
    ("F4", "MCP initialize with the refreshed token", True),
    ("S3", "REST API enforces scope (a read-only token cannot POST)", False),
)
REVOKE_STEPS = (
    ("V1", "Revoked access token → 401 with the discovery challenge", True),
    ("V2", "Revoked refresh token cannot be used", True),
    ("V3", "Revoking an unknown token answers 200 (RFC 7009 §2.2)", False),
)
EXTENDED_STEPS = (
    ("X1", "Signed in: an unregistered redirect_uri is not redirected to"),
    ("X2", "Signed in: authorization without code_challenge"),
    ("X3", "Signed in: code_challenge_method=plain"),
    ("X4", "Signed in: an undefined scope"),
)


class Verifier:
    """Runs the verification steps in order and collects the evidence."""

    def __init__(
        self,
        config: Config,
        client: httpx.Client,
        redactor: Redactor,
        events: EventSource,
        log: TextIO = sys.stderr,
        checkout_src: Path | None = None,
    ) -> None:
        self.cfg = config
        self.http = client
        self.redactor = redactor
        self.events = events
        self.log = log
        self.steps: list[Step] = []
        self.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)
        self.started_at = utc_now()
        self.base = config.base_url
        self.mcp_url = self.base + config.mcp_path
        # Discovery.
        self.resource: str | None = None
        self.authorization_servers: list[str] = []
        self.as_meta: dict[str, Any] = {}
        self.deployed: dict[str, Any] = {}
        # Registration and consent.
        self.callback: CallbackServer | None = None
        self.redirect_uri: str | None = None
        self.client_id: str | None = None
        self.registrations: list[dict[str, Any]] = []
        self.code: str | None = None
        self.code_verifier: str | None = None
        self.consent_via: list[str] = []
        # Tokens and MCP.
        self.tokens: list[TokenPair] = []
        self.current: TokenPair | None = None
        self.session_id: str | None = None
        self.legacy_tool_count: int | None = None
        src = checkout_src
        self.instructions_base = (
            read_module_constant(src / "mcp_server" / "transport.py", "SERVER_INSTRUCTIONS_BASE")
            if src
            else None
        )
        self.checkout_version = (
            read_module_constant(src / "config" / "constants.py", "APP_VERSION") if src else None
        )

    # ------------------------------------------------------------ recording

    def say(self, message: str) -> None:
        """Progress output on the log stream, scrubbed."""
        print(self.redactor.scrub_text(message), file=self.log, flush=True)

    def prompt(self, message: str) -> None:
        """Operator instructions; not scrubbed because they carry the URL to open."""
        print(message, file=self.log, flush=True)

    @contextlib.contextmanager
    def step(self, step_id: str, section: str, title: str, required: bool) -> Iterator[Step]:
        """Record one step; an exception ends it as failed (or skipped)."""
        step = Step(step_id, section, title, required, started_at=utc_now())
        start = time.monotonic()
        try:
            yield step
        except (_SkipStep, ConsentSkipped) as e:
            step.status, step.note = STATUS_SKIP, str(e)
            step.summary = step.summary or str(e)
        except (StepFailed, ConsentTimeout, RedirectParseError, StateMismatchError) as e:
            step.status, step.note = STATUS_FAIL, str(e)
            step.summary = step.summary or str(e)
        except httpx.HTTPError as e:
            step.status = STATUS_FAIL
            step.summary = step.note = f"request failed: {type(e).__name__}"
        except (ConsentAborted, KeyboardInterrupt):
            step.status = STATUS_FAIL
            step.summary = step.note = "interrupted by the operator"
            self._finish(step, start)
            raise
        except Exception as e:  # noqa: BLE001 - any failure is evidence, not a crash
            step.status = STATUS_FAIL
            step.summary = step.note = f"unexpected error: {type(e).__name__}: {e}"
        else:
            failed = [c["name"] for c in step.checks if not c["ok"]]
            if failed:
                step.status = STATUS_FAIL
                step.summary = "; ".join(
                    filter(None, [step.summary, "failed: " + ", ".join(failed)])
                )
            else:
                step.status = STATUS_INFO if step.observation_only else STATUS_PASS
        self._finish(step, start)

    def _finish(self, step: Step, start: float) -> None:
        step.duration_ms = int((time.monotonic() - start) * 1000)
        self.steps.append(step)
        self.say(f"[{step.status.upper():4}] {step.id} {step.title}: {step.summary}")

    def skipped(self, step_id: str, section: str, title: str, required: bool, reason: str) -> None:
        """Record a step that could not run."""
        step = Step(step_id, section, title, required, started_at=utc_now())
        step.summary = step.note = reason
        self._finish(step, time.monotonic())

    def passed(self, step_id: str) -> bool:
        """True when step ``step_id`` ran and passed."""
        return any(s.id == step_id and s.status == STATUS_PASS for s in self.steps)

    def secret(self, value: str | None, kind: str) -> str | None:
        """Register a secret before it can reach any evidence; returns it."""
        self.redactor.register(value, kind)
        return value

    # ------------------------------------------------------------------ HTTP

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET without following redirects unless asked."""
        return self.http.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        """POST without following redirects."""
        return self.http.post(url, **kwargs)

    def token_request(self, data: dict[str, str]) -> httpx.Response:
        """POST to the token endpoint as a public client.

        Carries ``resource`` like the authorization request: the MCP
        authorization spec requires it on both (RFC 8707 §2).
        """
        form = {**data, "client_id": self.client_id or ""}
        if self.resource:
            form["resource"] = self.resource
        return self.post(
            self.as_meta["token_endpoint"], data=form, headers={"Accept": "application/json"}
        )

    def revoke(self, token: str, hint: str) -> httpx.Response:
        """RFC 7009 revocation as a public client."""
        return self.post(
            self.as_meta["revocation_endpoint"],
            data={"token": token, "token_type_hint": hint, "client_id": self.client_id or ""},
        )

    def refresh(self, pair: TokenPair, scope: str | None = None) -> httpx.Response:
        """Refresh grant, optionally with a ``scope``."""
        data = {"grant_type": "refresh_token", "refresh_token": pair.refresh_token or ""}
        if scope is not None:
            data["scope"] = scope
        return self.token_request(data)

    def mcp_post(
        self,
        token: str | None,
        body: dict[str, Any],
        session_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """POST one JSON-RPC message to the MCP endpoint."""
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        headers.update(extra_headers or {})
        return self.post(self.mcp_url, content=json.dumps(body), headers=headers)

    def initialize(self, token: str | None, request_id: int = 1) -> httpx.Response:
        """A legacy-era ``initialize`` request (opens a session)."""
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "verify_remote_oauth", "version": SCRIPT_VERSION},
            },
        }
        return self.mcp_post(token, body)

    def rpc(self, token: str, method: str, request_id: int, **params: Any) -> httpx.Response:
        """A request on the current MCP session."""
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params:
            body["params"] = params
        return self.mcp_post(token, body, session_id=self.session_id)

    def modern(self, token: str, method: str, request_id: int) -> httpx.Response:
        """A stateless MCP 2026-07-28 request (per-request ``_meta``)."""
        meta = {
            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
            CLIENT_CAPABILITIES_META_KEY: {},
            CLIENT_INFO_META_KEY: {"name": "verify_remote_oauth", "version": SCRIPT_VERSION},
        }
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {"_meta": meta}}
        headers = {"MCP-Protocol-Version": MODERN_PROTOCOL_VERSION, "Mcp-Method": method}
        return self.mcp_post(token, body, extra_headers=headers)

    # ------------------------------------------------------- evidence shapes

    @staticmethod
    def challenge_evidence(resp: httpx.Response) -> dict[str, Any]:
        """Status and parsed ``WWW-Authenticate`` of an MCP answer."""
        challenge = parse_www_authenticate(resp.headers.get("www-authenticate"))
        return {
            "status": resp.status_code,
            "www_authenticate": resp.headers.get("www-authenticate"),
            "challenge_scheme": challenge.get("scheme"),
            "challenge_error": challenge.get("error"),
            "resource_metadata": challenge.get("resource_metadata"),
        }

    @staticmethod
    def is_discovery_challenge(resp: httpx.Response) -> bool:
        """401 with a ``Bearer`` challenge naming the protected-resource metadata."""
        challenge = parse_www_authenticate(resp.headers.get("www-authenticate"))
        return (
            resp.status_code == 401
            and challenge.get("scheme", "").lower() == "bearer"
            and bool(challenge.get("resource_metadata"))
        )

    def token_evidence(self, resp: httpx.Response) -> dict[str, Any]:
        """Describe a token-endpoint answer without any token value."""
        body = json_dict(resp)
        self.secret(body.get("access_token"), "access_token")
        self.secret(body.get("refresh_token"), "refresh_token")
        out: dict[str, Any] = {"status": resp.status_code}
        if "error" in body:
            out["error"] = body.get("error")
            out["error_description"] = body.get("error_description")
        else:
            out.update(
                {
                    "token_type": body.get("token_type"),
                    "expires_in": body.get("expires_in"),
                    "scope": body.get("scope"),
                    "access_token": mask(body.get("access_token")),
                    "refresh_token": mask(body.get("refresh_token")),
                    "refresh_token_issued": bool(body.get("refresh_token")),
                    "cache_control": resp.headers.get("cache-control"),
                }
            )
        return out

    def keep_tokens(self, resp: httpx.Response, label: str) -> TokenPair | None:
        """Track a successful token response so cleanup can revoke it."""
        body = json_dict(resp)
        if resp.status_code != 200 or not body.get("access_token"):
            return None
        pair = TokenPair(
            label=label,
            access_token=self.secret(body["access_token"], "access_token") or "",
            refresh_token=self.secret(body.get("refresh_token"), "refresh_token"),
            scope=body.get("scope"),
        )
        self.tokens.append(pair)
        return pair

    def adopt(self, resp: httpx.Response, label: str) -> TokenPair | None:
        """Keep a refresh response's pair and make it the current one."""
        pair = self.keep_tokens(resp, label)
        if pair is not None:
            self.current = pair
        return pair

    # ------------------------------------------------------------------- run

    def run(self) -> dict[str, Any]:
        """Run every section; always finishes with cleanup and a report."""
        try:
            self.discovery()
            if not self.cfg.discovery_only:
                self.main_flow()
        except (ConsentAborted, KeyboardInterrupt):
            self.say("Interrupted: revoking issued tokens and writing partial evidence.")
        finally:
            if not self.cfg.discovery_only:
                self.cleanup()
        return self.report()

    def main_flow(self) -> None:
        """Sections 2-7 in order; each stops when its prerequisite failed."""
        needed = ("authorization_endpoint", "token_endpoint", "registration_endpoint")
        missing = [key for key in needed if not self.as_meta.get(key)]
        if missing:
            self.skipped("R1", "registration", "DCR", True, f"metadata lacks {', '.join(missing)}")
            return
        self.registration()
        if not self.passed("R1"):
            return
        self.authorization()
        if self.code is None:
            return
        self.token_exchange()
        if self.current is None:
            return
        self.introspection()
        self.mcp_session()
        self.refresh_and_scope()
        self.revocation()
        if self.cfg.extended_authorize_checks:
            self.extended_checks()
        else:
            for step_id, title in EXTENDED_STEPS:
                self.skipped(
                    step_id,
                    "authorization",
                    title,
                    False,
                    "post-login only; run with --extended-authorize-checks",
                )

    # ------------------------------------------------------------ 1 discovery

    def discovery(self) -> None:
        """D1-D5: challenge, RFC 9728 / RFC 8414 metadata and deployed version."""
        prm_url = self.base + "/.well-known/oauth-protected-resource"
        with self.step(
            "D1",
            "discovery",
            "Unauthenticated POST /mcp answers 401 with a discovery challenge",
            True,
        ) as s:
            resp = self.initialize(None)
            s.evidence.update(self.challenge_evidence(resp))
            s.check("status 401", resp.status_code == 401)
            s.check("no redirect", not 300 <= resp.status_code < 400)
            s.check("Bearer challenge", s.evidence["challenge_scheme"] == "Bearer")
            s.check("resource_metadata present", bool(s.evidence["resource_metadata"]))
            prm_url = s.evidence["resource_metadata"] or prm_url
            s.summary = (
                f"{resp.status_code}, {s.evidence['challenge_scheme']} challenge, resource_metadata "
                f"{'present' if s.evidence['resource_metadata'] else 'missing'}"
            )

        with self.step("D2", "discovery", "Protected-resource metadata (RFC 9728)", True) as s:
            resp = self.get(prm_url, follow_redirects=True)
            meta = json_dict(resp) if resp.status_code == 200 else {}
            self.resource = meta.get("resource") or self.mcp_url
            self.authorization_servers = list(meta.get("authorization_servers") or [])
            suffixed = self.get(
                f"{self.base}/.well-known/oauth-protected-resource{self.cfg.mcp_path}",
                follow_redirects=True,
            )
            s.evidence.update(
                {
                    "url": prm_url,
                    "status": resp.status_code,
                    "resource": meta.get("resource"),
                    "authorization_servers": meta.get("authorization_servers"),
                    "scopes_supported": meta.get("scopes_supported"),
                    "bearer_methods_supported": meta.get("bearer_methods_supported"),
                    "path_suffixed_statuses": [r.status_code for r in suffixed.history]
                    + [suffixed.status_code],
                }
            )
            s.check("status 200", resp.status_code == 200)
            s.check("resource is the MCP endpoint", meta.get("resource") == self.mcp_url)
            s.check("authorization_servers listed", bool(self.authorization_servers))
            s.check(
                "header bearer method", "header" in (meta.get("bearer_methods_supported") or [])
            )
            s.summary = f"resource {meta.get('resource')}"

        with self.step("D3", "discovery", "Authorization-server metadata (RFC 8414)", True) as s:
            issuer = (self.authorization_servers or [self.base])[0].rstrip("/")
            url = issuer + "/.well-known/oauth-authorization-server"
            resp = self.get(url)
            meta = json_dict(resp) if resp.status_code == 200 else {}
            self.as_meta = meta
            endpoint_keys = (
                "authorization_endpoint",
                "token_endpoint",
                "registration_endpoint",
                "revocation_endpoint",
                "introspection_endpoint",
            )
            endpoints = {key: meta.get(key) for key in endpoint_keys}
            list_keys = (
                "code_challenge_methods_supported",
                "grant_types_supported",
                "response_types_supported",
                "token_endpoint_auth_methods_supported",
                "scopes_supported",
            )
            s.evidence.update(
                {"url": url, "status": resp.status_code, "issuer": meta.get("issuer"), **endpoints}
            )
            s.evidence.update({key: meta.get(key) for key in list_keys})
            s.check("status 200", resp.status_code == 200)
            s.check("issuer matches", str(meta.get("issuer", "")).rstrip("/") == issuer)
            for key in endpoint_keys[:4]:
                s.check(f"{key} present", bool(endpoints[key]))
            if self.base.startswith("https://"):
                s.check(
                    "every endpoint is https",
                    all(str(v).startswith("https://") for v in endpoints.values() if v),
                )
            s.check(
                "S256 advertised", "S256" in (meta.get("code_challenge_methods_supported") or [])
            )
            grants = meta.get("grant_types_supported") or []
            s.check("authorization_code grant", "authorization_code" in grants)
            s.check("refresh_token grant", "refresh_token" in grants)
            s.check(
                "public clients (auth method none)",
                "none" in (meta.get("token_endpoint_auth_methods_supported") or []),
            )
            s.summary = (
                f"PKCE methods {meta.get('code_challenge_methods_supported')}, registration "
                f"endpoint {'present' if endpoints['registration_endpoint'] else 'missing'}"
            )

        with self.step(
            "D4", "discovery", "OpenID discovery document agrees with the RFC 8414 metadata", False
        ) as s:
            if not self.as_meta:
                s.skip("no RFC 8414 metadata")
            issuer = str(self.as_meta.get("issuer", self.base)).rstrip("/")
            resp = self.get(issuer + "/.well-known/openid-configuration")
            oidc = json_dict(resp) if resp.status_code == 200 else {}
            keys = (
                "issuer",
                "authorization_endpoint",
                "token_endpoint",
                "registration_endpoint",
                "revocation_endpoint",
                "code_challenge_methods_supported",
            )
            differing = [key for key in keys if oidc.get(key) != self.as_meta.get(key)]
            s.evidence.update({"status": resp.status_code, "differing_fields": differing})
            s.check("status 200", resp.status_code == 200)
            s.check("same endpoints and PKCE methods", not differing)
            s.summary = "consistent" if not differing else f"differs: {', '.join(differing)}"

        with self.step("D5", "discovery", "Deployed version (public system info)", True) as s:
            resp = self.get(self.base + SYSTEM_INFO_PATH)
            info = json_dict(resp) if resp.status_code == 200 else {}
            self.deployed = {key: info.get(key) for key in ("name", "version", "environment")}
            s.evidence.update({"status": resp.status_code, **self.deployed})
            s.evidence["checkout_version"] = self.checkout_version
            s.check("status 200", resp.status_code == 200)
            s.check("version reported", bool(info.get("version")))
            if self.cfg.expect_version:
                s.check(
                    f"version is {self.cfg.expect_version}",
                    info.get("version") == self.cfg.expect_version,
                )
            s.summary = f"version {info.get('version')} ({info.get('environment')})"

    # --------------------------------------------------------- 2 registration

    def registration(self) -> None:
        """R1-R2: register a loopback public client and the Claude redirect URI."""
        self.callback = CallbackServer(self.events, self.cfg.callback_port)
        self.callback.start()
        self.redirect_uri = f"http://127.0.0.1:{self.callback.port}/callback"
        with self.step("R1", "registration", "DCR: public client, loopback redirect", True) as s:
            self.client_id = self.register_client(s, self.redirect_uri)
        with self.step("R2", "registration", "DCR: Claude's redirect URI is accepted", True) as s:
            self.register_client(s, CLAUDE_REDIRECT_URI)

    def register_client(self, s: Step, redirect_uri: str) -> str | None:
        """Register one public client and check the RFC 7591 response."""
        payload = {
            "client_name": f"{self.cfg.client_name} {self.run_id}",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        resp = self.post(self.as_meta["registration_endpoint"], json=payload)
        body = json_dict(resp)
        client_id = self.secret(body.get("client_id"), "client_id")
        management_uri = body.get("registration_client_uri")
        management_token = self.secret(
            body.get("registration_access_token"), "registration_access_token"
        )
        if resp.status_code in (200, 201) and client_id:
            self.registrations.append(
                {
                    "client_name": payload["client_name"],
                    "redirect_uri": redirect_uri,
                    "uri": management_uri,
                    "token": management_token,
                }
            )
        s.evidence.update(
            {
                "status": resp.status_code,
                "client_name": payload["client_name"],
                "client_id_fingerprint": fingerprint(client_id) if client_id else None,
                "client_secret_field_present": "client_secret" in body,
                "redirect_uris": body.get("redirect_uris"),
                "token_endpoint_auth_method": body.get("token_endpoint_auth_method"),
                "grant_types": body.get("grant_types"),
                "response_types": body.get("response_types"),
                "scope": body.get("scope"),
                "rfc7592_management": bool(management_uri and management_token),
            }
        )
        if "error" in body:
            s.evidence["error"] = body.get("error")
            s.evidence["error_description"] = body.get("error_description")
        s.check("status 201", resp.status_code == 201)
        s.check("client_id issued", bool(client_id))
        s.check(
            "no client secret", not body.get("client_secret") and not body.get("plaintext_secret")
        )
        s.check("auth method none", body.get("token_endpoint_auth_method") == "none")
        s.check(
            "grant types authorization_code + refresh_token",
            {"authorization_code", "refresh_token"} <= set(body.get("grant_types") or []),
        )
        s.check("response type code", body.get("response_types") == ["code"])
        s.check("redirect URI registered", body.get("redirect_uris") == [redirect_uri])
        s.check("scope present", bool(body.get("scope")))
        s.summary = (
            f"{resp.status_code}, auth method {body.get('token_endpoint_auth_method')}, client "
            f"secret {'returned' if body.get('client_secret') else 'absent'}, scope "
            f"'{body.get('scope')}'"
        )
        return client_id

    # -------------------------------------------------------- 3 authorization

    def authorize_url(
        self,
        state: str,
        challenge: str | None,
        method: str | None,
        scope: str | None = None,
        redirect_uri: str | None = None,
    ) -> str:
        """Build an authorization request URL (RFC 6749 §4.1.1 + RFC 7636 + RFC 8707)."""
        params = {
            "response_type": "code",
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri or self.redirect_uri or "",
            "scope": scope if scope is not None else self.cfg.scope,
            "state": state,
        }
        if challenge is not None:
            params["code_challenge"] = challenge
        if method is not None:
            params["code_challenge_method"] = method
        if self.resource:
            params["resource"] = self.resource
        return self.as_meta["authorization_endpoint"] + "?" + urlencode(params)

    def pkce(self, method: str | None) -> tuple[str, str | None]:
        """A registered verifier and its challenge for ``method`` (``None``: no challenge)."""
        verifier = self.secret(new_code_verifier(), "code_verifier") or ""
        challenge = {"S256": s256_challenge(verifier), "plain": verifier}.get(method or "")
        return verifier, self.secret(challenge, "code_challenge")

    def new_state(self) -> str:
        """A registered ``state`` value."""
        return self.secret(secrets.token_urlsafe(24), "state") or ""

    def unauthenticated_authorize(self, url: str, redirect_uri: str) -> dict[str, Any]:
        """GET the authorization endpoint without a session and classify the answer."""
        resp = self.get(url)
        location = resp.headers.get("location", "")
        loc = urlsplit(location) if location else None
        if location.startswith(redirect_uri) and "code=" in location:
            outcome = "code_issued"
        elif 300 <= resp.status_code < 400 and loc and loc.path.rstrip("/").endswith("/login"):
            outcome = "sign_in_redirect"
        elif resp.status_code == 401:
            outcome = "sign_in_required"
        elif 400 <= resp.status_code < 500:
            outcome = "rejected"
        else:
            outcome = "other"
        return {
            "status": resp.status_code,
            "outcome": outcome,
            "location_path": loc.path if loc else None,
            "location_query_params": sorted(parse_qs(loc.query)) if loc else [],
        }

    def authorization(self) -> None:
        """A0-A2: no code without a session, pre-sign-in negatives, the consent."""
        assert self.redirect_uri is not None and self.callback is not None
        with self.step(
            "A0", "authorization", "No authorization code without a signed-in session", True
        ) as s:
            _, challenge = self.pkce("S256")
            result = self.unauthenticated_authorize(
                self.authorize_url(self.new_state(), challenge, "S256"), self.redirect_uri
            )
            s.evidence.update(result)
            s.check("no code issued", result["outcome"] != "code_issued")
            s.check(
                "sent to sign-in", result["outcome"] in ("sign_in_redirect", "sign_in_required")
            )
            s.summary = f"{result['status']} → {result['outcome']}"

        unregistered = f"http://127.0.0.1:{self.callback.port}/unregistered"
        cases = (
            (
                "A1a",
                "Before sign-in: authorization without code_challenge",
                None,
                self.redirect_uri,
            ),
            ("A1b", "Before sign-in: code_challenge_method=plain", "plain", self.redirect_uri),
            ("A1c", "Before sign-in: unregistered redirect_uri", "S256", unregistered),
        )
        for step_id, title, method, redirect in cases:
            with self.step(step_id, "authorization", title, False) as s:
                _, challenge = self.pkce(method)
                url = self.authorize_url(self.new_state(), challenge, method, redirect_uri=redirect)
                result = self.unauthenticated_authorize(url, redirect)
                s.evidence.update(result)
                s.summary = f"{result['status']} → {result['outcome']}"
                if not s.check("no code issued", result["outcome"] != "code_issued"):
                    continue
                if result["outcome"] in ("sign_in_redirect", "sign_in_required"):
                    s.skip(
                        "not observable before sign-in: the server asks for sign-in before it "
                        "validates this parameter (see the X checks)"
                    )
                if result["outcome"] == "other":
                    s.info(f"{result['status']}: neither a rejection nor a sign-in redirect")

        with self.step("A2", "authorization", "Browser consent (S256, state, resource)", True) as s:
            verifier, challenge = self.pkce("S256")
            self.code_verifier = verifier
            state = self.new_state()
            url = self.authorize_url(state, challenge, "S256")
            s.evidence.update(
                {
                    "code_challenge_method": "S256",
                    "scope_requested": self.cfg.scope,
                    "resource_requested": self.resource,
                    "authorize_url": redact_url(url),
                }
            )
            params = self.consent_round(
                "A2", url, "sign in with the dedicated verification account and approve access"
            )
            s.evidence["redirect_received_via"] = params.via
            s.evidence["iss_returned"] = params.iss is not None
            if params.error:
                s.evidence["error"] = params.error
                s.evidence["error_description"] = params.error_description
                raise StepFailed(f"authorization error: {params.error}")
            check_state(params, state)
            s.check("state matches", True)
            s.check("code returned", bool(params.code))
            self.code = params.code
            s.summary = f"approved; redirect via {params.via}; state verified"

    def consent_round(self, step_id: str, url: str, action: str) -> RedirectParams:
        """Print the authorization URL and wait for its redirect (loopback or paste)."""
        assert self.redirect_uri is not None
        self.prompt(
            f"\n== Consent required ({step_id}) ==\n"
            f"Open this URL in your browser, {action}:\n\n  {url}\n\n"
            f"The script listens on {self.redirect_uri} for the redirect. If the browser\n"
            "cannot reach it (for example WSL2 with NAT networking), copy the full URL from\n"
            "the address bar after approving, paste it here and press Enter.\n"
            f"Waiting up to {int(self.cfg.consent_timeout)} s (type 'abort' to stop).\n"
        )
        via, value = wait_for_redirect(
            self.events, (urlsplit(self.redirect_uri).path,), self.cfg.consent_timeout
        )
        params = parse_redirect(value, self.redirect_uri, via)
        self.secret(params.code, "code")
        self.secret(params.state, "state")
        self.consent_via.append(via)
        return params

    # ------------------------------------------------------- 4 token exchange

    def token_exchange(self) -> None:
        """T1-T5: PKCE / redirect_uri negatives on one code, the exchange, replay."""
        assert self.code and self.code_verifier and self.redirect_uri
        base = {
            "grant_type": "authorization_code",
            "code": self.code,
            "redirect_uri": self.redirect_uri,
        }
        wrong_verifier, _ = self.pkce(None)
        negatives = (
            ("T1", "Token request without code_verifier is rejected", {}),
            (
                "T2",
                "Token request with a wrong code_verifier is rejected",
                {"code_verifier": wrong_verifier},
            ),
            (
                "T3",
                "Token request with a different redirect_uri is rejected",
                {
                    "code_verifier": self.code_verifier,
                    "redirect_uri": self.redirect_uri.replace("/callback", "/other"),
                },
            ),
        )
        for step_id, title, extra in negatives:
            with self.step(step_id, "token", title, True) as s:
                resp = self.token_request({**base, **extra})
                s.evidence.update(self.token_evidence(resp))
                s.check("4xx", 400 <= resp.status_code < 500)
                s.check("no token issued", self.keep_tokens(resp, f"{step_id}-unexpected") is None)
                s.summary = f"{resp.status_code} {s.evidence.get('error')}"

        with self.step("T4", "token", "Token exchange with the S256 verifier", True) as s:
            resp = self.token_request({**base, "code_verifier": self.code_verifier})
            s.evidence.update(self.token_evidence(resp))
            s.evidence["cache_control_no_store"] = "no-store" in str(
                s.evidence.get("cache_control")
            )
            self.current = self.keep_tokens(resp, "authorization_code")
            s.check("status 200", resp.status_code == 200)
            s.check("access token issued", self.current is not None)
            s.check("token_type Bearer", str(s.evidence.get("token_type", "")).lower() == "bearer")
            s.check("expires_in present", isinstance(s.evidence.get("expires_in"), int))
            s.summary = (
                f"{resp.status_code}, {s.evidence.get('token_type')}, expires_in "
                f"{s.evidence.get('expires_in')}, scope '{s.evidence.get('scope')}', refresh token "
                f"{'issued' if s.evidence.get('refresh_token_issued') else 'not issued'}; the "
                "three rejected requests did not consume the code"
            )

        with self.step("T5", "token", "The authorization code is single-use", True) as s:
            if self.current is None:
                s.skip("no token from T4")
            resp = self.token_request({**base, "code_verifier": self.code_verifier})
            s.evidence.update(self.token_evidence(resp))
            s.check("4xx", 400 <= resp.status_code < 500)
            s.check("no second token", self.keep_tokens(resp, "T5-unexpected") is None)
            s.summary = f"replay → {resp.status_code} {s.evidence.get('error')}"

    # ----------------------------------------------- 5 resource / audience

    def introspection(self) -> None:
        """S1: the token's recorded audience and scope (RFC 7662)."""
        with self.step(
            "S1", "resource-scope", "Introspection: audience and scope of the token", False
        ) as s:
            endpoint = self.as_meta.get("introspection_endpoint")
            if not endpoint:
                s.skip("no introspection_endpoint advertised")
            assert self.current is not None
            resp = self.post(endpoint, data={"token": self.current.access_token})
            body = json_dict(resp)
            exp, iat = body.get("exp"), body.get("iat")
            s.evidence.update(
                {
                    "status": resp.status_code,
                    "active": body.get("active"),
                    "aud": body.get("aud"),
                    "scope": body.get("scope"),
                    "token_type": body.get("token_type"),
                    "client_id_matches": body.get("client_id") == self.client_id,
                    "lifetime_s": exp - iat
                    if isinstance(exp, int) and isinstance(iat, int)
                    else None,
                }
            )
            s.check("active", body.get("active") is True)
            s.check("aud is the requested resource", body.get("aud") == self.resource)
            s.check("client_id matches", body.get("client_id") == self.client_id)
            s.summary = (
                f"active={body.get('active')}, aud {body.get('aud')}, scope '{body.get('scope')}'"
            )

    # ------------------------------------------------------------------ 6 MCP

    def mcp_session(self) -> None:
        """M1-M7: initialize, tools/list, a read call, session reuse, stateless era."""
        assert self.current is not None
        token = self.current.access_token
        with self.step("M1", "mcp", "MCP initialize with the OAuth token (session era)", True) as s:
            resp = self.initialize(token)
            result = rpc_result(resp)
            self.session_id = self.secret(resp.headers.get("mcp-session-id"), "mcp_session_id")
            instructions = result.get("instructions")
            s.evidence.update(
                {
                    "status": resp.status_code,
                    "content_type": resp.headers.get("content-type"),
                    "protocol_version": result.get("protocolVersion"),
                    "server_info": result.get("serverInfo"),
                    "capabilities": sorted(result.get("capabilities") or {}),
                    "mcp_session_id": mask(self.session_id),
                    **self.instructions_evidence(instructions),
                }
            )
            s.check("status 200", resp.status_code == 200)
            s.check("JSON-RPC result", bool(result))
            s.check("Mcp-Session-Id issued", bool(self.session_id))
            s.check("tools capability", "tools" in (result.get("capabilities") or {}))
            s.summary = (
                f"{resp.status_code}, protocol {result.get('protocolVersion')}, server "
                f"{(result.get('serverInfo') or {}).get('version')}, session id "
                f"{'present' if self.session_id else 'absent'}, instructions "
                f"{s.evidence['instructions_length']} chars"
            )
        if not self.passed("M1"):
            for step_id, title, required in MCP_FOLLOW_UP_STEPS:
                self.skipped(step_id, "mcp", title, required, "prerequisite M1 failed")
            return

        with self.step("M2", "mcp", MCP_FOLLOW_UP_STEPS[0][1], True) as s:
            resp = self.mcp_post(
                token,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                session_id=self.session_id,
            )
            s.evidence["status"] = resp.status_code
            s.check("status 202", resp.status_code == 202)
            s.summary = str(resp.status_code)

        with self.step("M3", "mcp", MCP_FOLLOW_UP_STEPS[1][1], True) as s:
            resp = self.rpc(token, "tools/list", 2)
            tools = rpc_result(resp).get("tools")
            tools = tools if isinstance(tools, list) else []
            self.legacy_tool_count = len(tools)
            s.evidence.update({"status": resp.status_code, **tool_evidence(tools)})
            s.check("status 200", resp.status_code == 200)
            s.check("tools listed", bool(tools))
            s.check("every tool has a title", not s.evidence["tools_missing_title"])
            s.check("every tool has the four hints", not s.evidence["tools_missing_hints"])
            s.summary = (
                f"{len(tools)} tools; read-only {s.evidence['read_only']}, destructive "
                f"{s.evidence['destructive']}, open-world {s.evidence['open_world']}; "
                f"longest name {s.evidence['max_name_length']} chars"
            )

        with self.step("M4", "mcp", MCP_FOLLOW_UP_STEPS[2][1], True) as s:
            resp = self.rpc(token, "tools/call", 3, name=READ_TOOL, arguments={})
            result = rpc_result(resp)
            content = result.get("content")
            content = content if isinstance(content, list) else []
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            error = json_dict(resp).get("error")
            s.evidence.update(
                {
                    "status": resp.status_code,
                    "is_error": result.get("isError", False),
                    "jsonrpc_error_code": error.get("code") if isinstance(error, dict) else None,
                    "content_items": len(content),
                    "text_length": len(text),
                    "context_count": count_contexts(text),
                }
            )
            s.check("status 200", resp.status_code == 200)
            s.check("tool result", bool(result))
            s.check("not an error result", result.get("isError", False) is not True)
            s.summary = (
                f"{resp.status_code}, isError={s.evidence['is_error']}, "
                f"{s.evidence['context_count']} context(s), {len(text)} chars"
            )

        with self.step("M5", "mcp", MCP_FOLLOW_UP_STEPS[3][1], True) as s:
            resp = self.rpc(token, "ping", 4)
            same = resp.headers.get("mcp-session-id") == self.session_id
            s.evidence.update({"status": resp.status_code, "same_session_id_echoed": same})
            s.check("status 200", resp.status_code == 200)
            s.check("empty ping result", json_dict(resp).get("result") == {})
            s.summary = f"{resp.status_code}, same session id echoed: {same}"

        with self.step("M6", "mcp", MCP_FOLLOW_UP_STEPS[4][1], False) as s:
            resp = self.modern(token, "server/discover", 5)
            result = rpc_result(resp)
            s.evidence["discover_status"] = resp.status_code
            if resp.status_code != 200 or not result:
                s.skip(f"server/discover answered {resp.status_code}: stateless era not supported")
            listed = self.modern(token, "tools/list", 6)
            tools = rpc_result(listed).get("tools")
            tools = tools if isinstance(tools, list) else []
            s.evidence.update(
                {
                    "supported_versions": result.get("supportedVersions"),
                    **self.instructions_evidence(result.get("instructions")),
                    "tools_list_status": listed.status_code,
                    "tool_count": len(tools),
                    "session_id_minted": "mcp-session-id" in resp.headers
                    or "mcp-session-id" in listed.headers,
                }
            )
            s.check("tools/list 200", listed.status_code == 200)
            s.check("same tool count as the session era", len(tools) == self.legacy_tool_count)
            s.check("no Mcp-Session-Id minted", not s.evidence["session_id_minted"])
            s.summary = (
                f"discover {resp.status_code} (versions {result.get('supportedVersions')}), "
                f"tools/list {listed.status_code} with {len(tools)} tools"
            )

        with self.step("M7", "mcp", MCP_FOLLOW_UP_STEPS[5][1], False) as s:
            resp = self.mcp_post(
                token,
                {"jsonrpc": "2.0", "id": 7, "method": "ping"},
                session_id="verification-unknown-session",
            )
            error = json_dict(resp).get("error")
            error = error if isinstance(error, dict) else {}
            s.evidence.update(
                {
                    "status": resp.status_code,
                    "jsonrpc_error_code": error.get("code"),
                    "guidance": error.get("message"),
                }
            )
            s.check("status 404", resp.status_code == 404)
            s.summary = f"{resp.status_code}: {error.get('message')}"

    def instructions_evidence(self, instructions: Any) -> dict[str, Any]:
        """Length of the server ``instructions`` and whether it is the checkout's base text."""
        text = instructions if isinstance(instructions, str) else None
        return {
            "instructions_length": len(text) if text is not None else None,
            "instructions_equals_checkout_base": (
                text == self.instructions_base
                if text is not None and self.instructions_base
                else None
            ),
            "checkout_base_length": len(self.instructions_base) if self.instructions_base else None,
        }

    # ----------------------------------------------- 7 refresh / scope / revoke

    def refresh_and_scope(self) -> None:
        """S2, F1-F4, S3: widening refused, rotation, narrowed-token REST scope."""
        a = self.current
        assert a is not None
        if not a.refresh_token:
            for step_id, title, required in REFRESH_STEPS:
                self.skipped(step_id, "refresh", title, required, "no refresh token was issued")
            return

        with self.step("S2", "resource-scope", REFRESH_STEPS[0][1], True) as s:
            widened = f"{a.scope or ''} {UNKNOWN_SCOPE}".strip()
            resp = self.refresh(a, widened)
            s.evidence.update({"scope_requested": widened, **self.token_evidence(resp)})
            s.check("4xx", 400 <= resp.status_code < 500)
            s.check("no token issued", self.adopt(resp, "S2-unexpected") is None)
            s.summary = f"{resp.status_code} {s.evidence.get('error')}"

        a = self.current
        with self.step("F1", "refresh", REFRESH_STEPS[1][1], True) as s:
            resp = self.refresh(a)
            s.evidence.update(self.token_evidence(resp))
            b = self.adopt(resp, "refresh")
            rotated = bool(b and b.refresh_token and b.refresh_token != a.refresh_token)
            s.evidence["refresh_token_rotated"] = rotated
            s.check("status 200", resp.status_code == 200)
            s.check("new access token", b is not None and b.access_token != a.access_token)
            s.summary = (
                f"{resp.status_code}, expires_in {s.evidence.get('expires_in')}, scope "
                f"'{s.evidence.get('scope')}', new refresh token: {rotated}"
            )
        if self.current is a:
            for step_id, title, required in REFRESH_STEPS[2:]:
                self.skipped(step_id, "refresh", title, required, "prerequisite F1 failed")
            return
        b = self.current
        assert b is not None

        with self.step("F2", "refresh", REFRESH_STEPS[2][1], True) as s:
            resp = self.refresh(a)
            s.evidence.update(self.token_evidence(resp))
            unexpected = self.adopt(resp, "F2-unexpected")
            s.evidence["old_refresh_token_still_works"] = unexpected is not None
            s.check("4xx", 400 <= resp.status_code < 500)
            s.summary = (
                f"{resp.status_code} {s.evidence.get('error')}: the earlier refresh token is "
                f"{'still accepted' if unexpected else 'rejected'}"
            )
            b = unexpected or b

        with self.step("F3", "refresh", REFRESH_STEPS[3][1], False) as s:
            resp = self.initialize(a.access_token, 10)
            s.evidence.update(self.challenge_evidence(resp))
            s.check("401 with the discovery challenge", self.is_discovery_challenge(resp))
            s.summary = (
                f"{resp.status_code}: the earlier access token is "
                f"{'rejected' if resp.status_code == 401 else 'still accepted'}"
            )

        with self.step("F4", "refresh", REFRESH_STEPS[4][1], True) as s:
            resp = self.initialize(b.access_token, 11)
            new_session = self.secret(resp.headers.get("mcp-session-id"), "mcp_session_id")
            earlier = self.rpc(b.access_token, "ping", 12)
            s.evidence.update(
                {
                    "status": resp.status_code,
                    "mcp_session_id": mask(new_session),
                    "earlier_session_ping_status": earlier.status_code,
                }
            )
            s.check("status 200", resp.status_code == 200)
            s.summary = (
                f"{resp.status_code}; the session opened with the earlier token answers "
                f"{earlier.status_code} to the refreshed one"
            )

        with self.step("S3", "resource-scope", REFRESH_STEPS[5][1], False) as s:
            granted = (b.scope or "").split()
            if READ_ONLY_SCOPE not in granted or len(granted) < 2:
                s.skip(f"granted scope '{b.scope}' cannot be narrowed to {READ_ONLY_SCOPE}")
            resp = self.refresh(b, READ_ONLY_SCOPE)
            s.evidence["narrowing_refresh"] = self.token_evidence(resp)
            c = self.adopt(resp, "narrowed")
            if c is None:
                raise StepFailed(f"the narrowing refresh answered {resp.status_code}")
            probe = self.post(
                self.base + REST_SCOPE_PROBE_PATH,
                json={"query": "oauth verification probe", "k": 1},
                headers={"Authorization": f"Bearer {c.access_token}"},
            )
            challenge = parse_www_authenticate(probe.headers.get("www-authenticate"))
            mcp = self.initialize(c.access_token, 13)
            s.evidence.update(
                {
                    "narrowed_scope": c.scope,
                    "rest_probe": f"POST {REST_SCOPE_PROBE_PATH}",
                    "rest_status": probe.status_code,
                    "rest_challenge_error": challenge.get("error"),
                    "rest_challenge_scope": challenge.get("scope"),
                    "mcp_initialize_status": mcp.status_code,
                }
            )
            s.check("narrowed token scope", c.scope == READ_ONLY_SCOPE)
            s.check("REST 403", probe.status_code == 403)
            s.check("insufficient_scope challenge", challenge.get("error") == "insufficient_scope")
            s.summary = (
                f"narrowed to '{c.scope}': REST POST → {probe.status_code} "
                f"{challenge.get('error')}; MCP initialize → {mcp.status_code}"
            )

    def revocation(self) -> None:
        """V1-V4: revocation, the expired-token challenge, a bogus token."""
        pair = self.current
        assert pair is not None
        if not self.as_meta.get("revocation_endpoint"):
            for step_id, title, required in REVOKE_STEPS:
                self.skipped(step_id, "revoke", title, required, "no revocation_endpoint")
        else:
            with self.step("V1", "revoke", REVOKE_STEPS[0][1], True) as s:
                resp = self.revoke(pair.access_token, "access_token")
                after = self.initialize(pair.access_token, 20)
                s.evidence["revocation_status"] = resp.status_code
                s.evidence.update(self.challenge_evidence(after))
                s.check("revocation 200", resp.status_code == 200)
                s.check("401 with the discovery challenge", self.is_discovery_challenge(after))
                s.summary = (
                    f"revoke {resp.status_code}; /mcp → {after.status_code}, error="
                    f"{s.evidence['challenge_error']}, resource_metadata "
                    f"{'present' if s.evidence['resource_metadata'] else 'missing'}"
                )
            with self.step("V2", "revoke", REVOKE_STEPS[1][1], True) as s:
                if not pair.refresh_token:
                    s.skip("no refresh token")
                resp = self.revoke(pair.refresh_token or "", "refresh_token")
                after = self.refresh(pair)
                s.evidence["revocation_status"] = resp.status_code
                s.evidence["refresh_after_revocation"] = self.token_evidence(after)
                s.check("revocation 200", resp.status_code == 200)
                s.check(
                    "refresh rejected",
                    400 <= after.status_code < 500
                    and self.keep_tokens(after, "V2-unexpected") is None,
                )
                pair.revoked = all(c["ok"] for c in s.checks)
                s.summary = (
                    f"revoke {resp.status_code}; refresh → {after.status_code} "
                    f"{s.evidence['refresh_after_revocation'].get('error')}"
                )
            with self.step("V3", "revoke", REVOKE_STEPS[2][1], False) as s:
                resp = self.revoke(secrets.token_urlsafe(32), "access_token")
                s.evidence["status"] = resp.status_code
                s.check("status 200", resp.status_code == 200)
                s.summary = str(resp.status_code)

        with self.step(
            "V4", "revoke", "Bogus bearer token → 401 with the discovery challenge", True
        ) as s:
            resp = self.initialize(self.secret(secrets.token_urlsafe(32), "bogus_token"), 21)
            s.evidence.update(self.challenge_evidence(resp))
            s.check("401 with the discovery challenge", self.is_discovery_challenge(resp))
            s.summary = f"{resp.status_code}, error={s.evidence['challenge_error']}"

    # ------------------------------------------------- extended (post-login)

    def extended_checks(self) -> None:
        """X1-X4: the authorization negatives that are only observable signed in."""
        assert self.callback is not None and self.redirect_uri is not None
        unregistered = f"http://127.0.0.1:{self.callback.port}/unregistered"
        with self.step("X1", "authorization", EXTENDED_STEPS[0][1], False) as s:
            _, challenge = self.pkce("S256")
            url = self.authorize_url(self.new_state(), challenge, "S256", redirect_uri=unregistered)
            self.prompt(
                "\n== Extended check X1: unregistered redirect_uri ==\n"
                f"Open this URL while signed in:\n\n  {url}\n\n"
                "Expected: an error page and NO redirect. If the browser was redirected, paste\n"
                "the URL it landed on; otherwise press Enter ('skip' skips this check).\n"
            )
            via, value = wait_for_redirect(
                self.events, ("/unregistered",), self.cfg.consent_timeout, allow_empty=True
            )
            self.secret(value, "redirect_url")
            redirected = via == "loopback" or value.startswith(unregistered)
            s.evidence.update(
                {"redirected": redirected, "observed_by": via if redirected else "operator"}
            )
            s.check("no redirect to the unregistered URI", not redirected)
            s.summary = (
                f"redirected (via {via})"
                if redirected
                else "error page, no redirect (operator-observed)"
            )

        with self.step("X2", "authorization", EXTENDED_STEPS[1][1], False) as s:
            state = self.new_state()
            params = self.consent_round(
                "X2", self.authorize_url(state, None, None), "approve (no code_challenge is sent)"
            )
            self.code_must_be_unusable(s, params, state, verifier=None)

        with self.step("X3", "authorization", EXTENDED_STEPS[2][1], False) as s:
            verifier, challenge = self.pkce("plain")
            state = self.new_state()
            params = self.consent_round(
                "X3", self.authorize_url(state, challenge, "plain"), "approve (PKCE method plain)"
            )
            self.code_must_be_unusable(s, params, state, verifier=verifier)

        with self.step("X4", "authorization", EXTENDED_STEPS[3][1], False) as s:
            verifier, challenge = self.pkce("S256")
            state = self.new_state()
            scope = f"{self.cfg.scope} {UNKNOWN_SCOPE}"
            params = self.consent_round(
                "X4",
                self.authorize_url(state, challenge, "S256", scope=scope),
                "approve (an undefined scope is requested)",
            )
            check_state(params, state)
            s.evidence["scope_requested"] = scope
            if params.error:
                s.evidence["authorize_error"] = params.error
                s.summary = f"rejected at authorization: {params.error}"
                return
            resp = self.token_request(
                {
                    "grant_type": "authorization_code",
                    "code": params.code or "",
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": verifier,
                }
            )
            s.evidence["token"] = self.token_evidence(resp)
            pair = self.keep_tokens(resp, "X4")
            granted = (pair.scope or "").split() if pair else []
            s.evidence["undefined_scope_granted"] = UNKNOWN_SCOPE in granted
            if pair is None:
                s.summary = f"token request answered {resp.status_code}"
            elif UNKNOWN_SCOPE in granted:
                s.info(f"not enforced: the token carries the undefined scope ('{pair.scope}')")
            else:
                s.summary = f"undefined scope dropped; token scope '{pair.scope}'"

    def code_must_be_unusable(
        self, s: Step, params: RedirectParams, state: str, verifier: str | None
    ) -> None:
        """X2/X3: pass when authorization refuses, or its code yields no token."""
        assert self.redirect_uri is not None
        check_state(params, state)
        if params.error:
            s.evidence["authorize_error"] = params.error
            s.summary = f"rejected at authorization: {params.error}"
            return
        base = {
            "grant_type": "authorization_code",
            "code": params.code or "",
            "redirect_uri": self.redirect_uri,
        }
        second = verifier or self.pkce(None)[0]
        attempts = {
            "without_verifier": base,
            "with_verifier" if verifier else "with_random_verifier": {
                **base,
                "code_verifier": second,
            },
        }
        issued = []
        for name, data in attempts.items():
            resp = self.token_request(data)
            s.evidence[name] = self.token_evidence(resp)
            if self.keep_tokens(resp, f"{s.id}-{name}") is not None:
                issued.append(name)
        s.evidence["code_issued_at_authorization"] = True
        s.evidence["token_issued_by"] = issued
        s.check("no token for this code", not issued)
        s.summary = (
            "code issued, but no token could be obtained with it"
            if not issued
            else f"a token was issued ({', '.join(issued)})"
        )

    # ---------------------------------------------------------------- 8 cleanup

    def cleanup(self) -> None:
        """C1-C2: revoke every issued token; delete registrations if RFC 7592 allows."""
        with self.step("C1", "cleanup", "Revoke every token issued during the run", False) as s:
            if not self.tokens:
                s.skip("no tokens were issued")
            if not self.as_meta.get("revocation_endpoint"):
                s.skip("no revocation_endpoint: tokens expire on their own")
            statuses = []
            for pair in self.tokens:
                if pair.revoked:
                    continue
                statuses.append(self.revoke(pair.access_token, "access_token").status_code)
                if pair.refresh_token:
                    statuses.append(self.revoke(pair.refresh_token, "refresh_token").status_code)
                pair.revoked = True
            s.evidence.update({"token_pairs": len(self.tokens), "revocation_statuses": statuses})
            s.check("every revocation answered 200", all(code == 200 for code in statuses))
            s.summary = f"{len(self.tokens)} token pair(s), {len(statuses)} revocation call(s)"

        with self.step(
            "C2", "cleanup", "Delete the DCR registrations (RFC 7592), if supported", False
        ) as s:
            if not self.registrations:
                s.skip("no registrations")
            results = []
            for reg in self.registrations:
                entry = {"client_name": reg["client_name"], "redirect_uri": reg["redirect_uri"]}
                if not (reg["uri"] and reg["token"]):
                    results.append({**entry, "deleted": False})
                    continue
                resp = self.http.delete(
                    reg["uri"], headers={"Authorization": f"Bearer {reg['token']}"}
                )
                results.append(
                    {**entry, "deleted": resp.status_code == 204, "status": resp.status_code}
                )
            s.evidence["registrations"] = results
            if all("status" not in r for r in results):
                s.info(
                    f"{len(results)} registration(s) remain: the server offers no RFC 7592 "
                    "deletion (see the runbook's cleanup section)"
                )
            else:
                s.check("every registration deleted", all(r["deleted"] for r in results))
                s.summary = f"{sum(r['deleted'] for r in results)}/{len(results)} deleted"
        if self.callback is not None:
            self.callback.stop()
            self.callback = None

    # ----------------------------------------------------------------- report

    def report(self) -> dict[str, Any]:
        """The evidence document (scrubbed later by ``Redactor.dumps``)."""
        counts = dict.fromkeys((STATUS_PASS, STATUS_FAIL, STATUS_SKIP, STATUS_INFO), 0)
        for step in self.steps:
            counts[step.status] += 1
        not_passed = [s.id for s in self.steps if s.required and s.status != STATUS_PASS]
        parts = urlsplit(self.base)
        return {
            "schema": EVIDENCE_SCHEMA,
            "script_version": SCRIPT_VERSION,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "target": {
                "label": self.cfg.label,
                "scheme": parts.scheme,
                "is_loopback": is_loopback_host(parts.hostname or ""),
                "host": parts.netloc if self.cfg.record_host else None,
            },
            "deployed": self.deployed,
            "checkout": {"version": self.checkout_version},
            "options": {
                "scope": self.cfg.scope,
                "discovery_only": self.cfg.discovery_only,
                "extended_authorize_checks": self.cfg.extended_authorize_checks,
            },
            "consent_rounds": len(self.consent_via),
            "consent_redirect_via": self.consent_via,
            "summary": {
                "result": "PASS" if not not_passed else "FAIL",
                "passed": counts[STATUS_PASS],
                "failed": counts[STATUS_FAIL],
                "skipped": counts[STATUS_SKIP],
                "info": counts[STATUS_INFO],
                "required_not_passed": len(not_passed),
                "required_not_passed_ids": not_passed,
            },
            "steps": [step.as_dict() for step in self.steps],
        }


def tool_evidence(tools: list[Any]) -> dict[str, Any]:
    """Title / annotation coverage and hint counts of a ``tools/list`` result."""
    missing_title: list[str] = []
    missing_hints: list[str] = []
    counts = dict.fromkeys(("readOnlyHint", "destructiveHint", "openWorldHint"), 0)
    max_len = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name", ""))
        max_len = max(max_len, len(name))
        if not isinstance(tool.get("title"), str) or not tool.get("title"):
            missing_title.append(name)
        annotations = tool.get("annotations")
        annotations = annotations if isinstance(annotations, dict) else {}
        if not all(isinstance(annotations.get(hint), bool) for hint in TOOL_HINTS):
            missing_hints.append(name)
        for hint in counts:
            counts[hint] += annotations.get(hint) is True
    return {
        "tool_count": len(tools),
        "tools_missing_title": missing_title[:20],
        "tools_missing_hints": missing_hints[:20],
        "read_only": counts["readOnlyHint"],
        "destructive": counts["destructiveHint"],
        "open_world": counts["openWorldHint"],
        "max_name_length": max_len,
    }


# ============================================================================
# CLI
# ============================================================================

HELP_EPILOG = """\
consent step:
  After discovery and registration the script prints an authorization URL.
  Open it in your own browser, sign in with the dedicated verification
  account and approve. The redirect goes to a listener on 127.0.0.1; if the
  browser cannot reach it (WSL2 with NAT networking, a browser on another
  machine), copy the URL the browser lands on and paste it into the terminal.
  A rejected token request does not consume the code, so every token
  negative runs on this one consent. --extended-authorize-checks adds three
  more consents and one look at an error page (post-sign-in checks).

redaction guarantee:
  Access and refresh tokens, authorization codes, code verifiers and
  challenges, state, client secrets, cookies and MCP session ids are recorded
  by presence and length only; client ids as a short SHA-256 fingerprint.
  The target host is replaced by --label unless --record-host is given.
  Before the evidence JSON and the Markdown are written they are checked for
  every secret the run handled; a leak aborts the write.

exit status:
  0 when every required step passed, 1 otherwise, 2 for usage errors.
"""


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(
        prog="verify_remote_oauth.py",
        description=(
            "Verify Remote MCP OAuth (discovery, DCR, PKCE, consent, token, MCP, refresh, "
            "revocation) against a Kagura Memory Cloud deployment and write redacted evidence."
        ),
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add = parser.add_argument
    add("--base-url", required=True, help="deployment origin, e.g. https://memory.example.com")
    add("--out", type=Path, help="write the evidence JSON to this file")
    add("--markdown-out", type=Path, help="also write the Markdown summary to this file")
    add("--label", default="target", help="replaces the host in the evidence (default: target)")
    add("--record-host", action="store_true", help="keep the real host in the evidence")
    add("--scope", default=DEFAULT_SCOPE, help=f"scope to request (default: '{DEFAULT_SCOPE}')")
    add(
        "--client-name", default=DEFAULT_CLIENT_NAME, help="DCR client_name; the run id is appended"
    )
    add("--mcp-path", default="/mcp", help="MCP endpoint path (default: /mcp)")
    add("--callback-port", type=int, default=0, help="loopback callback port (default: any free)")
    add("--consent-timeout", type=float, default=600.0, help="seconds to wait for each redirect")
    add("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    add("--expect-version", help="fail D5 unless the deployment reports this version")
    add("--discovery-only", action="store_true", help="run the discovery steps only")
    add(
        "--extended-authorize-checks",
        action="store_true",
        help="add the post-sign-in authorization checks (three more consents)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
    events: EventSource | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the verification and return the exit status.

    ``transport`` and ``events`` let tests drive the run without a network or
    a terminal.
    """
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        base_url = normalize_base_url(args.base_url)
    except ValueError as e:
        print(f"error: {e}", file=stderr)
        return 2
    cfg = Config(
        base_url=base_url,
        label=args.label,
        record_host=args.record_host,
        scope=args.scope,
        client_name=args.client_name,
        mcp_path="/" + args.mcp_path.strip("/"),
        callback_port=args.callback_port,
        consent_timeout=args.consent_timeout,
        expect_version=args.expect_version,
        discovery_only=args.discovery_only,
        extended_authorize_checks=args.extended_authorize_checks,
    )
    redactor = Redactor(base_url, args.label, args.record_host)
    if events is None:
        events = EventSource()
        events.start_stdin(sys.stdin)
    with httpx.Client(
        timeout=args.timeout,
        follow_redirects=False,
        transport=transport,
        headers={"User-Agent": f"kagura-verify-remote-oauth/{SCRIPT_VERSION}"},
    ) as client:
        verifier = Verifier(
            cfg,
            client,
            redactor,
            events,
            log=stderr,
            checkout_src=Path(__file__).resolve().parents[1] / "src",
        )
        report = verifier.run()
    evidence = redactor.dumps(report)
    markdown = render_markdown(json.loads(evidence))
    redactor.check(markdown)
    if args.out:
        args.out.write_text(evidence + "\n", encoding="utf-8")
        print(f"evidence written to {args.out}", file=stderr)
    if args.markdown_out:
        args.markdown_out.write_text(markdown, encoding="utf-8")
    stdout.write(markdown)
    return 0 if report["summary"]["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
