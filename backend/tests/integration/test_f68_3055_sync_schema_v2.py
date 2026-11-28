"""#3055 (f68): the feed kind CHECK admits the kinds of record schema version 2.

1. Before f68 the CHECK admits ``product`` and ``faq``; at f68 it also admits
   ``page`` and ``review`` and is validated.
2. A rerun of the upgrade is a no-op; the downgrade refuses while a page or
   review feed exists and changes nothing; without one it restores the
   two-kind CHECK.

Not executed in the local unit run: needs the DB container.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from models.auth import Context, User, Workspace
from models.resource import Resource
from models.sync_feed import SyncFeed
from models.sync_installation import SyncInstallation, SyncService
from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f68_3055_sync_schema_v2"
BEFORE = "f67_3035_public_visitors"


def _check(conn) -> tuple[str, bool]:
    row = conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
            "WHERE conname = 'ck_sync_feed_kind' AND conrelid = 'sync_feeds'::regclass"
        )
    ).one()
    return row[0], row[1]


def _installation(db: Session) -> dict:
    uid, sid = "f68-" + uuid4().hex, "f68-service-" + uuid4().hex
    wid, rid, cid, iid = uuid4(), uuid4(), uuid4(), uuid4()
    slug = "sync-" + rid.hex
    db.add(User(user_id=uid, email=uid + "@example.test"))
    db.flush()
    db.add(Workspace(id=wid, name="f68", owner_user_id=uid))
    db.add(
        SyncService(
            service_id=sid, browser_client_id=sid + "-browser", service_client_id=sid + "-worker"
        )
    )
    db.flush()
    db.add(Resource(id=rid, workspace_id=wid, resource_id=slug))
    db.add(
        Context(
            id=cid,
            workspace_id=wid,
            name=slug,
            resource_id=slug,
            is_public=True,
            is_private=False,
        )
    )
    db.flush()
    db.add(
        SyncInstallation(
            id=iid,
            workspace_id=wid,
            service_id=sid,
            service_client_id=sid + "-worker",
            authorization_id=uuid4(),
            application_id="migration",
            operations=["records:write"],
            resource_pk=rid,
            context_id=cid,
            endpoint_id=uuid4(),
            resource_id=slug,
            state="active",
            generation=1,
        )
    )
    db.flush()
    return {"uid": uid, "sid": sid, "wid": wid, "rid": rid, "cid": cid, "iid": iid}


def test_f68_widens_the_kind_check_and_refuses_a_lossy_downgrade() -> None:
    engine = _sync_engine()
    try:
        _reset_alembic_state()
        _upgrade_to(BEFORE)
        with engine.connect() as conn:
            definition, validated = _check(conn)
        assert "'page'" not in definition and "'faq'" in definition and validated

        _upgrade_to(REVISION)
        with engine.connect() as conn:
            definition, validated = _check(conn)
        assert validated
        for kind in ("product", "faq", "page", "review"):
            assert f"'{kind}'" in definition

        with Session(engine) as db, db.begin():
            ids = _installation(db)
            db.add(
                SyncFeed(id=uuid4(), installation_id=ids["iid"], kind="page", config_hash="1" * 64)
            )
            db.add(
                SyncFeed(
                    id=uuid4(), installation_id=ids["iid"], kind="product", config_hash="1" * 64
                )
            )

        # A rerun is a no-op.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert _check(conn)[1]

        with pytest.raises(RuntimeError, match="page or review feeds"):
            _downgrade_to(BEFORE)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
            assert "'review'" in _check(conn)[0]

        with Session(engine) as db, db.begin():
            db.execute(
                delete(SyncFeed).where(
                    SyncFeed.installation_id == ids["iid"], SyncFeed.kind == "page"
                )
            )
        _downgrade_to(BEFORE)
        with engine.connect() as conn:
            definition, validated = _check(conn)
        assert "'page'" not in definition and validated
        _upgrade_to(REVISION)
    finally:
        try:
            # Drops every table (the rows above included) and leaves the
            # database at head for the other modules.
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
