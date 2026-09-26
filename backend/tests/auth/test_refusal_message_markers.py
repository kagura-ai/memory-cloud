"""403 refusal messages carry no credential-looking markers (#1719).

Both SDKs drop a server message that contains "bearer", "authorization" or
"api_key=" (case-insensitive), because such a message could echo a
credential, and show their own hint instead. On a session-only route that
hint ("requires the workspace OWNER's API key") is wrong, so the refusal
must say what is refused without those words.

- the guard scans every ``HTTPException(status_code=403, detail=...)`` under
  ``src/`` and fails if the literal parts of ``detail`` contain a marker;
  ``headers=`` (``WWW-Authenticate``), comments and docstrings are not
  messages and are not scanned; 403s raised through custom exception
  classes (e.g. ``AuthorizationError``) or ``JSONResponse`` are not covered;
- the wire test pins what a client sees on a real ``SessionUser`` route.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SRC = Path(__file__).resolve().parents[2] / "src"
# The substrings the SDK sanitizers check (python-sdk ``sanitize_server_detail``,
# typescript-sdk ``sanitizeServerDetail``).
MARKERS = ("bearer", "authorization", "api_key=")


def _markers_in(text: str) -> list[str]:
    lowered = text.lower()
    return [m for m in MARKERS if m in lowered]


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _arg(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def _is_403(node: ast.expr | None) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == 403
    if isinstance(node, ast.Attribute):
        return "_403_" in node.attr
    return False


def _forbidden_calls() -> list[tuple[str, int, ast.Call]]:
    calls: list[tuple[str, int, ast.Call]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_name(node) == "HTTPException"
                and _is_403(_arg(node, 0, "status_code"))
            ):
                calls.append((str(path.relative_to(_SRC)), node.lineno, node))
    return calls


def _literal_text(node: ast.expr) -> str:
    """The string-literal parts of ``node`` (f-string pieces included)."""
    return "".join(
        sub.value
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    )


class TestForbiddenDetailGuard:
    def test_scan_sees_the_call_sites(self) -> None:
        # A broken scan (wrong root, renamed class) must not pass silently.
        assert len(_forbidden_calls()) > 10

    def test_markers_are_detected(self) -> None:
        call = ast.parse('HTTPException(status_code=403, detail=f"Bearer {x} " "rejected")').body[0]
        assert isinstance(call, ast.Expr) and isinstance(call.value, ast.Call)
        assert _is_403(_arg(call.value, 0, "status_code"))
        detail = _arg(call.value, 1, "detail")
        assert detail is not None
        assert _markers_in(_literal_text(detail)) == ["bearer"]

    def test_no_403_detail_contains_a_marker(self) -> None:
        offenders = []
        for rel, lineno, call in _forbidden_calls():
            detail = _arg(call, 1, "detail")
            if detail is None:
                continue
            found = _markers_in(_literal_text(detail))
            if found:
                offenders.append(f"{rel}:{lineno}: {found}")
        assert not offenders, (
            "Both SDKs drop a 403 message containing "
            f"{MARKERS} and show a misleading hint instead — reword:\n" + "\n".join(offenders)
        )


class TestSessionOnlyRefusalOnTheWire:
    """``POST /api/v1/me/terms-acceptance`` is a ``SessionUser`` route."""

    @pytest.mark.parametrize(
        "token",
        ["kagura_anykey", "randomoauthtoken"],
        ids=["api_key", "oauth"],
    )
    def test_refusal_shape(self, token: str) -> None:
        from api.main import app

        resp = TestClient(app).post(
            "/api/v1/me/terms-acceptance",
            json={"version": "2026-09"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 403
        assert "www-authenticate" not in resp.headers
        body = resp.json()
        assert body["error"] == "HTTP-403"
        assert body["details"] == {}
        assert _markers_in(body["message"]) == []
        assert "API keys" in body["message"]
        assert "OAuth" in body["message"]
        assert "browser session" in body["message"]
