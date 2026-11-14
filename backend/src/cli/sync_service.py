"""Operator registration for workspace synchronization (no secrets or worker authentication)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from models.oidc import OIDCClient  # noqa: E402
from models.sync_installation import SyncService  # noqa: E402


def register_service(
    db: Session, service_id: str, browser_client_id: str, service_client_id: str
) -> SyncService:
    """Create an immutable binding; identical registration is an idempotent no-op."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", service_id):
        raise ValueError("Invalid service ID")
    if (
        any(
            not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", c)
            for c in (browser_client_id, service_client_id)
        )
        or browser_client_id == service_client_id
    ):
        raise ValueError("Browser and service client IDs must be valid and distinct")
    client = db.scalar(
        select(OIDCClient)
        .where(OIDCClient.client_id == browser_client_id)
        .with_for_update(read=True)
    )
    if db.get(OIDCClient, service_client_id) is not None:
        raise ValueError("Service client ID must not be an existing OIDC login client")
    if (
        client is None
        or client.revoked_at is not None
        or "integrations:manage" not in client.scope.split()
    ):
        raise ValueError("Register a live OIDC browser client with integrations:manage first")
    existing = db.get(SyncService, service_id)
    if existing is not None:
        if (existing.browser_client_id, existing.service_client_id) != (
            browser_client_id,
            service_client_id,
        ):
            raise ValueError("Existing client bindings are immutable")
        return existing
    service = SyncService(
        service_id=service_id,
        browser_client_id=browser_client_id,
        service_client_id=service_client_id,
    )
    db.add(service)
    db.flush()
    return service


def set_service_enabled(db: Session, service_id: str, enabled: bool) -> SyncService:
    """Advance generation on each real switch, invalidating older pending consent."""
    service = db.scalar(
        select(SyncService).where(SyncService.service_id == service_id).with_for_update()
    )
    if service is None:
        raise ValueError("Synchronization service was not found")
    if service.enabled != enabled:
        service.enabled = enabled
        service.generation += 1
    return service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--service-id", required=True)
    register.add_argument("--browser-client-id", required=True)
    register.add_argument("--service-client-id", required=True)
    for command in ("enable", "disable"):
        commands.add_parser(command).add_argument("--service-id", required=True)
    args = parser.parse_args(argv)
    from cli._env_guard import require_settings
    from cli.db import get_sync_database_url

    require_settings()
    try:
        engine = create_engine(get_sync_database_url(), echo=False, hide_parameters=True)
        try:
            with Session(engine) as db, db.begin():
                service = (
                    register_service(
                        db, args.service_id, args.browser_client_id, args.service_client_id
                    )
                    if args.command == "register"
                    else set_service_enabled(db, args.service_id, args.command == "enable")
                )
                outcome = {
                    "service_id": service.service_id,
                    "enabled": service.enabled,
                    "generation": service.generation,
                }
            print(json.dumps(outcome))
        finally:
            engine.dispose()
    except ValueError as exc:
        print(f"Synchronization registration refused: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print(
            "Synchronization registration failed; no successful commit was confirmed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
