"""Fail-closed signing configuration for the isolated OIDC provider.

Key material is operator-provided, never generated or logged by the API. A JWK
set may retain previous public keys while a new private key becomes active.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from joserfc.errors import SecurityWarning
from joserfc.jwk import RSAKey

_MAX_SECRET_BYTES = 1024 * 1024
_KID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


class OIDCConfigurationError(ValueError):
    """An unavailable or invalid operator configuration, without secret values."""


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    active_kid: str
    private_key: RSAKey = field(repr=False)
    public_jwks: dict[str, Any]


def read_private_file(path: str | Path, *, label: str) -> bytes:
    """Read a bounded regular secret file, rejecting a replaced final component.

    POSIX hosts require owner-only permissions; Windows deployments must apply
    equivalent file ACLs. The path and file content never appear in errors.
    """
    return _read_private_file(path, label=label)[0]


def _read_private_file(path: str | Path, *, label: str) -> tuple[bytes, os.stat_result]:
    """``read_private_file`` plus the status of the very file the bytes came from."""
    try:
        target = Path(path)
        before = target.lstat()
        if not stat.S_ISREG(before.st_mode) or target.is_symlink():
            raise OIDCConfigurationError(f"{label} must be a regular non-symlink file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(target, flags)
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise OIDCConfigurationError(f"{label} changed while being opened")
            if os.name != "nt" and stat.S_IMODE(opened.st_mode) & 0o077:
                raise OIDCConfigurationError(f"{label} requires owner-only permissions")
            content = source.read(_MAX_SECRET_BYTES + 1)
        if not content or len(content) > _MAX_SECRET_BYTES:
            raise OIDCConfigurationError(f"{label} is empty or exceeds its size limit")
        return content, opened
    except OIDCConfigurationError:
        raise
    except (OSError, ValueError):
        raise OIDCConfigurationError(f"{label} could not be read safely") from None


def _issuer(value: object) -> str:
    if not isinstance(value, str) or any(ord(char) <= 32 or char == "\\" for char in value):
        raise OIDCConfigurationError("OIDC_ISSUER must be a fixed HTTPS origin followed by /oidc")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and parsed.path == "/oidc"
            and not parsed.query
            and not parsed.fragment
            and (port is None or 1 <= port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise OIDCConfigurationError("OIDC_ISSUER must be a fixed HTTPS origin followed by /oidc")
    return value


def _import_rsa(value: dict[str, Any] | bytes, kid: str) -> RSAKey:
    if isinstance(value, dict):
        operations = value.get("key_ops")
        if (
            value.get("kty") != "RSA"
            or value.get("alg", "RS256") != "RS256"
            or value.get("use", "sig") != "sig"
            or (
                operations is not None
                and (
                    not isinstance(operations, list)
                    or not operations
                    or any(operation not in ("sign", "verify") for operation in operations)
                )
            )
        ):
            raise OIDCConfigurationError("OIDC signing keys must be RSA signing keys for RS256")
    try:
        # Weak RSA material is refused, rather than accepted with a warning.
        with warnings.catch_warnings():
            warnings.simplefilter("error", SecurityWarning)
            key = RSAKey.import_key(value, parameters={"kid": kid, "alg": "RS256", "use": "sig"})
        if key.public_key.key_size < 2048:
            raise OIDCConfigurationError("OIDC signing RSA keys must have at least 2048 bits")
        return key
    except OIDCConfigurationError:
        raise
    except Exception:
        # JOSE/cryptography errors can contain input fragments. Do not propagate
        # their text through API errors or a startup traceback.
        raise OIDCConfigurationError("OIDC signing key material is invalid") from None


_KEY_FILE_LABEL = "OIDC_SIGNING_KEYS_FILE"


@dataclass(frozen=True)
class _CachedKeyRing:
    settings: tuple[str, str, str]
    file: tuple[int, ...]
    config: OIDCConfig


@dataclass
class _KeyRingCache:
    """The last key ring parsed in this process, guarded by one lock.

    Importing private RSA material is expensive, so the parsed ring is reused
    while the key file and the settings it was parsed with stay the same.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    entry: _CachedKeyRing | None = None


