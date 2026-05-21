r"""Unit tests for ``_safe_redirect_url`` (Issue #776).

Closes a CWE-601 defense-in-depth gap on the server-side redirect validator
used by every login callback (password, MFA, Google, GitHub).

The validator previously accepted any input whose ``urlparse().netloc`` was
empty — including ``javascript:``, ``data:``, ``vbscript:``, and the
backslash trick ``/\evil.com/path`` (browsers normalize ``\`` → ``/``,
turning it into a cross-origin nav). The frontend ``safeReturnTo`` helper
(#773) rejected these, but the server-side equivalent did not, so a Redis
write-side attacker could bypass the frontend layer.

TDD order — these tests are written BEFORE the function hardening so the
new cases (scheme allow-list, backslash, whitespace) start RED and turn
GREEN when the function is fixed.
"""

from __future__ import annotations

import pytest

from api.routes.auth import _safe_redirect_url


@pytest.fixture(autouse=True)
def _frontend_url(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin FRONTEND_URL/API_URL to the dev defaults so the same-origin
    allow-list is deterministic regardless of the operator's shell env."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    monkeypatch.setenv("API_URL", "http://localhost:8080")
    return "http://localhost:3000"


DEFAULT = "http://localhost:3000/workspace/dashboard"


class TestPreservedInputs:
    """Inputs that must pass through unchanged."""

    def test_relative_path_preserved(self):
        assert _safe_redirect_url("/workspace/dashboard") == "/workspace/dashboard"

    def test_same_origin_frontend_preserved(self):
        assert _safe_redirect_url("http://localhost:3000/foo") == "http://localhost:3000/foo"

    def test_relative_with_query_preserved(self):
        assert _safe_redirect_url("/profile?refreshed=1") == "/profile?refreshed=1"


class TestCrossOriginRejection:
    """Existing cross-origin defense — regression pins."""

    def test_cross_origin_falls_back(self):
        assert _safe_redirect_url("https://evil.com/path") == DEFAULT

    def test_protocol_relative_falls_back(self):
        # //evil.com/path → urlparse sees netloc=evil.com → cross-origin path
        assert _safe_redirect_url("//evil.com/path") == DEFAULT


class TestDangerousSchemes:
    """NEW (Issue #776) — scheme allow-list (http/https only)."""

    def test_javascript_scheme_falls_back(self):
        assert _safe_redirect_url("javascript:alert(1)") == DEFAULT

    def test_data_uri_falls_back(self):
        assert _safe_redirect_url("data:text/html,<script>alert(1)</script>") == DEFAULT

    def test_vbscript_scheme_falls_back(self):
        assert _safe_redirect_url("vbscript:msgbox") == DEFAULT

    def test_file_scheme_falls_back(self):
        assert _safe_redirect_url("file:///etc/passwd") == DEFAULT

    def test_uppercase_scheme_falls_back(self):
        # urlparse lowercases scheme, so allow-list comparison catches this.
        # Pin here so a future refactor that introspects raw input doesn't
        # silently regress.
        assert _safe_redirect_url("JAVASCRIPT:alert(1)") == DEFAULT


class TestBackslashTrick:
    r"""NEW (Issue #776) — browser normalizes ``\`` → ``/`` so
    ``/\evil.com/path`` becomes ``//evil.com/path`` and navigates
    cross-origin. urlparse leaves netloc empty (it stops at the first ``/``),
    so the old impl returned the input verbatim."""

    def test_backslash_after_slash_falls_back(self):
        assert _safe_redirect_url("/\\evil.com/path") == DEFAULT

    def test_double_backslash_falls_back(self):
        assert _safe_redirect_url("\\\\evil.com/path") == DEFAULT


class TestWhitespaceAndControlChars:
    """NEW (Issue #776) — strip-and-compare defense.

    Leading/trailing/embedded whitespace and control characters can confuse
    URL parsers vs. browsers. CR/LF in particular enables header injection
    if the value ever reaches a response Location header before FastAPI's
    own validation. Belt and suspenders."""

    def test_leading_whitespace_falls_back(self):
        assert _safe_redirect_url(" http://evil.com") == DEFAULT

    def test_trailing_whitespace_falls_back(self):
        # Symmetric with leading: strict strip() rejection (frontend
        # ``safeReturnTo`` does the same).
        assert _safe_redirect_url("/foo ") == DEFAULT

    def test_embedded_newline_falls_back(self):
        assert _safe_redirect_url("/foo\nLocation: evil.com") == DEFAULT

    def test_embedded_carriage_return_falls_back(self):
        assert _safe_redirect_url("/foo\rLocation: evil.com") == DEFAULT

    def test_embedded_tab_falls_back(self):
        assert _safe_redirect_url("/foo\tbar") == DEFAULT

    def test_null_byte_falls_back(self):
        assert _safe_redirect_url("/foo\x00bar") == DEFAULT


class TestEmptyInputs:
    """Existing default behavior — pinned so the if/else collapse in
    google/github callback (which relies on this branch) stays correct."""

    def test_none_returns_default(self):
        assert _safe_redirect_url(None) == DEFAULT

    def test_empty_string_returns_default(self):
        assert _safe_redirect_url("") == DEFAULT
