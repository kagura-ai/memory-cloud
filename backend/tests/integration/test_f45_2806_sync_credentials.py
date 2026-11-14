"""Real migration parity, constraints and target-preserving credential downgrade."""

from datetime import timedelta
from uuid import uuid4

import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from auth.sync_service_jwt import key_fingerprint, public_key
from models.auth import Context, User, Workspace
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from models.sync_credential import (
    SyncAccessToken,
    SyncAssertionUse,
    SyncCredentialClient,
    SyncSigningKey,
)
from models.sync_installation import SYNC_OPERATIONS, SyncInstallation, SyncService
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)
from utils.datetime import utcnow

REVISION = "f45_2806_sync_credentials"
PARENT = "f44_2802_sync_installations"


def test_credential_migration_roundtrip_and_constraints_preserve_existing_target():
    engine = _sync_engine()
    uid, sid, client = ("migration-" + uuid4().hex for _ in range(3))
    wid, rid, cid, eid, iid, key_id = (uuid4() for _ in range(6))
    slug = "sync-" + rid.hex
    document = public_key(
        RSAKey.generate_key(2048, parameters={"kid": "migration-key", "alg": "RS256"}).as_dict(
            private=False
        )
    )
    fingerprint = key_fingerprint(document)
    try:
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PARENT)
        with Session(engine) as db, db.begin():
            db.add(User(user_id=uid, email=uid + "@example.test"))
            db.flush()
            db.add(Workspace(id=wid, name="Preserved", owner_user_id=uid))
            db.flush()
            db.add(Resource(id=rid, workspace_id=wid, resource_id=slug))
            db.add(
                Context(
                    id=cid,
                    workspace_id=wid,
                    name=slug,
                    resource_id=slug,
                    is_private=False,
                    is_public=True,
                )
            )
            db.add(
                SyncService(service_id=sid, browser_client_id="browser", service_client_id=client)
            )
            db.flush()
            db.add(
                PublicMcpEndpoint(
                    id=eid,
                    workspace_id=wid,
                    resource_pk=rid,
                    context_id=cid,
                    resource_id=slug,
                    public_name="Preserved",
                    created_by=uid,
                )
            )
            db.add(
                SyncInstallation(
                    id=iid,
                    workspace_id=wid,
                    service_id=sid,
                    service_client_id=client,
                    authorization_id=uuid4(),
                    application_id="migration",
                    operations=list(SYNC_OPERATIONS),
                    resource_pk=rid,
                    context_id=cid,
                    endpoint_id=eid,
                    resource_id=slug,
                    state="active",
                    generation=1,
                    created_at=utcnow(),
                )
            )
        for _cycle in range(2):
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), REVISION)
            now = utcnow()

            def token(now=now, **changes):
                values = {
                    "token_hash": "1" * 64,
                    "installation_id": iid,
                    "workspace_id": wid,
                    "service_id": sid,
                    "client_id": client,
                    "key_id": key_id,
                    "key_fingerprint": fingerprint,
                    "service_generation": 1,
                    "client_generation": 1,
                    "installation_generation": 1,
                    "resource_pk": rid,
                    "context_id": cid,
                    "endpoint_id": eid,
                    "resource_id": slug,
                    "operations": list(SYNC_OPERATIONS),
                    "issuer": "https://identity.example.test/oidc",
                    "resource": "https://identity.example.test/oidc/sync/installations/" + str(iid),
                    "scope": "resource.sync",
                    "created_at": now,
                    "expires_at": now + timedelta(seconds=300),
                }
                return SyncAccessToken(**(values | changes))

            with Session(engine) as db, db.begin():
                assert db.scalar(select(func.count()).select_from(SyncCredentialClient)) == 0
                db.add(SyncCredentialClient(client_id=client, service_id=sid))
                db.flush()
                db.add(
                    SyncSigningKey(
                        id=key_id,
                        client_id=client,
                        kid=document["kid"],
                        public_jwk=document,
                        fingerprint=fingerprint,
                    )
                )
                db.add(
                    SyncAssertionUse(
                        client_id=client, jti_hash="a" * 64, expires_at=now + timedelta(seconds=60)
                    )
                )
                db.flush()
                db.add(token())
            with pytest.raises(IntegrityError) as duplicate, Session(engine) as db, db.begin():
                db.add(
                    SyncSigningKey(
                        client_id=client,
                        kid="alias",
                        public_jwk={**document, "kid": "alias"},
                        fingerprint=fingerprint,
                    )
                )
                db.flush()
            assert "uq_sync_key_client_material" in str(duplicate.value)
            for changes, constraint in [
                ({"token_hash": "2" * 64, "scope": "memory:admin"}, "ck_sync_access_scope"),
                ({"token_hash": "2" * 64, "expires_at": now}, "ck_sync_access_window"),
                ({"token_hash": "short"}, "ck_sync_access_hash"),
            ]:
                with pytest.raises(IntegrityError) as error, Session(engine) as db, db.begin():
                    db.add(token(**changes))
                    db.flush()
                assert constraint in str(error.value)
            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PARENT)
            with Session(engine) as db:
                assert db.get(SyncInstallation, iid).resource_pk == rid
                assert db.get(Resource, rid) is not None
                assert db.get(Context, cid) is not None
                assert db.get(PublicMcpEndpoint, eid) is not None
    finally:
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
        with Session(engine) as db, db.begin():
            workspace = db.get(Workspace, wid)
            if workspace is not None:
                db.delete(workspace)
                db.flush()
            service = db.get(SyncService, sid)
            if service is not None:
                db.delete(service)
            user = db.scalar(select(User).where(User.user_id == uid))
            if user is not None:
                db.delete(user)
        engine.dispose()
