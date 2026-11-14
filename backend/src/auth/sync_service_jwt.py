"""Bounded, locally registered public-key assertions for sync clients (#2806)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from joserfc import jwt
from joserfc.errors import SecurityWarning
from joserfc.jwk import RSAKey

KID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
JTI = re.compile(r"[A-Za-z0-9._-]{16,128}\Z")
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class InvalidAssertion(ValueError):
    """Fixed credential error; never include JOSE input or library exception text."""


@dataclass(frozen=True)
class ClientAssertion:
    """Verified client proof, without retaining the signed assertion or raw JTI."""

    client_id: str
    kid: str
    jti_hash: str = field(repr=False)
    expires_at: datetime


def _integer(value: int) -> str:
    return (
        base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big"))
        .decode("ascii")
        .rstrip("=")
    )


def public_key(document: dict[str, Any]) -> dict[str, str]:
    """Accept only a bounded local public RSA signing JWK, never secret/remote fields."""
    try:
        if (
            set(document) - {"kty", "n", "e", "kid", "alg", "use", "key_ops"}
            or document.get("kty") != "RSA"
            or document.get("alg", "RS256") != "RS256"
            or document.get("use", "sig") != "sig"
            or document.get("key_ops", ["verify"]) != ["verify"]
            or not isinstance(document.get("kid"), str)
            or not KID.fullmatch(document["kid"])
            or any(
                not isinstance(document.get(k), str) or len(document[k]) > 1024 for k in ("n", "e")
            )
        ):
            raise ValueError()
        with warnings.catch_warnings():
            warnings.simplefilter("error", SecurityWarning)
            key = RSAKey.import_key(document)
        if key.is_private or not 2048 <= key.public_key.key_size <= 4096:
            raise ValueError()
        values = key.public_key.public_numbers()
        return {
            "kty": "RSA",
            "n": _integer(values.n),
            "e": _integer(values.e),
            "kid": document["kid"],
            "alg": "RS256",
            "use": "sig",
        }
    except Exception:
        raise ValueError("Invalid public synchronization signing key") from None


def key_fingerprint(document: dict[str, Any]) -> str:
    """Fingerprint public material independently of aliases for the same key."""
    key = public_key(document)
    material = {name: key[name] for name in ("kty", "n", "e")}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def assertion_header(value: str) -> dict[str, Any]:
    """Inspect bounded headers for local key selection; this grants no authority."""
    try:
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError()
        parts = value.split(".")
        if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
            raise ValueError()
        objects = []
        for part in parts[:2]:
            raw = base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True)
            obj = json.loads(raw, object_pairs_hook=_unique)
            if not isinstance(obj, dict):
                raise ValueError()
            objects.append(obj)
        header = objects[0]
        if (
            set(header) - {"alg", "kid", "typ"}
            or header.get("alg") != "RS256"
            or header.get("typ") not in (None, "JWT", "client-authentication+jwt")
            or not isinstance(header.get("kid"), str)
            or not KID.fullmatch(header["kid"])
        ):
            raise ValueError()
        return header
    except Exception:
        raise InvalidAssertion("invalid_client") from None


def verify_assertion(
    value: str, document: dict[str, Any], *, client_id: str, issuer: str, now: int
) -> ClientAssertion:
    """Verify signature and the exact issuer-only RFC10070 first-party profile."""
    try:
        header = assertion_header(value)
        key = public_key(document)
        if header["kid"] != key["kid"]:
            raise ValueError()
        claims = jwt.decode(value, RSAKey.import_key(key), algorithms=["RS256"]).claims
        if (
            set(claims) - {"iss", "sub", "aud", "iat", "exp", "nbf", "jti"}
            or claims.get("iss") != client_id
            or claims.get("sub") != client_id
            or claims.get("aud") != issuer
            or type(claims.get("iat")) is not int
            or type(claims.get("exp")) is not int
            or not claims["iat"] <= now < claims["exp"]
            or not 0 < claims["exp"] - claims["iat"] <= 60
            or ("nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now))
            or not isinstance(claims.get("jti"), str)
            or not JTI.fullmatch(claims["jti"])
        ):
            raise ValueError()
        return ClientAssertion(
            client_id,
            header["kid"],
            hashlib.sha256(claims["jti"].encode()).hexdigest(),
            datetime.fromtimestamp(claims["exp"], UTC).replace(tzinfo=None),
        )
    except Exception:
        raise InvalidAssertion("invalid_client") from None
