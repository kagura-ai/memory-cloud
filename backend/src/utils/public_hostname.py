"""Host names of public MCP endpoint aliases (#2809).

Pure functions, no import from the application (the settings validators use
them):

- ``normalize_hostname``: the one stored form of a claimed name (ASCII,
  lower-case, no trailing dot, at least two labels, no port, no wildcard, no
  address literal). ``xn--`` labels are taken as given; no Unicode form is
  ever produced.
- Two suffix lists, never one. ``PASS_THROUGH_SUFFIXES`` (and ``home.arpa``)
  name networks no public registry delegates: a Host there is an internal
  caller. ``RESERVED_SPECIAL_USE`` adds ``arpa`` and ``onion``: never
  claimable, but names under them can be created by third parties, so they
  are not internal callers either.
- ``host_from_header`` / ``is_internal_shape``: the name a request's Host
  header carries, and whether it is an internal caller's shape (empty, an
  address literal, a single label or a pass-through name).
"""

from __future__ import annotations

import ipaddress
import re

# 253 (the longest DNS name) minus the challenge label ``_kagura-public-mcp``
# and its dot: the proof record of the longest claimable name still fits.
MAX_HOSTNAME_LENGTH = 234
MIN_HOSTNAME_LENGTH = 4
# #2979: the CNAME proof target is ``<label>.<zone>`` with a label of
# CNAME_LABEL_HEX_CHARS hex characters; the zone is bounded so that the target
# is itself a host name of at most MAX_HOSTNAME_LENGTH characters.
CNAME_LABEL_HEX_CHARS = 32
CNAME_PROOF_ZONE_MAX_LENGTH = 200

PASS_THROUGH_SUFFIXES: tuple[str, ...] = (
    "localhost",
    "local",
    "internal",
    "invalid",
    "test",
    "example",
    "home",
    "lan",
    "corp",
)
HOME_ARPA = "home.arpa"
RESERVED_SPECIAL_USE: tuple[str, ...] = PASS_THROUGH_SUFFIXES + ("arpa", "onion")

_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def normalize_hostname(raw: str) -> str | None:
    """Return the stored form of ``raw``, or ``None`` when it is not a host name.

    Surrounding whitespace and ONE trailing dot are removed and the name is
    lower-cased. Refused: non-ASCII, any character outside ``[a-z0-9.-]``
    (so a port, ``*``, ``_``, ``@``, ``/``, brackets, whitespace and control
    characters), fewer than two labels, a label that is empty, longer than
    63 or starts or ends with a hyphen, a total length outside
    4..``MAX_HOSTNAME_LENGTH``, and an all-digit last label (address
    literals included).
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if name.endswith("."):
        name = name[:-1]
    # ASCII first: some non-ASCII characters lower-case to ASCII ones.
    if not name.isascii():
        return None
    name = name.lower()
    if not MIN_HOSTNAME_LENGTH <= len(name) <= MAX_HOSTNAME_LENGTH:
        return None
    labels = name.split(".")
    if len(labels) < 2:
        return None
    if not all(_LABEL_RE.fullmatch(label) for label in labels):
        return None
    if labels[-1].isdigit():
        return None
    return name


def _last_label(name: str) -> str:
    return name.rstrip(".").rsplit(".", 1)[-1].lower()


def is_pass_through_name(name: str) -> bool:
    """Whether ``name`` is under a suffix no public registry delegates."""
    lowered = name.rstrip(".").lower()
    if lowered == HOME_ARPA or lowered.endswith("." + HOME_ARPA):
        return True
    return _last_label(lowered) in PASS_THROUGH_SUFFIXES


def is_reserved_special_use(name: str) -> bool:
    """Whether ``name`` is under a special-use suffix (never claimable)."""
    return _last_label(name) in RESERVED_SPECIAL_USE


def host_from_header(value: bytes) -> str:
    """The host name a ``Host`` header value carries.

    The port is removed (a bracketed IPv6 literal is kept with its brackets),
    the result is lower-cased and one trailing dot is removed. Nothing else
    is parsed.
    """
    text = value.decode("latin-1")
    if text.startswith("["):
        end = text.find("]")
        host = text[: end + 1] if end != -1 else text
    elif text.count(":") == 1:
        host = text.split(":", 1)[0]
    else:
        host = text
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    return host


def _is_address_literal(host: str) -> bool:
    candidate = host
    if host.startswith("[") and host.endswith("]"):
        candidate = host[1:-1]
    elif ":" in host:
        return False
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def is_internal_shape(host: str) -> bool:
    """Whether ``host`` (from ``host_from_header``) is an internal caller's shape.

    Empty, an IPv4 or bracketed IPv6 literal, a single label, or a
    pass-through name. Names under ``arpa`` (other than ``home.arpa``) and
    ``onion`` are not internal.
    """
    if not host:
        return True
    if _is_address_literal(host):
        return True
    if "." not in host:
        return True
    return is_pass_through_name(host)
