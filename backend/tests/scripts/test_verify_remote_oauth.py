"""Tests for ``scripts/verify_remote_oauth.py`` (#1686).

Pure parts (PKCE, redaction, redirect parsing, Markdown) plus a full run of
the step sequence against an in-process fake deployment served through
``httpx.MockTransport``, with the consent answered by pasting the redirect.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

# Scripts are not an importable package — same sys.path pattern as the other
# tests in this directory.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import verify_remote_oauth as vro  # noqa: E402

BASE = "https://memory.example.test"


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def test_s256_challenge_matches_rfc7636_appendix_b() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert vro.s256_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_s256_challenge_agrees_with_the_server_library() -> None:
    from authlib.oauth2.rfc7636 import create_s256_code_challenge

    verifier = vro.new_code_verifier()
    assert vro.s256_challenge(verifier) == create_s256_code_challenge(verifier)


def test_new_code_verifier_is_valid_and_unique() -> None:
    first, second = vro.new_code_verifier(), vro.new_code_verifier()
    assert re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", first)
    assert len(first) == 64
    assert first != second


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _redactor(record_host: bool = False) -> vro.Redactor:
    return vro.Redactor(BASE, "target", record_host)


def test_redactor_leaves_no_secret_in_evidence() -> None:
    redactor = _redactor()
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    code, verifier = secrets.token_urlsafe(32), vro.new_code_verifier()
    state, session = secrets.token_urlsafe(24), "mcp-" + secrets.token_hex(8)
    for value, kind in (
        (access, "access_token"),
        (refresh, "refresh_token"),
        (code, "code"),
        (verifier, "code_verifier"),
        (state, "state"),
        (session, "mcp_session_id"),
    ):
        redactor.register(value, kind)
    unregistered_secret = secrets.token_urlsafe(20)
    evidence = {
        "access_token": access,  # sensitive key
        "nested": [{"refresh_token": refresh}, {"note": f"echoed {access} in a message"}],
        "location": f"http://127.0.0.1:5000/callback?code={code}&state={state}",
        "header": f"Mcp-Session-Id: {session}",
        "client_secret": unregistered_secret,  # never registered: the key alone masks it
        "free_text": f"verifier {verifier}",
    }

    text = redactor.dumps(evidence)

    for value in (access, refresh, code, verifier, state, session, unregistered_secret):
        assert value not in text
    data = json.loads(text)
    assert data["access_token"] == {"redacted": True, "present": True, "length": len(access)}
    assert data["client_secret"]["length"] == len(unregistered_secret)
    assert "<redacted:code>" in data["location"]
    assert data["location"].startswith("http://127.0.0.1:5000/callback")  # loopback kept


def test_redactor_replaces_hosts_unless_recorded() -> None:
    evidence = {
        "resource": f"{BASE}/mcp",
        "authorize": f"{BASE}/api/v1/oauth/authorize?resource=https%3A%2F%2Fmemory.example.test%2Fmcp",
        "mention": "served by memory.example.test",
        "other": "https://api.elsewhere.test/x",
        "claude": vro.CLAUDE_REDIRECT_URI,
    }

    scrubbed = json.loads(_redactor().dumps(evidence))
    assert scrubbed["resource"] == "https://<target>/mcp"
    assert "memory.example.test" not in json.dumps(scrubbed)
    assert "https%3A%2F%2F<target>%2Fmcp" in scrubbed["authorize"]
    assert scrubbed["other"] == "https://<target-other-1>/x"
    assert scrubbed["claude"] == vro.CLAUDE_REDIRECT_URI

    kept = json.loads(_redactor(record_host=True).dumps(evidence))
    assert kept["resource"] == f"{BASE}/mcp"


def test_redactor_check_refuses_a_leak() -> None:
    redactor = _redactor()
    token = secrets.token_urlsafe(32)
    redactor.register(token, "access_token")
    with pytest.raises(RuntimeError, match="access_token"):
        redactor.check(f"| T4 | {token} |")
    redactor.check("| T4 | clean |")


def test_redact_url_masks_sensitive_query_values() -> None:
    url = "https://h.test/cb?code=abc123456&state=xyz987654&scope=memory%3Aread&return_to=%2Fx"
    redacted = vro.redact_url(url)
    assert "abc123456" not in redacted and "xyz987654" not in redacted
    assert "scope=memory%3Aread" in redacted
    assert "code=<redacted>" in redacted and "return_to=<redacted>" in redacted


# ---------------------------------------------------------------------------
# Redirect parsing and state
# ---------------------------------------------------------------------------

REDIRECT = "http://127.0.0.1:43123/callback"


def test_parse_redirect_full_url_and_query_only() -> None:
    params = vro.parse_redirect(
        f"{REDIRECT}?code=c0de-value&state=s1&iss=https%3A%2F%2Fa", REDIRECT
    )
    assert (params.code, params.state, params.iss, params.via) == (
        "c0de-value",
        "s1",
        "https://a",
        "paste",
    )
    pasted = vro.parse_redirect("  ?code=c0de-value&state=s1\n", REDIRECT, via="loopback")
    assert pasted.code == "c0de-value" and pasted.via == "loopback"


def test_parse_redirect_error_response() -> None:
    params = vro.parse_redirect(f"{REDIRECT}?error=access_denied&state=s1", REDIRECT)
    assert params.error == "access_denied" and params.code is None


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("http://127.0.0.1:9999/callback?code=x&state=s", "registered redirect URI"),
        (f"{REDIRECT}/other?code=x&state=s", "registered redirect URI"),
        ("https://evil.test/callback?code=x&state=s", "registered redirect URI"),
        (f"{REDIRECT}?code=a&code=b&state=s", "repeated"),
        (f"{REDIRECT}?state=s", "neither code nor error"),
        ("", "empty"),
    ],
)
def test_parse_redirect_rejects(value: str, message: str) -> None:
    with pytest.raises(vro.RedirectParseError, match=message):
        vro.parse_redirect(value, REDIRECT)


def test_check_state() -> None:
    params = vro.parse_redirect(f"{REDIRECT}?code=x&state=expected-state", REDIRECT)
    vro.check_state(params, "expected-state")
    with pytest.raises(vro.StateMismatchError):
        vro.check_state(params, "another-state")
    missing = vro.parse_redirect(f"{REDIRECT}?code=x", REDIRECT)
    with pytest.raises(vro.StateMismatchError):
        vro.check_state(missing, "expected-state")


def test_wait_for_redirect_sources() -> None:
    events = vro.EventSource()
    events.put("callback", "http://127.0.0.1:1/favicon-ish")  # other path: ignored
    events.put("eof", "")  # a closed stdin does not end the wait
    events.put("paste", "\n")  # blank line ignored unless allow_empty
    events.put("callback", "http://127.0.0.1:1/callback?code=x")
    assert vro.wait_for_redirect(events, ("/callback",), 1) == (
        "loopback",
        "http://127.0.0.1:1/callback?code=x",
    )
    events.put("paste", "\n")
    assert vro.wait_for_redirect(events, ("/callback",), 1, allow_empty=True) == ("paste", "")
    events.put("paste", "skip\n")
    with pytest.raises(vro.ConsentSkipped):
        vro.wait_for_redirect(events, ("/callback",), 1)
    events.put("paste", "ABORT\n")
    with pytest.raises(vro.ConsentAborted):
        vro.wait_for_redirect(events, ("/callback",), 1)
    with pytest.raises(vro.ConsentTimeout):
        vro.wait_for_redirect(events, ("/callback",), 0.05)


# ---------------------------------------------------------------------------
# Small helpers and Markdown
# ---------------------------------------------------------------------------


def test_parse_www_authenticate() -> None:
    header = (
        'Bearer realm="Kagura", error="invalid_token", '
        'resource_metadata="https://h.test/.well-known/oauth-protected-resource"'
    )
    parsed = vro.parse_www_authenticate(header)
    assert parsed["scheme"] == "Bearer"
    assert parsed["error"] == "invalid_token"
    assert parsed["resource_metadata"].endswith("/oauth-protected-resource")
    assert vro.parse_www_authenticate(None) == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://memory.example.test/", "https://memory.example.test"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://localhost:8080/", "http://localhost:8080"),
    ],
)
def test_normalize_base_url_accepts(value: str, expected: str) -> None:
    assert vro.normalize_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    ["http://memory.example.test", "https://memory.example.test/mcp", "ftp://h.test", "h.test"],
)
def test_normalize_base_url_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        vro.normalize_base_url(value)


def test_read_module_constant(tmp_path: Path) -> None:
    module = tmp_path / "m.py"
    module.write_text('X = 1\nTEXT = (\n    "one "\n    "two"\n)\n', encoding="utf-8")
    assert vro.read_module_constant(module, "TEXT") == "one two"
    assert vro.read_module_constant(module, "X") is None
    assert vro.read_module_constant(tmp_path / "missing.py", "TEXT") is None


def test_render_markdown_table() -> None:
    report = {
        "run_id": "20260925T000000Z-abcd",
        "script_version": vro.SCRIPT_VERSION,
        "started_at": "2026-09-25T00:00:00Z",
        "finished_at": "2026-09-25T00:01:00Z",
        "target": {"label": "production", "scheme": "https", "is_loopback": False, "host": None},
        "deployed": {"version": "0.79.0", "environment": "production"},
        "checkout": {"version": "0.79.0"},
        "consent_rounds": 1,
        "summary": {"result": "FAIL", "passed": 1, "failed": 1, "required_not_passed": 1},
        "steps": [
            {
                "id": "D1",
                "title": "Challenge",
                "required": True,
                "status": "pass",
                "summary": "401",
            },
            {
                "id": "T1",
                "title": "Missing | verifier",
                "required": True,
                "status": "fail",
                "summary": "200\nissued",
            },
        ],
    }
    markdown = vro.render_markdown(report)
    assert "### Remote OAuth verification — production" in markdown
    assert "**FAIL**" in markdown and "0.79.0" in markdown and "host not recorded" in markdown
    assert "| D1 | Challenge | yes | PASS | 401 |" in markdown
    assert "| T1 | Missing \\| verifier | yes | FAIL | 200 issued |" in markdown


def test_help_explains_consent_and_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        vro.main(["--help"])
    text = capsys.readouterr().out
    assert "consent step:" in text and "redaction guarantee:" in text


# ---------------------------------------------------------------------------
# Full run against a fake deployment
# ---------------------------------------------------------------------------


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class FakeDeployment:
    """Just enough of the OAuth + MCP surface for the step sequence."""

    def __init__(self, accept_missing_verifier: bool = False) -> None:
        self.accept_missing_verifier = accept_missing_verifier
        self.clients: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, str] = {}
        self.sessions: set[str] = set()
        self.issued: list[str] = []  # every secret value handed out
        self.paths: list[str] = []

    # -- consent (what the browser + the operator do) --------------------
    def approve(self, authorize_url: str) -> str:
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        code = secrets.token_urlsafe(32)
        self.codes[code] = {
            "client_id": query["client_id"],
            "redirect_uri": query["redirect_uri"],
            "challenge": query.get("code_challenge"),
            "scope": query.get("scope", ""),
        }
        self.issued += [code, query["state"], query["client_id"]]
        return f"{query['redirect_uri']}?{urlencode({'code': code, 'state': query['state']})}"

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def error(status: int, error: str) -> httpx.Response:
        return httpx.Response(status, json={"error": error, "error_description": error})

    def issue(self, client_id: str, scope: str, resource: str | None) -> httpx.Response:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.tokens[access] = {
            "client_id": client_id,
            "scope": scope,
            "resource": resource,
            "revoked": False,
            "refresh": refresh,
            "refresh_revoked": False,
        }
        self.refresh[refresh] = access
        self.issued += [access, refresh]
        body = {
            "token_type": "Bearer",
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 3600,
            "scope": scope,
        }
        return httpx.Response(200, json=body, headers={"Cache-Control": "no-store"})

    def bearer(self, request: httpx.Request) -> dict[str, Any] | None:
        auth = request.headers.get("authorization", "")
        token = self.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        return token if token and not token["revoked"] else None

    # -- routing ----------------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(f"{request.method} {path}")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        prm = f"{BASE}/.well-known/oauth-protected-resource"
        if path == "/mcp":
            return self.mcp(request)
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": f"{BASE}/mcp",
                    "authorization_servers": [BASE],
                    "scopes_supported": ["memory:read", "memory:write"],
                    "bearer_methods_supported": ["header"],
                },
            )
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(301, headers={"location": prm})
        if path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
            return httpx.Response(200, json=self.metadata())
        if path == "/api/v1/system/info":
            return httpx.Response(
                200, json={"name": "Fake", "version": "9.9.9", "environment": "test"}
            )
        if path == "/api/v1/oauth/register":
            body = json.loads(request.content)
            client_id = "oauth_" + secrets.token_urlsafe(16)
            self.clients[client_id] = body
            return httpx.Response(
                201,
                json={
                    **body,
                    "client_id": client_id,
                    "scope": "memory:read memory:write offline_access",
                },
            )
        if path == "/api/v1/oauth/authorize":
            return httpx.Response(307, headers={"location": f"{BASE}/login?return_to=x"})
        if path == "/api/v1/oauth/token":
            return self.token(form)
        if path == "/api/v1/oauth/introspect":
            token = self.tokens.get(form.get("token", ""))
            if not token or token["revoked"]:
                return httpx.Response(200, json={"active": False})
            return httpx.Response(
                200,
                json={
                    "active": True,
                    "client_id": token["client_id"],
                    "scope": token["scope"],
                    "aud": token["resource"],
                    "iat": 1000,
                    "exp": 4600,
                    "token_type": "Bearer",
                },
            )
        if path == "/api/v1/oauth/revoke":
            value = form.get("token", "")
            if value in self.tokens:
                self.tokens[value]["revoked"] = True
            if value in self.refresh:
                self.tokens[self.refresh[value]]["refresh_revoked"] = True
            return httpx.Response(200, json={"status": "ok"})
        if path == vro.REST_SCOPE_PROBE_PATH:
            token = self.bearer(request)
            if token is None:
                return httpx.Response(401)
            if "memory:write" not in token["scope"].split():
                return httpx.Response(
                    403,
                    headers={
                        "www-authenticate": 'Bearer error="insufficient_scope", scope="memory:write"'
                    },
                )
            return httpx.Response(422)
        return httpx.Response(404)

    def metadata(self) -> dict[str, Any]:
        oauth = f"{BASE}/api/v1/oauth"
        return {
            "issuer": BASE,
            "authorization_endpoint": f"{oauth}/authorize",
            "token_endpoint": f"{oauth}/token",
            "registration_endpoint": f"{oauth}/register",
            "revocation_endpoint": f"{oauth}/revoke",
            "introspection_endpoint": f"{oauth}/introspect",
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["none"],
        }

    def token(self, form: dict[str, str]) -> httpx.Response:
        if form.get("grant_type") == "authorization_code":
            entry = self.codes.get(form.get("code", ""))
            if entry is None or entry["client_id"] != form.get("client_id"):
                return self.error(400, "invalid_grant")
            if form.get("redirect_uri") != entry["redirect_uri"]:
                return self.error(400, "invalid_grant")
            verifier = form.get("code_verifier")
            if not verifier and not self.accept_missing_verifier:
                return self.error(400, "invalid_request")
            if verifier and (not entry["challenge"] or _s256(verifier) != entry["challenge"]):
                return self.error(400, "invalid_grant")
            del self.codes[form["code"]]
            return self.issue(entry["client_id"], entry["scope"], form.get("resource"))
        access = self.refresh.get(form.get("refresh_token", ""))
        token = self.tokens.get(access or "")
        if token is None or token["refresh_revoked"]:
            return self.error(400, "invalid_grant")
        scope = form.get("scope") or token["scope"]
        if not set(scope.split()) <= set(token["scope"].split()):
            return self.error(400, "invalid_scope")
        token["revoked"] = token["refresh_revoked"] = True
        return self.issue(token["client_id"], scope, token["resource"])

    def mcp(self, request: httpx.Request) -> httpx.Response:
        if self.bearer(request) is None:
            challenge = f'Bearer error="invalid_token", resource_metadata="{BASE}/.well-known/oauth-protected-resource"'
            return httpx.Response(401, headers={"www-authenticate": challenge}, json={})
        body = json.loads(request.content)
        method, request_id = body.get("method"), body.get("id")
        tools = [
            {
                "name": name,
                "title": name.title(),
                "annotations": {
                    "readOnlyHint": read_only,
                    "destructiveHint": not read_only,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            }
            for name, read_only in (("list_contexts", True), ("forget", False))
        ]

        def result(value: dict[str, Any], session: str | None = None) -> httpx.Response:
            headers = {"mcp-session-id": session} if session else {}
            return httpx.Response(
                200, headers=headers, json={"jsonrpc": "2.0", "id": request_id, "result": value}
            )

        if "_meta" in (body.get("params") or {}):
            if method == "server/discover":
                return result({"supportedVersions": ["2026-07-28"], "instructions": "Fake."})
            return result({"tools": tools})
        if method == "initialize":
            session = "mcp-" + secrets.token_hex(8)
            self.sessions.add(session)
            self.issued.append(session)
            return result(
                {
                    "protocolVersion": body["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "9.9.9"},
                    "instructions": "Fake instructions.",
                },
                session,
            )
        session = request.headers.get("mcp-session-id")
        if session not in self.sessions:
            return httpx.Response(
                404,
                json={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": "re-initialize"},
                },
            )
        if request_id is None:
            return httpx.Response(202)
        if method == "tools/list":
            return result({"tools": tools}, session)
        if method == "tools/call":
            text = json.dumps({"contexts": [{"name": "verification-sample"}]})
            return result({"content": [{"type": "text", "text": text}]}, session)
        return result({}, session)


class ConsentingEvents(vro.EventSource):
    """Answers each consent prompt by pasting the fake deployment's redirect."""

    def __init__(self, deployment: FakeDeployment, log: io.StringIO) -> None:
        super().__init__()
        self.deployment = deployment
        self.log = log
        self.answered = 0

    def get(self, timeout: float) -> tuple[str, str] | None:
        urls = re.findall(r"^\s+(https://\S+/oauth/authorize\?\S+)$", self.log.getvalue(), re.M)
        if len(urls) > self.answered:
            self.answered = len(urls)
            return "paste", self.deployment.approve(urls[-1]) + "\n"
        return super().get(min(timeout, 0.05))


