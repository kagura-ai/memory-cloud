"""Operator client namespaces cannot race across their separate storage tables."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from cli.oidc_client import register_client
from cli.sync_service import register_service
from models.oidc import OIDCClient
from models.sync_installation import SyncService
from tests.integration.test_alembic_migrations import _sync_engine


@pytest.mark.parametrize("first_kind", ["service", "browser"])
def test_racing_operator_registrations_have_one_namespace_owner(async_engine, first_kind):
    engine = _sync_engine()
    prefix = "namespace-" + uuid4().hex
    browser, worker, sid = prefix + "-browser", prefix + "-worker", prefix
    staged, finish, pids = Event(), Event(), Queue()
    pool = ThreadPoolExecutor(max_workers=2)

    def register(db, kind):
        if kind == "service":
            register_service(db, sid, browser, worker)
        else:
            register_client(
                db,
                client_id=worker,
                client_name="Namespace fixture",
                redirect_uris=["https://fixture.example.test/callback"],
                client_secret="fixture-private-client-secret-" + "x" * 32,
            )

    def first():
        with Session(engine) as db, db.begin():
            register(db, first_kind)
            staged.set()
            if not finish.wait(15):
                raise RuntimeError("Test did not release namespace registration")

    def second():
        with Session(engine) as db, db.begin():
            pids.put(db.scalar(text("SELECT pg_backend_pid()")))
            try:
                register(db, "browser" if first_kind == "service" else "service")
            except ValueError as exc:
                return str(exc)
            return "unexpected namespace admission"

    try:
        with Session(engine) as db, db.begin():
            db.add(
                OIDCClient(
                    client_id=browser,
                    client_name="Browser",
                    client_secret_hash="unused",
                    redirect_uris=["https://fixture.example.test/callback"],
                    scope="openid integrations:manage",
                )
            )
        winner = pool.submit(first)
        assert staged.wait(8)
        loser = pool.submit(second)
        pid = pids.get(timeout=8)
        blocked, end = False, monotonic() + 5
        while monotonic() < end:
            with engine.connect() as connection:
                blocked = connection.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
                )
            if blocked:
                break
            sleep(0.01)
        assert blocked, "No real PostgreSQL namespace-lock wait was observed"
        finish.set()
        winner.result(timeout=8)
        refused = loser.result(timeout=8)
        assert ("reserved" if first_kind == "service" else "OIDC login") in refused
        with Session(engine) as db:
            assert (db.get(OIDCClient, worker) is not None) == (first_kind == "browser")
            assert (
                db.scalar(select(SyncService).where(SyncService.service_client_id == worker))
                is not None
            ) == (first_kind == "service")
    finally:
        finish.set()
        pool.shutdown(wait=True, cancel_futures=True)
        with Session(engine) as db, db.begin():
            db.execute(delete(SyncService).where(SyncService.service_id == sid))
            db.execute(delete(OIDCClient).where(OIDCClient.client_id.in_([browser, worker])))
        engine.dispose()
