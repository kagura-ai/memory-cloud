"""Migration constraints, erasure semantics and target-preserving downgrade (#2802)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from models.auth import Context, User, Workspace
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from models.sync_installation import (
    SYNC_OPERATIONS,
    SyncAuthorization,
    SyncInstallation,
    SyncService,
)
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)
from utils.datetime import utcnow

REVISION = "f44_2802_sync_installations"
PARENT = "f43_2798_paid_integrations"


def test_sync_migration_round_trip_preserves_targets_and_enforces_authority_constraints():
    engine = _sync_engine()
    wid, rid, cid, eid = (uuid4() for _ in range(4))
    uid, sid = "sync-migration-" + uuid4().hex, "migration-" + uuid4().hex
    slug = "sync-" + rid.hex
    try:
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PARENT)
        with Session(engine) as db, db.begin():
            # Raw SQL: the User model maps columns of later revisions
            # (f46_2803_admission_record), which do not exist at PARENT.
            db.execute(
                text(
                    "INSERT INTO users (email, user_id, role, timezone, locale, "
                    "is_initial_admin, auth_method) "
                    "VALUES (:email, :uid, 'user', 'UTC', 'en', false, 'oauth')"
                ),
                {"email": uid + "@example.test", "uid": uid},
            )
            db.add(Workspace(id=wid, name="Keep workspace", owner_user_id=uid))
            db.flush()
            db.add(Resource(id=rid, workspace_id=wid, resource_id=slug))
            db.add(
                Context(
                    id=cid,
                    workspace_id=wid,
                    name=slug,
                    is_private=False,
                    is_public=True,
                    resource_id=slug,
                    display_name="Retained canonical data",
                )
            )
            db.flush()
            db.add(
                PublicMcpEndpoint(
                    id=eid,
                    workspace_id=wid,
                    context_id=cid,
                    resource_pk=rid,
                    resource_id=slug,
                    public_name="Retained publication",
                    created_by=uid,
                )
            )
        for cycle in range(2):
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), REVISION)
            aid = uuid4()
            with Session(engine) as db, db.begin():
                db.add(
                    SyncService(
                        service_id=sid, browser_client_id="browser", service_client_id="worker"
                    )
                )
                db.flush()
                db.add(
                    SyncAuthorization(
                        id=aid,
                        workspace_id=wid,
                        request_id=uuid4(),
                        application_id="application",
                        service_id=sid,
                        service_generation=1,
                        browser_client_id="browser",
                        service_client_id="worker",
                        operations=list(SYNC_OPERATIONS),
                        authorized_by=uid,
                        ownership_epoch=0,
                        request_hash="a" * 64,
                        public_name="Approved",
                        created_at=utcnow(),
                        expires_at=utcnow() + timedelta(days=7),
                    )
                )
                db.flush()
                db.add(
                    SyncInstallation(
                        id=uuid4(),
                        workspace_id=wid,
                        service_id=sid,
                        service_client_id="worker",
                        authorization_id=aid,
                        application_id="application",
                        operations=list(SYNC_OPERATIONS),
                        resource_pk=rid,
                        context_id=cid,
                        endpoint_id=eid,
                        resource_id=slug,
                        state="active",
                        generation=1,
                    )
                )
            for sql in (
                "UPDATE sync_services SET generation=0 WHERE service_id=:sid",
                "UPDATE sync_authorizations SET ownership_epoch=-1 WHERE workspace_id=:wid",
                "UPDATE sync_authorizations SET expires_at=created_at WHERE workspace_id=:wid",
                "UPDATE sync_authorizations SET request_hash='short' WHERE workspace_id=:wid",
                "UPDATE sync_installations SET generation=0 WHERE workspace_id=:wid",
                "UPDATE sync_installations SET state='unknown' WHERE workspace_id=:wid",
            ):
                with pytest.raises(IntegrityError), engine.begin() as conn:
                    conn.execute(text(sql), {"sid": sid, "wid": wid})
            with pytest.raises(IntegrityError), Session(engine) as db, db.begin():
                db.add(
                    SyncInstallation(
                        id=uuid4(),
                        workspace_id=wid,
                        service_id=sid,
                        service_client_id="worker",
                        authorization_id=uuid4(),
                        application_id="different",
                        operations=list(SYNC_OPERATIONS),
                        resource_pk=rid,
                        context_id=cid,
                        endpoint_id=eid,
                        resource_id=slug,
                        state="active",
                        generation=1,
                    )
                )
                db.flush()
            if cycle == 1:
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": uid})
                with Session(engine) as db:
                    assert db.get(SyncAuthorization, aid).authorized_by is None
            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PARENT)
            with Session(engine) as db:
                assert db.get(Context, cid).display_name == "Retained canonical data"
                assert db.get(PublicMcpEndpoint, eid).public_name == "Retained publication"
                assert db.get(Resource, rid).resource_id == slug
                assert db.scalar(text("SELECT to_regclass('sync_installations')")) is None
    finally:
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
        with Session(engine) as db, db.begin():
            workspace = db.get(Workspace, wid)
            if workspace is not None:
                db.delete(workspace)
            user = db.scalar(select(User).where(User.user_id == uid))
            if user is not None:
                db.delete(user)
        engine.dispose()
