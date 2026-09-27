"""Redirect URI matching with wildcard support.

OAuth2 normally requires exact matching of redirect_uri (RFC 6749). However,
ChatGPT and Claude.ai both generate per-connector dynamic callback URLs:

- ChatGPT: https://chatgpt.com/connector/oauth/<unique-connector-id>
- Claude.ai: https://claude.ai/api/mcp/auth_callback (currently fixed but
  may become per-connector)

To support these without forcing users to register a new OAuth client per
connector, this module implements a tightly-scoped wildcard pattern:

- A trailing ``/*`` matches exactly one additional path segment
- Scheme and host (netloc) must match exactly
- Path prefix before ``*`` must match exactly
- The variable segment must not contain ``/``, ``..``, query, or fragment

The wildcard is intentionally limited to a single trailing path segment to
prevent open-redirect attacks where an attacker could pivot to a different
endpoint or append ``?next=evil`` tricks.

Loopback redirects (RFC 8252 §7.3, #1741): native clients such as Claude
Code listen on an ephemeral port, so when the stored and the incoming URI are
both ``http`` on a loopback host (``localhost``, ``127.0.0.1``, ``::1``), the
port is not compared. Scheme, host, path and query still are, and the host
must be the same one: RFC 8252 treats ``localhost`` and ``127.0.0.1`` as
different hosts.

Issue #207.
"""

from urllib.parse import ParseResult, unquote, urlparse

WILDCARD_SUFFIX = "/*"

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _loopback_parts(uri: str) -> ParseResult | None:
    """Parse ``uri`` when it is an ``http`` loopback URI, else return None.

    A URI with userinfo or an unparsable port is not treated as loopback, so
    it falls back to exact matching.
    """
    try:
        parsed = urlparse(uri)
        if parsed.scheme != "http" or (parsed.hostname or "") not in LOOPBACK_HOSTS:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port  # raises ValueError on a non-integer or out-of-range port
    except ValueError:
        return None
    return parsed


def is_loopback_redirect_uri(uri: str) -> bool:
    """Return True when ``uri`` is an ``http`` redirect to a loopback host.

    Args:
        uri: A redirect URI or a stored pattern (a trailing ``/*`` is fine).

    Returns:
        True for ``http://localhost``, ``http://127.0.0.1`` and ``http://[::1]``
        URIs with any port, False otherwise.
    """
    return _loopback_parts(uri) is not None


def redirect_uri_display_host(uri: str) -> str:
    """Return the host a user is sent to, as shown on the consent page.

    The host is lowercased and keeps an explicit port (always present on a
    loopback redirect, which is what tells two local apps apart). IPv6
    addresses keep their brackets.

    Args:
        uri: A redirect URI that already matched a registered pattern.

    Returns:
        ``host`` or ``host:port``; an empty string if ``uri`` has no host.
    """
    try:
        parsed = urlparse(uri)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


def _loopback_authority_matches(stored: ParseResult, incoming: ParseResult) -> bool:
    """Compare two loopback URIs' hosts, ignoring the port (RFC 8252 §7.3)."""
    return stored.hostname == incoming.hostname


def is_valid_redirect_uri_pattern(pattern: str) -> bool:
    """Check whether a stored redirect_uri pattern is well-formed.

    A pattern is valid if it is either:

    - An **exact** URI with no ``*`` characters anywhere. Query strings and
      fragments are allowed (RFC 6749 §3.1.2 permits them), since exact-match
      semantics make them unambiguous.
    - A **wildcard** URI with exactly one trailing ``/*`` and no other ``*``.
      The path prefix before ``/*`` must contain at least one non-empty
      segment — a host-root wildcard like ``https://example.com/*`` is
      rejected because it would whitelist any single-segment path on the
      host, violating the "path prefix pinned" security goal. Wildcard
      patterns must not carry a query string or fragment (both are stripped
      from the incoming URI at match time, so they would be unreachable).

    Args:
        pattern: Stored redirect_uri pattern to validate.

    Returns:
        True if the pattern is well-formed, False otherwise.
    """
    if not pattern:
        return False

    is_wildcard = pattern.endswith(WILDCARD_SUFFIX)

    # Reject any '*' that is not the trailing wildcard suffix.
    if "*" in pattern:
        if not is_wildcard:
            return False
        if pattern.count("*") != 1:
            return False

    target = pattern[: -len(WILDCARD_SUFFIX)] if is_wildcard else pattern
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False

    if is_wildcard:
        # Wildcard patterns must have a pinned path prefix with at least one
        # non-empty segment, and must not carry query/fragment.
        if parsed.query or parsed.fragment:
            return False
        if not parsed.path.strip("/"):
            return False

    return True