def _run(tmp_path: Path, deployment: FakeDeployment, *extra: str) -> tuple[int, dict, str]:
    log, out = io.StringIO(), io.StringIO()
    evidence_path = tmp_path / "evidence.json"
    status = vro.main(
        ["--base-url", BASE, "--out", str(evidence_path), "--consent-timeout", "5", *extra],
        transport=httpx.MockTransport(deployment),
        events=ConsentingEvents(deployment, log),
        stdout=out,
        stderr=log,
    )
    raw = evidence_path.read_text(encoding="utf-8") if evidence_path.exists() else "{}"
    return status, json.loads(raw), raw + out.getvalue()


def test_happy_path_run_passes_with_one_consent_and_redacted_evidence(tmp_path: Path) -> None:
    deployment = FakeDeployment()
    status, report, emitted = _run(tmp_path, deployment)

    by_id = {step["id"]: step for step in report["steps"]}
    assert status == 0, report["summary"]
    assert report["summary"]["result"] == "PASS"
    assert report["consent_rounds"] == 1
    for step_id in ("D1", "D2", "D3", "D5", "R1", "R2", "A0", "A2", "T1", "T2", "T3", "T4", "T5"):
        assert by_id[step_id]["status"] == "pass", by_id[step_id]
    for step_id in ("S1", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "S2", "F1", "F2", "F3", "F4"):
        assert by_id[step_id]["status"] == "pass", by_id[step_id]
    for step_id in ("S3", "V1", "V2", "V3", "V4", "C1"):
        assert by_id[step_id]["status"] == "pass", by_id[step_id]
    assert by_id["A1a"]["status"] == "skip"  # sign-in comes first on this server
    assert by_id["X1"]["status"] == "skip"  # extended checks are opt-in
    assert by_id["C2"]["status"] == "info"  # no RFC 7592 management URI
    assert by_id["M3"]["evidence"]["tool_count"] == 2
    assert by_id["T4"]["evidence"]["refresh_token_issued"] is True
    assert by_id["S1"]["evidence"]["aud"] == "https://<target>/mcp"

    # Every secret handed out stays out of the evidence and the Markdown.
    assert deployment.issued
    for value in deployment.issued:
        assert value not in emitted
    assert "memory.example.test" not in emitted
    # Every token issued during the run was revoked before exit.
    assert all(
        token["revoked"] and token["refresh_revoked"] for token in deployment.tokens.values()
    )


