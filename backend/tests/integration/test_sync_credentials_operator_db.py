"""Public-key overlap and explicit retirement never revive earlier credentials."""

from uuid import UUID

import pytest
from joserfc.jwk import RSAKey
from sqlalchemy.orm import Session

from cli import sync_credentials as operator
from cli.oidc_client import register_client as register_browser
from services import sync_credentials as service
from tests.integration import sync_credential_fixtures as fixtures
from tests.integration.sync_credential_fixtures import mint
from tests.integration.test_alembic_migrations import _sync_engine

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store
worker = fixtures.worker


async def test_key_rotation_overlap_then_retirement_invalidates_only_old_key_tokens(worker):
    old = await mint(worker)
    second_key = RSAKey.generate_key(2048, parameters={"kid": "next", "alg": "RS256"})
    engine = _sync_engine()
    try:
        with Session(engine) as db, db.begin():
            assert (
                operator.register_client(db, worker.store.service_id).client_id == worker.client_id
            )
            row = operator.add_key(db, worker.client_id, second_key.as_dict(private=False))
            assert (
                operator.add_key(db, worker.client_id, second_key.as_dict(private=False)).id
                == row.id
            )
        new = await mint(worker, key=second_key, kid="next")
        for value in (old, new):
            async with worker.factory() as db:
                async with service.sync_token_boundary(
                    db,
                    value["access_token"],
                    UUID(worker.target["installation_id"]),
                    operation="results:read",
                ):
                    pass
        with Session(engine) as db, db.begin():
            operator.revoke_key(db, worker.client_id, "current")
        async with worker.factory() as db:
            with pytest.raises(service.SyncCredentialError):
                async with service.sync_token_boundary(
                    db,
                    old["access_token"],
                    UUID(worker.target["installation_id"]),
                    operation="results:read",
                ):
                    pytest.fail("Retired key admitted")
        async with worker.factory() as db:
            async with service.sync_token_boundary(
                db,
                new["access_token"],
                UUID(worker.target["installation_id"]),
                operation="results:read",
            ):
                pass
        with pytest.raises(ValueError, match="retired"), Session(engine) as db, db.begin():
            operator.add_key(db, worker.client_id, worker.key.as_dict(private=False))
        with pytest.raises(ValueError, match="another key ID"), Session(engine) as db, db.begin():
            operator.add_key(
                db,
                worker.client_id,
                {**worker.key.as_dict(private=False), "kid": "renamed-retired"},
            )
    finally:
        engine.dispose()


async def test_disabled_then_reenabled_client_never_revives_old_tokens(worker):
    old = await mint(worker)
    engine = _sync_engine()
    try:
        with Session(engine) as db, db.begin():
            assert operator.set_client_enabled(db, worker.client_id, False).generation == 2
            assert operator.set_client_enabled(db, worker.client_id, False).generation == 2
            assert operator.set_client_enabled(db, worker.client_id, True).generation == 3
        async with worker.factory() as db:
            with pytest.raises(service.SyncCredentialError):
                async with service.sync_token_boundary(
                    db,
                    old["access_token"],
                    UUID(worker.target["installation_id"]),
                    operation="results:read",
                ):
                    pytest.fail("Old generation admitted")
        assert (await mint(worker))["expires_in"] == 300
    finally:
        engine.dispose()


async def test_reserved_worker_id_cannot_be_registered_as_a_browser_client(worker):
    engine = _sync_engine()
    try:
        with pytest.raises(ValueError, match="reserved"), Session(engine) as db, db.begin():
            register_browser(
                db,
                client_id=worker.client_id,
                client_name="Wrong namespace",
                redirect_uris=["https://example.test/callback"],
                client_secret="fixture-distinct-secret-with-at-least-32-characters",
            )
    finally:
        engine.dispose()


async def test_expired_cleanup_preserves_live_verifiers_and_replay_receipts(worker, monkeypatch):
    from datetime import timedelta

    from sqlalchemy import select

    from models.sync_credential import SyncAccessToken, SyncAssertionUse

    old = await mint(worker)
    worker.store.clock.now += timedelta(seconds=301)
    current = await mint(worker)
    monkeypatch.setattr(operator, "utcnow", lambda: worker.store.clock.now)
    engine = _sync_engine()
    try:
        with Session(engine) as db, db.begin():
            assert operator.prune_expired(db) == {"tokens": 1, "assertions": 1}
        async with worker.factory() as db:
            assert len((await db.scalars(select(SyncAccessToken))).all()) == 1
            assert len((await db.scalars(select(SyncAssertionUse))).all()) == 1
            async with service.sync_token_boundary(
                db,
                current["access_token"],
                UUID(worker.target["installation_id"]),
                operation="results:read",
            ):
                pass
        async with worker.factory() as db:
            with pytest.raises(service.SyncCredentialError):
                async with service.sync_token_boundary(
                    db,
                    old["access_token"],
                    UUID(worker.target["installation_id"]),
                    operation="results:read",
                ):
                    pytest.fail("Expired token was retained")
    finally:
        engine.dispose()


async def test_operator_cli_outputs_metadata_only(worker, monkeypatch, tmp_path, capsys):
    import json

    import cli._env_guard
    import cli.db

    engine = _sync_engine()
    monkeypatch.setattr(cli._env_guard, "require_settings", lambda: None)
    monkeypatch.setattr(cli.db, "get_sync_database_url", lambda: "not-used")
    monkeypatch.setattr(operator, "create_engine", lambda *args, **kwargs: engine)
    path = tmp_path / "public.jwk"
    path.write_text(json.dumps(worker.key.as_dict(private=False)), encoding="utf-8")
    assert (
        operator.main(["add-key", "--client-id", worker.client_id, "--public-jwk-file", str(path)])
        == 0
    )
    captured = capsys.readouterr()
    assert set(json.loads(captured.out)) == {"key_id", "kid", "revoked"}
    assert "public_jwk" not in captured.out and "fingerprint" not in captured.out
