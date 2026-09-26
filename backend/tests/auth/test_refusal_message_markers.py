"""403 refusal messages carry no credential-looking markers (#1719).

The workspace clients of both SDKs drop a 403 message that contains
"bearer", "authorization" or "api_key=" (case-insensitive), because such a
message could echo a credential, and show their owner-key hint instead
("requires the workspace OWNER's API key"). A session-only route refuses the
owner's key too, so that hint is wrong there, and the refusal must say what
is refused without those words.

- the guard scans every ``HTTPException`` under ``src/`` whose status is
  ``403``, ``[status.]HTTP_403_FORBIDDEN`` or ``[http.]HTTPStatus.FORBIDDEN``
  and fails if the literal parts of ``detail`` contain a marker, reading
  through names assigned in the same module (``detail=_MSG``,
  ``f"{_PREFIX} ..."``); ``headers=`` (``WWW-Authenticate``), comments and
  docstrings are not messages and are not scanned. Not covered: 403s raised
  through custom exception classes (e.g. ``AuthorizationError``) or
  ``JSONResponse``, and details taken from another module
  (``messages.X``) or built at runtime (``str(exc)``);
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
    """``403``, ``[status.]HTTP_403_FORBIDDEN`` or ``[http.]HTTPStatus.FORBIDDEN``."""
    if isinstance(node, ast.Constant):
        return node.value == 403
    if isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.Name):
        name = node.id
    else:
        return False
    return "_403_" in name or name == "FORBIDDEN"


def _forbidden_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _call_name(node) == "HTTPException"
        and _is_403(_arg(node, 0, "status_code"))
    ]


def _string_bindings(tree: ast.Module) -> dict[str, list[ast.expr]]:
    """Every value assigned to a plain name anywhere in the module.

    Lets ``detail=_MSG`` (or ``f"{_PREFIX} ..."``) be read through the
    name. Scope is ignored on purpose: a name bound in another function
    may flag a false positive, never hide a marker.
    """
    bindings: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings.setdefault(target.id, []).append(value)
    return bindings


def _literal_text(
    node: ast.expr,
    bindings: dict[str, list[ast.expr]],
    _seen: frozenset[str] = frozenset(),
) -> str:
    """The string-literal parts of ``node`` (f-string pieces and names bound
    in the same module included)."""
    parts: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            parts.append(sub.value)
        elif isinstance(sub, ast.Name) and sub.id in bindings and sub.id not in _seen:
            parts.extend(
                _literal_text(value, bindings, _seen | {sub.id}) for value in bindings[sub.id]
            )
    return "".join(parts)


def _offenders(tree: ast.Module) -> list[tuple[int, list[str]]]:
    """``(lineno, markers)`` for each 403 ``HTTPException`` whose detail has a marker."""
    bindings = _string_bindings(tree)
    offenders: list[tuple[int, list[str]]] = []
    for call in _forbidden_calls(tree):
        detail = _arg(call, 1, "detail")
        if detail is None:
            continue
        found = _markers_in(_literal_text(detail, bindings))
        if found:
            offenders.append((call.lineno, found))
    return offenders


def _src_modules() -> list[tuple[str, ast.Module]]:
    return [
        (
            str(path.relative_to(_SRC)),
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
        for path in sorted(_SRC.rglob("*.py"))
    ]


class TestForbiddenDetailGuard:
    def test_scan_sees_the_call_sites(self) -> None:
        # A broken scan (wrong root, renamed class) must not pass silently.
        assert sum(len(_forbidden_calls(tree)) for _, tree in _src_modules()) > 10

    @pytest.mark.parametrize(
        "source",
        [
            'raise HTTPException(403, "Missing Authorization header")',
            'raise HTTPException(detail="Bearer tokens are refused", status_code=403)',
            'raise fastapi.HTTPException(status.HTTP_403_FORBIDDEN, detail=f"api_key={k} refused")',
            'raise HTTPException(HTTP_403_FORBIDDEN, detail="Bearer tokens are refused")',
            'raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail="Bearer refused")',
            'raise HTTPException(http.HTTPStatus.FORBIDDEN, "Bearer tokens are refused")',
            'raise HTTPException(status_code=403, detail=f"Bearer {x} " "rejected")',
            "\n".join(
                [
                    '_MSG = "Bearer tokens are refused"',
                    "def f():",
                    "    raise HTTPException(403, detail=_MSG)",
                ]
            ),
            "\n".join(
                [
                    '_PREFIX: str = "Bearer"',
                    "def f(x):",
                    '    raise HTTPException(403, detail=f"{_PREFIX} {x} refused")',
                ]
            ),
        ],
        ids=[
            "positional",
            "keywords-swapped",
            "status-constant-fstring",
            "imported-status-name",
            "httpstatus",
            "qualified-httpstatus",
            "implicit-concat",
            "module-constant",
            "annotated-constant-in-fstring",
        ],
    )
    def test_a_marker_is_flagged(self, source: str) -> None:
        offenders = _offenders(ast.parse(source))
        assert len(offenders) == 1
        assert offenders[0][1]

    @pytest.mark.parametrize(
        "source",
        [
            # 401 challenges legitimately name the scheme.
            'raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})',
            'raise HTTPException(HTTPStatus.UNAUTHORIZED, detail="Bearer token required")',
            # Headers on a 403 are not the message.
            'raise HTTPException(403, "Not allowed", headers={"WWW-Authenticate": "Bearer"})',
            '_MSG = "Not allowed"\nraise HTTPException(403, detail=_MSG)',
        ],
        ids=["401-challenge", "httpstatus-401", "403-header", "clean-constant"],
    )
    def test_a_clean_call_is_not_flagged(self, source: str) -> None:
        assert _offenders(ast.parse(source)) == []

    def test_no_403_detail_contains_a_marker(self) -> None:
        offenders = [
            f"{rel}:{lineno}: {found}"
            for rel, tree in _src_modules()
            for lineno, found in _offenders(tree)
        ]
        assert not offenders, (
            "The SDK workspace clients drop a 403 message containing "
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