def test_run_fails_when_a_verifier_is_not_required(tmp_path: Path) -> None:
    deployment = FakeDeployment(accept_missing_verifier=True)
    status, report, _ = _run(tmp_path, deployment)

    assert status == 1
    assert report["summary"]["result"] == "FAIL"
    assert "T1" in report["summary"]["required_not_passed_ids"]
    # The token issued by the faulty T1 exchange was still revoked by cleanup.
    assert deployment.tokens and all(t["revoked"] for t in deployment.tokens.values())


def test_discovery_only_registers_nothing(tmp_path: Path) -> None:
    deployment = FakeDeployment()
    status, report, _ = _run(tmp_path, deployment, "--discovery-only", "--expect-version", "9.9.9")

    assert status == 0
    assert [step["id"] for step in report["steps"]] == ["D1", "D2", "D3", "D4", "D5"]
    assert not any("register" in path for path in deployment.paths)


def test_expect_version_mismatch_fails(tmp_path: Path) -> None:
    status, report, _ = _run(
        tmp_path, FakeDeployment(), "--discovery-only", "--expect-version", "1.0.0"
    )
    assert status == 1
    assert report["summary"]["required_not_passed_ids"] == ["D5"]


def test_http_base_url_is_refused_for_a_remote_host() -> None:
    err = io.StringIO()
    assert vro.main(["--base-url", "http://memory.example.test"], stderr=err) == 2
    assert "loopback" in err.getvalue()
