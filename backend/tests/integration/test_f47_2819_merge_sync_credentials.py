"""Upgrade either deployed branch and preserve data through the merge node (#2819)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import text
from sqlalchemy.orm import Session

from alembic import command
from auth.sync_service_jwt import key_fingerprint, public_key
from models.sync_credential import SyncAssertionUse, SyncCredentialClient, SyncSigningKey
from models.sync_installation import SyncService
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)
from utils.datetime import utcnow

MERGE = "f47_2819_merge_sync_credentials"
CREDENTIALS = "f45_2806_sync_credentials"
ADMISSION = "f46_2803_admission_record"


@pytest.mark.parametrize("starting_head", [CREDENTIALS, ADMISSION])
def test_upgrade_either_branch_and_merge_roundtrip_preserve_data(starting_head):
    engine = _sync_engine()
    uid, service_id, client_id = ("merge-" + uuid4().hex for _ in range(3))
    notice_id, key_id = uuid4(), uuid4()
    document = public_key(
        RSAKey.generate_key(2048, parameters={"kid": "merge-key"}).as_dict(private=False)
    )

    def migrate(operation, revision):
        with _alembic_at_test_db():
            operation(_get_alembic_config(), revision)

    def assert_preserved():
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT user_id, email, admitted_via FROM users WHERE user_id = :uid"),
                {"uid": uid},
            ).one()
            assert tuple(row) == (
                uid,
                uid + "@example.test",
                "open_registration" if starting_head == ADMISSION else "unknown",
            )
            notice = conn.execute(
                text("SELECT text, plans FROM mcp_notices WHERE id = :id"), {"id": notice_id}
            ).one()
            assert tuple(notice) == (
                "Preserved notice",
                ["max"] if starting_head == ADMISSION else None,
            )
        if starting_head == CREDENTIALS:
            with Session(engine) as db:
                assert db.get(SyncCredentialClient, client_id).service_id == service_id
                key = db.get(SyncSigningKey, key_id)
                assert key.public_jwk == document
                assert key.fingerprint == key_fingerprint(document)
                assert db.get(SyncAssertionUse, (client_id, "a" * 64)) is not None

    try:
        _reset_alembic_state()
        migrate(command.upgrade, starting_head)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (email, user_id, role, timezone, locale, "
                    "is_initial_admin, auth_method) "
                    "VALUES (:email, :uid, 'user', 'UTC', 'en', false, 'oauth')"
                ),
                {"uid": uid, "email": uid + "@example.test"},
            )
            conn.execute(
                text(
                    "INSERT INTO mcp_notices (id, text, starts_at, expires_at) "
                    "VALUES (:id, 'Preserved notice', now(), now() + interval '1 hour')"
                ),
                {"id": notice_id},
            )
            if starting_head == ADMISSION:
                conn.execute(
                    text(
                        "UPDATE users SET admitted_via = 'open_registration' WHERE user_id = :uid"
                    ),
                    {"uid": uid},
                )
                conn.execute(
                    text("UPDATE mcp_notices SET plans = ARRAY['max'] WHERE id = :id"),
                    {"id": notice_id},
                )
        if starting_head == CREDENTIALS:
            with Session(engine) as db, db.begin():
                db.add(
                    SyncService(
                        service_id=service_id,
                        browser_client_id="browser-" + uuid4().hex,
                        service_client_id=client_id,
                    )
                )
                db.flush()
                db.add(SyncCredentialClient(client_id=client_id, service_id=service_id))
                db.flush()
                db.add(
                    SyncSigningKey(
                        id=key_id,
                        client_id=client_id,
                        kid=document["kid"],
                        public_jwk=document,
                        fingerprint=key_fingerprint(document),
                    )
                )
                db.add(
                    SyncAssertionUse(
                        client_id=client_id,
                        jti_hash="a" * 64,
                        expires_at=utcnow() + timedelta(seconds=60),
                    )
                )
        migrate(command.upgrade, "head")
        assert_preserved()
        # Pin the rollback to this merge even after later migrations are added.
        migrate(command.downgrade, MERGE)
        # Relative -1 is ambiguous at a merge; either explicit parent unmerges
        # the version marker without undoing the other parent's schema.
        migrate(command.downgrade, starting_head)
        with engine.connect() as conn:
            assert set(conn.scalars(text("SELECT version_num FROM alembic_version"))) == {
                CREDENTIALS,
                ADMISSION,
            }
        assert_preserved()
        migrate(command.upgrade, "head")
        assert_preserved()
    finally:
        _reset_alembic_state()
        migrate(command.upgrade, "head")
        engine.dispose()
