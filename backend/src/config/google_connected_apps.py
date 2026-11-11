"""Settings parsing for Google connected apps (#2773).

Google's connected apps program links an MCP server into Google's apps through
OAuth. Its client registers by RFC 7591 with app-specific redirect URIs that
the Connected Console shows, and asks the server to keep the refresh token on
refresh ("persistent linking"). The operator copies those URIs into
``GOOGLE_CONNECTED_APPS_REDIRECT_URIS``; only a client whose every redirect URI
is in that list is treated as Google's (``auth.google_connected_apps``).

The list is matched character for character, so each entry must already be
the exact URI: ``https`` on one of Google's OAuth redirector hosts, as written
(no userinfo, port, case variant or trailing dot), a path of one or more
non-empty segments, no query or fragment, nothing that reads as a pattern
(``*``, a trailing ``/``) and nothing under the custom-MCP binding prefix the
``gemini`` provider owns (#2222). A bad entry refuses the configuration at
startup, as ``OAUTH_DCR_RATE_LIMIT_EXEMPT_CIDRS`` does.

The verification tokens (``GOOGLE_CONNECTED_APPS_VERIFICATION_TOKENS``) are
served at ``/.well-known/google-connected-apps``; they are not secret.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

# Google's OAuth redirector hosts: production, sandbox and test (#2222, #2232).
GOOGLE_OAUTH_REDIRECTOR_HOSTS: tuple[str, ...] = (
    "oauth-redirect.googleusercontent.com",
    "oauth-redirect-sandbox.googleusercontent.com",
    "oauth-redirect-test.googleusercontent.com",
)

# The redirector's per-user custom-MCP binding namespace, owned by the
# ``gemini`` DCR provider (#2222). A connected-apps URI never lives under it.
GEMINI_CUSTOM_MCP_PATH_PREFIX = "/r/user_bound_custom-mcp-"

# One or more non-empty path segments of RFC 3986 unreserved characters or
# percent-encodings; no trailing ``/``, no ``;``, ``?``, ``#`` or ``*``.
_SEGMENT = r"(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+"
_PATH_RE = re.compile(rf"(?:/{_SEGMENT})+")

# The redirector's namespaces themselves (``/r/*`` and ``/a/*`` in Google's
# guide): an app's URI lies below one, never at it.
_NAMESPACE_ROOTS = frozenset({"/r", "/a"})


def _check_redirect_uri(entry: str) -> None:
    """Raise ``ValueError`` unless ``entry`` is an exact redirector URI."""
    if any(c.isspace() for c in entry) or "*" in entry:
        raise ValueError("it contains whitespace or '*'")
    for host in GOOGLE_OAUTH_REDIRECTOR_HOSTS:
        base = f"https://{host}"
        if entry.startswith(base + "/"):
            path = entry[len(base) :]
            break
    else:
        raise ValueError(
            "it is not https:// on a Google OAuth redirector host "
            f"({', '.join(GOOGLE_OAUTH_REDIRECTOR_HOSTS)}) written exactly "
            "(no userinfo, port or case variant)"
        )
    if not _PATH_RE.fullmatch(path):
        raise ValueError(
            "its path must be one or more non-empty segments with no query, "
            "fragment, parameters or trailing '/'"
        )
    decoded = unquote(path)
    if decoded.lower() in _NAMESPACE_ROOTS:
        raise ValueError("it is a redirector namespace root, not an app's redirect URI")
    if any(segment in (".", "..") for segment in decoded.split("/")[1:]):
        raise ValueError("its path has a '.' or '..' segment")
    if decoded.lower().startswith(GEMINI_CUSTOM_MCP_PATH_PREFIX.lower()):
        raise ValueError(
            f"it is under {GEMINI_CUSTOM_MCP_PATH_PREFIX}, which belongs to the gemini provider"
        )


def parse_redirect_uris(raw: str) -> tuple[str, ...]:
    """Parse ``GOOGLE_CONNECTED_APPS_REDIRECT_URIS`` (comma-separated).

    Blanks around an entry and blank entries are skipped. The error names the
    entry by position, never by value.

    Raises:
        ValueError: An entry is not an exact redirector URI (see the module
            docstring), or an entry is listed twice.
    """
    entries = tuple(part.strip() for part in raw.split(",") if part.strip())
    for position, entry in enumerate(entries, start=1):
        try:
            _check_redirect_uri(entry)
        except ValueError as e:
            raise ValueError(f"GOOGLE_CONNECTED_APPS_REDIRECT_URIS entry {position}: {e}") from None
    if len(set(entries)) != len(entries):
        raise ValueError("GOOGLE_CONNECTED_APPS_REDIRECT_URIS lists an entry twice")
    return entries


def parse_verification_tokens(raw: str) -> tuple[str, ...]:
    """Parse ``GOOGLE_CONNECTED_APPS_VERIFICATION_TOKENS`` (comma-separated).

    Blanks around a token and blank entries are skipped. The error names the
    token by position, never by value.

    Raises:
        ValueError: A token contains whitespace or a control character (a
            likely paste error), or is listed twice.
    """
    tokens = tuple(part.strip() for part in raw.split(",") if part.strip())
    for position, token in enumerate(tokens, start=1):
        if any(c.isspace() or not c.isprintable() for c in token):
            raise ValueError(
                f"GOOGLE_CONNECTED_APPS_VERIFICATION_TOKENS entry {position} contains "
                "whitespace or a control character"
            )
    if len(set(tokens)) != len(tokens):
        raise ValueError("GOOGLE_CONNECTED_APPS_VERIFICATION_TOKENS lists a token twice")
    return tokens
