"""Operator-only service client/key rotation and expired credential cleanup."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, delete, func, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from auth.client_registration import reserve_client_id  # noqa: E402
from auth.sync_service_jwt import key_fingerprint, public_key  # noqa: E402
from models.auth import AuditLog  # noqa: E402
from models.oidc import OIDCClient  # noqa: E402
from models.sync_credential import (  # noqa: E402
    SyncAccessToken,
    SyncAssertionUse,
    SyncCredentialClient,
    SyncSigningKey,
)
from models.sync_installation import SyncService  # noqa: E402
from utils.datetime import utcnow  # noqa: E402


def _audit(db: Session, client_id: str, action: str, kid: str | None = None) -> None:
    db.add(
        AuditLog(
            user_id="service:sync-credential-operator",
            user_email="system:operator",
            action=action,
            resource="sync-client:" + client_id,
            user_metadata={"kid": kid} if kid else {},
        )
    )


def register_client(db: Session, service_id: str) -> SyncCredentialClient:
    """Register exactly the reserved worker identity; never add a human grant."""
    service = db.get(SyncService, service_id)
    if service is None:
        raise ValueError("Synchronization service was not found")
    client_id = service.service_client_id
    reserve_client_id(db, client_id)
    service = db.scalar(
        select(SyncService)
        .where(SyncService.service_id == service_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        service is None
        or service.service_client_id != client_id
        or db.get(OIDCClient, client_id) is not None
    ):
        raise ValueError("Service identity must be separate from OIDC clients")
    client = db.get(SyncCredentialClient, client_id)
    if client is not None:
        if client.service_id != service_id:
            raise ValueError("Service client binding is immutable")
        return client
    client = SyncCredentialClient(client_id=client_id, service_id=service_id)
    db.add(client)
    db.flush()
    _audit(db, client_id, "sync.client.registered")
    return client


def _client(db: Session, client_id: str) -> SyncCredentialClient:
    initial = db.get(SyncCredentialClient, client_id)
    if initial is None:
        raise ValueError("Service client was not found")
    db.scalar(
        select(SyncService)
        .where(SyncService.service_id == initial.service_id)
        .with_for_update(read=True)
    )
    client = db.scalar(
        select(SyncCredentialClient)
        .where(SyncCredentialClient.client_id == client_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if client is None:
        raise ValueError("Service client was not found")
    return client


def add_key(db: Session, client_id: str, document: dict[str, Any]) -> SyncSigningKey:
    """Add the new public key first; keep old keys live until explicit revocation."""
    key = public_key(document)
    fingerprint = key_fingerprint(key)
    _client(db, client_id)
    existing = db.scalar(
        select(SyncSigningKey).where(
            SyncSigningKey.client_id == client_id, SyncSigningKey.kid == key["kid"]
        )
    )
    if existing is not None:
        if existing.fingerprint != fingerprint or existing.revoked_at is not None:
            raise ValueError("Key identity is immutable or retired; use a fresh key ID")
        return existing
    if (
        db.scalar(
            select(SyncSigningKey.id).where(
                SyncSigningKey.client_id == client_id, SyncSigningKey.fingerprint == fingerprint
            )
        )
        is not None
    ):
        raise ValueError("Signing material cannot be reused under another key ID")
    active = db.scalar(
        select(func.count())
        .select_from(SyncSigningKey)
        .where(SyncSigningKey.client_id == client_id, SyncSigningKey.revoked_at.is_(None))
    )
    if active is not None and active >= 8:
        raise ValueError("At most eight live signing keys are allowed per service client")
    row = SyncSigningKey(
        client_id=client_id, kid=key["kid"], public_jwk=key, fingerprint=fingerprint
    )
    db.add(row)
    db.flush()
    _audit(db, client_id, "sync.key.added", key["kid"])
    return row


def revoke_key(db: Session, client_id: str, kid: str) -> SyncSigningKey:
    """Serialize with all admitted operations, revoking assertions and issued tokens."""
    _client(db, client_id)
    row = db.scalar(
        select(SyncSigningKey)
        .where(SyncSigningKey.client_id == client_id, SyncSigningKey.kid == kid)
        .with_for_update()
    )
    if row is None:
        raise ValueError("Service signing key was not found")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        _audit(db, client_id, "sync.key.revoked", kid)
    return row


def set_client_enabled(db: Session, client_id: str, enabled: bool) -> SyncCredentialClient:
    """Every real switch advances generation; old access tokens never revive."""
    client = _client(db, client_id)
    if client.enabled != enabled:
        client.enabled = enabled
        client.generation += 1
        _audit(db, client_id, "sync.client.enabled" if enabled else "sync.client.disabled")
    return client


def prune_expired(db: Session) -> dict[str, int]:
    """Delete expired verifiers/receipts only; never remove a live replay fence."""
    result = {}
    for model, label in ((SyncAccessToken, "tokens"), (SyncAssertionUse, "assertions")):
        rows = db.execute(
            delete(model).where(model.expires_at <= utcnow()).returning(model.client_id)
        )
        result[label] = len(rows.all())
    return result


def _read_public(path: str) -> dict[str, Any]:
    try:
        target = Path(path)
        before = target.lstat()
        if not stat.S_ISREG(before.st_mode) or target.is_symlink():
            raise ValueError()
        descriptor = os.open(
            target,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise ValueError()
            content = source.read(16385)
        if len(content) > 16384:
            raise ValueError()
        document = json.loads(content)
        if not isinstance(document, dict):
            raise ValueError()
        return public_key(document)
    except Exception:
        raise ValueError("Public JWK file is unavailable or invalid") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--service-id", required=True)
    add = commands.add_parser("add-key")
    add.add_argument("--client-id", required=True)
    add.add_argument("--public-jwk-file", required=True)
    revoke = commands.add_parser("revoke-key")
    revoke.add_argument("--client-id", required=True)
    revoke.add_argument("--kid", required=True)
    for name in ("enable", "disable"):
        commands.add_parser(name).add_argument("--client-id", required=True)
    commands.add_parser("prune")
    args = parser.parse_args(argv)
    from cli._env_guard import require_settings
    from cli.db import get_sync_database_url

    require_settings()
    try:
        document = _read_public(args.public_jwk_file) if args.command == "add-key" else None
        engine = create_engine(get_sync_database_url(), echo=False, hide_parameters=True)
        try:
            with Session(engine) as db, db.begin():
                if args.command == "register":
                    client = register_client(db, args.service_id)
                    output = {
                        "client_id": client.client_id,
                        "generation": client.generation,
                        "enabled": client.enabled,
                    }
                elif args.command == "add-key":
                    assert document is not None
                    key = add_key(db, args.client_id, document)
                    output = {
                        "key_id": str(key.id),
                        "kid": key.kid,
                        "revoked": key.revoked_at is not None,
                    }
                elif args.command == "revoke-key":
                    key = revoke_key(db, args.client_id, args.kid)
                    output = {"key_id": str(key.id), "kid": key.kid, "revoked": True}
                elif args.command == "prune":
                    output = prune_expired(db)
                else:
                    client = set_client_enabled(db, args.client_id, args.command == "enable")
                    output = {
                        "client_id": client.client_id,
                        "generation": client.generation,
                        "enabled": client.enabled,
                    }
            print(json.dumps(output))
        finally:
            engine.dispose()
    except ValueError as exc:
        print(f"Sync credential operation refused: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print(
            "Sync credential operation failed; no successful commit was confirmed", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