def redirect_uri_matches(pattern: str, redirect_uri: str) -> bool:
    """Check whether ``redirect_uri`` matches a stored ``pattern``.

    The pattern may be either an exact URI or a wildcard pattern with a
    trailing ``/*``. Matching is strict on scheme and host; the wildcard only
    accepts exactly one additional path segment. When both URIs are ``http``
    on the same loopback host, the port is not compared (RFC 8252 §7.3).

    Args:
        pattern: Stored pattern (exact URI or trailing-wildcard URI).
        redirect_uri: Incoming redirect_uri to validate.

    Returns:
        True if ``redirect_uri`` is allowed by ``pattern``, False otherwise.
    """
    if not pattern:
        return False

    # Reject incoming URIs that contain a literal '*'. Checked before the
    # exact-match short-circuit so that a pattern string of '*' (rejected at
    # write time, but defensive) cannot match an incoming '*' verbatim.
    if "*" in redirect_uri:
        return False

    if pattern == redirect_uri:
        return True

    incoming_loopback = _loopback_parts(redirect_uri)

    if not pattern.endswith(WILDCARD_SUFFIX):
        stored_loopback = _loopback_parts(pattern)
        if stored_loopback is None or incoming_loopback is None:
            return False
        return (
            _loopback_authority_matches(stored_loopback, incoming_loopback)
            and stored_loopback.path == incoming_loopback.path
            and stored_loopback.params == incoming_loopback.params
            and stored_loopback.query == incoming_loopback.query
            and stored_loopback.fragment == incoming_loopback.fragment
        )

    stored = urlparse(pattern[: -len(WILDCARD_SUFFIX)])
    incoming = urlparse(redirect_uri)

    if stored.scheme != incoming.scheme:
        return False
    stored_loopback = _loopback_parts(pattern[: -len(WILDCARD_SUFFIX)])
    if stored_loopback is not None and incoming_loopback is not None:
        if not _loopback_authority_matches(stored_loopback, incoming_loopback):
            return False
    elif stored.netloc != incoming.netloc:
        return False
    if incoming.query or incoming.fragment:
        return False

    # Incoming path must extend the stored path by exactly one segment.
    # `urlparse` does not decode percent-encoding, so we unquote the suffix
    # before checking for separators and traversal — otherwise an attacker
    # could submit `%2F` ("/") or `%2E%2E` ("..") to pivot endpoints.
    prefix = stored.path + "/"
    if not incoming.path.startswith(prefix):
        return False
    suffix = unquote(incoming.path[len(prefix) :])
    if not suffix:
        return False
    if "/" in suffix:
        return False
    if suffix == "." or ".." in suffix:
        return False
    return True


def any_redirect_uri_matches(patterns: list[str] | None, redirect_uri: str) -> bool:
    """Check whether any pattern in ``patterns`` matches ``redirect_uri``.

    Args:
        patterns: Iterable of stored patterns (may be None or empty).
        redirect_uri: Incoming redirect_uri to validate.

    Returns:
        True if at least one pattern matches, False otherwise.
    """
    if not patterns:
        return False
    return any(redirect_uri_matches(p, redirect_uri) for p in patterns)
