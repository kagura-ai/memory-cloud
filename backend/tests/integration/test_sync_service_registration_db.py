"""Operator registration is immutable, separate from login, and revocable by generation."""

from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from cli.sync_service import register_service, set_service_enabled
from models.oidc import OIDCClient
from models.sync_installation import SyncService
from tests.integration.test_alembic_migrations import _sync_engine


def test_operator_binding_and_generation_are_idempotent_and_separate_from_oidc(async_engine):
    engine = _sync_engine()
    sid = "sync-operator-" + uuid4().hex
    browser, worker, other = sid + "-browser", sid + "-worker", sid + "-other"
    try:
        with Session(engine) as db, db.begin():
            db.add_all(
                [
                    OIDCClient(
                        client_id=c,
                        client_name="Operator fixture",
                        client_secret_hash="unused",
                        redirect_uris=["https://fixture.example.test/callback"],
                        scope="openid integrations:manage",
                    )
                    for c in (browser, other)
                ]
            )
        with Session(engine) as db, db.begin():
            row = register_service(db, sid, browser, worker)
            assert row.generation == 1 and row.enabled
            assert register_service(db, sid, browser, worker) is row
            assert set_service_enabled(db, sid, False).generation == 2
            assert set_service_enabled(db, sid, False).generation == 2
            assert set_service_enabled(db, sid, True).generation == 3
        with pytest.raises(ValueError, match="immutable"), Session(engine) as db, db.begin():
            register_service(db, sid, other, worker)
        with pytest.raises(ValueError, match="OIDC login"), Session(engine) as db, db.begin():
            register_service(db, sid + "-new", browser, other)
        with pytest.raises(ValueError), Session(engine) as db, db.begin():
            register_service(db, sid + "-new", browser, browser)
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(SyncService).where(SyncService.service_id == sid))
            db.execute(delete(OIDCClient).where(OIDCClient.client_id.in_([browser, other])))
        engine.dispose()
