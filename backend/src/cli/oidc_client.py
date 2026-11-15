"""Explicit operator provisioning for isolated OIDC clients.

Run ``python -m src.cli.oidc_client --help`` from the backend directory. Secrets
are supplied through a protected file or a hidden terminal prompt, never argv.
"""

from __future__ import annotations

import argparse
import getpass
import hmac
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from auth.oidc_config import read_private_file  # noqa: E402
from auth.password import hash_password  # noqa: E402
from utils.datetime import utcnow  # noqa: E402

if TYPE_CHECKING:
    from models.oidc import OIDCClient

DEFAULT_CLIENT_SCOPE = "openid profile workspaces:read"
_ALLOWED_SCOPES = frozenset(
    {"openid", "profile", "workspaces:read", "integrations:manage", "sync.records:read"}
)
_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


def validate_registration(
    client_id: str, client_name: str, redirect_uris: list[str], scope: str
) -> tuple[list[str], str]:
    """Validate exact redirect registrations and return canonical scope order."""
    if not _CLIENT_ID.fullmatch(client_id):
        raise ValueError(
            "Client ID must use 1–128 ASCII letters, digits, dots, underscores or hyphens"
        )
    if (
        not client_name.strip()
        or len(client_name) > 255
        or any(ord(char) < 32 for char in client_name)
    ):
        raise ValueError("Client name must be nonempty text of at most 255 characters")
    if not 1 <= len(redirect_uris) <= 20 or len(set(redirect_uris)) != len(redirect_uris):
        raise ValueError("Register 1–20 distinct exact HTTPS redirect URIs")
    for uri in redirect_uris:
        if (
            len(uri) > 2048
            or any(ord(char) <= 32 or char == "\\" for char in uri)
            or "*" in unquote(uri)
        ):
            raise ValueError("Redirect URIs must be exact HTTPS URLs without wildcards or controls")
        try:
            parsed = urlsplit(uri)
            port = parsed.port
            valid = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
                and (port is None or 1 <= port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "Redirect URIs must be exact HTTPS URLs without credentials or fragments"
            )
    scopes = scope.split()
    if "openid" not in scopes or not set(scopes) <= _ALLOWED_SCOPES:
        raise ValueError("Register openid and only supported account/integration scopes")
    return list(redirect_uris), " ".join(dict.fromkeys(scopes))


def _validate_secret(secret: str) -> str:
    try:
        encoded = secret.encode("utf-8")
    except UnicodeError:
        raise ValueError("OIDC client secret must be valid UTF-8") from None
    if not 32 <= len(encoded) <= 72 or any(ord(char) < 32 for char in secret):
        raise ValueError("OIDC client secret must contain 32–72 UTF-8 bytes without controls")
    return secret


def read_client_secret(secret_file: str | None = None) -> str:
    """Obtain a secret without printing it or accepting command-line plaintext."""
    if secret_file:
        content = read_private_file(secret_file, label="OIDC client secret file")
        try:
            secret = content.decode("utf-8")
        except UnicodeError:
            raise ValueError("OIDC client secret file must contain UTF-8") from None
        # A text file may end in the one line ending an editor appends. Spaces
        # belong to the secret and are not silently stripped.
        if secret.endswith("\n"):
            secret = secret[:-1]
            if secret.endswith("\r"):
                secret = secret[:-1]
        return _validate_secret(secret)
    if not sys.stdin.isatty():
        raise ValueError("Use --secret-file for noninteractive client registration")
    secret = _validate_secret(getpass.getpass("OIDC client secret: "))
    confirmation = getpass.getpass("Confirm client secret: ")
    if not hmac.compare_digest(secret.encode("utf-8"), confirmation.encode("utf-8")):
        raise ValueError("OIDC client secret confirmation did not match")
    return secret


def register_client(
    db: Session,
    *,
    client_id: str,
    client_name: str,
    redirect_uris: list[str],
    client_secret: str,
    scope: str = DEFAULT_CLIENT_SCOPE,
) -> OIDCClient:
    """Stage one explicit client; the caller owns commit/rollback."""
    from models.oidc import OIDCClient

    redirects, scopes = validate_registration(client_id, client_name, redirect_uris, scope)
    secret = _validate_secret(client_secret)
    from auth.client_registration import reserve_oidc_client_id

    reserve_oidc_client_id(db, client_id)
    if db.get(OIDCClient, client_id) is not None:
        raise ValueError("OIDC client ID already exists; register a distinct client ID")
    client = OIDCClient(
        client_id=client_id,
        client_name=client_name,
        client_secret_hash=hash_password(secret),
        redirect_uris=redirects,
        scope=scopes,
    )
    db.add(client)
    db.flush()
    return client


def revoke_client(db: Session, client_id: str) -> bool:
    """Disable one client; token verification also checks this live state."""
    from models.oidc import OIDCClient

    if not _CLIENT_ID.fullmatch(client_id):
        raise ValueError("Invalid OIDC client ID")
    client = db.execute(
        select(OIDCClient).where(OIDCClient.client_id == client_id).with_for_update()
    ).scalar_one_or_none()
    if client is None:
        return False
    if client.revoked_at is None:
        client.revoked_at = utcnow()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("register", help="Register an exact confidential OIDC client")
    create.add_argument("--client-id", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--redirect-uri", action="append", required=True)
    create.add_argument("--scope", default=DEFAULT_CLIENT_SCOPE)
    create.add_argument("--secret-file")
    revoke = commands.add_parser("revoke", help="Revoke a client and refuse its credentials")
    revoke.add_argument("--client-id", required=True)
    args = parser.parse_args(argv)

    # Keep the production guard before any DB connection or secret prompt.
    from cli._env_guard import require_settings
    from cli.db import get_sync_database_url

    require_settings()
    try:
        secret = ""
        if args.command == "register":
            validate_registration(args.client_id, args.name, args.redirect_uri, args.scope)
            secret = read_client_secret(args.secret_file)
        engine = create_engine(get_sync_database_url(), echo=False, hide_parameters=True)
        try:
            with Session(engine) as db, db.begin():
                if args.command == "register":
                    client = register_client(
                        db,
                        client_id=args.client_id,
                        client_name=args.name,
                        redirect_uris=args.redirect_uri,
                        client_secret=secret,
                        scope=args.scope,
                    )
                    outcome = {
                        "client_id": client.client_id,
                        "scope": client.scope,
                        "status": "registered",
                    }
                else:
                    if not revoke_client(db, args.client_id):
                        raise ValueError("OIDC client was not found")
                    outcome = {"client_id": args.client_id, "status": "revoked"}
            print(json.dumps(outcome))
        finally:
            engine.dispose()
    except (OSError, UnicodeError):
        # UnicodeError is a ValueError; protect input failures before the
        # general validation-error handler can interpolate their details.
        print("OIDC provisioning could not read its protected inputs", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"OIDC provisioning refused: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("OIDC provisioning failed; no successful commit was confirmed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