_key_ring_cache = _KeyRingCache()


def _file_identity(status: os.stat_result) -> tuple[int, ...]:
    """What tells a changed key file apart: a new file (atomic replacement),
    new content (size, modification time) or new permissions or file type."""
    return (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size, status.st_mode)


def load_oidc_config(settings: Any = None) -> OIDCConfig:
    """Return the validated key ring; disabled/unconfigured providers refuse.

    The parsed ring is reused until the key file or the settings change: each
    call compares the file's identity (device, inode, modification time, size
    and mode) and the issuer, active key ID and path with the ones the ring was
    parsed from, and re-reads the file when any differs. An atomically replaced
    key file therefore takes effect on the next call. A replacement that cannot
    be read or validated is refused; the previously parsed keys are never used
    for it. The caller maps failures to a safe 503.
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    if not getattr(settings, "oidc_enabled", False):
        raise OIDCConfigurationError("OIDC provider is disabled")
    issuer = _issuer(getattr(settings, "oidc_issuer", ""))
    active_kid = getattr(settings, "oidc_active_signing_kid", "")
    if not isinstance(active_kid, str) or not _KID.fullmatch(active_kid):
        raise OIDCConfigurationError("OIDC_ACTIVE_SIGNING_KID must identify the active key")
    path = getattr(settings, "oidc_signing_keys_file", "")
    if not path:
        raise OIDCConfigurationError("OIDC_SIGNING_KEYS_FILE is required")
    wanted = (issuer, active_kid, str(path))
    cache = _key_ring_cache
    with cache.lock:
        entry = cache.entry
        if entry is not None and entry.settings == wanted:
            try:
                current = _file_identity(Path(path).lstat())
            except (OSError, ValueError):
                current = None
            if current == entry.file:
                return entry.config
        # Whatever happens below, the old ring no longer answers for this file.
        cache.entry = None
        content, opened = _read_private_file(path, label=_KEY_FILE_LABEL)
        config = _parse_key_ring(content, issuer, active_kid)
        cache.entry = _CachedKeyRing(wanted, _file_identity(opened), config)
        return config


def _parse_key_ring(content: bytes, issuer: str, active_kid: str) -> OIDCConfig:
    keys: dict[str, RSAKey] = {}
    if content.lstrip().startswith(b"{"):
        try:
            document = json.loads(content)
            entries = document["keys"]
            if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise OIDCConfigurationError(
                "OIDC signing file must contain a bounded JWK set"
            ) from None
        for entry in entries:
            if not isinstance(entry, dict):
                raise OIDCConfigurationError("OIDC signing JWK entries must be objects")
            kid = entry.get("kid")
            if not isinstance(kid, str) or not _KID.fullmatch(kid) or kid in keys:
                raise OIDCConfigurationError("OIDC signing keys require distinct nonempty key IDs")
            keys[kid] = _import_rsa(entry, kid)
            if kid == active_kid and "key_ops" in entry and "sign" not in entry["key_ops"]:
                raise OIDCConfigurationError("OIDC active signing key must permit signing")
    else:
        keys[active_kid] = _import_rsa(content, active_kid)
    active = keys.get(active_kid)
    if active is None or not active.is_private:
        raise OIDCConfigurationError("OIDC active signing key requires private RSA material")
    public_keys = []
    for kid, key in keys.items():
        public = key.as_dict(private=False)
        # Project the exact public wire fields; never forward private integers,
        # arbitrary JWK metadata, remote key URLs or an operator's annotations.
        public_keys.append(
            {
                "kty": "RSA",
                "n": public["n"],
                "e": public["e"],
                "kid": kid,
                "alg": "RS256",
                "use": "sig",
            }
        )
    return OIDCConfig(issuer, active_kid, active, {"keys": public_keys})


def oidc_config(settings: Any = None) -> OIDCConfig:
    """Short alias for callers that load the current configuration."""
    return load_oidc_config(settings)
