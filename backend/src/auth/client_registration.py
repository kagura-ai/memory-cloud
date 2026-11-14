"""Serialize operator allocation of IDs across isolated client namespaces."""

from sqlalchemy import select, text
from sqlalchemy.orm import Session


def reserve_client_id(db: Session, client_id: str) -> None:
    """Hold one PostgreSQL transaction advisory lock, with no credential material."""
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
        {"name": "client-registration:" + client_id},
    )


def reserve_oidc_client_id(db: Session, client_id: str) -> None:
    """Refuse an ID already reserved by a synchronization service, under its lock."""
    from models.sync_installation import SyncService

    reserve_client_id(db, client_id)
    if (
        db.scalar(select(SyncService.service_id).where(SyncService.service_client_id == client_id))
        is not None
    ):
        raise ValueError("Client ID is reserved for service authentication")
