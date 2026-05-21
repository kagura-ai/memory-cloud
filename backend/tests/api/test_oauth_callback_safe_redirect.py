"""Call-site composition pins for the OAuth callbacks (Issue #776).

The CWE-601 defense for ``return_to`` is split into two layers:

1. **Behaviour** — ``_safe_redirect_url`` rejects dangerous schemes,
   backslash tricks, whitespace, and cross-origin URLs. Covered
   exhaustively by 19 unit tests in ``test_safe_redirect_url.py``.
2. **Composition** — every OAuth callback that reads ``return_to`` from
   Redis MUST delegate the resulting URL through ``_safe_redirect_url``
   before passing it to ``RedirectResponse``. Otherwise the function's
   defenses are bypassed at the call site.

This file pins layer 2. It does NOT exercise the full callbacks via
ASGITransport — those handlers depend on Authlib, the OAuth2 manager,
a real DB session, and Redis, and no full-callback integration tests
exist in this repo to model from. Source-introspection is the right
trade-off here: it catches the regression we care about ("someone
re-introduced the verbatim redirect") without the cost of a brand-new
heavy fixture stack.

If a future refactor extracts the redirect-decision block into a
testable helper, those tests should replace these pins.
"""

from __future__ import annotations

import inspect
import re

from api.routes import auth as auth_module

_DELEGATION_PATTERN = re.compile(r"_safe_redirect_url\s*\(\s*return_to_url\s*\)")


class TestGoogleCallbackComposition:
    """``google_callback`` must delegate ``return_to_url`` to
    ``_safe_redirect_url`` before constructing ``RedirectResponse``."""

    def test_google_callback_delegates_to_safe_redirect_url(self):
        src = inspect.getsource(auth_module.google_callback)
        assert _DELEGATION_PATTERN.search(src), (
            "google_callback no longer delegates redirect_url to "
            "_safe_redirect_url(return_to_url) — CWE-601 server-side "
            "defense bypassed. See Issue #776."
        )

    def test_google_callback_no_verbatim_redirect(self):
        """Defense-in-depth pin: the exact pre-#776 bug shape was
        ``redirect_url = return_to_url`` (assigning the Redis value
        verbatim). Catch any future re-introduction by name."""
        src = inspect.getsource(auth_module.google_callback)
        # Allow ``_safe_redirect_url(return_to_url)`` but reject the bare
        # ``redirect_url = return_to_url`` assignment.
        bare_assignment = re.search(
            r"redirect_url\s*=\s*return_to_url\s*(?:$|[\r\n])", src, re.MULTILINE
        )
        assert bare_assignment is None, (
            "google_callback contains a bare `redirect_url = return_to_url` "
            "assignment — CWE-601 verbatim redirect re-introduced. See #776."
        )


class TestGithubCallbackComposition:
    """``github_callback`` must delegate ``return_to_url`` to
    ``_safe_redirect_url`` before constructing ``RedirectResponse``."""

    def test_github_callback_delegates_to_safe_redirect_url(self):
        src = inspect.getsource(auth_module.github_callback)
        assert _DELEGATION_PATTERN.search(src), (
            "github_callback no longer delegates redirect_url to "
            "_safe_redirect_url(return_to_url) — CWE-601 server-side "
            "defense bypassed. See Issue #776."
        )

    def test_github_callback_no_verbatim_or_short_circuit(self):
        """Pre-#776 bug shape for GitHub was
        ``redirect_url = return_to_url or f"{frontend_url}/workspace/dashboard"``.
        Catch any future re-introduction (including the ``or``-default short-circuit)."""
        src = inspect.getsource(auth_module.github_callback)
        bare_or_default = re.search(r"redirect_url\s*=\s*return_to_url\s+or\s+", src)
        assert bare_or_default is None, (
            "github_callback contains a `redirect_url = return_to_url or ...` "
            "short-circuit — CWE-601 verbatim redirect re-introduced. See #776."
        )


class TestExistingCallbackRegressions:
    """Regression pins for the password / MFA callbacks that already used
    ``_safe_redirect_url`` before #776. If these stop calling it, the
    asymmetry that prompted #776 returns."""

    def test_safe_redirect_url_used_at_module_scope(self):
        """Module-level usage count must include both the new Google +
        GitHub call sites and the pre-existing password + MFA call sites.

        Expected canonical sites (4): google_callback, github_callback,
        password login response, MFA verify response. Allowing ≥4 leaves
        room for future safe usages."""
        src = inspect.getsource(auth_module)
        # Exclude the function definition itself.
        call_sites = re.findall(r"_safe_redirect_url\s*\(", src)
        # 1 definition + ≥4 call sites.
        assert len(call_sites) >= 5, (
            f"_safe_redirect_url is referenced only {len(call_sites)} times in "
            "auth.py — expected ≥5 (1 definition + 4 callbacks). A regression "
            "likely removed it from one of password/MFA/google/github."
        )
